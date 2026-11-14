"""Unit tests for Repo360 upgrade module."""

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

from src.upgrade import (
    ReleaseTarget,
    UpgradeResult,
    _build_target_spec,
    _detect_pipx_backend,
    _detect_pipx_venv_name,
    _list_remote_tags,
    _parse_semver_tag,
    _prepare_windows_binaries,
    _restore_windows_binaries,
    build_upgrade_command,
    detect_installer,
    execute_upgrade,
    find_uv_executable,
    format_version_output,
    get_installation_details,
    get_local_version_info,
    get_upgrade_commands,
    is_output_already_up_to_date,
    normalize_release_tag,
    resolve_commit_target,
    resolve_release_target,
    run_upgrade_cli,
    run_version_cli,
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
            assert "1 private key detected" in captured.out


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

        _restore_windows_binaries(renamed, success=False)
        assert exe.is_file()
        assert not staged.is_file()

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
    local_details = {
        "version": "v1.0.0",
        "commit": "abcdef123456",
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    target = ReleaseTarget(tag="v1.0.0", commit="abcdef123456", source="github-release")
    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=target) as mock_res:
        ret = run_upgrade_cli([])
        assert ret == 0
        mock_res.assert_called_once_with("https://github.com/Adithya-Meda/repo360.git", requested_tag=None)
        captured = capsys.readouterr().out.strip()
        assert captured == "Repo360 1.0.0 (abcdef1) is already up to date."


def test_run_upgrade_cli_already_up_to_date_via_installer(capsys):
    local_details = {
        "version": "v1.0.0",
        "commit": None,
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    target = ReleaseTarget(tag="v1.0.0", commit="abcdef123456", source="github-release")
    up_to_date_result = UpgradeResult(0, already_up_to_date=True, version="v1.0.0", commit="abcdef12")
    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=target), \
         patch("src.upgrade.execute_upgrade", return_value=up_to_date_result):
        ret = run_upgrade_cli([])
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Repo360 1.0.0 (abcdef1) is already up to date." in captured


def test_run_upgrade_cli_fresh_upgrade(capsys):
    local_details = {
        "version": "v1.0.0",
        "commit": None,
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    target = ReleaseTarget(tag="v1.1.0", commit="fedcba987654", source="github-release")
    fresh_result = UpgradeResult(0, already_up_to_date=False, version="v1.1.0", commit="fedcba98")
    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=target), \
         patch("src.upgrade.execute_upgrade", return_value=fresh_result):
        ret = run_upgrade_cli([])
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Upgrading Repo360 to v1.1.0..." in captured
        assert "Repo360 upgraded to 1.1.0 (fedcba9)." in captured


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


def test_compare_installed_tree_and_stale_local_snapshot(tmp_path) -> None:
    """Verify stale non-editable local installs are flagged instead of reporting repo HEAD."""
    import json
    from src.upgrade import _compare_installed_tree_to_source, get_installation_details

    installed_pkg = tmp_path / "site-packages" / "src"
    source_repo = tmp_path / "checkout"
    source_pkg = source_repo / "src"
    installed_pkg.mkdir(parents=True)
    source_pkg.mkdir(parents=True)

    (installed_pkg / "upgrade.py").write_text("# v1\n", encoding="utf-8")
    (source_pkg / "upgrade.py").write_text("# v1\n", encoding="utf-8")
    assert _compare_installed_tree_to_source(installed_pkg, source_pkg) == (True, [])

    (source_pkg / "setup_app.py").write_text("# new setup\n", encoding="utf-8")
    (installed_pkg / "setup_app.py").write_text("# old setup\n", encoding="utf-8")
    in_sync, mismatched = _compare_installed_tree_to_source(installed_pkg, source_pkg)
    assert in_sync is False
    assert mismatched == ["setup_app.py"]

    mock_dist = MagicMock()
    mock_dist.version = "1.0.0"
    mock_dist.read_text.side_effect = lambda name: {
        "INSTALLER": "uv",
        "direct_url.json": json.dumps({"url": source_repo.as_uri(), "dir_info": {}}),
    }.get(name)

    fake_git = {
        "commit": "79098e85192fdb486d4dc6c6c55e7540f374612a",
        "commit_date": "2026-11-11T13:05:22+05:30",
        "commit_subject": "feat: fixed exit command time execution",
        "branch": "main",
        "is_dirty": False,
    }
    with patch("src.upgrade.Path.resolve", side_effect=[installed_pkg, installed_pkg, source_pkg]), \
         patch("importlib.metadata.distribution", return_value=mock_dist), \
         patch("src.upgrade._get_git_commit_details", return_value=fake_git):
        details_stale = get_installation_details()
        assert details_stale["is_stale_snapshot"] is True
        assert details_stale["commit"] is None
        assert details_stale["commit_short"] == "stale"
        assert details_stale["local_repo_head"] == fake_git["commit"]
        assert "STALE local snapshot" in details_stale["commit_source"]


def test_run_version_cli_minimal_standard_output_without_network(capsys) -> None:
    """Verify 'repo360 version' prints one clean minimal line and makes zero network requests."""
    details_vcs = {
        "version": "v1.0.0",
        "commit": "79098e85192fdb486d4dc6c6c55e7540f374612a",
        "is_stale_snapshot": False,
    }
    with patch("src.upgrade.get_installation_details", return_value=details_vcs), \
         patch("src.upgrade.resolve_release_target") as mock_release, \
         patch("requests.get") as mock_get:
        assert run_version_cli([]) == 0
        assert capsys.readouterr().out.strip() == "repo360 1.0.0 (79098e8)"
        mock_release.assert_not_called()
        mock_get.assert_not_called()

    assert format_version_output({"version": "1.2.0", "commit": None, "is_stale_snapshot": False}) == "repo360 1.2.0"
    assert format_version_output({"version": "v1.0.0", "commit": None, "is_stale_snapshot": True}) == "repo360 1.0.0 (stale)"


@pytest.mark.parametrize(
    "runner,bad_args",
    [
        (run_version_cli, ["--check"]),
        (run_version_cli, ["--offline"]),
        (run_version_cli, ["--strict"]),
        (run_version_cli, ["--source", "https://example.com"]),
        (run_upgrade_cli, ["--check"]),
        (run_upgrade_cli, ["--force"]),
        (run_upgrade_cli, ["--pip"]),
        (run_upgrade_cli, ["--dry-run"]),
        (run_upgrade_cli, ["--source", "https://example.com"]),
    ],
)
def test_version_and_upgrade_reject_removed_flags(runner, bad_args) -> None:
    """Verify removed CLI flags fail cleanly with argparse exit code 2."""
    with pytest.raises(SystemExit) as exc:
        runner(bad_args)
    assert exc.value.code == 2


def test_run_upgrade_cli_forces_reinstall_when_stale_or_outdated() -> None:
    """Verify run_upgrade_cli passes force=True and target_ref=tag when local commit is None/stale or outdated."""
    local_details = {
        "version": "v1.0.0",
        "commit": None,
        "is_dirty": False,
        "is_stale_snapshot": True,
    }
    target = ReleaseTarget(tag="v1.1.0", commit="79098e85", source="github-release")
    fresh_result = UpgradeResult(0, already_up_to_date=False, version="v1.1.0", commit="79098e85")
    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.get_upgrade_commands", return_value=[("uv", ["uv", "pip", "install", "--reinstall-package", "repo360"])]) as mock_cmds, \
         patch("src.upgrade.execute_upgrade", return_value=fresh_result), \
         patch("src.upgrade.resolve_release_target", return_value=target):
        rc = run_upgrade_cli([])
        assert rc == 0
        mock_cmds.assert_called_once()
        assert mock_cmds.call_args.kwargs.get("force") is True
        assert mock_cmds.call_args.kwargs.get("target_ref") == "v1.1.0"


def test_build_target_spec_pins_commit_and_preserves_existing_refs() -> None:
    assert (
        _build_target_spec("https://github.com/Adithya-Meda/repo360.git", "abc12345")
        == "git+https://github.com/Adithya-Meda/repo360.git@abc12345"
    )
    assert (
        _build_target_spec("git+https://github.com/Adithya-Meda/repo360.git", "abc12345")
        == "git+https://github.com/Adithya-Meda/repo360.git@abc12345"
    )
    assert (
        _build_target_spec("https://github.com/Adithya-Meda/repo360.git@v1.2.3", "abc12345")
        == "git+https://github.com/Adithya-Meda/repo360.git@v1.2.3"
    )
    assert _build_target_spec("file:///C:/repo/repo360", "abc12345") == "file:///C:/repo/repo360"
    assert (
        _build_target_spec("https://github.com/Adithya-Meda/repo360.git", None)
        == "git+https://github.com/Adithya-Meda/repo360.git"
    )


def test_pipx_metadata_backend_and_venv_name_detection(tmp_path: Path) -> None:
    venv_dir = tmp_path / "pipx" / "venvs" / "repo360-dev"
    venv_dir.mkdir(parents=True)
    (venv_dir / "pipx_metadata.json").write_text(json.dumps({"backend": "uv"}), encoding="utf-8")

    with patch.object(sys, "prefix", str(venv_dir)):
        assert _detect_pipx_venv_name() == "repo360-dev"
        # pipx_metadata.json takes precedence over INSTALLER metadata
        with patch("src.upgrade.get_installation_details", return_value=("pip", "1.0.0", "git://...", "1234")):
            assert _detect_pipx_backend() == "uv"

    with patch.object(sys, "prefix", "C:\\Python312"):
        assert _detect_pipx_venv_name() == "repo360"


def test_find_uv_executable_discovers_bundled_uv(tmp_path: Path) -> None:
    bundled_uv = tmp_path / ("uv.exe" if sys.platform == "win32" else "uv")
    bundled_uv.write_text("stub", encoding="utf-8")
    fake_uv_module = MagicMock()
    fake_uv_module.find_uv_bin.return_value = str(bundled_uv)
    with patch("src.upgrade.shutil.which", return_value=None), \
         patch.dict(sys.modules, {"uv": fake_uv_module}):
        assert find_uv_executable() == str(bundled_uv)


def test_get_upgrade_commands_strategies_for_pipx_uv_and_uv_tool() -> None:
    with patch("src.upgrade._is_uv_tool_install", return_value=True), \
         patch.object(sys, "prefix", "C:\\uv\\tools\\repo360"), \
         patch("src.upgrade.find_uv_executable", return_value="C:\\bin\\uv.exe"), \
         patch("src.upgrade.find_pipx_executable", return_value=None):
        cmds = get_upgrade_commands(
            repo_url="https://github.com/Adithya-Meda/repo360.git",
            force=True,
            target_ref="deadbeef",
        )
        labels = [label for label, _ in cmds]
        assert "uv-tool" in labels
        uv_tool_cmd = dict(cmds)["uv-tool"]
        assert "tool" in uv_tool_cmd
        assert any("deadbeef" in arg for arg in uv_tool_cmd)

    with patch("src.upgrade._is_uv_tool_install", return_value=False), \
         patch.object(sys, "prefix", "C:\\Users\\test\\pipx\\venvs\\repo360-alt"), \
         patch("src.upgrade.find_pipx_executable", return_value="C:\\bin\\pipx.exe"), \
         patch("src.upgrade._detect_pipx_venv_name", return_value="repo360-alt"), \
         patch("src.upgrade._detect_pipx_backend", return_value="uv"), \
         patch("src.upgrade._pip_available_for_python", return_value=False), \
         patch("src.upgrade.find_uv_executable", return_value="C:\\bin\\uv.exe"):
        cmds = get_upgrade_commands(
            repo_url="https://github.com/Adithya-Meda/repo360.git",
            force=True,
            target_ref="cafebabe",
        )
        labels = [label for label, _ in cmds]
        assert "pipx" in labels
        assert "uv" in labels
        assert "pip" not in labels
        pipx_cmd = dict(cmds)["pipx"]
        assert "repo360-alt" in pipx_cmd
        assert "--reinstall-package=repo360" in pipx_cmd or any(
            "--reinstall-package" in arg for arg in pipx_cmd
        )
        assert "--refresh-package=repo360" in pipx_cmd or any(
            "--refresh-package" in arg for arg in pipx_cmd
        )
        assert any("cafebabe" in arg for arg in pipx_cmd)

    with patch("src.upgrade._is_uv_tool_install", return_value=False), \
         patch.object(sys, "prefix", "C:\\Users\\test\\pipx\\venvs\\repo360"), \
         patch("src.upgrade.find_pipx_executable", return_value="C:\\bin\\pipx.exe"), \
         patch("src.upgrade._detect_pipx_venv_name", return_value="repo360"), \
         patch("src.upgrade._detect_pipx_backend", return_value="pip"), \
         patch("src.upgrade._pip_available_for_python", return_value=True), \
         patch("src.upgrade.find_uv_executable", return_value=None):
        cmds = get_upgrade_commands(
            repo_url="https://github.com/Adithya-Meda/repo360.git",
            force=True,
            target_ref="cafebabe",
        )
        labels = [label for label, _ in cmds]
        assert "pipx" in labels
        assert "pipx-isolated" in labels
        assert "--no-deps" in dict(cmds)["pipx-isolated"]


def test_get_upgrade_commands_pip_includes_isolated_fallback() -> None:
    with patch("src.upgrade._is_uv_tool_install", return_value=False), \
         patch.object(sys, "prefix", "C:\\Python312"), \
         patch("src.upgrade._pip_available_for_python", return_value=True), \
         patch("src.upgrade.find_uv_executable", return_value=None):
        cmds = get_upgrade_commands(
            repo_url="https://github.com/Adithya-Meda/repo360.git",
            force=True,
            target_ref="11223344",
        )
        labels = [label for label, _ in cmds]
        assert "pip" in labels
        assert "pip-isolated" in labels
        pip_cmd = dict(cmds)["pip"]
        pip_iso_cmd = dict(cmds)["pip-isolated"]
        assert "--no-cache-dir" in pip_cmd
        assert "--no-deps" in pip_iso_cmd
        assert "--no-cache-dir" in pip_iso_cmd


def test_windows_binaries_staging_restores_missing_exe_even_on_noop_success(tmp_path: Path) -> None:
    exe = tmp_path / "repo360.exe"
    other = tmp_path / "other.exe"
    exe.write_bytes(b"original-binary")
    other.write_bytes(b"leave-alone")

    renamed = _prepare_windows_binaries(tmp_path)
    assert len(renamed) == 1
    orig_path, staged_path = renamed[0]
    assert orig_path == exe
    assert not exe.exists()
    assert staged_path.exists()
    assert other.exists()

    _restore_windows_binaries(renamed, success=True)
    assert exe.exists()
    assert exe.read_bytes() == b"original-binary"
    assert not staged_path.exists()


def test_windows_binaries_restore_cleans_backup_when_new_exe_exists(tmp_path: Path) -> None:
    exe = tmp_path / "repo360.exe"
    exe.write_bytes(b"old-binary")
    renamed = _prepare_windows_binaries([tmp_path])
    staged_path = renamed[0][1]

    exe.write_bytes(b"new-binary")
    _restore_windows_binaries(renamed, success=True)
    assert exe.read_bytes() == b"new-binary"
    assert not staged_path.exists()


def test_is_output_already_up_to_date_avoids_checked_false_positive() -> None:
    assert is_output_already_up_to_date("Checked 1 package in 12ms", "") is False
    assert is_output_already_up_to_date("Requirement already satisfied: repo360", "") is True
    assert is_output_already_up_to_date("", "Audited 1 package in 8ms") is True


def test_execute_upgrade_preserves_primary_error_and_invalidates_caches(capsys) -> None:
    primary_fail = MagicMock(returncode=1, stdout="", stderr="Git clone failed: connection refused")
    secondary_no_pip = MagicMock(returncode=1, stdout="", stderr="No module named pip")

    with patch("src.upgrade.get_local_version_info", return_value=("v1.0.0", "11223344")), \
         patch("src.upgrade.run_interruptible_subprocess", side_effect=[primary_fail, secondary_no_pip]), \
         patch("src.upgrade._prepare_windows_binaries", return_value=[]), \
         patch("src.upgrade._restore_windows_binaries"):
        res = execute_upgrade([("pipx", ["pipx", "runpip", "..."]), ("pip", ["python", "-m", "pip", "..."])])
        assert int(res) != 0
        out = capsys.readouterr().out
        assert "Git clone failed: connection refused" in out
        assert "No module named pip" not in out

    uv_ok = MagicMock(returncode=0, stdout="Audited 1 package in 5ms", stderr=" + repo360==1.0.0")
    with patch("src.upgrade.get_local_version_info", return_value=("v1.0.0", "abcd1234")), \
         patch("src.upgrade.run_interruptible_subprocess", return_value=uv_ok), \
         patch("src.upgrade._prepare_windows_binaries", return_value=[]), \
         patch("src.upgrade._restore_windows_binaries"), \
         patch("src.upgrade.importlib.invalidate_caches") as mock_invalidate:
        res = execute_upgrade([("pipx", ["pipx", "runpip", "..."])])
        assert int(res) == 0
        mock_invalidate.assert_called()


def test_run_upgrade_cli_forces_when_tag_commit_sha_unavailable() -> None:
    local_details = {
        "version": "v1.0.0",
        "commit": "11223344",
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    target = ReleaseTarget(tag="v1.1.0", commit=None, source="github-release")
    fresh_result = UpgradeResult(0, already_up_to_date=False, version="v1.1.0", commit="99887766")
    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=target), \
         patch("src.upgrade.get_upgrade_commands", return_value=[("pip", ["pip", "install"])]) as mock_cmds, \
         patch("src.upgrade.execute_upgrade", return_value=fresh_result):
        rc = run_upgrade_cli([])
        assert rc == 0
        assert mock_cmds.call_args.kwargs.get("force") is True
        assert mock_cmds.call_args.kwargs.get("target_ref") == "v1.1.0"


def test_normalize_release_tag_and_semver_parsing() -> None:
    assert normalize_release_tag(" v1.2.3 ") == "v1.2.3"
    assert normalize_release_tag("1.0.0") == "1.0.0"
    assert normalize_release_tag("v2.0.0-rc1") == "v2.0.0-rc1"

    for bad_tag in ("", "   ", "-v1.0.0", "--upload-pack=evil", "v1..0", "main@{1}", "v1.0.0/", "v1.0.0.lock", "bad tag"):
        with pytest.raises(ValueError):
            normalize_release_tag(bad_tag)

    assert _parse_semver_tag("v1.10.2") == (1, 10, 2)
    assert _parse_semver_tag("1.2.0") == (1, 2, 0)
    assert _parse_semver_tag("v1.2.0-rc1") is None
    assert _parse_semver_tag("main") is None


def test_list_remote_tags_dereferences_annotated_tags() -> None:
    ls_out = (
        "1111111111111111111111111111111111111111\trefs/tags/v1.0.0\n"
        "aaaaaaa111111111111111111111111111111111\trefs/tags/v1.0.0^{}\n"
        "bbbbbbb222222222222222222222222222222222\trefs/tags/v1.1.0\n"
    )
    mock_proc = MagicMock(returncode=0, stdout=ls_out)
    with patch("src.upgrade.run_interruptible_subprocess", return_value=mock_proc) as mock_sub:
        tags = _list_remote_tags("https://github.com/Adithya-Meda/repo360.git")
        assert tags == {
            "v1.0.0": "aaaaaaa111111111111111111111111111111111",
            "v1.1.0": "bbbbbbb222222222222222222222222222222222",
        }
        cmd = mock_sub.call_args[0][0]
        assert cmd[:4] == ["git", "ls-remote", "--tags", "--"]

    assert _list_remote_tags("-oProxyCommand=evil") is None


def test_resolve_release_target_latest_and_specific_tag() -> None:
    api_ok = MagicMock(status_code=200)
    api_ok.json.return_value = {"tag_name": "v1.2.0", "draft": False, "prerelease": False}
    with patch("requests.get", return_value=api_ok), \
         patch("src.upgrade._list_remote_tags", return_value={"v1.2.0": "ccc123456789"}):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git")
        assert res == ReleaseTarget(tag="v1.2.0", commit="ccc123456789", source="github-release")

    api_403 = MagicMock(status_code=403)
    tags_map = {
        "v1.2.0": "sha120",
        "v1.10.0": "sha1100",
        "v2.0.0-rc1": "sha200rc1",
        "latest": "shalatest",
    }
    with patch("requests.get", return_value=api_403), \
         patch("src.upgrade._list_remote_tags", return_value=tags_map):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git")
        assert res == ReleaseTarget(tag="v1.10.0", commit="sha1100", source="semver-tag")

    api_404 = MagicMock(status_code=404)
    with patch("requests.get", return_value=api_404), \
         patch("src.upgrade._list_remote_tags", return_value={}):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git")
        assert res == ReleaseTarget(tag=None, commit=None, source="no-releases")

    with patch("src.upgrade._list_remote_tags", return_value={"v1.0.0": "sha100"}):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git", requested_tag="1.0.0")
        assert res == ReleaseTarget(tag="v1.0.0", commit="sha100", source="git-tag")

    with patch("src.upgrade._list_remote_tags", return_value={}):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git", requested_tag="v9.9.9")
        assert res == ReleaseTarget(tag=None, commit=None, source="tag-not-found")

    rel_ok = MagicMock(status_code=200)
    with patch("src.upgrade._list_remote_tags", return_value=None), \
         patch("requests.get", return_value=rel_ok), \
         patch("src.upgrade._fetch_commit_summary", return_value={"sha": "apisha123"}):
        res = resolve_release_target("https://github.com/Adithya-Meda/repo360.git", requested_tag="v1.0.0")
        assert res == ReleaseTarget(tag="v1.0.0", commit="apisha123", source="github-release")


def test_run_upgrade_cli_specific_tag_and_error_cases(capsys) -> None:
    local_details = {
        "version": "v1.1.0",
        "commit": "bbb22222",
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    target_v1 = ReleaseTarget(tag="v1.0.0", commit="aaa11111", source="git-tag")
    switched_result = UpgradeResult(0, already_up_to_date=False, version="v1.0.0", commit="aaa11111")

    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=target_v1) as mock_res, \
         patch("src.upgrade.get_upgrade_commands", return_value=[("pip", ["pip", "install"])]) as mock_cmds, \
         patch("src.upgrade.execute_upgrade", return_value=switched_result):
        rc = run_upgrade_cli(["--tag", "v1.0.0"])
        assert rc == 0
        mock_res.assert_called_once_with("https://github.com/Adithya-Meda/repo360.git", requested_tag="v1.0.0")
        assert mock_cmds.call_args.kwargs.get("target_ref") == "v1.0.0"
        assert mock_cmds.call_args.kwargs.get("force") is True
        out = capsys.readouterr().out
        assert "Installing Repo360 v1.0.0..." in out
        assert "Repo360 1.0.0 (aaa1111) installed (v1.0.0)." in out

    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_release_target", return_value=ReleaseTarget(tag=None, commit=None, source="no-releases")):
        rc = run_upgrade_cli([])
        assert rc == 1
        assert "No published releases found" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        run_upgrade_cli(["--tag", "--bad"])
    assert exc.value.code == 2


def test_get_installation_details_trusts_dist_version_even_when_1_0_0() -> None:
    """Verify installed distribution version '1.0.0' is preserved when package __version__ is higher."""
    mock_dist = MagicMock()
    mock_dist.version = "1.0.0"
    mock_dist.read_text.side_effect = lambda name: {
        "INSTALLER": "pip",
        "direct_url.json": json.dumps({"url": "https://github.com/Adithya-Meda/repo360.git", "vcs_info": {"commit_id": "abc1234def5678", "requested_revision": "v1.0.0"}}),
    }.get(name)

    with patch("importlib.metadata.distribution", return_value=mock_dist), \
         patch("src.upgrade.PACKAGE_VERSION", "1.2.0"):
        details = get_installation_details()
        assert details["version"] == "v1.0.0"
        assert details["branch"] == "v1.0.0"


def test_posix_tool_discovery_falls_back_to_user_and_cargo_bins(tmp_path: Path) -> None:
    """POSIX pipx and uv discovery check user local and Cargo bin dirs when omitted from PATH."""
    from src.upgrade import find_pipx_executable

    local_bin = tmp_path / ".local" / "bin"
    cargo_bin = tmp_path / ".cargo" / "bin"
    local_bin.mkdir(parents=True)
    cargo_bin.mkdir(parents=True)
    pipx_bin = local_bin / "pipx"
    uv_bin = cargo_bin / "uv"
    pipx_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    uv_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    with patch("src.upgrade.sys.platform", "linux"), \
         patch("src.upgrade.shutil.which", return_value=None), \
         patch("src.upgrade.os.path.expanduser", return_value=str(tmp_path)), \
         patch.dict(sys.modules, {"uv": None}):
        assert find_pipx_executable() == str(pipx_bin)
        assert find_uv_executable() == str(uv_bin)


def test_resolve_commit_target_head_branch_and_sha() -> None:
    head_proc = MagicMock(returncode=0, stdout="deadbeef1234567890abcdef1234567890abcdef\tHEAD\n")
    with patch("src.upgrade.run_interruptible_subprocess", return_value=head_proc):
        res = resolve_commit_target("https://github.com/Adithya-Meda/repo360.git")
        assert res == ReleaseTarget(
            tag="HEAD",
            commit="deadbeef1234567890abcdef1234567890abcdef",
            source="git-head",
        )

    branch_proc = MagicMock(returncode=0, stdout="cafebabe1234567890abcdef1234567890abcdef\trefs/heads/main\n")
    with patch("src.upgrade.run_interruptible_subprocess", return_value=branch_proc):
        res = resolve_commit_target("https://github.com/Adithya-Meda/repo360.git", requested_ref="main")
        assert res == ReleaseTarget(
            tag="main",
            commit="cafebabe1234567890abcdef1234567890abcdef",
            source="git-branch",
        )

    with patch("src.upgrade._fetch_commit_summary", return_value={"sha": "12805b0d15f995f8ced59b4b8c576f875f65cf71"}):
        res = resolve_commit_target("https://github.com/Adithya-Meda/repo360.git", requested_ref="12805b0")
        assert res == ReleaseTarget(
            tag="12805b0",
            commit="12805b0d15f995f8ced59b4b8c576f875f65cf71",
            source="github-commit",
        )

    with patch("src.upgrade._fetch_commit_summary", return_value=None):
        res = resolve_commit_target("https://github.com/Adithya-Meda/repo360.git", requested_ref="12805b0")
        assert res == ReleaseTarget(tag=None, commit=None, source="ref-not-found")


def test_run_upgrade_cli_latest_commit_and_ref_modes(capsys) -> None:
    local_details = {
        "version": "v1.0.0",
        "commit": "11111111",
        "is_dirty": False,
        "is_stale_snapshot": False,
    }
    head_target = ReleaseTarget(tag="HEAD", commit="abcdef1234567890", source="git-head")
    ok_res = UpgradeResult(0, already_up_to_date=False, version="v1.0.0", commit="abcdef1234567890")

    with patch("src.upgrade.get_installation_details", return_value=local_details), \
         patch("src.upgrade.resolve_commit_target", return_value=head_target) as mock_res, \
         patch("src.upgrade.get_upgrade_commands", return_value=[("pip", ["pip", "install"])]) as mock_cmds, \
         patch("src.upgrade.execute_upgrade", return_value=ok_res):
        rc = run_upgrade_cli(["--latest-commit"])
        assert rc == 0
        mock_res.assert_called_once_with("https://github.com/Adithya-Meda/repo360.git", requested_ref=None)
        assert mock_cmds.call_args.kwargs.get("target_ref") == "abcdef1234567890"
        assert "Upgrading Repo360 to latest commit (abcdef1)..." in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        run_upgrade_cli(["--tag", "v1.0.0", "--latest-commit"])
    assert exc.value.code == 2

