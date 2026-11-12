"""End-to-end integration tests for Repo360 CLI execution."""

import os
import sys
import tempfile
from unittest.mock import patch

from src.main import main, run_audit_cli


def test_bare_repo360_shows_help_without_executing_audit(capsys) -> None:
    """Verify that bare 'repo360' prints available commands and does NOT execute any audit."""
    with patch("sys.argv", ["repo360"]):
        with patch("src.main._run_cli") as mock_run:
            exit_code = main()
            assert exit_code == 0
            mock_run.assert_not_called()

    captured = capsys.readouterr()
    assert "Usage:" in captured.out
    assert "audit" in captured.out
    assert "setup" in captured.out


def test_unknown_subcommand_returns_error(capsys) -> None:
    """Verify that an invalid subcommand prints an error message and exits with code 1."""
    with patch("sys.argv", ["repo360", "invalid-cmd"]):
        with patch("src.main._run_cli") as mock_run:
            exit_code = main()
            assert exit_code == 1
            mock_run.assert_not_called()

    captured = capsys.readouterr()
    assert "Error: Unknown command 'invalid-cmd'" in captured.out


def test_main_mock_mode_execution() -> None:
    """Run full CLI execution in mock mode."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "repo360",
            "audit",
            "--output-dir",
            temp_dir,
            "--mock-mode",
        ]

        with patch("sys.argv", test_args):
            exit_code = main()

        assert exit_code == 0

        files = os.listdir(temp_dir)
        assert any(f.endswith(".xlsx") for f in files)
        assert not any(f.endswith(".csv") for f in files)
        assert not any(f.endswith(".html") for f in files)


def test_main_zero_config_execution() -> None:
    """Verify that repo360 audit runs with zero configuration files (pure built-in defaults)."""
    with tempfile.TemporaryDirectory() as temp_work_dir:
        old_cwd = os.getcwd()
        os.chdir(temp_work_dir)
        try:
            output_dir = os.path.join(temp_work_dir, "output")
            test_args = [
                "repo360",
                "audit",
                "--output-dir",
                output_dir,
                "--mock-mode",
            ]

            with patch("sys.argv", test_args):
                exit_code = main()

            assert exit_code == 0

            files = os.listdir(output_dir)
            assert any(f.endswith(".xlsx") for f in files)
            assert not any(f.endswith(".csv") for f in files)
            assert not any(f.endswith(".html") for f in files)
        finally:
            os.chdir(old_cwd)


def test_main_keyboard_interrupt_clean_exit() -> None:
    """Verify that Ctrl+C in CLI exits cleanly with code 130 without unhandled exception."""
    test_args = ["repo360", "audit", "--mock-mode"]

    with patch("sys.argv", test_args):
        with patch("src.main._run_cli", side_effect=KeyboardInterrupt):
            exit_code = main()
            assert exit_code == 130


def test_multi_org_mock_mode_execution(capsys) -> None:
    """Verify multi-org audit execution in mock mode with --org flag."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "repo360",
            "audit",
            "--org",
            "fintech-core,payments-svc",
            "--output-dir",
            temp_dir,
            "--mock-mode",
        ]

        with patch("sys.argv", test_args):
            exit_code = main()

        assert exit_code == 0

        captured = capsys.readouterr()
        assert "Organizations Audited:" in captured.out
        assert "2 (fintech-core, payments-svc)" in captured.out
        assert "Repositories Audited :" in captured.out
        assert "4" in captured.out

        files = os.listdir(temp_dir)
        assert any(f.endswith(".xlsx") for f in files)
        assert not any(f.endswith(".csv") for f in files)
        assert not any(f.endswith(".html") for f in files)


def test_multi_org_live_filtering() -> None:
    """Verify that --org filters live GitHub App installations and audits matching orgs."""
    mock_installations = [
        {"id": 101, "account": {"login": "OrgAlpha"}, "repository_selection": "all"},
        {"id": 102, "account": {"login": "OrgBeta"}, "repository_selection": "all"},
        {"id": 103, "account": {"login": "OrgGamma"}, "repository_selection": "all"},
    ]

    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "repo360",
            "audit",
            "--org",
            "OrgAlpha,OrgBeta",
            "--output-dir",
            temp_dir,
        ]

        with patch("sys.argv", test_args):
            with patch("src.credentials.resolve_runtime_credentials") as mock_creds:
                mock_creds.return_value = {
                    "app_id": "12345",
                    "installation_id": None,
                    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----",
                    "api_base_url": "https://api.github.com",
                    "llm_provider": "gemini",
                    "llm_model": "gemini-2.5-flash",
                    "llm_api_key": "",
                    "llm_enabled": False,
                }
                with patch("src.auth.github_app_auth.GitHubAppAuth._load_private_key", return_value="dummy_key"):
                    with patch("src.auth.github_app_auth.GitHubAppAuth.discover_installations", return_value=mock_installations):
                        with patch("src.collector.repo_inspector.RepoInspector.list_installation_repositories") as mock_list_repos:
                            mock_list_repos.side_effect = [
                                [{"name": "repo-a", "owner": {"login": "OrgAlpha"}, "default_branch": "main"}],
                                [{"name": "repo-b", "owner": {"login": "OrgBeta"}, "default_branch": "main"}],
                            ]
                            with patch("src.main.run_audit", return_value=[]):
                                exit_code = main()
                                assert exit_code == 0
                                assert mock_list_repos.call_count == 2
                                call_args_ids = [c[1]["installation_id"] for c in mock_list_repos.call_args_list]
                                assert 101 in call_args_ids
                                assert 102 in call_args_ids
                                assert 103 not in call_args_ids
