"""Clean, cross-platform uninstaller for the Repo360 CLI suite.

Removes Repo360 packages, isolated virtual environments, shims, and configuration
directories (~/.repo360) while strictly preserving all generated reports in
repo360-output.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .common.interrupts import interruptible_input, run_interruptible_subprocess
from .credentials import resolve_user_config_dir
from .upgrade import find_pipx_executable, get_installation_details

logger = logging.getLogger("repo360.uninstall")

PROTECTED_REPORT_DIR_NAME = "repo360-output"
_OWNED_CONFIG_DIR_NAMES = frozenset({".repo360", "repo360"})

# Baseline settling duration (3.0s) + additional confirmation grace timer (4.0s)
DEFAULT_UNINSTALL_SETTLING_DELAY_SECONDS: float = 3.0
DEFAULT_UNINSTALL_GRACE_TIMER_SECONDS: float = 4.0
DEFAULT_UNINSTALL_DELAY_SECONDS: float = (
    DEFAULT_UNINSTALL_SETTLING_DELAY_SECONDS + DEFAULT_UNINSTALL_GRACE_TIMER_SECONDS
)

_WIN_DEFERRED_UNINSTALL_SCRIPT = """import ctypes
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


def _on_rm_error(func, path, exc_info=None):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _rmtree(target: Path) -> None:
    kwargs = {"onexc": _on_rm_error} if sys.version_info >= (3, 12) else {"onerror": _on_rm_error}
    shutil.rmtree(target, **kwargs)


def _is_safe_purge_target(target: Path) -> bool:
    try:
        resolved = target.resolve()
    except OSError:
        return False
    if resolved == resolved.parent or resolved == Path.home().resolve():
        return False
    if resolved.name.lower() not in ("repo360", ".repo360"):
        return False
    if any(part.lower() == "repo360-output" for part in resolved.parts):
        return False
    return True


