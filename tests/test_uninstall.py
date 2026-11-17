"""Unit tests for Repo360 clean uninstallation and directory protection guards."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repo360.credentials import resolve_user_config_dir
from repo360.uninstall import (
    _is_protected_path,
    _safe_rmtree,
    _spawn_windows_deferred_uninstall,
    build_uninstall_plan,
    execute_uninstall,
    run_uninstall_cli,
)


def test_is_protected_path_blocks_reports_and_system_roots(tmp_path: Path, monkeypatch) -> None:
    """Protect repo360-output, home, cwd, filesystem root, and system Python prefix."""
    monkeypatch.chdir(tmp_path)
    reports_dir = tmp_path / "repo360-output"
    reports_dir.mkdir()
    nested_report = reports_dir / "org_audit"
    nested_report.mkdir()

    assert _is_protected_path(reports_dir) is True
    assert _is_protected_path(nested_report) is True
    assert _is_protected_path(tmp_path) is True
    assert _is_protected_path(Path.home()) is True
    assert _is_protected_path(Path(sys.base_prefix)) is True
    assert _is_protected_path(Path(tmp_path.anchor)) is True

    safe_cfg = tmp_path / ".repo360-test"
    safe_cfg.mkdir()
    assert _is_protected_path(safe_cfg) is False


def test_safe_rmtree_deletes_readonly_and_refuses_protected(tmp_path: Path, monkeypatch) -> None:
    """Clear read-only attributes on private keys while blocking protected report directories."""
    monkeypatch.chdir(tmp_path)
    reports_dir = tmp_path / "repo360-output"
    reports_dir.mkdir()
    report_file = reports_dir / "audit_report.xlsx"
    report_file.write_text("important-data", encoding="utf-8")

    ok_protected, msg_protected = _safe_rmtree(reports_dir)
    assert ok_protected is False
    assert "protected" in msg_protected.lower()
    assert report_file.is_file()

    cfg_dir = tmp_path / "custom_repo360_cfg"
    secrets_dir = cfg_dir / "secrets"
    secrets_dir.mkdir(parents=True)
    pem_file = secrets_dir / "repo360.2026-11-11.private-key.pem"
    pem_file.write_text("PRIVATE KEY DATA", encoding="utf-8")
    os.chmod(pem_file, stat.S_IREAD)

    ok_cfg, _ = _safe_rmtree(cfg_dir)
    assert ok_cfg is True
    assert not cfg_dir.exists()


def test_run_uninstall_cli_removes_related_dirs_and_preserves_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    """Verify complete deletion of config/secrets, venv, shims, and staged binaries without touching repo360-output."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(tmp_path / ".repo360"))

    cfg_dir = Path(resolve_user_config_dir())
    (cfg_dir / "secrets").mkdir(parents=True)
    (cfg_dir / "config.json").write_text('{"github": {"app_id": 1}}', encoding="utf-8")
    pem_path = cfg_dir / "secrets" / "app.private-key.pem"
    pem_path.write_text("secret-key", encoding="utf-8")
    os.chmod(pem_path, stat.S_IREAD)

    reports_dir = tmp_path / "repo360-output"
    reports_dir.mkdir()
    workbook = reports_dir / "governance_report.xlsx"
    workbook.write_text("keep-me", encoding="utf-8")

    mock_venv = tmp_path / "pipx" / "venvs" / "repo360"
    (mock_venv / "Scripts").mkdir(parents=True)
    (mock_venv / "Scripts" / "python.exe").write_text("exe", encoding="utf-8")

    mock_scripts = tmp_path / "scripts"
    mock_scripts.mkdir()
    stale_bin = mock_scripts / "repo360.exe.old_999"
    stale_bin.write_text("stale", encoding="utf-8")

    mock_shim = tmp_path / "bin" / "repo360.exe"
    mock_shim.parent.mkdir(parents=True)
    mock_shim.write_text("shim", encoding="utf-8")

    with (
        patch("repo360.uninstall.find_pipx_executable", return_value="C:/bin/pipx.exe"),
        patch("repo360.uninstall._find_pipx_venv_dir", return_value=mock_venv),
        patch("repo360.uninstall.get_installation_details", return_value={"installer": "uv"}),
        patch("repo360.uninstall.shutil.which", return_value=str(mock_shim)),
        patch("repo360.uninstall._requires_deferred_windows_cleanup", return_value=False),
        patch("repo360.uninstall.subprocess.run", return_value=MagicMock(returncode=0, stdout="uninstalled", stderr="")),
    ):
        plan = build_uninstall_plan(keep_config=False)
        plan["scripts_dir"] = mock_scripts
        rc = execute_uninstall(plan, dry_run=False, keep_config=False, delay_seconds=0)

    assert rc == 0
    assert not cfg_dir.exists()
    assert not mock_venv.exists()
    assert not mock_shim.exists()
    assert not stale_bin.exists()
    assert reports_dir.is_dir()
    assert workbook.read_text(encoding="utf-8") == "keep-me"
    out = capsys.readouterr().out
    assert "Uninstalling Repo360..." in out
    assert "Successfully uninstalled Repo360." in out
    assert "[PASS]" not in out
    assert "[INFO]" not in out
    assert "Note: To remove the GitHub App from your organization" in out


