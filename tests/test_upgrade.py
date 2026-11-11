"""Unit tests for Repo360 upgrade module."""

import sys
from unittest.mock import MagicMock, patch
import pytest

from src.upgrade import (
    UpgradeResult,
    build_upgrade_command,
    check_remote_status,
    detect_installer,
    execute_upgrade,
    get_local_version_info,
    get_remote_commit_sha,
    is_output_already_up_to_date,
    run_upgrade_cli,
    verify_local_state,
)


def test_detect_installer_pipx():
    with patch.object(sys, "prefix", "C:\\Users\\test\\AppData\\Local\\pipx\\venvs\\repo360"):
        with patch("src.upgrade.find_pipx_executable", return_value="C:\\bin\\pipx.exe"):
            installer, cmd = detect_installer()
            assert installer == "pipx"
            assert cmd == ["C:\\bin\\pipx.exe", "runpip", "repo360", "install", "--upgrade"]


def test_detect_installer_pip_fallback():
    with patch.object(sys, "prefix", "C:\\Python312"):
        installer, cmd = detect_installer()
        assert installer == "pip"
        assert cmd == [sys.executable, "-m", "pip", "install", "--upgrade"]


def test_build_upgrade_command():
    installer, cmd = build_upgrade_command(
        repo_url="https://github.com/Adithya-Meda/repo360.git",
        installer_override="pip",
    )
    assert installer == "pip"
    assert "git+https://github.com/Adithya-Meda/repo360.git" in cmd

    installer, cmd = build_upgrade_command(
        repo_url="https://github.com/Adithya-Meda/repo360.git",
        installer_override="pipx",
    )
    assert installer == "pipx"
    assert "git+https://github.com/Adithya-Meda/repo360.git" in cmd


def test_execute_upgrade_dry_run(capsys):
    ret = execute_upgrade([("pipx", ["pipx", "runpip", "repo360", "install", "--upgrade", "pkg"])], dry_run=True)
    assert ret == 0
    captured = capsys.readouterr()
    assert "[DRY-RUN]" in captured.out


def test_execute_upgrade_subprocess_success():
    mock_res = MagicMock()
    mock_res.returncode = 0
    with patch("subprocess.run", return_value=mock_res) as mock_run:
        ret = execute_upgrade([("pip", ["pip", "install", "pkg"])], dry_run=False)
        assert ret == 0
        assert mock_run.called


def test_check_remote_status():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "sha": "abcdef123456",
        "commit": {
            "committer": {"date": "2026-11-04T12:00:00Z"},
            "message": "feat: release v1.1.0\n\nFull changelog",
        },
    }
    with patch("requests.get", return_value=mock_resp):
        ok, msg = check_remote_status("https://github.com/Adithya-Meda/repo360.git")
        assert ok is True
        assert "abcdef12" in msg
        assert "release v1.1.0" in msg


