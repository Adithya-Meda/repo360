"""Production-grade upgrade utility for the Repo360 CLI suite."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import requests

from . import __version__ as PACKAGE_VERSION
from .common.interrupts import run_interruptible_subprocess
from .credentials import get_credentials_path, get_secrets_dir

logger = logging.getLogger("repo360.upgrade")

DEFAULT_REPO_URL = "https://github.com/Adithya-Meda/repo360.git"
_VALID_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _posix_bin_candidates(exe_name: str) -> List[Path]:
    """Return common POSIX user and Homebrew/system bin locations for CLI tools."""
    home = Path(os.path.expanduser("~"))
    return [
        home / ".local" / "bin" / exe_name,
        Path("/opt/homebrew/bin") / exe_name,
        Path("/usr/local/bin") / exe_name,
        Path("/usr/bin") / exe_name,
    ]


def _first_existing_file(candidates: Sequence[Path]) -> Optional[str]:
    """Return the string path of the first existing file in candidates."""
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def find_pipx_executable() -> Optional[str]:
    """Locate pipx executable in PATH or standard user package script directories."""
    path_hit = shutil.which("pipx")
    if path_hit:
        return path_hit

    if sys.platform != "win32":
        return _first_existing_file(_posix_bin_candidates("pipx"))

    home = Path(os.path.expanduser("~"))
    candidates: List[Path] = [
        home / ".local" / "bin" / "pipx.exe",
        home / ".local" / "bin" / "pipx",
    ]
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        candidates.extend([
            Path(appdata) / "Programs" / "Python" / "Scripts" / "pipx.exe",
            Path(appdata) / "pipx" / "pipx.exe",
        ])
        packages_dir = Path(appdata) / "Packages"
        if packages_dir.is_dir():
            for p in packages_dir.glob("PythonSoftwareFoundation.Python.*/LocalCache/local-packages/Python*/Scripts/pipx.exe"):
                candidates.append(p)

    return _first_existing_file(candidates)


def find_uv_executable() -> Optional[str]:
    """Locate uv executable in PATH, user tool bins, or bundled inside pipx[uv]."""
    path_hit = shutil.which("uv")
    if path_hit:
        return path_hit

    exe_name = "uv.exe" if sys.platform == "win32" else "uv"
    candidates: List[Path] = []
    if sys.platform == "win32":
        home = Path(os.path.expanduser("~"))
        candidates.extend([
            home / ".local" / "bin" / "uv.exe",
            home / ".cargo" / "bin" / "uv.exe",
        ])
    else:
        home = Path(os.path.expanduser("~"))
        candidates.extend(_posix_bin_candidates("uv"))
        candidates.append(home / ".cargo" / "bin" / "uv")

    pipx_bin = find_pipx_executable()
    if pipx_bin:
        candidates.append(Path(pipx_bin).parent / exe_name)

    try:
        uv_pkg = importlib.import_module("uv")
        find_bin = getattr(uv_pkg, "find_uv_bin", None)
        if callable(find_bin):
            bundled = Path(str(find_bin()))
            if bundled.is_file():
                return str(bundled)
    except Exception:
        pass

    return _first_existing_file(candidates)


def _is_uv_tool_install() -> bool:
    """Return True if the active interpreter belongs to a 'uv tool' managed environment."""
    prefix_path = Path(sys.prefix)
    if (prefix_path / "uv-receipt.toml").is_file():
        return True
    parts = [p.lower() for p in prefix_path.parts]
    return "uv" in parts and "tools" in parts


def _detect_pipx_venv_name() -> str:
    """Return the active pipx venv directory name (supports suffixed venvs cleanly)."""
    prefix_path = Path(sys.prefix)
    parts_lower = [p.lower() for p in prefix_path.parts]
    if "venvs" in parts_lower and prefix_path.name:
        return prefix_path.name
    return "repo360"


def _pip_available_for_python() -> bool:
    """Return True when the active interpreter environment has pip installed."""
    scripts_name = "Scripts" if sys.platform == "win32" else "bin"
    pip_exe = Path(sys.prefix) / scripts_name / ("pip.exe" if sys.platform == "win32" else "pip")
    if pip_exe.is_file():
        return True
    return importlib.util.find_spec("pip") is not None


def _detect_pipx_backend() -> str:
    """Determine whether the pipx venv uses 'uv' or 'pip' as its backend."""
    meta_file = Path(sys.prefix) / "pipx_metadata.json"
    if meta_file.is_file():
        try:
            meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
            backend = str(meta_data.get("backend") or "").strip().lower()
            if backend in ("uv", "pip"):
                return backend
        except Exception:
            pass

    info = get_installation_details()
    installer = str(info.get("installer") or "").lower()
    if installer in ("uv", "pip"):
        return installer

    if not _pip_available_for_python() and find_uv_executable():
        return "uv"
    return "pip"


def _build_target_spec(repo_url: str, target_ref: Optional[str] = None) -> str:
    """Build a PEP 508 VCS target specifier optionally pinned to a release tag or commit SHA."""
    raw = (repo_url or DEFAULT_REPO_URL).strip()
    if raw.lower().startswith("file://") or Path(raw).exists():
        return raw
    clean_url = raw[4:] if raw.lower().startswith("git+") else raw
    if target_ref and "@" not in clean_url and "#" not in clean_url:
        return f"git+{clean_url}@{target_ref.strip()}"
    return f"git+{clean_url}"


def detect_installer() -> Tuple[str, List[str]]:
    """Detect whether Repo360 is running within pipx or standard pip/virtualenv.

    Returns:
        Tuple of (installer_name, base_command_tokens).
    """
    prefix = sys.prefix.lower()
    pipx_bin = find_pipx_executable()

    if "pipx" in prefix and pipx_bin:
        # Upgrade in place via 'pipx runpip' to avoid Windows file locks on python.exe
        return "pipx", [pipx_bin, "runpip", _detect_pipx_venv_name(), "install", "--upgrade"]

    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade"]


def get_upgrade_commands(
    repo_url: str = DEFAULT_REPO_URL,
    installer_override: Optional[str] = None,
    force: bool = False,
    target_ref: Optional[str] = None,
) -> List[Tuple[str, List[str]]]:
    """Generate prioritized upgrade command candidates for resilient execution across platforms.

    Returns:
        List of (strategy_name, command_tokens).
    """
    target_spec = _build_target_spec(repo_url, target_ref=target_ref)
    commands: List[Tuple[str, List[str]]] = []

    prefix = sys.prefix.lower()
    pipx_bin = find_pipx_executable()
    uv_bin = find_uv_executable()

    if installer_override == "pip":
        pip_flags = ["--upgrade", "--force-reinstall", "--no-cache-dir"] if force else ["--upgrade"]
        commands.append(("pip", [sys.executable, "-m", "pip", "install", *pip_flags, target_spec]))
        if force:
            # Skip dependencies on retry so locked C extensions like cryptography don't block upgrade
            commands.append(
                ("pip-isolated", [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-cache-dir", target_spec])
            )
        return commands

    if _is_uv_tool_install() and uv_bin and installer_override != "pipx":
        uv_tool_flags = ["--refresh-package", "repo360"]
        if force:
            uv_tool_flags.extend(["--force", "--reinstall-package", "repo360"])
        commands.append(("uv-tool", [uv_bin, "tool", "install", *uv_tool_flags, target_spec]))

    # Upgrade in place via 'pipx runpip' to avoid Windows venv rebuild locks
    if ("pipx" in prefix or installer_override == "pipx") and pipx_bin:
        venv_name = _detect_pipx_venv_name()
        backend = _detect_pipx_backend()
        if backend == "uv":
            pipx_flags = (
                ["--refresh-package=repo360", "--reinstall-package=repo360"]
                if force
                else ["--upgrade", "--refresh-package=repo360"]
            )
        else:
            pipx_flags = ["--upgrade", "--force-reinstall", "--no-cache-dir"] if force else ["--upgrade"]
        commands.append(("pipx", [pipx_bin, "runpip", venv_name, "install", *pipx_flags, target_spec]))
        if backend == "pip" and force:
            commands.append(
                ("pipx-isolated", [pipx_bin, "runpip", venv_name, "install", "--force-reinstall", "--no-deps", "--no-cache-dir", target_spec])
            )

    # Direct uv pip handles pip-less uv/pipx venvs
    if uv_bin:
        uv_flags = (
            ["--refresh-package", "repo360", "--reinstall-package", "repo360"]
            if force
            else ["--upgrade", "--refresh-package", "repo360"]
        )
        commands.append(("uv", [uv_bin, "pip", "install", "--python", sys.executable, *uv_flags, target_spec]))

    if _pip_available_for_python() or not commands:
        pip_flags = ["--upgrade", "--force-reinstall", "--no-cache-dir"] if force else ["--upgrade"]
        commands.append(("pip", [sys.executable, "-m", "pip", "install", *pip_flags, target_spec]))
        if force:
            commands.append(
                ("pip-isolated", [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-cache-dir", target_spec])
            )

    return commands


def build_upgrade_command(
    repo_url: str = DEFAULT_REPO_URL,
    installer_override: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Construct the primary shell command to upgrade Repo360 to the latest version.

    Args:
        repo_url: Git repository clone URL.
        installer_override: Force 'pipx' or 'pip'.

    Returns:
        Tuple of (installer_type, command_list).
    """
    candidates = get_upgrade_commands(repo_url, installer_override)
    return candidates[0][0], candidates[0][1]