def test_uninstall_never_wipes_custom_shared_config_dir(tmp_path: Path, monkeypatch) -> None:
    """A REPO360_CONFIG_DIR pointing at a shared folder must only lose Repo360's own files."""
    shared = tmp_path / "Documents"
    (shared / "secrets").mkdir(parents=True)
    (shared / "config.json").write_text("{}", encoding="utf-8")
    (shared / "secrets" / "repo360.2026-01-01.private-key.pem").write_text("k", encoding="utf-8")
    unrelated_secret = shared / "secrets" / "personal-ssh.pem"
    unrelated_secret.write_text("keep", encoding="utf-8")
    unrelated_doc = shared / "thesis.docx"
    unrelated_doc.write_text("keep", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(shared))

    with (
        patch("repo360.uninstall.find_pipx_executable", return_value=None),
        patch("repo360.uninstall._find_pipx_venv_dir", return_value=None),
        patch("repo360.uninstall.get_installation_details", return_value={"installer": "pip"}),
        patch("repo360.uninstall.shutil.which", return_value=None),
        patch("repo360.uninstall._requires_deferred_windows_cleanup", return_value=False),
        patch("repo360.uninstall.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")),
    ):
        plan = build_uninstall_plan(keep_config=False)
        plan["scripts_dir"] = tmp_path / "scripts"
        assert execute_uninstall(plan, dry_run=False, keep_config=False, delay_seconds=0) == 0

    assert not (shared / "config.json").exists()
    assert not (shared / "secrets" / "repo360.2026-01-01.private-key.pem").exists()
    assert unrelated_secret.read_text(encoding="utf-8") == "keep"
    assert unrelated_doc.read_text(encoding="utf-8") == "keep"