def main() -> None:
    if len(sys.argv) < 6:
        return
    parent_pid = int(sys.argv[1])
    pipx_bin = sys.argv[2]
    venv_arg = sys.argv[3]
    shim_arg = sys.argv[4]
    config_arg = sys.argv[5]
    if len(sys.argv) >= 8 and (";" in sys.argv[6] or sys.argv[6] == "" or Path(sys.argv[6]).is_dir()):
        script_dirs_arg = sys.argv[6]
        site_dirs_arg = sys.argv[7]
        cmd = sys.argv[8:]
    else:
        script_dirs_arg = ""
        site_dirs_arg = ""
        cmd = sys.argv[6:]

    # Wait until the parent repo360 Python process terminates cleanly so Windows unlocks executables.
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, parent_pid)
    if handle:
        k32.WaitForSingleObject(handle, 15000)
        k32.CloseHandle(handle)
    time.sleep(0.5)

    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    si.wShowWindow = 0
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    for _ in range(4):
        if cmd:
            res = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=si,
                check=False,
            )
            if res.returncode == 0:
                break
        elif pipx_bin:
            res = subprocess.run(
                [pipx_bin, "uninstall", "repo360"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                startupinfo=si,
                check=False,
            )
            if res.returncode == 0:
                break
        time.sleep(0.4)

    # Purge all repo360 binaries and stale staged files across all script directories
    for s_dir_str in script_dirs_arg.split(";"):
        if not s_dir_str.strip():
            continue
        s_dir = Path(s_dir_str)
        if not s_dir.is_dir():
            continue
        for pattern in (
            "repo360*.exe",
            "repo360*.exe.old_*",
            "repo360*.old_*",
            "repo360*.cmd",
            "repo360*.bat",
            "repo360*.ps1",
            "repo360-script.py",
        ):
            for f in s_dir.glob(pattern):
                for _ in range(5):
                    try:
                        os.chmod(f, stat.S_IWRITE)
                        f.unlink(missing_ok=True)
                        break
                    except OSError:
                        time.sleep(0.2)

    # Purge site-packages leftovers (__editable__.repo360*, dist-info, etc.)
    for site_dir_str in site_dirs_arg.split(";"):
        if not site_dir_str.strip():
            continue
        site_dir = Path(site_dir_str)
        if not site_dir.is_dir():
            continue
        for pattern in (
            "repo360",
            "repo360.egg-link",
            "repo360*.dist-info",
            "repo360*.egg-info",
            "__editable__.repo360*.pth",
            "__editable___repo360_*.pth",
            "repo360.pth",
        ):
            for target in site_dir.glob(pattern):
                for _ in range(5):
                    try:
                        if target.is_dir():
                            _rmtree(target)
                        else:
                            os.chmod(target, stat.S_IWRITE)
                            target.unlink(missing_ok=True)
                        break
                    except OSError:
                        time.sleep(0.2)

    if venv_arg:
        venv_dir = Path(venv_arg)
        if venv_dir.exists() and _is_safe_purge_target(venv_dir):
            for _ in range(3):
                try:
                    _rmtree(venv_dir)
                except OSError:
                    pass
                if not venv_dir.exists():
                    break
                time.sleep(0.4)

    if shim_arg:
        shim_path = Path(shim_arg)
        if shim_path.parent.is_dir():
            for p in (shim_path, *shim_path.parent.glob(f"{shim_path.stem}*")):
                if p.is_file() and p.stem.lower().startswith("repo360"):
                    for _ in range(5):
                        try:
                            os.chmod(p, stat.S_IWRITE)
                            p.unlink(missing_ok=True)
                            break
                        except OSError:
                            time.sleep(0.2)

    if config_arg:
        cfg_dir = Path(config_arg)
        if cfg_dir.exists() and _is_safe_purge_target(cfg_dir):
            for _ in range(3):
                try:
                    _rmtree(cfg_dir)
                except OSError:
                    pass
                if not cfg_dir.exists():
                    break
                time.sleep(0.4)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            Path(sys.argv[0]).unlink(missing_ok=True)
        except OSError:
            pass
"""


def _handle_remove_readonly(func: Callable[[str], Any], path: str, _exc_info: Any = None) -> None:
    """Clear read-only attribute and retry deletion on Windows/POSIX."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def _is_protected_path(candidate: Path) -> bool:
    """Return True if candidate must never be deleted (reports, home, cwd, roots, or system prefix)."""
    try:
        resolved = candidate.resolve()
    except OSError:
        return True

    if resolved == resolved.parent:
        return True

    if any(part.casefold() == PROTECTED_REPORT_DIR_NAME for part in resolved.parts):
        return True

    cwd_resolved = Path.cwd().resolve()
    home_resolved = Path.home().resolve()
    base_prefix_resolved = Path(sys.base_prefix).resolve()
    protected_exact = {cwd_resolved, home_resolved, base_prefix_resolved}
    if resolved in protected_exact:
        return True

    reports_dir = cwd_resolved / PROTECTED_REPORT_DIR_NAME
    if reports_dir == resolved or reports_dir.is_relative_to(resolved):
        return True

    return False


def _safe_rmtree(target: Path) -> Tuple[bool, str]:
    """Safely remove a directory tree after validating protection guards."""
    if not target.exists() and not target.is_symlink():
        return True, "not present"

    if _is_protected_path(target):
        return False, f"Refused to delete protected path: {target}"

    if target.is_symlink() or target.is_file():
        try:
            target.unlink()
        except PermissionError:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            target.unlink()
        return True, f"Removed {target}"

    kwargs: Dict[str, Any] = (
        {"onexc": _handle_remove_readonly}
        if sys.version_info >= (3, 12)
        else {"onerror": _handle_remove_readonly}
    )
    shutil.rmtree(target, **kwargs)
    if target.exists():
        return False, f"Directory could not be completely removed (files may be in use): {target}"
    return True, f"Removed {target}"


def _find_pipx_venv_dir(pipx_bin: Optional[str]) -> Optional[Path]:
    """Locate the isolated pipx virtual environment directory for repo360 if installed via pipx."""
    prefix_path = Path(sys.prefix)
    if "pipx" in sys.prefix.casefold() and prefix_path.name.casefold() == "repo360" and prefix_path.is_dir():
        return prefix_path

    if pipx_bin:
        try:
            extra_kwargs: Dict[str, Any] = {}
            if sys.platform == "win32":
                extra_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                si = subprocess.STARTUPINFO()
                si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
                si.wShowWindow = 0
                extra_kwargs["startupinfo"] = si
            res = subprocess.run(
                [pipx_bin, "environment", "--value", "PIPX_LOCAL_VENVS"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                **extra_kwargs,
            )
            if res.returncode == 0 and res.stdout.strip():
                cand = Path(res.stdout.strip().splitlines()[0]) / "repo360"
                if cand.is_dir():
                    return cand
        except Exception as exc:
            logger.debug("Best-effort step skipped: %s", exc)

    home = Path.home()
    candidates: List[Path] = []
    env_local_venvs = os.environ.get("PIPX_LOCAL_VENVS")
    if env_local_venvs:
        candidates.append(Path(env_local_venvs) / "repo360")
    env_pipx_home = os.environ.get("PIPX_HOME")
    if env_pipx_home:
        candidates.append(Path(env_pipx_home) / "venvs" / "repo360")

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.extend([
            Path(local_appdata) / "pipx" / "pipx" / "venvs" / "repo360",
            Path(local_appdata) / "pipx" / "venvs" / "repo360",
        ])

    if sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "pipx" / "venvs" / "repo360")

    candidates.extend([
        home / ".local" / "pipx" / "venvs" / "repo360",
        home / ".local" / "share" / "pipx" / "venvs" / "repo360",
    ])

    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def _is_running_inside_dir(target_dir: Optional[Path]) -> bool:
    """Return True if the current Python interpreter executes from within target_dir."""
    if not target_dir:
        return False
    try:
        return Path(sys.executable).resolve().is_relative_to(target_dir.resolve())
    except OSError:
        return False


def _find_external_python(exclude_dir: Optional[Path] = None) -> Optional[str]:
    """Locate a Python executable outside exclude_dir for deferred Windows cleanup."""
    candidates: List[str] = []
    base_exec = getattr(sys, "_base_executable", None)
    if base_exec:
        candidates.append(str(base_exec))

    if sys.platform == "win32":
        for name in ("pythonw", "python", "python3"):
            hit = shutil.which(name)
            if hit:
                candidates.append(hit)
        candidates.append(str(Path(sys.base_prefix) / "pythonw.exe"))
        candidates.append(str(Path(sys.base_prefix) / "python.exe"))
    else:
        for name in ("python3", "python"):
            hit = shutil.which(name)
            if hit:
                candidates.append(hit)
        candidates.append(str(Path(sys.base_prefix) / "python3"))

    resolved_exclude: Optional[Path] = None
    if exclude_dir:
        try:
            resolved_exclude = exclude_dir.resolve()
        except OSError:
            resolved_exclude = None

    for cand in candidates:
        cand_path = Path(cand)
        # On Windows, prefer pythonw.exe over python.exe to prevent any console window popup
        if sys.platform == "win32" and cand_path.stem.lower() == "python":
            gui_cand = cand_path.with_name("pythonw.exe")
            if gui_cand.is_file():
                cand_path = gui_cand

        if not cand_path.is_file():
            continue
        try:
            if resolved_exclude and cand_path.resolve().is_relative_to(resolved_exclude):
                continue
        except OSError:
            continue
        return str(cand_path)
    return None


def _resolve_package_uninstall_command(
    pipx_bin: Optional[str],
    pipx_venv_dir: Optional[Path],
    details: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Determine the primary package manager command to uninstall repo360."""
    if pipx_bin and (pipx_venv_dir is not None or "pipx" in sys.prefix.casefold()):
        return "pipx", [pipx_bin, "uninstall", "repo360"]

    uv_bin = shutil.which("uv")
    if details.get("installer") == "uv" and uv_bin:
        return "uv", [uv_bin, "pip", "uninstall", "--python", sys.executable, "repo360"]

    return "pip", [sys.executable, "-m", "pip", "uninstall", "-y", "repo360"]


def _discover_script_dirs() -> List[Path]:
    """Discover all standard and environment script directories where CLI executables may reside."""
    dirs: List[Path] = []
    py_dir = Path(sys.executable).parent
    dirs.append(py_dir)
    dirs.append(py_dir / "Scripts")

    base_dir = Path(sys.base_prefix)
    dirs.append(base_dir)
    dirs.append(base_dir / "Scripts")

    try:
        import sysconfig

        s_def = sysconfig.get_path("scripts")
        if s_def:
            dirs.append(Path(s_def))
        if f"{os.name}_user" in sysconfig.get_scheme_names():
            s_user = sysconfig.get_path("scripts", f"{os.name}_user")
            if s_user:
                dirs.append(Path(s_user))
    except Exception as exc:
        logger.debug("sysconfig script discovery skipped: %s", exc)

    try:
        import site

        if hasattr(site, "USER_BASE") and site.USER_BASE:
            dirs.append(Path(site.USER_BASE) / "Scripts")
            dirs.append(Path(site.USER_BASE) / "bin")
    except Exception as exc:
        logger.debug("site USER_BASE discovery skipped: %s", exc)

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        dirs.append(Path(local_appdata) / "pipx" / "pipx")
        dirs.append(Path(local_appdata) / "Programs" / "Python")

    which_bin = shutil.which("repo360")
    if which_bin:
        dirs.append(Path(which_bin).parent)

    if sys.argv and "repo360" in Path(sys.argv[0]).name.casefold():
        dirs.append(Path(sys.argv[0]).parent)

    for env_path in os.environ.get("PATH", "").split(os.pathsep):
        if env_path.strip():
            p = Path(env_path.strip())
            if p.is_dir():
                try:
                    if any(p.glob("repo360*")):
                        dirs.append(p)
                except OSError:
                    pass

    unique: List[Path] = []
    seen = set()
    for d in dirs:
        try:
            resolved = d.resolve()
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                unique.append(resolved)
        except OSError:
            pass
    return unique


def _discover_site_packages_dirs() -> List[Path]:
    """Discover all standard and user site-packages directories."""
    dirs: List[Path] = []
    try:
        import site

        if hasattr(site, "getsitepackages"):
            for p in site.getsitepackages():
                dirs.append(Path(p))
        if hasattr(site, "getusersitepackages"):
            dirs.append(Path(site.getusersitepackages()))
    except Exception as exc:
        logger.debug("site-packages discovery skipped: %s", exc)

    try:
        import sysconfig

        for key in ("purelib", "platlib"):
            p = sysconfig.get_path(key)
            if p:
                dirs.append(Path(p))
            if f"{os.name}_user" in sysconfig.get_scheme_names():
                p_user = sysconfig.get_path(key, f"{os.name}_user")
                if p_user:
                    dirs.append(Path(p_user))
    except Exception as exc:
        logger.debug("sysconfig site-packages discovery skipped: %s", exc)

    unique: List[Path] = []
    seen = set()
    for d in dirs:
        try:
            resolved = d.resolve()
            if resolved not in seen and resolved.is_dir():
                seen.add(resolved)
                unique.append(resolved)
        except OSError:
            pass
    return unique


def _schedule_windows_delete_on_reboot(path: Path) -> bool:
    """Register a file with the Windows NT kernel to be deleted automatically on reboot."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        # MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
        return bool(ctypes.windll.kernel32.MoveFileExW(str(path), None, 0x00000004))
    except Exception as exc:
        logger.debug("MoveFileExW delay until reboot skipped: %s", exc)
        return False


def _stage_rename_windows_binaries(
    script_dirs: Optional[List[Path]] = None,
    bin_shim: Optional[Path] = None,
) -> List[Path]:
    """Rename or delete active Windows CLI binaries to prevent in-use locking during uninstallation.

    Windows NT does not allow unlinking a running executable, but it permits renaming within
    the same volume. Renaming removes the executable from PATH immediately so that subsequent
    CLI invocations cleanly fail with standard command-not-found errors, and package managers
    do not encounter permission errors.
    """
    if sys.platform != "win32":
        return []

    targets: List[Path] = []
    dirs_to_scan = list(script_dirs or [])
    if bin_shim and bin_shim.parent.is_dir():
        dirs_to_scan.append(bin_shim.parent)
        if bin_shim.is_file():
            targets.append(bin_shim)

    for s_dir in dirs_to_scan:
        if not s_dir.is_dir():
            continue
        for pattern in (
            "repo360.exe",
            "repo360.cmd",
            "repo360.bat",
            "repo360.ps1",
            "repo360-script.py",
            "repo360",
        ):
            targets.extend(s_dir.glob(pattern))

    staged_list: List[Path] = []
    seen = set()
    for binary_path in targets:
        try:
            resolved = binary_path.resolve()
            if resolved in seen or not resolved.is_file() or _is_protected_path(resolved):
                continue
            seen.add(resolved)
        except OSError:
            continue

        # Attempt immediate unlinking first (works cleanly if binary is not currently running)
        try:
            os.chmod(resolved, stat.S_IWRITE)
            resolved.unlink()
            continue
        except (PermissionError, OSError):
            pass

        # If locked by an active process, rename to .old_<pid>_<timestamp>
        staged = resolved.with_name(f"{resolved.name}.old_{os.getpid()}_{int(time.time() * 1000)}")
        try:
            resolved.rename(staged)
            _schedule_windows_delete_on_reboot(staged)
            staged_list.append(staged)
        except OSError as exc:
            logger.debug("Could not stage-rename locked binary %s: %s", resolved, exc)

    return staged_list


def _cleanup_stale_staged_binaries(scripts_dirs: Union[Path, List[Path]]) -> List[str]:
    """Remove leftover *.exe.old_* and repo360*.old_* files across script directories."""
    removed: List[str] = []
    dirs = [scripts_dirs] if isinstance(scripts_dirs, Path) else list(scripts_dirs)
    for s_dir in dirs:
        if not s_dir.is_dir():
            continue
        for old_file in list(s_dir.glob("*.exe.old_*")) + list(s_dir.glob("repo360*.old_*")):
            try:
                os.chmod(old_file, stat.S_IWRITE)
                old_file.unlink()
                removed.append(str(old_file))
            except OSError:
                pass
    return removed


def _purge_orphaned_site_packages(site_dirs: Optional[List[Path]] = None) -> List[str]:
    """Remove any orphaned repo360 packages, editable .pth files, or dist-info directories."""
    removed: List[str] = []
    dirs = site_dirs or _discover_site_packages_dirs()
    for s_dir in dirs:
        if not s_dir.is_dir():
            continue
        for pattern in (
            "repo360",
            "repo360.egg-link",
            "repo360*.dist-info",
            "repo360*.egg-info",
            "__editable__.repo360*.pth",
            "__editable___repo360_*.pth",
            "repo360.pth",
        ):
            for target in s_dir.glob(pattern):
                try:
                    if target.is_dir():
                        ok, _ = _safe_rmtree(target)
                        if ok:
                            removed.append(str(target))
                    elif target.is_file():
                        os.chmod(target, stat.S_IWRITE)
                        target.unlink()
                        removed.append(str(target))
                except OSError as exc:
                    logger.debug("Could not remove site-packages target %s: %s", target, exc)
    return removed


def _requires_deferred_windows_cleanup(
    pipx_venv_dir: Optional[Path] = None,
    bin_shim: Optional[Path] = None,
    script_dirs: Optional[List[Path]] = None,
) -> bool:
    """Determine whether active executable locking on Windows requires post-exit removal."""
    if sys.platform != "win32":
        return False
    if _is_running_inside_dir(pipx_venv_dir):
        return True
    argv0 = Path(sys.argv[0]).name.casefold() if sys.argv else ""
    if "repo360" in argv0:
        return True
    if bin_shim and bin_shim.is_file():
        return True
    if shutil.which("repo360"):
        return True
    for s_dir in (script_dirs or _discover_script_dirs()):
        if any(s_dir.glob("repo360*")):
            return True
    return False


def _config_purge_targets(config_dir: Path) -> List[Path]:
    """Return the Repo360-owned paths to delete for a config directory.

    Only the default ``~/.repo360`` directory is removed recursively. A custom
    ``REPO360_CONFIG_DIR`` is never recursively deleted (it may be a shared folder or a
    checkout); only the files Repo360 itself writes there (``config.json`` and
    ``secrets/*.private-key.pem``) are removed, followed by now-empty directories.
    """
    if not os.getenv("REPO360_CONFIG_DIR") and config_dir.name.casefold() in _OWNED_CONFIG_DIR_NAMES:
        return [config_dir]
    targets = [config_dir / "config.json"]
    secrets_dir = config_dir / "secrets"
    if secrets_dir.is_dir():
        targets.extend(sorted(secrets_dir.glob("*.private-key.pem")))
    return [p for p in targets if p.exists()]


def build_uninstall_plan(keep_config: bool = False) -> Dict[str, Any]:
    """Collect all directories, shims, and protected report paths involved in uninstallation."""
    config_dir = Path(resolve_user_config_dir())
    pipx_bin = find_pipx_executable()
    pipx_venv_dir = _find_pipx_venv_dir(pipx_bin)
    details = get_installation_details()
    strategy, package_cmd = _resolve_package_uninstall_command(pipx_bin, pipx_venv_dir, details)

    script_dirs = _discover_script_dirs()
    site_dirs = _discover_site_packages_dirs()

    which_repo360 = shutil.which("repo360")
    bin_shim = Path(which_repo360) if which_repo360 else None
    if bin_shim and bin_shim.stem.casefold() != "repo360":
        bin_shim = None

    dirs_to_delete: List[Path] = []
    config_targets: List[Path] = []
    if not keep_config and config_dir.exists() and not _is_protected_path(config_dir):
        config_targets = _config_purge_targets(config_dir)
        if config_targets:
            dirs_to_delete.append(config_dir)
    if pipx_venv_dir and pipx_venv_dir.exists() and not _is_protected_path(pipx_venv_dir):
        dirs_to_delete.append(pipx_venv_dir)

    reports_dir = Path.cwd() / PROTECTED_REPORT_DIR_NAME
    preserved_dirs: List[Path] = []
    if reports_dir.exists():
        preserved_dirs.append(reports_dir)
    if keep_config and config_dir.exists():
        preserved_dirs.append(config_dir)

    primary_scripts_dir = script_dirs[0] if script_dirs else Path(sys.executable).parent

    return {
        "config_dir": config_dir,
        "config_targets": config_targets,
        "pipx_bin": pipx_bin,
        "pipx_venv_dir": pipx_venv_dir,
        "strategy": strategy,
        "package_cmd": package_cmd,
        "bin_shim": bin_shim,
        "scripts_dir": primary_scripts_dir,
        "script_dirs": script_dirs,
        "site_dirs": site_dirs,
        "dirs_to_delete": dirs_to_delete,
        "preserved_dirs": preserved_dirs,
        "deferred_windows": _requires_deferred_windows_cleanup(pipx_venv_dir, bin_shim, script_dirs),
    }


def _spawn_windows_deferred_uninstall(plan: Dict[str, Any], keep_config: bool = False) -> Tuple[bool, str]:
    """Spawn a detached external Python helper to uninstall and purge locked files after CLI exit."""
    pipx_venv_dir: Optional[Path] = plan.get("pipx_venv_dir")
    external_python = _find_external_python(exclude_dir=pipx_venv_dir)
    if not external_python:
        return False, "Could not locate an external Python interpreter for post-exit Windows cleanup."

    fd, script_path = tempfile.mkstemp(prefix="repo360_uninstall_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_WIN_DEFERRED_UNINSTALL_SCRIPT)

        no_window_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        new_group_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creationflags = no_window_flag | new_group_flag
        si = subprocess.STARTUPINFO()
        si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
        si.wShowWindow = 0
        config_arg = "" if keep_config else str(plan.get("config_dir") or "")
        script_dirs_str = ";".join(
            str(p)
            for p in (plan.get("script_dirs") or ([plan.get("scripts_dir")] if plan.get("scripts_dir") else []))
            if p
        )
        site_dirs_str = ";".join(str(p) for p in plan.get("site_dirs", []) if p)
        cmd = [
            external_python,
            script_path,
            str(os.getpid()),
            plan.get("pipx_bin") or "",
            str(pipx_venv_dir) if pipx_venv_dir else "",
            str(plan["bin_shim"]) if plan.get("bin_shim") else "",
            config_arg,
            script_dirs_str,
            site_dirs_str,
            *plan["package_cmd"],
        ]
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            startupinfo=si,
        )
        return True, script_path
    except Exception as exc:
        try:
            Path(script_path).unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(exc)


def _print_uninstall_plan(plan: Dict[str, Any], keep_config: bool) -> None:
    """Display an explicit summary of deletions and preserved report folders."""
    print("\nRepo360 Uninstall Summary:")
    print(f"  Package Removal  : {' '.join(plan['package_cmd'])} ({plan['strategy']})")

    config_dir: Path = plan["config_dir"]
    if keep_config:
        print(f"  [KEEP] Config    : {config_dir} (--keep-config enabled)")
    elif config_dir in plan["dirs_to_delete"]:
        targets = plan.get("config_targets") or [config_dir]
        if targets == [config_dir]:
            print(f"  [DEL]  Config    : {config_dir} (credentials, config.json, secrets)")
        else:
            for target in targets:
                print(f"  [DEL]  Config    : {target}")
    else:
        print(f"  [INFO] Config    : {config_dir} (not present)")

    venv_dir: Optional[Path] = plan.get("pipx_venv_dir")
    if venv_dir and venv_dir in plan["dirs_to_delete"]:
        print(f"  [DEL]  Venv Dir  : {venv_dir}")

    if plan.get("bin_shim"):
        print(f"  [DEL]  CLI Shim  : {plan['bin_shim']}")

    reports_dir = Path.cwd() / PROTECTED_REPORT_DIR_NAME
    print(f"  [KEEP] Reports   : {reports_dir} (always preserved)")
    print("")


def execute_uninstall(
    plan: Dict[str, Any],
    dry_run: bool = False,
    keep_config: bool = False,
    delay_seconds: float = DEFAULT_UNINSTALL_DELAY_SECONDS,
) -> int:
    """Execute package uninstallation and clean up Repo360 directories.

    Args:
        plan: Prepared uninstall plan mapping directories and uninstall commands.
        dry_run: When True, preview actions without modifying files or packages.
        keep_config: When True, preserve ~/.repo360 configuration and keys.
        delay_seconds: Settling and verification delay (in seconds) before displaying
            the completion text. Defaults to 7.0s (3.0s baseline settling + 4.0s grace timer).

    Returns:
        Exit code: 0 on successful uninstallation, non-zero on failure.
    """
    if dry_run:
        print("\033[96m[DRY-RUN]\033[0m No files or packages were removed.")
        return 0

    print("Uninstalling Repo360...")
    exit_code = 0

    config_dir: Path = plan["config_dir"]
    if not keep_config and config_dir.exists():
        for target in plan.get("config_targets") or _config_purge_targets(config_dir):
            ok, msg = _safe_rmtree(target)
            if not ok:
                print(f"\033[91m[FAIL]\033[0m {msg}")
                exit_code = 1
        for leftover in (config_dir / "secrets", config_dir):
            if leftover.is_dir() and not _is_protected_path(leftover):
                try:
                    leftover.rmdir()
                except OSError:
                    logger.debug("Kept non-empty directory %s", leftover)

    script_dirs = list(plan.get("script_dirs") or [])
    if plan.get("scripts_dir") and plan["scripts_dir"] not in script_dirs:
        script_dirs.append(plan["scripts_dir"])
    site_dirs = plan.get("site_dirs")

    # On Windows, stage-rename active/locked binaries first so repo360 is immediately removed from PATH
    if sys.platform == "win32":
        _stage_rename_windows_binaries(script_dirs, bin_shim=plan.get("bin_shim"))

    _cleanup_stale_staged_binaries(script_dirs)

    if plan.get("deferred_windows"):
        _purge_orphaned_site_packages(site_dirs)
        ok, info = _spawn_windows_deferred_uninstall(plan, keep_config=keep_config)
        if not ok:
            print(f"\033[91m[FAIL]\033[0m Could not schedule Windows package removal: {info}")
            exit_code = 1
    else:
        res = run_interruptible_subprocess(
            plan["package_cmd"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()
            # If the package was already not installed, treat as successful idempotent uninstallation
            if "not installed" not in err.lower() and "skipping" not in err.lower():
                print(f"\033[93m[WARN]\033[0m Installer returned code {res.returncode}: {err}")
                exit_code = 1

        venv_dir: Optional[Path] = plan.get("pipx_venv_dir")
        if venv_dir and venv_dir.exists():
            ok, msg = _safe_rmtree(venv_dir)
            if not ok:
                print(f"\033[91m[FAIL]\033[0m {msg}")
                exit_code = 1

        bin_shim: Optional[Path] = plan.get("bin_shim")
        if bin_shim and (bin_shim.is_file() or bin_shim.is_symlink()) and not _is_protected_path(bin_shim):
            try:
                bin_shim.unlink()
            except OSError as exc:
                print(f"\033[93m[WARN]\033[0m Could not remove CLI executable {bin_shim}: {exc}")

        for s_dir in script_dirs:
            if not s_dir.is_dir():
                continue
            cand = s_dir / "repo360"
            if (cand.is_file() or cand.is_symlink()) and not _is_protected_path(cand):
                try:
                    cand.unlink()
                except OSError as exc:
                    print(f"\033[93m[WARN]\033[0m Could not remove CLI executable {cand}: {exc}")

        _purge_orphaned_site_packages(site_dirs)

    if delay_seconds > 0:
        time.sleep(delay_seconds)

    if exit_code == 0:
        print("Successfully uninstalled Repo360.")

    print("\nNote: To remove the GitHub App from your organization, revoke/delete it under GitHub Organization Settings.\n")
    return exit_code


def _prompt_confirmation(prompt: str) -> str:
    """Prompt for user confirmation cleanly while aborting immediately on Ctrl+C."""
    return interruptible_input(prompt, eof_default="").strip().lower()


def run_uninstall_cli(argv: Optional[List[str]] = None) -> int:
    """CLI handler for 'repo360 uninstall' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 uninstall",
        description="Remove Repo360, its virtual environment, and configuration while keeping reports.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview removal plan without deleting files.",
    )
    parser.add_argument(
        "--keep-config",
        action="store_true",
        help="Preserve ~/.repo360 config and keys.",
    )
    args = parser.parse_args(argv)

    plan = build_uninstall_plan(keep_config=args.keep_config)
    if args.dry_run:
        _print_uninstall_plan(plan, keep_config=args.keep_config)

    if not args.dry_run and not args.yes:
        answer = _prompt_confirmation("Proceed with Repo360 uninstall? [y/N]: ")
        if answer not in ("y", "yes"):
            print("Uninstall cancelled.")
            return 1

    return execute_uninstall(plan, dry_run=args.dry_run, keep_config=args.keep_config)