def _parse_github_owner_repo(repo_url: str) -> Optional[Tuple[str, str]]:
    """Extract (owner, repo) from a GitHub HTTPS repository URL."""
    if not repo_url:
        return None
    clean_url = repo_url.strip().rstrip("/")
    if clean_url.lower().startswith("git+"):
        clean_url = clean_url[4:]
    if clean_url.endswith(".git"):
        clean_url = clean_url[:-4]
    parsed = urllib.parse.urlparse(clean_url)
    host = parsed.netloc.lower()
    if host != "github.com" and not parsed.scheme:
        parts = [p for p in clean_url.split("/") if p]
    else:
        if host and host != "github.com":
            return None
        parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None


def _build_github_api_headers() -> Dict[str, str]:
    """Build standard GitHub REST API headers with optional environment token."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Repo360-Upgrade/1.0"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _fetch_commit_summary(
    repo_url: str,
    ref_or_sha: str,
    timeout: float = 4.0,
) -> Optional[Dict[str, str]]:
    """Fetch commit SHA, ISO timestamp, and subject line from GitHub REST API."""
    parsed = _parse_github_owner_repo(repo_url)
    if not parsed or not ref_or_sha:
        return None
    owner, repo_name = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/commits/{urllib.parse.quote(ref_or_sha, safe='')}"
    try:
        resp = requests.get(api_url, headers=_build_github_api_headers(), timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            commit_block = data.get("commit", {}) or {}
            committer_date = (commit_block.get("committer", {}) or {}).get("date") or ""
            author_date = (commit_block.get("author", {}) or {}).get("date") or ""
            raw_message = str(commit_block.get("message") or "")
            subject = raw_message.splitlines()[0] if raw_message else ""
            return {
                "sha": str(data.get("sha") or ""),
                "date": str(committer_date or author_date),
                "subject": subject,
            }
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class ReleaseTarget:
    """Resolved release tag metadata for upgrade/install operations."""

    tag: Optional[str] = None
    commit: Optional[str] = None
    source: str = ""


def normalize_release_tag(raw_tag: str) -> str:
    """Validate and normalize a release tag/ref string against git ref and CLI injection rules."""
    tag = (raw_tag or "").strip()
    if (
        not tag
        or tag.startswith("-")
        or ".." in tag
        or "@{" in tag
        or "//" in tag
        or tag.endswith(("/", ".", ".lock"))
        or not _VALID_GIT_REF_RE.match(tag)
    ):
        raise ValueError(
            f"Invalid release tag '{raw_tag}'. Expected a valid tag name such as 'v1.0.0'."
        )
    return tag


def _parse_semver_tag(tag_name: str) -> Optional[Tuple[int, int, int]]:
    """Parse a stable SemVer tag (e.g. 'v1.2.3' or '1.2.3') into a comparable tuple."""
    match = _SEMVER_TAG_RE.match((tag_name or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _list_remote_tags(
    repo_url: str = DEFAULT_REPO_URL,
    ref_patterns: Sequence[str] = ("refs/tags/*",),
) -> Optional[Dict[str, str]]:
    """Query remote tags via 'git ls-remote', dereferencing annotated tags to commit SHAs."""
    clean_url = (repo_url or "").strip()
    if not clean_url or clean_url.startswith("-"):
        return None
    try:
        res = run_interruptible_subprocess(
            ["git", "ls-remote", "--tags", "--", clean_url, *ref_patterns],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
    except Exception:
        return None

    if res.returncode != 0:
        return None

    raw_tags: Dict[str, str] = {}
    peeled_tags: Dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if not sha or not ref.startswith("refs/tags/"):
            continue
        if ref.endswith("^{}"):
            tag_name = ref[len("refs/tags/"):-3]
            if tag_name:
                peeled_tags[tag_name] = sha
        else:
            tag_name = ref[len("refs/tags/"):]
            if tag_name:
                raw_tags[tag_name] = sha

    merged_keys = list(raw_tags.keys()) + [k for k in peeled_tags if k not in raw_tags]
    return {tag: peeled_tags.get(tag, raw_tags.get(tag, "")) for tag in merged_keys}


def _resolve_latest_github_release_tag(
    repo_url: str = DEFAULT_REPO_URL,
    timeout: float = 5.0,
) -> Tuple[Optional[str], Optional[int]]:
    """Fetch the latest non-draft, non-prerelease tag_name from GitHub Releases API."""
    parsed = _parse_github_owner_repo(repo_url)
    if not parsed:
        return None, None
    owner, repo_name = parsed
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest"
    try:
        resp = requests.get(api_url, headers=_build_github_api_headers(), timeout=timeout)
    except Exception:
        return None, None

    if resp.status_code != 200:
        return None, resp.status_code

    try:
        data = resp.json() or {}
    except Exception:
        return None, resp.status_code

    if data.get("draft") or data.get("prerelease"):
        return None, resp.status_code

    raw_tag = str(data.get("tag_name") or "").strip()
    if not raw_tag:
        return None, resp.status_code
    try:
        return normalize_release_tag(raw_tag), 200
    except ValueError:
        return None, resp.status_code


def _check_github_release_tag(
    repo_url: str,
    tag_name: str,
    timeout: float = 5.0,
) -> Tuple[bool, Optional[int]]:
    """Check whether a specific release tag exists via GitHub Releases API."""
    parsed = _parse_github_owner_repo(repo_url)
    if not parsed:
        return False, None
    owner, repo_name = parsed
    encoded = urllib.parse.quote(tag_name, safe="")
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/releases/tags/{encoded}"
    try:
        resp = requests.get(api_url, headers=_build_github_api_headers(), timeout=timeout)
        return resp.status_code == 200, resp.status_code
    except Exception:
        return False, None


def _resolve_tag_commit_sha(repo_url: str, tag_name: str) -> Optional[str]:
    """Resolve the underlying commit SHA for a release tag via git ls-remote or GitHub API."""
    tag_map = _list_remote_tags(
        repo_url,
        ref_patterns=(f"refs/tags/{tag_name}", f"refs/tags/{tag_name}^{{}}"),
    )
    if tag_map and tag_map.get(tag_name):
        return tag_map[tag_name]

    summary = _fetch_commit_summary(repo_url, tag_name, timeout=5.0)
    if summary and summary.get("sha"):
        return summary["sha"]
    return None


def resolve_release_target(
    repo_url: str = DEFAULT_REPO_URL,
    requested_tag: Optional[str] = None,
) -> ReleaseTarget:
    """Resolve the target GitHub Release (latest stable release or an explicit '--tag')."""
    if requested_tag:
        clean_tag = normalize_release_tag(requested_tag)
        candidates = [clean_tag]
        if not clean_tag.startswith("v") and _parse_semver_tag(clean_tag):
            candidates.append(f"v{clean_tag}")

        patterns: List[str] = []
        for cand in candidates:
            patterns.extend((f"refs/tags/{cand}", f"refs/tags/{cand}^{{}}"))

        tag_map = _list_remote_tags(repo_url, ref_patterns=patterns)
        if tag_map is not None:
            for cand in candidates:
                if cand in tag_map:
                    return ReleaseTarget(tag=cand, commit=tag_map[cand] or None, source="git-tag")
            return ReleaseTarget(tag=None, commit=None, source="tag-not-found")

        # Fall back to GitHub Releases API when git CLI is unavailable
        saw_not_found = False
        for cand in candidates:
            exists, status_code = _check_github_release_tag(repo_url, cand)
            if exists:
                summary = _fetch_commit_summary(repo_url, cand, timeout=5.0)
                return ReleaseTarget(
                    tag=cand,
                    commit=summary.get("sha") if summary else None,
                    source="github-release",
                )
            if status_code == 404:
                saw_not_found = True

        return ReleaseTarget(
            tag=None,
            commit=None,
            source="tag-not-found" if saw_not_found else "error",
        )

    latest_tag, _ = _resolve_latest_github_release_tag(repo_url)
    if latest_tag:
        commit_sha = _resolve_tag_commit_sha(repo_url, latest_tag)
        return ReleaseTarget(tag=latest_tag, commit=commit_sha, source="github-release")

    # Fall back to highest SemVer tag via git ls-remote when Releases API is unavailable
    all_tags = _list_remote_tags(repo_url)
    if all_tags is None:
        return ReleaseTarget(tag=None, commit=None, source="error")

    semver_candidates = [
        (parsed_ver, tag_name, sha)
        for tag_name, sha in all_tags.items()
        if (parsed_ver := _parse_semver_tag(tag_name)) is not None
    ]
    if not semver_candidates:
        return ReleaseTarget(tag=None, commit=None, source="no-releases")

    _, best_tag, best_sha = max(semver_candidates, key=lambda item: item[0])
    return ReleaseTarget(tag=best_tag, commit=best_sha or None, source="semver-tag")


def resolve_commit_target(
    repo_url: str = DEFAULT_REPO_URL,
    requested_ref: Optional[str] = None,
) -> ReleaseTarget:
    """Resolve the latest HEAD commit or an explicit branch/commit SHA for testing upgrades without a release."""
    clean_url = (repo_url or "").strip()
    if requested_ref is None:
        ref_label = "HEAD"
        if clean_url and not clean_url.startswith("-"):
            try:
                res = run_interruptible_subprocess(
                    ["git", "ls-remote", "--", clean_url, "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=8,
                )
                if res.returncode == 0:
                    for line in (res.stdout or "").splitlines():
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2 and parts[1].strip() == "HEAD" and parts[0].strip():
                            sha = parts[0].strip()
                            return ReleaseTarget(tag=ref_label, commit=sha, source="git-head")
            except Exception:
                pass
        summary = _fetch_commit_summary(repo_url, "HEAD", timeout=5.0)
        if summary and summary.get("sha"):
            return ReleaseTarget(tag=ref_label, commit=summary["sha"], source="github-head")
        return ReleaseTarget(tag=None, commit=None, source="error")

    clean_ref = normalize_release_tag(requested_ref)
    if clean_url and not clean_url.startswith("-") and not _HEX_SHA_RE.match(clean_ref):
        try:
            res = run_interruptible_subprocess(
                ["git", "ls-remote", "--heads", "--", clean_url, f"refs/heads/{clean_ref}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            if res.returncode == 0:
                for line in (res.stdout or "").splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2 and parts[0].strip():
                        return ReleaseTarget(tag=clean_ref, commit=parts[0].strip(), source="git-branch")
        except Exception:
            pass

    summary = _fetch_commit_summary(repo_url, clean_ref, timeout=5.0)
    if summary and summary.get("sha"):
        return ReleaseTarget(tag=clean_ref, commit=summary["sha"], source="github-commit")

    return ReleaseTarget(tag=None, commit=None, source="ref-not-found")


class UpgradeResult(int):
    """Upgrade exit code with metadata tracking whether code was updated or already at latest."""

    already_up_to_date: bool = False
    version: str = ""
    commit: Optional[str] = None

    def __new__(
        cls,
        code: int,
        already_up_to_date: bool = False,
        version: str = "",
        commit: Optional[str] = None,
    ) -> UpgradeResult:
        instance = super().__new__(cls, code)
        instance.already_up_to_date = already_up_to_date
        instance.version = version
        instance.commit = commit
        return instance


def _commits_match(local_sha: Optional[str], remote_sha: Optional[str]) -> bool:
    """Compare full or short git commit SHAs case-insensitively."""
    if not local_sha or not remote_sha:
        return False
    a = local_sha.strip().lower()
    b = remote_sha.strip().lower()
    if min(len(a), len(b)) < 7:
        return False
    return a == b or a.startswith(b[:8]) or b.startswith(a[:8])


def _parse_file_url(file_url: str) -> Optional[Path]:
    """Convert a PEP 610 file:// URL into a local filesystem Path."""
    if not file_url.lower().startswith("file://"):
        return None
    parsed = urllib.parse.urlparse(file_url)
    path_str = urllib.request.url2pathname(parsed.path)
    # Strip leading slash on Windows drive paths (e.g. '/C:/...' -> 'C:/...')
    if sys.platform == "win32" and len(path_str) >= 3 and path_str[0] in ("/", "\\") and path_str[2] == ":":
        path_str = path_str[1:]
    return Path(path_str)