def test_run_uninstall_cli_dry_run_and_keep_config_and_abort(tmp_path: Path, monkeypatch, capsys) -> None:
    """Verify --dry-run, prompt cancellation, and --keep-config preserve directories appropriately."""
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / ".repo360"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(cfg_dir))

    with (
        patch("repo360.uninstall.find_pipx_executable", return_value=None),
        patch("repo360.uninstall._find_pipx_venv_dir", return_value=None),
        patch("repo360.uninstall.get_installation_details", return_value={"installer": "pip"}),
        patch("repo360.uninstall.shutil.which", return_value=None),
        patch("repo360.uninstall._requires_deferred_windows_cleanup", return_value=False),
    ):
        assert run_uninstall_cli(["--dry-run"]) == 0
        assert cfg_dir.exists()
        out_dry = capsys.readouterr().out
        assert "Repo360 Uninstall Summary:" in out_dry

        with patch("builtins.input", return_value="n"):
            assert run_uninstall_cli([]) == 1
        assert cfg_dir.exists()
        out_abort = capsys.readouterr().out
        assert "Repo360 Uninstall Summary:" not in out_abort

        with (
            patch("repo360.uninstall.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("repo360.uninstall.time.sleep") as mock_sleep,
        ):
            assert run_uninstall_cli(["--yes", "--keep-config"]) == 0
            assert mock_run.called
            assert mock_sleep.called
        assert cfg_dir.exists()
        out_yes = capsys.readouterr().out
        assert "Repo360 Uninstall Summary:" not in out_yes


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows subprocess startup APIs")
def test_spawn_windows_deferred_uninstall_creates_helper_and_spawns(tmp_path: Path) -> None:
    """Verify deferred Windows helper passes exact paths via argv without shell interpolation."""
    mock_venv = tmp_path / "pipx" / "venvs" / "repo360"
    mock_venv.mkdir(parents=True)
    cfg_dir = tmp_path / ".repo360"
    cfg_dir.mkdir()
    plan = {
        "config_dir": cfg_dir,
        "pipx_bin": "C:/tools/pipx.exe",
        "pipx_venv_dir": mock_venv,
        "bin_shim": tmp_path / "repo360.exe",
        "package_cmd": ["C:/tools/pipx.exe", "uninstall", "repo360"],
    }

    with (
        patch("repo360.uninstall._find_external_python", return_value="C:/Python312/python.exe"),
        patch("repo360.uninstall.subprocess.Popen") as mock_popen,
    ):
        ok, script_path = _spawn_windows_deferred_uninstall(plan, keep_config=False)

    assert ok is True
    assert Path(script_path).is_file()
    Path(script_path).unlink(missing_ok=True)
    popen_cmd = mock_popen.call_args[0][0]
    assert popen_cmd[0] == "C:/Python312/python.exe"
    assert str(mock_venv) in popen_cmd
    assert str(cfg_dir) in popen_cmd
    assert mock_popen.call_args[1]["close_fds"] is True
    creationflags = mock_popen.call_args[1].get("creationflags", 0)
    assert creationflags & getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert not (creationflags & getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    startupinfo = mock_popen.call_args[1].get("startupinfo")
    assert startupinfo is not None
    assert startupinfo.dwFlags & getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    assert startupinfo.wShowWindow == 0


def test_find_pipx_venv_dir_supports_env_and_macos_paths(tmp_path: Path, monkeypatch) -> None:
    """Resolve pipx venv from PIPX_HOME env var and macOS Application Support fallback."""
    from repo360.uninstall import _find_pipx_venv_dir

    custom_venv = tmp_path / "custom-pipx" / "venvs" / "repo360"
    custom_venv.mkdir(parents=True)
    monkeypatch.setenv("PIPX_HOME", str(tmp_path / "custom-pipx"))
    monkeypatch.delenv("PIPX_LOCAL_VENVS", raising=False)
    with patch("repo360.uninstall.sys.prefix", str(tmp_path / "other")):
        assert _find_pipx_venv_dir(None) == custom_venv

    monkeypatch.delenv("PIPX_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    mac_venv = tmp_path / "Library" / "Application Support" / "pipx" / "venvs" / "repo360"
    mac_venv.mkdir(parents=True)
    with patch("repo360.uninstall.sys.prefix", str(tmp_path / "other")), \
         patch("repo360.uninstall.sys.platform", "darwin"), \
         patch("repo360.uninstall.Path.home", return_value=tmp_path):
        assert _find_pipx_venv_dir(None) == mac_venv


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows executable discovery")
def test_find_external_python_prefers_pythonw_on_windows(tmp_path: Path, monkeypatch) -> None:
    """Ensure _find_external_python prefers pythonw.exe over python.exe on Windows for windowless execution."""
    from repo360.uninstall import _find_external_python

    py_exe = tmp_path / "python.exe"
    py_exe.write_text("dummy", encoding="utf-8")
    pyw_exe = tmp_path / "pythonw.exe"
    pyw_exe.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(sys, "_base_executable", str(py_exe))
    monkeypatch.setattr(sys, "platform", "win32")

    found = _find_external_python()
    assert found is not None
    assert Path(found).name.lower() == "pythonw.exe"


def test_execute_uninstall_delay_timer(tmp_path: Path) -> None:
    """Verify execute_uninstall applies the 7.0s default timer (3s baseline + 4s timer) before completion."""
    from repo360.uninstall import (
        DEFAULT_UNINSTALL_DELAY_SECONDS,
        DEFAULT_UNINSTALL_GRACE_TIMER_SECONDS,
        DEFAULT_UNINSTALL_SETTLING_DELAY_SECONDS,
        execute_uninstall,
    )

    assert DEFAULT_UNINSTALL_SETTLING_DELAY_SECONDS == 3.0
    assert DEFAULT_UNINSTALL_GRACE_TIMER_SECONDS == 4.0
    assert DEFAULT_UNINSTALL_DELAY_SECONDS == 7.0

    plan = {
        "config_dir": tmp_path / "nonexistent",
        "scripts_dir": tmp_path / "scripts",
        "deferred_windows": False,
        "package_cmd": ["echo", "uninstalled"],
    }
    with (
        patch("repo360.uninstall.run_interruptible_subprocess", return_value=MagicMock(returncode=0)),
        patch("repo360.uninstall.time.sleep") as mock_sleep,
    ):
        rc = execute_uninstall(plan)
        assert rc == 0
        mock_sleep.assert_called_once_with(7.0)



def test_uninstall_never_rmtrees_custom_config_dir_named_repo360(tmp_path: Path, monkeypatch) -> None:
    """A custom REPO360_CONFIG_DIR that happens to be named 'repo360' (e.g. a git clone) must not be wiped."""
    from repo360.uninstall import _config_purge_targets

    checkout = tmp_path / "src" / "repo360"
    checkout.mkdir(parents=True)
    (checkout / "config.json").write_text("{}", encoding="utf-8")
    (checkout / "README.md").write_text("keep", encoding="utf-8")
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(checkout))

    assert _config_purge_targets(checkout) == [checkout / "config.json"]
