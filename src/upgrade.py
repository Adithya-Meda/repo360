"""Production-grade upgrade utility for the Repo360 CLI suite."""

from __future__ import annotations

import argparse
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

from .common.interrupts import run_interruptible_subprocess
from .credentials import get_credentials_path, get_secrets_dir

logger = logging.getLogger("repo360.upgrade")

DEFAULT_REPO_URL = "https://github.com/Adithya-Meda/repo360.git"
GITHUB_API_COMMITS = "https://api.github.com/repos/Adithya-Meda/repo360/commits/main"


def find_pipx_executable() -> Optional[str]:
    """Locate pipx executable in PATH or standard user package script directories."""
    path_hit = shutil.which("pipx")
    if path_hit:
        return path_hit

    appdata = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(os.path.expanduser("~")) / ".local" / "bin" / "pipx.exe",
        Path(os.path.expanduser("~")) / ".local" / "bin" / "pipx",
    ]
    if appdata:
        candidates.extend([
            Path(appdata) / "Programs" / "Python" / "Scripts" / "pipx.exe",
            Path(appdata) / "pipx" / "pipx.exe",
        ])
        packages_dir = Path(appdata) / "Packages"
        if packages_dir.is_dir():
            for p in packages_dir.glob("PythonSoftwareFoundation.Python.*/LocalCache/local-packages/Python*/Scripts/pipx.exe"):
                candidates.append(p)

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_uv_executable() -> Optional[str]:
    """Locate uv executable in PATH, user tool bins, or bundled inside pipx[uv]."""
    path_hit = shutil.which("uv")
    if path_hit:
        return path_hit

    exe_name = "uv.exe" if sys.platform == "win32" else "uv"
    candidates: List[Path] = [
        Path(os.path.expanduser("~")) / ".local" / "bin" / exe_name,
    ]
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

    for c in candidates:
        if c.is_file():
            return str(c)
    return None


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
    """Build a PEP 508 VCS target specifier optionally pinned to an exact commit SHA."""
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
        # Use 'pipx runpip' to safely upgrade in-place without tearing down the venv,
        # preventing Windows OS Error 32 (file lock on running python.exe)
        return "pipx", [pipx_bin, "runpip", _detect_pipx_venv_name(), "install", "--upgrade"]

    # Fallback to python -m pip using the current active interpreter
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
            # Windows fallback: avoid failing on locked C extensions (e.g. cryptography)
            commands.append(
                ("pip-isolated", [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", "--no-cache-dir", target_spec])
            )
        return commands

    # 1. In uv-tool: native uv tool install updates both venv, receipt, and exposed entrypoints
    if _is_uv_tool_install() and uv_bin and installer_override != "pipx":
        uv_tool_flags = ["--refresh-package", "repo360"]
        if force:
            uv_tool_flags.extend(["--force", "--reinstall-package", "repo360"])
        commands.append(("uv-tool", [uv_bin, "tool", "install", *uv_tool_flags, target_spec]))

    # 2. In pipx: in-place upgrade via 'pipx runpip' avoids OS Error 32 on Windows and venv rebuilds
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

    # 3. Direct uv pip install fallback (works seamlessly even inside pip-less uv/pipx venvs)
    if uv_bin:
        uv_flags = (
            ["--refresh-package", "repo360", "--reinstall-package", "repo360"]
            if force
            else ["--upgrade", "--refresh-package", "repo360"]
        )
        commands.append(("uv", [uv_bin, "pip", "install", "--python", sys.executable, *uv_flags, target_spec]))

    # 4. Universal Python interpreter pip fallback (only if pip exists or no other installer matched)
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


def check_remote_status(repo_url: str = DEFAULT_REPO_URL) -> Tuple[bool, str]:
    """Check remote GitHub repository for the latest commit/release on main.

    Returns:
        Tuple of (success_bool, message_or_commit_info).
    """
    summary = _fetch_commit_summary(repo_url, "HEAD", timeout=5.0)
    if summary and summary.get("sha"):
        sha = summary["sha"][:8]
        date = summary.get("date", "")
        msg = summary.get("subject", "")
        return True, f"Latest version on main: {sha} ({date}) - '{msg}'"

    # Protocol-level fallback via 'git ls-remote' when GitHub REST API rate limit is exceeded
    try:
        res = run_interruptible_subprocess(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if res.returncode == 0 and res.stdout.strip():
            sha = res.stdout.strip().splitlines()[0].split()[0][:8]
            return True, f"Latest version on main: {sha} (via git remote)"
    except Exception:
        pass

    return False, "Could not query remote GitHub repository."


def get_remote_commit_sha(repo_url: str = DEFAULT_REPO_URL) -> Optional[str]:
    """Fetch the latest commit SHA for the default branch from git ls-remote or GitHub API.

    Returns:
        Full commit SHA string if retrieved successfully, or None.
    """
    # 1. Primary: Use 'git ls-remote' - protocol-level query with zero REST API rate limit
    try:
        res = run_interruptible_subprocess(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().splitlines()[0]
            sha = first_line.split()[0]
            if len(sha) >= 7:
                return sha
    except Exception:
        pass

    # 2. Secondary: Fallback to GitHub REST API with optional auth headers
    summary = _fetch_commit_summary(repo_url, "HEAD", timeout=5.0)
    if summary and summary.get("sha"):
        return summary["sha"]
    return None


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
    # Strip extra leading slash on Windows drive paths (e.g. '/C:/...' -> 'C:/...')
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
    version = "1.0.0"
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
        version = dist.version or version
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

    if not version or version == "1.0.0":
        try:
            from . import __version__

            version = __version__
        except Exception:
            version = "1.0.0"

    # Fall back to package root .git checkout only (never an unrelated CWD repo).
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

    # Explicit indications that new files or packages were installed (pip, pipx, and uv)
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

    # Explicit indications that requirements were already satisfied
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

        # Clean up stale .old files from prior runs
        for old_file in scripts_dir.glob("*.exe.old_*"):
            try:
                old_file.unlink(missing_ok=True)
            except OSError:
                pass

        # Rename repo360* entrypoints to avoid OS error 5 / 32 during wheel extraction
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

    # Notify the import system that the filesystem has changed.
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

    # Invalidate stale .pyc bytecache so the next process invocation loads the
    # freshly installed source rather than cached bytecode from the old version.
    if success and not already_up_to_date:
        _invalidate_package_bytecache()
    else:
        try:
            importlib.invalidate_caches()
        except Exception:
            pass

    post_version, post_commit = get_local_version_info()

    # If the installer output didn't explicitly match, verify if commit hash
    # remained unchanged without any new repo360 packages being installed
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


def _extract_remote_sha_from_status(msg: str) -> Optional[str]:
    """Extract commit SHA prefix from check_remote_status() text when available."""
    match = re.search(r"Latest version on main:\s*([0-9a-fA-F]{7,40})", msg or "")
    return match.group(1) if match else None


def _resolve_commit_metadata(details: Dict[str, Any], repo_url: str) -> Dict[str, Any]:
    """Enrich installed commit details with subject/date from GitHub API when absent locally."""
    enriched = dict(details)
    commit_sha = enriched.get("commit")
    if commit_sha and (not enriched.get("commit_subject") or not enriched.get("commit_date")):
        source_url = str(enriched.get("source_url") or "")
        lookup_url = source_url if _parse_github_owner_repo(source_url) else repo_url
        summary = _fetch_commit_summary(lookup_url, str(commit_sha), timeout=3.0)
        if summary:
            if not enriched.get("commit_subject") and summary.get("subject"):
                enriched["commit_subject"] = summary["subject"]
            if not enriched.get("commit_date") and summary.get("date"):
                enriched["commit_date"] = summary["date"]
    return enriched


def _print_installed_details(details: Dict[str, Any]) -> None:
    """Print standardized local installation and commit metadata cleanly."""
    version_display = details.get("version", "v1.0.0")
    source_display = details.get("commit_source", "unknown")
    installer_display = details.get("installer", "")
    source_suffix = f" via {installer_display}" if installer_display else ""

    print(f"Repo360 {version_display}")
    if details.get("is_stale_snapshot"):
        print(f"  Installed Commit : stale (source: {source_display}{source_suffix})")
        if details.get("local_repo_head"):
            print(f"  Local Repo HEAD  : {details['local_repo_head']} (installed site-packages files differ)")
    else:
        commit_sha = details.get("commit") or "unknown"
        branch = details.get("branch")
        branch_tag = f" [{branch}]" if branch else ""
        dirty_tag = " [uncommitted edits]" if details.get("is_dirty") else ""
        print(f"  Installed Commit : {commit_sha}{branch_tag}{dirty_tag} (source: {source_display}{source_suffix})")

    if details.get("commit_subject"):
        print(f"  Commit Message   : {details['commit_subject']}")
    if details.get("commit_date"):
        print(f"  Commit Date      : {details['commit_date']}")
    print(f"  Package Path     : {details.get('package_path', 'unknown')}")


def check_installation_against_remote(
    repo_url: str = DEFAULT_REPO_URL,
    strict: bool = False,
) -> int:
    """Print local version/commit details and compare against remote main HEAD."""
    details = _resolve_commit_metadata(get_installation_details(), repo_url)
    local_sha = details.get("commit")
    is_stale = bool(details.get("is_stale_snapshot"))

    _print_installed_details(details)

    print(f"\n\033[96m[INFO]\033[0m Checking {repo_url} for latest commit...")
    ok, msg = check_remote_status(repo_url)
    if ok:
        print(f"\033[92m[PASS]\033[0m {msg}")
    else:
        print(f"\033[93m[WARN]\033[0m {msg}")

    remote_sha = get_remote_commit_sha(repo_url) or _extract_remote_sha_from_status(msg)
    if is_stale:
        remote_hint = f" Latest main is {remote_sha[:7]}." if remote_sha else ""
        print(
            f"\033[93m[STALE]\033[0m Installed package copy in site-packages does not match the local git checkout.{remote_hint} "
            "Reinstall with 'uv pip install --python <env> --reinstall .' or 'repo360 upgrade --force'.\n"
        )
        return 1 if strict else 0

    if not remote_sha:
        print("\033[93m[WARN]\033[0m Unable to verify remote commit SHA right now.")
        return 1 if strict else 0

    if not local_sha:
        print(
            f"\033[93m[WARN]\033[0m Local commit SHA is unavailable (latest main is {remote_sha[:7]}). "
            "Run 'repo360 upgrade --force' for a tracked VCS install."
        )
        return 1 if strict else 0

    if _commits_match(local_sha, remote_sha):
        print(f"\033[92m[UP-TO-DATE]\033[0m Installed commit ({local_sha[:7]}) matches latest main ({remote_sha[:7]}).\n")
        return 0

    print(
        f"\033[93m[OUTDATED]\033[0m Installed commit ({local_sha[:7]}) is behind latest main ({remote_sha[:7]}). "
        "Run 'repo360 upgrade --force' to install the latest commit.\n"
    )
    return 1 if strict else 0


def run_version_cli(argv: Optional[List[str]] = None) -> int:
    """CLI handler for 'repo360 version' / '--version' / '-V'."""
    parser = argparse.ArgumentParser(
        prog="repo360 version",
        description="Display installed Repo360 version, commit details, and remote sync status.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify whether the installed commit matches the latest commit on GitHub main.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Show local installation and commit details without querying GitHub.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if the installed commit is outdated, stale, or unknown.",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_REPO_URL,
        help=f"Custom Git repository source URL (default: {DEFAULT_REPO_URL}).",
    )
    args = parser.parse_args(argv)

    if args.offline and not args.check:
        details = get_installation_details()
        _print_installed_details(details)
        print("  Remote Check     : Skipped (--offline mode).\n")
        return 1 if (args.strict and (details.get("is_stale_snapshot") or not details.get("commit"))) else 0

    return check_installation_against_remote(args.source, strict=args.strict)


def run_upgrade_cli(argv: Optional[List[str]] = None) -> int:
    """CLI orchestrator for 'repo360 upgrade' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 upgrade",
        description="Upgrade Repo360 CLI to the latest version directly from GitHub.",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_REPO_URL,
        help=f"Custom Git repository source URL (default: {DEFAULT_REPO_URL}).",
    )
    parser.add_argument(
        "--pip",
        action="store_true",
        help="Force using standard 'python -m pip' instead of auto-detecting pipx.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check local commit vs remote repository status without installing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force clean package reinstallation from remote source, bypassing caches.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned upgrade command without executing it.",
    )

    args = parser.parse_args(argv)

    installer_override = "pip" if args.pip else None

    # Handle --check
    if args.check:
        code = check_installation_against_remote(args.source, strict=False)
        installer, cmd = build_upgrade_command(args.source, installer_override)
        print(f"Run 'repo360 upgrade' to install via {installer}: {' '.join(cmd)}\n")
        return code

    local_ver, local_sha = get_local_version_info()
    remote_sha: Optional[str] = None

    # Pre-flight check: resolve remote default branch HEAD so we can short-circuit
    # when already synced, or pin @<remote_sha> to bypass stale 1.0.0 caches.
    if not args.dry_run:
        remote_sha = get_remote_commit_sha(args.source)
        if not args.force and local_sha and remote_sha and _commits_match(local_sha, remote_sha):
            print(f"\033[92m[SUCCESS]\033[0m Repo360 is already on the latest version ({local_ver} @ {remote_sha[:7]}).\n")
            return 0

    # Because package version stays 1.0.0 across commits, force reinstall when updating commits,
    # refreshing stale/unknown snapshots, or when remote status could not be confirmed pre-flight.
    effective_force = bool(
        args.force
        or (not args.dry_run and (local_sha is None or not remote_sha or not _commits_match(local_sha, remote_sha)))
    )
    candidates = get_upgrade_commands(
        args.source,
        installer_override,
        force=effective_force,
        target_ref=remote_sha if not args.dry_run else None,
    )
    print("\033[96m[INFO]\033[0m Upgrading Repo360 to the latest version...")

    res = execute_upgrade(candidates, dry_run=args.dry_run)
    exit_code = int(res)
    if exit_code == 0 and not args.dry_run:
        display_ver = getattr(res, "version", "") or local_ver
        post_commit = getattr(res, "commit", None)
        commit_suffix = f" @ {post_commit[:7]}" if post_commit else ""
        if not remote_sha:
            remote_sha = get_remote_commit_sha(args.source)

        if getattr(res, "already_up_to_date", False):
            print(f"\033[92m[SUCCESS]\033[0m Repo360 is already on the latest version ({display_ver}{commit_suffix}).")
        else:
            print(f"\033[92m[SUCCESS]\033[0m Repo360 upgraded successfully ({display_ver}{commit_suffix}).")

        if remote_sha and post_commit:
            if _commits_match(post_commit, remote_sha):
                if not getattr(res, "already_up_to_date", False):
                    print(
                        f"\033[92m[VERIFIED]\033[0m Installed commit {post_commit[:7]} matches "
                        f"remote main HEAD ({remote_sha[:7]}). Latest changes are live locally."
                    )
            else:
                print(
                    f"\033[93m[WARN]\033[0m Installed commit ({post_commit[:7]}) does not match "
                    f"remote main ({remote_sha[:7]}). Run 'repo360 upgrade --force' to refresh "
                    "cached artifacts."
                )
        elif remote_sha and not post_commit:
            print(
                f"\033[93m[WARN]\033[0m Could not resolve installed commit SHA. "
                f"Remote main is at {remote_sha[:7]}. "
                "Run 'repo360 version --check' to confirm sync status."
            )
    elif exit_code != 0:
        print(f"\033[91m[FAIL]\033[0m Upgrade failed with exit code {exit_code}.\n")

    return exit_code