def _get_git_commit_details(repo_dir: Path) -> Dict[str, Any]:
    """Return commit SHA, subject, ISO timestamp, branch, and dirty flag for a git directory."""
    details: Dict[str, Any] = {
        "commit": None,
        "commit_date": "",
        "commit_subject": "",
        "branch": "",
        "is_dirty": False,
    }
    if not repo_dir.is_dir():
        return details

    try:
        res = run_interruptible_subprocess(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%H%x1f%cI%x1f%s"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("\x1f", 2)
            if parts[0].strip():
                details["commit"] = parts[0].strip()
            if len(parts) > 1:
                details["commit_date"] = parts[1].strip()
            if len(parts) > 2:
                details["commit_subject"] = parts[2].strip()
    except Exception:
        return details

    try:
        branch_res = run_interruptible_subprocess(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if branch_res.returncode == 0 and branch_res.stdout.strip():
            details["branch"] = branch_res.stdout.strip()
    except Exception:
        pass

    try:
        status_res = run_interruptible_subprocess(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if status_res.returncode == 0 and status_res.stdout.strip():
            details["is_dirty"] = True
    except Exception:
        pass

    return details


def _git_head_for_dir(repo_dir: Path) -> Optional[str]:
    """Return git HEAD commit SHA for a specific directory without falling back to CWD."""
    return _get_git_commit_details(repo_dir).get("commit")


def _hash_python_tree(root_dir: Path) -> Dict[str, str]:
    """Build relative POSIX path -> SHA-256 hex digest map for Python files under root_dir."""
    hashes: Dict[str, str] = {}
    if not root_dir.is_dir():
        return hashes
    for file_path in root_dir.rglob("*.py"):
        if "__pycache__" in file_path.parts:
            continue
        try:
            rel = file_path.relative_to(root_dir).as_posix()
            hashes[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def _compare_installed_tree_to_source(
    installed_pkg_dir: Path,
    source_pkg_dir: Path,
) -> Tuple[bool, List[str]]:
    """Verify whether non-editable installed package files match the local git checkout."""
    try:
        if installed_pkg_dir.resolve() == source_pkg_dir.resolve():
            return True, []
    except OSError:
        pass

    if not installed_pkg_dir.is_dir() or not source_pkg_dir.is_dir():
        return True, []

    installed_hashes = _hash_python_tree(installed_pkg_dir)
    source_hashes = _hash_python_tree(source_pkg_dir)
    all_keys = set(installed_hashes) | set(source_hashes)
    mismatched = [
        key for key in sorted(all_keys)
        if installed_hashes.get(key) != source_hashes.get(key)
    ]
    return len(mismatched) == 0, mismatched


def get_installation_details() -> Dict[str, Any]:
    """Collect installed package version, commit SHA, commit metadata, origin source, and path."""
    version: Optional[str] = None
    commit_sha: Optional[str] = None
    commit_source = "unknown"
    commit_subject = ""
    commit_date = ""
    branch = ""
    is_dirty = False
    is_stale_snapshot = False
    stale_file_count = 0
    local_repo_head: Optional[str] = None
    installer = "pipx" if "pipx" in sys.prefix.lower() else "pip"
    source_url = ""
    pkg_dir = Path(__file__).resolve().parent

    try:
        import importlib.metadata as meta

        importlib.invalidate_caches()
        dist = meta.distribution("repo360")
        version = (dist.version or "").strip() or None
        installer_text = (dist.read_text("INSTALLER") or "").strip()
        if installer_text:
            installer = installer_text.lower()
        direct_url_text = dist.read_text("direct_url.json")
        if direct_url_text:
            data = json.loads(direct_url_text)
            source_url = str(data.get("url") or "")
            vcs_info = data.get("vcs_info") or {}
            vcs_sha = vcs_info.get("commit_id")
            if vcs_sha:
                commit_sha = str(vcs_sha).strip()
                commit_source = "direct_url.json (vcs_info)"
                branch = str(vcs_info.get("requested_revision") or "")
            elif source_url.lower().startswith("file://"):
                local_repo = _parse_file_url(source_url)
                if local_repo:
                    dir_info = data.get("dir_info") or {}
                    editable = bool(dir_info.get("editable", False))
                    git_info = _get_git_commit_details(local_repo)
                    source_pkg_dir = local_repo / "src"
                    in_sync = True
                    mismatched: List[str] = []
                    if not editable and source_pkg_dir.is_dir():
                        in_sync, mismatched = _compare_installed_tree_to_source(pkg_dir, source_pkg_dir)

                    if not in_sync:
                        is_stale_snapshot = True
                        stale_file_count = len(mismatched)
                        local_repo_head = git_info.get("commit")
                        commit_source = (
                            f"STALE local snapshot ({stale_file_count} file(s) out of sync with {local_repo})"
                        )
                    elif git_info.get("commit"):
                        commit_sha = git_info["commit"]
                        commit_subject = str(git_info.get("commit_subject") or "")
                        commit_date = str(git_info.get("commit_date") or "")
                        branch = str(git_info.get("branch") or "")
                        is_dirty = bool(git_info.get("is_dirty"))
                        kind = "editable local git" if editable else "local git"
                        dirty_suffix = ", dirty" if is_dirty else ""
                        commit_source = f"direct_url.json ({kind}{dirty_suffix})"
    except Exception:
        pass

    if not version:
        version = PACKAGE_VERSION

    # Only check the package root git checkout, never an unrelated CWD repo
    if not commit_sha and not is_stale_snapshot:
        pkg_root = pkg_dir.parent
        if (pkg_root / ".git").exists():
            git_info = _get_git_commit_details(pkg_root)
            if git_info.get("commit"):
                commit_sha = git_info["commit"]
                commit_subject = str(git_info.get("commit_subject") or "")
                commit_date = str(git_info.get("commit_date") or "")
                branch = str(git_info.get("branch") or "")
                is_dirty = bool(git_info.get("is_dirty"))
                commit_source = "package git checkout (dirty)" if is_dirty else "package git checkout"

    commit_short = "stale" if is_stale_snapshot else (commit_sha[:7] if commit_sha else "unknown")

    return {
        "version": f"v{version.lstrip('v')}",
        "commit": commit_sha,
        "commit_short": commit_short,
        "commit_source": commit_source,
        "commit_subject": commit_subject,
        "commit_date": commit_date,
        "branch": branch,
        "is_dirty": is_dirty,
        "is_stale_snapshot": is_stale_snapshot,
        "stale_file_count": stale_file_count,
        "local_repo_head": local_repo_head,
        "installer": installer,
        "source_url": source_url,
        "package_path": str(pkg_dir),
    }


def get_local_version_info() -> Tuple[str, Optional[str]]:
    """Retrieve currently installed Repo360 version and git commit hash if available.

    Returns:
        Tuple of (version_str, commit_sha_or_none).
    """
    details = get_installation_details()
    return details["version"], details["commit"]


def is_output_already_up_to_date(stdout: str, stderr: str) -> bool:
    """Inspect installer stdout/stderr to determine if the package was already up-to-date."""
    combined = f"{stdout}\n{stderr}".lower()

    install_markers = [
        "successfully installed repo360",
        "installing collected packages: repo360",
        "upgraded repo360",
    ]
    if any(marker in combined for marker in install_markers):
        return False
    if re.search(r"^\s*\+\s+repo360(?:\s|==|@)", combined, re.MULTILINE):
        return False
    if re.search(r"\binstalled\s+\d+\s+package", combined) and "repo360" in combined:
        return False

    satisfied_markers = [
        "requirement already satisfied: repo360",
        "requirement already satisfied",
        "requirement already up-to-date",
        "already up to date",
        "already up-to-date",
        "repo360 is already at latest version",
        "repo360 is already at the latest version",
        "package repo360 is already up-to-date",
        "repo360 is unchanged",
        "unchanged at version",
        "nothing to do",
        "0 packages installed",
        "audited 1 package",
    ]
    return any(marker in combined for marker in satisfied_markers)


def _discover_windows_script_dirs() -> List[Path]:
    """Collect candidate Windows script directories where repo360*.exe launchers reside."""
    if sys.platform != "win32":
        return []
    candidates: List[Path] = [
        Path(sys.executable).parent,
        Path(sys.executable).parent / "Scripts",
    ]
    sys_scripts = sysconfig.get_path("scripts")
    if sys_scripts:
        candidates.append(Path(sys_scripts))
    if sys.prefix == sys.base_prefix:
        user_scripts = sysconfig.get_path("scripts", f"{os.name}_user")
        if user_scripts:
            candidates.append(Path(user_scripts))
    if _is_uv_tool_install():
        candidates.append(Path(os.path.expanduser("~")) / ".local" / "bin")

    seen = set()
    dirs: List[Path] = []
    for cand in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(cand)))
        except OSError:
            continue
        if key not in seen and cand.is_dir():
            seen.add(key)
            dirs.append(cand)
    return dirs


def _prepare_windows_binaries(scripts_dirs: Union[Path, Sequence[Path]]) -> List[Tuple[Path, Path]]:
    """On Windows, running .exe files cannot be deleted or overwritten in-place.

    Windows NT allows renaming running executables. Renaming existing entrypoints
    to temporary names allows the package installer to write clean new .exe files.
    """
    renamed: List[Tuple[Path, Path]] = []
    if sys.platform != "win32":
        return renamed

    target_dirs = [scripts_dirs] if isinstance(scripts_dirs, Path) else list(scripts_dirs)
    pid = os.getpid()
    seen_files = set()

    for scripts_dir in target_dirs:
        if not scripts_dir.is_dir():
            continue

        for old_file in scripts_dir.glob("*.exe.old_*"):
            try:
                old_file.unlink(missing_ok=True)
            except OSError:
                pass

        # Rename repo360* entrypoints to avoid Windows file locks during wheel extraction
        exe_candidates = [scripts_dir / "repo360.exe", *scripts_dir.glob("repo360-*.exe")]
        for exe_file in exe_candidates:
            if not exe_file.is_file():
                continue
            norm_path = os.path.normcase(os.path.abspath(str(exe_file)))
            if norm_path in seen_files:
                continue
            seen_files.add(norm_path)
            staged = scripts_dir / f"{exe_file.name}.old_{pid}"
            try:
                exe_file.rename(staged)
                renamed.append((exe_file, staged))
            except OSError as exc:
                logger.debug("Could not stage rename for %s: %s", exe_file, exc)

    return renamed


def _restore_windows_binaries(renamed: List[Tuple[Path, Path]], success: bool) -> None:
    """Restore executables if missing (failed or no-op upgrade), or clean up staged files."""
    if sys.platform != "win32":
        return

    for original, staged in renamed:
        if not staged.is_file():
            continue
        if not original.is_file():
            try:
                staged.rename(original)
            except OSError:
                pass
        elif success:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                # Windows holds running exe lock until process exits; cleaned up on next run
                pass


def _invalidate_package_bytecache() -> None:
    """Delete all __pycache__ directories under the installed repo360 package tree.

    After a git-based pip/pipx upgrade the installer extracts new .py source
    files but Python's import machinery may still load stale .pyc bytecode from
    the old version cached in __pycache__ directories.  Removing them forces
    fresh compilation on the next invocation, guaranteeing the upgraded code is
    actually executed — which is especially critical when running on a machine
    that previously had an older version installed.
    """
    import importlib
    import shutil as _shutil

    pkg_dir = Path(__file__).resolve().parent
    removed: List[str] = []
    for cache_dir in pkg_dir.rglob("__pycache__"):
        if cache_dir.is_dir():
            try:
                _shutil.rmtree(cache_dir)
                removed.append(str(cache_dir))
            except OSError:
                pass

    try:
        importlib.invalidate_caches()
    except Exception:
        pass

    if removed:
        logger.debug("Invalidated %d __pycache__ director(ies) after upgrade.", len(removed))


def execute_upgrade(candidates: List[Tuple[str, List[str]]], dry_run: bool = False) -> UpgradeResult:
    """Execute the upgrade process via subprocess with resilient fallback across strategies."""
    local_version, local_commit = get_local_version_info()
    if dry_run:
        chosen = candidates[0]
        print(f"\n\033[96m[DRY-RUN]\033[0m Would execute upgrade ({chosen[0]}):\n  {' '.join(chosen[1])}\n")
        return UpgradeResult(0, already_up_to_date=False, version=local_version, commit=local_commit)

    renamed = _prepare_windows_binaries(_discover_windows_script_dirs())
    child_env = {**os.environ, "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    success = False
    already_up_to_date = False
    stdout_str = ""
    stderr_str = ""
    strategy_errors: List[Tuple[str, str]] = []

    for strategy_name, cmd in candidates:
        logger.debug("Executing upgrade via %s: %s", strategy_name, " ".join(cmd))
        try:
            result = run_interruptible_subprocess(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
                env=child_env,
            )
            stdout_str = getattr(result, "stdout", "") or ""
            stderr_str = getattr(result, "stderr", "") or ""
            if result.returncode == 0:
                success = True
                already_up_to_date = is_output_already_up_to_date(stdout_str, stderr_str)
                break
            err_lines = [line.strip() for line in (stderr_str or stdout_str).splitlines() if line.strip()]
            err_msg = err_lines[-1] if err_lines else f"Exited with code {result.returncode}"
            strategy_errors.append((strategy_name, err_msg))
            logger.debug("Upgrade strategy '%s' failed: %s", strategy_name, err_msg)
        except subprocess.TimeoutExpired:
            logger.error("Upgrade command timed out after 300 seconds: %s", " ".join(cmd))
            strategy_errors.append((strategy_name, "Execution timed out after 300 seconds"))
        except Exception as exc:
            strategy_errors.append((strategy_name, str(exc) or type(exc).__name__))

    _restore_windows_binaries(renamed, success=success)

    # Clear .pyc bytecode so the next run loads the updated package
    if success and not already_up_to_date:
        _invalidate_package_bytecache()
    else:
        try:
            importlib.invalidate_caches()
        except Exception:
            pass

    post_version, post_commit = get_local_version_info()

    # Check commit hash if installer output doesn't show whether anything changed
    if success and not already_up_to_date:
        combined_output = f"{stdout_str}\n{stderr_str}".lower()
        has_new_install = any(
            marker in combined_output
            for marker in ("successfully installed repo360", "installing collected packages: repo360", "upgraded repo360")
        ) or bool(re.search(r"^\s*\+\s+repo360(?:\s|==|@)", combined_output, re.MULTILINE))
        if not has_new_install and local_commit and post_commit and local_commit.lower() == post_commit.lower():
            already_up_to_date = True

    if success:
        return UpgradeResult(0, already_up_to_date=already_up_to_date, version=post_version, commit=post_commit)

    meaningful_errors = [
        (name, msg)
        for name, msg in strategy_errors
        if "no module named pip" not in msg.lower()
    ]
    chosen_strategy, last_error = (
        meaningful_errors[0]
        if meaningful_errors
        else (strategy_errors[0] if strategy_errors else ("upgrade", "Unknown upgrade error"))
    )
    print(f"\n\033[91m[FAIL]\033[0m Upgrade failed ({chosen_strategy}): {last_error}\n")
    return UpgradeResult(1, already_up_to_date=False, version=local_version, commit=local_commit)


def verify_local_state() -> None:
    """Verify that user credentials and configurations remain intact post-upgrade."""
    config_file = Path(get_credentials_path())
    secrets_dir = Path(get_secrets_dir())

    config_ok = config_file.is_file()
    pem_count = len(list(secrets_dir.glob("*.pem"))) if secrets_dir.is_dir() else 0

    print("\033[96m[VERIFY]\033[0m Verifying configuration integrity:")
    if config_ok:
        print(f"  Configuration file : \033[92m[OK]\033[0m {config_file}")
    else:
        print(f"  Configuration file : \033[93m[NOTICE]\033[0m Not configured yet (run 'repo360 setup')")

    if pem_count > 0:
        print(f"  GitHub App keys    : \033[92m[OK]\033[0m {pem_count} private key(s) detected in {secrets_dir}")
    else:
        print(f"  GitHub App keys    : \033[93m[NOTICE]\033[0m No stored private keys found in {secrets_dir}")


def format_version_output(details: Dict[str, Any]) -> str:
    """Format a minimal standard CLI version string (e.g. 'repo360 1.0.0 (79098e8)')."""
    fallback_ver = PACKAGE_VERSION.lstrip("v") or "1.0.0"
    raw_ver = str(details.get("version") or fallback_ver).strip().lstrip("v") or fallback_ver
    if details.get("is_stale_snapshot"):
        return f"repo360 {raw_ver} (stale)"
    commit_sha = str(details.get("commit") or "").strip()
    if commit_sha:
        return f"repo360 {raw_ver} ({commit_sha[:7]})"
    return f"repo360 {raw_ver}"


def run_version_cli(argv: Optional[List[str]] = None) -> int:
    """CLI handler for 'repo360 version'."""
    parser = argparse.ArgumentParser(
        prog="repo360 version",
        description="Show installed Repo360 version.",
    )
    parser.parse_args(argv)
    print(format_version_output(get_installation_details()))
    return 0


def run_upgrade_cli(argv: Optional[List[str]] = None) -> int:
    """CLI orchestrator for 'repo360 upgrade' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 upgrade",
        description="Upgrade Repo360 to the latest release, a specific tag, or the latest commit for testing.",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--tag",
        metavar="TAG",
        default="",
        help="Specific release tag to install or switch to (e.g. 'v1.0.0'; default: latest release).",
    )
    target_group.add_argument(
        "--latest-commit",
        "--edge",
        dest="latest_commit",
        action="store_true",
        help="Upgrade directly to the latest commit on the default branch (for testing without a release).",
    )
    target_group.add_argument(
        "--ref",
        metavar="REF",
        default="",
        help="Upgrade to a specific git branch or commit SHA (e.g. 'main' or '12805b0'; for testing).",
    )
    args = parser.parse_args(argv)

    requested_tag = (args.tag or "").strip()
    requested_ref = (args.ref or "").strip()
    use_commit_mode = bool(args.latest_commit or requested_ref)

    if requested_tag:
        try:
            requested_tag = normalize_release_tag(requested_tag)
        except ValueError as exc:
            parser.error(str(exc))
    if requested_ref:
        try:
            requested_ref = normalize_release_tag(requested_ref)
        except ValueError as exc:
            parser.error(str(exc))

    local_details = get_installation_details()
    local_ver = str(local_details.get("version") or PACKAGE_VERSION)
    local_sha = local_details.get("commit")
    local_dirty = bool(local_details.get("is_dirty"))
    is_stale = bool(local_details.get("is_stale_snapshot"))

    if use_commit_mode:
        target = resolve_commit_target(
            DEFAULT_REPO_URL,
            requested_ref=None if args.latest_commit else requested_ref,
        )
        if not target.commit:
            if requested_ref:
                print(f"Branch or commit '{requested_ref}' was not found on {DEFAULT_REPO_URL}.")
            else:
                print(
                    f"Unable to resolve the latest commit from {DEFAULT_REPO_URL}. "
                    "Check network connectivity and try again."
                )
            return 1
        install_ref = target.commit
        target_label = f"{target.tag} ({target.commit[:7]})" if target.tag and target.tag != "HEAD" else target.commit[:7]
    else:
        target = resolve_release_target(DEFAULT_REPO_URL, requested_tag=requested_tag or None)
        if not target.tag:
            if requested_tag:
                print(f"Release tag '{requested_tag}' was not found on {DEFAULT_REPO_URL}.")
            elif target.source == "no-releases":
                print(
                    f"No published releases found for {DEFAULT_REPO_URL}. "
                    "Create a release tag (e.g. v1.0.0) or run 'repo360 upgrade --latest-commit' for testing."
                )
            else:
                print(
                    f"Unable to resolve release information from {DEFAULT_REPO_URL}. "
                    "Check network connectivity and try again."
                )
            return 1
        install_ref = target.tag
        target_label = target.tag

    if (
        not is_stale
        and not local_dirty
        and local_sha
        and target.commit
        and _commits_match(local_sha, target.commit)
    ):
        ver_display = local_ver.lstrip("v")
        if requested_tag or requested_ref:
            print(f"Repo360 {ver_display} ({target.commit[:7]}) is already at {target_label}.")
        else:
            print(f"Repo360 {ver_display} ({target.commit[:7]}) is already up to date.")
        return 0

    force = bool(
        is_stale
        or local_dirty
        or local_sha is None
        or not target.commit
        or not _commits_match(local_sha, target.commit)
    )
    candidates = get_upgrade_commands(
        DEFAULT_REPO_URL,
        force=force,
        target_ref=install_ref,
    )
    if requested_tag or requested_ref:
        print(f"Installing Repo360 {target_label}...")
    elif args.latest_commit:
        print(f"Upgrading Repo360 to latest commit ({target_label})...")
    else:
        print(f"Upgrading Repo360 to {target_label}...")

    res = execute_upgrade(candidates)
    exit_code = int(res)
    if exit_code == 0:
        display_ver = (getattr(res, "version", "") or local_ver).lstrip("v")
        post_commit = getattr(res, "commit", None)
        commit_suffix = f" ({post_commit[:7]})" if post_commit else ""

        if getattr(res, "already_up_to_date", False):
            if requested_tag or requested_ref:
                print(f"Repo360 {display_ver}{commit_suffix} is already at {target_label}.")
            else:
                print(f"Repo360 {display_ver}{commit_suffix} is already up to date.")
        elif requested_tag or requested_ref:
            print(f"Repo360 {display_ver}{commit_suffix} installed ({target_label}).")
        else:
            print(f"Repo360 upgraded to {display_ver}{commit_suffix}.")

        if target.commit and post_commit and not _commits_match(post_commit, target.commit):
            expected_desc = f"{target.tag} ({target.commit[:7]})" if not use_commit_mode else target_label
            print(
                f"Warning: Installed commit ({post_commit[:7]}) differs from {expected_desc}."
            )
    else:
        print(f"Upgrade failed with exit code {exit_code}.")

    return exit_code