def test_run_upgrade_cli_dry_run(capsys):
    ret = run_upgrade_cli(["--dry-run"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "[DRY-RUN]" in captured.out
    assert "git+https://github.com/Adithya-Meda/repo360.git" in captured.out


def test_run_upgrade_cli_check(capsys):
    with patch("src.upgrade.check_remote_status", return_value=(True, "Latest version on main: 12345678")):
        ret = run_upgrade_cli(["--check"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "[PASS]" in captured.out
        assert "Latest version on main: 12345678" in captured.out


def test_verify_local_state(capsys, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "key.pem").write_text("dummy", encoding="utf-8")

    with patch("src.upgrade.get_credentials_path", return_value=str(config_file)):
        with patch("src.upgrade.get_secrets_dir", return_value=str(secrets_dir)):
            verify_local_state()
            captured = capsys.readouterr()
            assert "[VERIFY]" in captured.out
            assert "Configuration file" in captured.out
            assert "1 private key(s) detected" in captured.out


def test_prepare_and_restore_windows_binaries(tmp_path):
    from src.upgrade import _prepare_windows_binaries, _restore_windows_binaries

    exe = tmp_path / "repo360.exe"
    exe.write_text("mock binary", encoding="utf-8")

    with patch("sys.platform", "win32"):
        renamed = _prepare_windows_binaries(tmp_path)
        assert len(renamed) == 1
        assert not exe.is_file()
        staged = renamed[0][1]
        assert staged.is_file()

        # Test failure restore
        _restore_windows_binaries(renamed, success=False)
        assert exe.is_file()
        assert not staged.is_file()

        # Test success cleanup
        renamed = _prepare_windows_binaries(tmp_path)
        assert len(renamed) == 1
        staged = renamed[0][1]
        assert staged.is_file()
        _restore_windows_binaries(renamed, success=True)
        assert not staged.is_file()


def test_is_output_already_up_to_date():
    satisfied_output = (
        "Requirement already satisfied: repo360 from git+https://github.com/Adithya-Meda/repo360.git@main\n"
        "Requirement already satisfied: requests in c:\\python\\lib\\site-packages\n"
    )
    assert is_output_already_up_to_date(satisfied_output, "") is True

    installed_output = (
        "Collecting repo360 from git+https://...\n"
        "Installing collected packages: repo360\n"
        "Successfully installed repo360-1.1.0\n"
    )
    assert is_output_already_up_to_date(installed_output, "") is False

    mixed_output = (
        "Requirement already satisfied: certifi in c:\\lib\n"
        "Successfully installed repo360-1.0.1\n"
    )
    assert is_output_already_up_to_date(mixed_output, "") is False

    pipx_up_to_date = "package repo360 is already up-to-date\n"
    assert is_output_already_up_to_date(pipx_up_to_date, "") is True


def test_upgrade_result_type():
    res = UpgradeResult(0, already_up_to_date=True, version="v1.0.0", commit="1234567")
    assert isinstance(res, int)
    assert res == 0
    assert res.already_up_to_date is True
    assert res.version == "v1.0.0"
    assert res.commit == "1234567"

    res_fail = UpgradeResult(1, already_up_to_date=False, version="v1.0.0")
    assert res_fail == 1
    assert res_fail.already_up_to_date is False


def test_execute_upgrade_already_up_to_date():
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "Requirement already satisfied: repo360 in c:\\site-packages"
    mock_res.stderr = ""
    with patch("subprocess.run", return_value=mock_res):
        ret = execute_upgrade([("pip", ["pip", "install", "pkg"])], dry_run=False)
        assert ret == 0
        assert getattr(ret, "already_up_to_date", False) is True


def test_execute_upgrade_freshly_installed():
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "Successfully installed repo360-1.1.0"
    mock_res.stderr = ""
    with patch("subprocess.run", return_value=mock_res):
        ret = execute_upgrade([("pip", ["pip", "install", "pkg"])], dry_run=False)
        assert ret == 0
        assert getattr(ret, "already_up_to_date", False) is False


def test_run_upgrade_cli_already_up_to_date_via_preflight(capsys):
    with patch("src.upgrade.get_local_version_info", return_value=("v1.0.0", "abcdef123456")):
        with patch("src.upgrade.get_remote_commit_sha", return_value="abcdef123456"):
            ret = run_upgrade_cli([])
            assert ret == 0
            captured = capsys.readouterr()
            assert "[SUCCESS]" in captured.out
            assert "already on the latest version" in captured.out
            assert "abcdef1" in captured.out


def test_run_upgrade_cli_already_up_to_date_via_installer(capsys):
    up_to_date_result = UpgradeResult(0, already_up_to_date=True, version="v1.0.0", commit="abcdef12")
    with patch("src.upgrade.get_local_version_info", return_value=("v1.0.0", None)):
        with patch("src.upgrade.execute_upgrade", return_value=up_to_date_result):
            ret = run_upgrade_cli([])
            assert ret == 0
            captured = capsys.readouterr()
            assert "[SUCCESS]" in captured.out
            assert "already on the latest version" in captured.out


def test_run_upgrade_cli_fresh_upgrade(capsys):
    fresh_result = UpgradeResult(0, already_up_to_date=False, version="v1.1.0", commit="fedcba98")
    with patch("src.upgrade.get_local_version_info", return_value=("v1.0.0", None)):
        with patch("src.upgrade.execute_upgrade", return_value=fresh_result):
            ret = run_upgrade_cli([])
            assert ret == 0
            captured = capsys.readouterr()
            assert "[SUCCESS]" in captured.out
            assert "upgraded successfully" in captured.out


def test_is_output_already_up_to_date_uv_markers() -> None:
    """uv package install output containing '+ repo360' must not be misclassified as up-to-date."""
    from src.upgrade import is_output_already_up_to_date

    assert is_output_already_up_to_date("", "+ repo360==1.0.0\nInstalled 1 package in 18ms\n") is False
    assert is_output_already_up_to_date("", "Prepared 1 package in 1.2s\nInstalled 1 package in 15ms\n+ repo360==1.0.0\n") is False
    assert is_output_already_up_to_date("Requirement already satisfied: repo360", "") is True


def test_get_installation_details_and_force_commands() -> None:
    """Verify installation metadata inspection and --force command generation."""
    from unittest.mock import patch
    from src.upgrade import get_installation_details, get_upgrade_commands

    details = get_installation_details()
    for key in ("version", "commit", "commit_short", "commit_source", "installer", "source_url", "package_path"):
        assert key in details

    with patch("src.upgrade.get_installation_details", return_value={"installer": "uv"}):
        cmds = get_upgrade_commands(installer_override="pipx", force=True)
        joined = " ".join(cmds[0][1])
        assert "--reinstall-package=repo360" in joined

    cmds_pip = get_upgrade_commands(installer_override="pip", force=True)
    assert "--force-reinstall" in cmds_pip[0][1]


def test_check_installation_against_remote_match_and_mismatch(capsys) -> None:
    """Verify commit verification against origin/main handles match, outdated, and --strict mode."""
    from unittest.mock import patch
    from src.upgrade import check_installation_against_remote, run_version_cli

    with patch("src.upgrade.get_installation_details", return_value={
        "version": "1.0.0",
        "commit": "cdfeb8143bd88aa17a02d03d47175c52d251002e",
        "commit_short": "cdfeb814",
        "commit_source": "direct_url.json",
        "installer": "uv",
        "source_url": "https://github.com/org/repo.git",
        "package_path": "C:/env/repo360",
    }), patch("src.upgrade.get_remote_commit_sha", return_value="cdfeb8143bd88aa17a02d03d47175c52d251002e"):
        rc = run_version_cli(["--check", "--strict"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "UP-TO-DATE" in out
        assert "cdfeb814" in out

    with patch("src.upgrade.get_installation_details", return_value={
        "version": "1.0.0",
        "commit": "228a2fe606fb904c0b475bde69da3ee95a7fd39c",
        "commit_short": "228a2fe6",
        "commit_source": "direct_url.json",
        "installer": "uv",
        "source_url": "https://github.com/org/repo.git",
        "package_path": "C:/env/repo360",
    }), patch("src.upgrade.get_remote_commit_sha", return_value="cdfeb8143bd88aa17a02d03d47175c52d251002e"):
        assert check_installation_against_remote(strict=False) == 0
        out_warn = capsys.readouterr().out
        assert "OUTDATED" in out_warn
        assert check_installation_against_remote(strict=True) == 1
