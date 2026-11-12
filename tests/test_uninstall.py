"""Unit tests for Repo360 clean uninstallation and directory protection guards."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.credentials import resolve_user_config_dir
from src.uninstall import (
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
        patch("src.uninstall.find_pipx_executable", return_value="C:/bin/pipx.exe"),
        patch("src.uninstall._find_pipx_venv_dir", return_value=mock_venv),
        patch("src.uninstall.get_installation_details", return_value={"installer": "uv"}),
        patch("src.uninstall.shutil.which", return_value=str(mock_shim)),
        patch("src.uninstall._requires_deferred_windows_cleanup", return_value=False),
        patch("src.uninstall.subprocess.run", return_value=MagicMock(returncode=0, stdout="uninstalled", stderr="")),
    ):
        plan = build_uninstall_plan(keep_config=False)
        plan["scripts_dir"] = mock_scripts
        rc = execute_uninstall(plan, dry_run=False, keep_config=False)

    assert rc == 0
    assert not cfg_dir.exists()
    assert not mock_venv.exists()
    assert not mock_shim.exists()
    assert not stale_bin.exists()
    assert reports_dir.is_dir()
    assert workbook.read_text(encoding="utf-8") == "keep-me"
    out = capsys.readouterr().out
    assert "Preserved reports directory untouched" in out


def test_run_uninstall_cli_dry_run_and_keep_config_and_abort(tmp_path: Path, monkeypatch) -> None:
    """Verify --dry-run, prompt cancellation, and --keep-config preserve directories appropriately."""
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / ".repo360"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REPO360_CONFIG_DIR", str(cfg_dir))

    with (
        patch("src.uninstall.find_pipx_executable", return_value=None),
        patch("src.uninstall._find_pipx_venv_dir", return_value=None),
        patch("src.uninstall.get_installation_details", return_value={"installer": "pip"}),
        patch("src.uninstall.shutil.which", return_value=None),
        patch("src.uninstall._requires_deferred_windows_cleanup", return_value=False),
    ):
        assert run_uninstall_cli(["--dry-run"]) == 0
        assert cfg_dir.exists()

        with patch("builtins.input", return_value="n"):
            assert run_uninstall_cli([]) == 1
        assert cfg_dir.exists()

        with patch("src.uninstall.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            assert run_uninstall_cli(["--yes", "--keep-config"]) == 0
            assert mock_run.called
        assert cfg_dir.exists()


def test_spawn_windows_deferred_uninstall_creates_helper_and_spawns(tmp_path: Path) -> None:
    """Verify deferred Windows helper passes exact paths via argv without shell interpolation."""
    mock_venv = tmp_path / "pipx" / "venvs" / "repo360"
    mock_venv.mkdir(parents=True)
    plan = {
        "pipx_bin": "C:/tools/pipx.exe",
        "pipx_venv_dir": mock_venv,
        "bin_shim": tmp_path / "repo360.exe",
        "package_cmd": ["C:/tools/pipx.exe", "uninstall", "repo360"],
    }

    with (
        patch("src.uninstall._find_external_python", return_value="C:/Python312/python.exe"),
        patch("src.uninstall.subprocess.Popen") as mock_popen,
    ):
        ok, script_path = _spawn_windows_deferred_uninstall(plan)

    assert ok is True
    assert Path(script_path).is_file()
    Path(script_path).unlink(missing_ok=True)
    popen_cmd = mock_popen.call_args[0][0]
    assert popen_cmd[0] == "C:/Python312/python.exe"
    assert str(mock_venv) in popen_cmd
    assert mock_popen.call_args[1]["close_fds"] is True
