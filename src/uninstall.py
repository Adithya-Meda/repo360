"""Clean, cross-platform uninstaller for the Repo360 CLI suite.

Removes Repo360 packages, isolated virtual environments, shims, and configuration
directories (~/.repo360) while strictly preserving all generated reports in
repo360-output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .credentials import resolve_user_config_dir
from .upgrade import find_pipx_executable, get_installation_details

PROTECTED_REPORT_DIR_NAME = "repo360-output"

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


def _is_safe_venv(target: Path) -> bool:
    try:
        resolved = target.resolve()
    except OSError:
        return False
    if resolved.name.lower() != "repo360":
        return False
    if any(part.lower() == "repo360-output" for part in resolved.parts):
        return False
    return True


def main() -> None:
    parent_pid = int(sys.argv[1])
    pipx_bin = sys.argv[2]
    venv_arg = sys.argv[3]
    shim_arg = sys.argv[4]
    cmd = sys.argv[5:]

    # Wait until the parent repo360 Python process terminates cleanly so Windows unlocks executables.
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x00100000, False, parent_pid)
    if handle:
        k32.WaitForSingleObject(handle, 15000)
        k32.CloseHandle(handle)
    time.sleep(0.35)

    for _ in range(4):
        if cmd:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if res.returncode == 0:
                break
        elif pipx_bin:
            res = subprocess.run(
                [pipx_bin, "uninstall", "repo360"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                break
        time.sleep(0.4)

    if venv_arg:
        venv_dir = Path(venv_arg)
        if venv_dir.exists() and _is_safe_venv(venv_dir):
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
        if shim_path.is_file() and shim_path.stem.lower() == "repo360":
            try:
                shim_path.unlink()
            except OSError:
                pass


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

    # Never remove a directory containing ./repo360-output
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
            res = subprocess.run(
                [pipx_bin, "environment", "--value", "PIPX_LOCAL_VENVS"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                cand = Path(res.stdout.strip().splitlines()[0]) / "repo360"
                if cand.is_dir():
                    return cand
        except Exception:
            pass

    home = Path.home()
    candidates = [
        home / ".local" / "pipx" / "venvs" / "repo360",
        home / ".local" / "share" / "pipx" / "venvs" / "repo360",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.insert(0, Path(local_appdata) / "pipx" / "pipx" / "venvs" / "repo360")
        candidates.insert(1, Path(local_appdata) / "pipx" / "venvs" / "repo360")

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

    for name in ("python3", "python"):
        hit = shutil.which(name)
        if hit:
            candidates.append(hit)

    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    candidates.append(str(Path(sys.base_prefix) / exe_name))

    resolved_exclude: Optional[Path] = None
    if exclude_dir:
        try:
            resolved_exclude = exclude_dir.resolve()
        except OSError:
            resolved_exclude = None

    for cand in candidates:
        cand_path = Path(cand)
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


def _requires_deferred_windows_cleanup(pipx_venv_dir: Optional[Path]) -> bool:
    """Determine whether active executable locking on Windows requires post-exit removal."""
    if sys.platform != "win32":
        return False
    if _is_running_inside_dir(pipx_venv_dir):
        return True
    argv0 = Path(sys.argv[0]).name.casefold() if sys.argv else ""
    if "repo360" in argv0 and Path(sys.executable).parent.joinpath("repo360.exe").is_file():
        return True
    return False


def _cleanup_stale_staged_binaries(scripts_dir: Path) -> List[str]:
    """Remove any leftover *.exe.old_* files created during prior Windows upgrades."""
    removed: List[str] = []
    if not scripts_dir.is_dir():
        return removed
    for old_file in scripts_dir.glob("*.exe.old_*"):
        try:
            old_file.unlink()
            removed.append(str(old_file))
        except OSError:
            pass
    return removed


def build_uninstall_plan(keep_config: bool = False) -> Dict[str, Any]:
    """Collect all directories, shims, and protected report paths involved in uninstallation."""
    config_dir = Path(resolve_user_config_dir())
    pipx_bin = find_pipx_executable()
    pipx_venv_dir = _find_pipx_venv_dir(pipx_bin)
    details = get_installation_details()
    strategy, package_cmd = _resolve_package_uninstall_command(pipx_bin, pipx_venv_dir, details)

    which_repo360 = shutil.which("repo360")
    bin_shim = Path(which_repo360) if which_repo360 else None
    if bin_shim and bin_shim.stem.casefold() != "repo360":
        bin_shim = None

    dirs_to_delete: List[Path] = []
    if not keep_config and config_dir.exists() and not _is_protected_path(config_dir):
        dirs_to_delete.append(config_dir)
    if pipx_venv_dir and pipx_venv_dir.exists() and not _is_protected_path(pipx_venv_dir):
        dirs_to_delete.append(pipx_venv_dir)

    reports_dir = Path.cwd() / PROTECTED_REPORT_DIR_NAME
    preserved_dirs: List[Path] = []
    if reports_dir.exists():
        preserved_dirs.append(reports_dir)
    if keep_config and config_dir.exists():
        preserved_dirs.append(config_dir)

    return {
        "config_dir": config_dir,
        "pipx_bin": pipx_bin,
        "pipx_venv_dir": pipx_venv_dir,
        "strategy": strategy,
        "package_cmd": package_cmd,
        "bin_shim": bin_shim,
        "scripts_dir": Path(sys.executable).parent,
        "dirs_to_delete": dirs_to_delete,
        "preserved_dirs": preserved_dirs,
        "deferred_windows": _requires_deferred_windows_cleanup(pipx_venv_dir),
    }


def _spawn_windows_deferred_uninstall(plan: Dict[str, Any]) -> Tuple[bool, str]:
    """Spawn a detached external Python helper to uninstall and purge locked files after CLI exit."""
    pipx_venv_dir: Optional[Path] = plan.get("pipx_venv_dir")
    external_python = _find_external_python(exclude_dir=pipx_venv_dir)
    if not external_python:
        return False, "Could not locate an external Python interpreter for post-exit Windows cleanup."

    fd, script_path = tempfile.mkstemp(prefix="repo360_uninstall_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_WIN_DEFERRED_UNINSTALL_SCRIPT)

        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )
        cmd = [
            external_python,
            script_path,
            str(os.getpid()),
            plan.get("pipx_bin") or "",
            str(pipx_venv_dir) if pipx_venv_dir else "",
            str(plan["bin_shim"]) if plan.get("bin_shim") else "",
            *plan["package_cmd"],
        ]
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
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
        print(f"  [DEL]  Config    : {config_dir} (credentials, config.json, secrets)")
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


def execute_uninstall(plan: Dict[str, Any], dry_run: bool = False, keep_config: bool = False) -> int:
    """Execute package uninstallation and clean up Repo360 directories."""
    if dry_run:
        print("\033[96m[DRY-RUN]\033[0m No files or packages were removed.")
        return 0

    exit_code = 0

    # 1. Remove configuration & secrets directory synchronously (~/.repo360)
    config_dir: Path = plan["config_dir"]
    if not keep_config:
        if config_dir.exists():
            ok, msg = _safe_rmtree(config_dir)
            if ok:
                print(f"\033[92m[PASS]\033[0m Removed configuration & secrets directory: {config_dir}")
            else:
                print(f"\033[91m[FAIL]\033[0m {msg}")
                exit_code = 1
        else:
            print(f"[INFO] Configuration directory not found (skipping): {config_dir}")

    # 2. Clean any stale staged binaries in Scripts/ before uninstalling
    _cleanup_stale_staged_binaries(plan["scripts_dir"])

    # 3. Uninstall Python package & isolated pipx virtual environment
    if plan.get("deferred_windows"):
        ok, info = _spawn_windows_deferred_uninstall(plan)
        if ok:
            print(
                f"\033[96m[INFO]\033[0m Scheduled post-exit Windows cleanup ({plan['strategy']}) "
                "once this CLI process releases file locks."
            )
        else:
            print(f"\033[91m[FAIL]\033[0m Could not schedule Windows package removal: {info}")
            exit_code = 1
    else:
        res = subprocess.run(
            plan["package_cmd"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if res.returncode == 0:
            print(f"\033[92m[PASS]\033[0m Uninstalled repo360 package via {plan['strategy']}.")
        else:
            err = (res.stderr or res.stdout or "").strip()
            print(f"\033[93m[WARN]\033[0m Installer returned code {res.returncode}: {err}")

        venv_dir: Optional[Path] = plan.get("pipx_venv_dir")
        if venv_dir and venv_dir.exists():
            ok, msg = _safe_rmtree(venv_dir)
            if ok:
                print(f"\033[92m[PASS]\033[0m Removed residual virtual environment directory: {venv_dir}")
            else:
                print(f"\033[91m[FAIL]\033[0m {msg}")
                exit_code = 1

        bin_shim: Optional[Path] = plan.get("bin_shim")
        if bin_shim and bin_shim.is_file() and not _is_protected_path(bin_shim):
            try:
                bin_shim.unlink()
                print(f"\033[92m[PASS]\033[0m Removed residual CLI executable: {bin_shim}")
            except OSError as exc:
                print(f"\033[93m[WARN]\033[0m Could not remove CLI executable {bin_shim}: {exc}")

    # 4. Reconfirm reports preservation and remind about remote GitHub App
    reports_dir = Path.cwd() / PROTECTED_REPORT_DIR_NAME
    if reports_dir.exists():
        print(f"\033[92m[KEEP]\033[0m Preserved reports directory untouched: {reports_dir}")
    print("\nNote: To remove the GitHub App from your organization, revoke/delete it under GitHub Organization Settings.\n")
    return exit_code


def run_uninstall_cli(argv: Optional[List[str]] = None) -> int:
    """CLI handler for 'repo360 uninstall' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 uninstall",
        description="Completely uninstall Repo360, its virtual environment, and config directories while keeping reports.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip interactive confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview directories and commands that would be removed without making changes.",
    )
    parser.add_argument(
        "--keep-config",
        action="store_true",
        help="Uninstall the CLI package/venv only while preserving ~/.repo360 configuration and keys.",
    )
    args = parser.parse_args(argv)

    plan = build_uninstall_plan(keep_config=args.keep_config)
    _print_uninstall_plan(plan, keep_config=args.keep_config)

    if not args.dry_run and not args.yes:
        try:
            answer = input("Proceed with Repo360 uninstall? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Uninstall cancelled.")
            return 1

    return execute_uninstall(plan, dry_run=args.dry_run, keep_config=args.keep_config)
