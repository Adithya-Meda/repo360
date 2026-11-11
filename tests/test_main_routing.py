"""Tests for main CLI subcommand routing."""

import sys
from unittest.mock import MagicMock, patch
import pytest
from src.main import _print_root_help, main


def test_print_root_help(capsys):
    ret = _print_root_help()
    assert ret == 0
    captured = capsys.readouterr()
    assert "audit" in captured.out
    assert "setup" in captured.out
    assert "activity" in captured.out
    assert "runners" in captured.out
    assert "upgrade" in captured.out


def test_main_bare_invocation(capsys):
    with patch.object(sys, "argv", ["repo360"]):
        ret = main()
        assert ret == 0
        captured = capsys.readouterr()
        assert "repo360 <command>" in captured.out


def test_main_help_flag(capsys):
    with patch.object(sys, "argv", ["repo360", "--help"]):
        ret = main()
        assert ret == 0
        captured = capsys.readouterr()
        assert "repo360 <command>" in captured.out


def test_main_route_activity():
    with patch.object(sys, "argv", ["repo360", "activity", "--help"]):
        with patch("src.activity.cli.run_activity_cli", return_value=0) as mock_act:
            ret = main()
            assert ret == 0
            assert mock_act.called


def test_main_route_runners():
    with patch.object(sys, "argv", ["repo360", "runners", "--help"]):
        with patch("src.runners.cli.run_runners_cli", return_value=0) as mock_run:
            ret = main()
            assert ret == 0
            assert mock_run.called


def test_main_route_finops():
    with patch.object(sys, "argv", ["repo360", "finops", "--help"]):
        with patch("src.finops.cli.run_finops_cli", return_value=0) as mock_fin:
            ret = main()
            assert ret == 0
            assert mock_fin.called


def test_main_route_dora():
    with patch.object(sys, "argv", ["repo360", "dora", "--help"]):
        with patch("src.dora.cli.run_dora_cli", return_value=0) as mock_dor:
            ret = main()
            assert ret == 0
            assert mock_dor.called


def test_main_route_bypass():
    with patch.object(sys, "argv", ["repo360", "bypass", "--help"]):
        with patch("src.bypass.cli.run_bypass_cli", return_value=0) as mock_byp:
            ret = main()
            assert ret == 0
            assert mock_byp.called


def test_main_route_upgrade():
    with patch.object(sys, "argv", ["repo360", "upgrade", "--dry-run"]):
        with patch("src.upgrade.run_upgrade_cli", return_value=0) as mock_up:
            ret = main()
            assert ret == 0
            assert mock_up.called

    with patch.object(sys, "argv", ["repo360", "update", "--dry-run"]):
        with patch("src.upgrade.run_upgrade_cli", return_value=0) as mock_up:
            ret = main()
            assert ret == 0
            assert mock_up.called


def test_main_route_health(capsys):
    with patch.object(sys, "argv", ["repo360", "health"]):
        with patch("src.credentials.resolve_runtime_credentials", return_value={"app_id": 12345, "private_key": "dummy", "api_base_url": "https://api.github.com"}):
            with patch("src.auth.github_app_auth.GitHubAppAuth._load_private_key", return_value="mock-pem"):
                with patch("src.auth.github_app_auth.GitHubAppAuth.discover_installations", return_value=[{"id": 1}]):
                    with patch("src.ai.init_ai_engine") as mock_ai:
                        mock_ai.return_value.is_available.return_value = True
                        ret = main()
                        assert ret == 0
                        captured = capsys.readouterr()
                        assert '"status": "HEALTHY"' in captured.out


def test_main_unknown_command(capsys):
    with patch.object(sys, "argv", ["repo360", "nonexistent"]):
        ret = main()
        assert ret == 1
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out


def test_run_audit_concurrent_execution():
    """Verify that run_audit executes across multiple repositories concurrently and prefetches trees."""
    from src.main import run_audit
    from src.validator import RepoTarget

    mock_inspector = MagicMock()
    mock_inspector.detect_tech_stack.return_value = {"languages": ["Python"]}
    mock_inspector.prefetch_repo_tree.return_value = {"README.md"}

    targets = [
        RepoTarget(org_name="fintech-core", repo_name="repo-1", target_personas={"all"}),
        RepoTarget(org_name="fintech-core", repo_name="repo-2", target_personas={"all"}),
        RepoTarget(org_name="fintech-core", repo_name="repo-3", target_personas={"all"}),
    ]

    config = {
        "enabled_checks": {"codeowners_validation": True},
        "policy_thresholds": {},
    }

    results = run_audit(
        inspector=mock_inspector,
        targets=targets,
        advisory_config=config,
        ai_engine=None,
        max_workers=3,
    )

    assert mock_inspector.prefetch_repo_tree.call_count == 3
    assert len(results) >= 3


def test_run_audit_keyboard_interrupt_stops_immediately():
    """Verify that KeyboardInterrupt during run_audit cancels executor and raises cleanly."""
    from src.main import run_audit
    from src.validator import RepoTarget

    mock_inspector = MagicMock()
    mock_inspector.prefetch_repo_tree.side_effect = KeyboardInterrupt()

    targets = [
        RepoTarget(org_name="fintech-core", repo_name="repo-1", target_personas={"all"}),
    ]

    with pytest.raises(KeyboardInterrupt):
        run_audit(
            inspector=mock_inspector,
            targets=targets,
            advisory_config={"enabled_checks": {}, "policy_thresholds": {}},
            ai_engine=None,
            max_workers=2,
        )


def test_log_repo_banner_advances_smoothly():
    """Verify that _log_repo_banner dynamically advances bar indicator and percentage without freezing."""
    import re
    from src.main import _log_repo_banner, logger
    from src.validator import RepoTarget

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    logged_messages = []
    with patch.object(logger, "info", side_effect=lambda msg, *args: logged_messages.append(msg % args if args else msg)):
        t = RepoTarget(org_name="org", repo_name="repo", target_personas={"all"})

        # 1. Start of 700 repo audit
        _log_repo_banner(1, 700, t)
        first_banner = ansi_re.sub("", logged_messages[1])
        assert "0.1%" in first_banner
        assert "[.-------------------]" in first_banner

        # 2. Advance to 5% (35 repos)
        logged_messages.clear()
        _log_repo_banner(35, 700, t)
        mid_banner = ansi_re.sub("", logged_messages[1])
        assert "5.0%" in mid_banner
        assert "[=.------------------]" in mid_banner

        # 3. Completion at 100%
        logged_messages.clear()
        _log_repo_banner(700, 700, t)
        final_banner = ansi_re.sub("", logged_messages[1])
        assert "100.0%" in final_banner
        assert "[====================]" in final_banner


def test_repo_inspector_seed_repo_metadata():
    """Verify that seed_repo_metadata eliminates network round trips for repository metadata."""
    from src.collector.repo_inspector import RepoInspector

    mock_client = MagicMock()
    inspector = RepoInspector(client=mock_client)

    # Seed metadata
    dummy_meta = {"name": "cached-repo", "language": "Go", "default_branch": "main"}
    inspector.seed_repo_metadata("my-org", "cached-repo", dummy_meta)

    # Fetch should return seeded data without calling mock_client
    res = inspector.get_repo_metadata("my-org", "cached-repo")
    assert res == dummy_meta
    assert mock_client.get.call_count == 0


def test_client_rate_limit_adaptive_delay_healthy():
    """Verify that client executes with zero delay when rate limit quota is healthy (>=1000)."""
    from src.collector.client import Repo360Client

    mock_auth = MagicMock()
    mock_auth.api_base_url = "https://api.github.com"
    client = Repo360Client(auth=mock_auth, adaptive_delay_ms=20)

    mock_resp = MagicMock()
    mock_resp.headers = {
        "x-ratelimit-remaining": "4500",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-reset": "1900000000",
    }

    with patch("time.sleep") as mock_sleep:
        client._handle_rate_limit(mock_resp)
        # Should NOT sleep when quota is healthy
        assert mock_sleep.call_count == 0


def test_run_audit_parallel_ai_enrichment_decoupled():
    """Verify that AI recommendations are dispatched and synthesized asynchronously without blocking repo audits."""
    from src.main import run_audit
    from src.validator import RepoTarget
    from src.checks.base import CheckResult, CheckStatus

    mock_inspector = MagicMock()
    mock_inspector.detect_tech_stack.return_value = {"languages": ["Python"]}
    mock_inspector.prefetch_repo_tree.return_value = {"README.md"}

    mock_ai = MagicMock()
    mock_ai.is_available.return_value = True
    mock_ai.is_circuit_active.return_value = False
    mock_ai.is_eligible_for_remediation.return_value = True

    def fake_enrich(result, tech_stack):
        result.remediation_snippet_after = "```yaml\n# Auto Enriched In Background\n```"
        result.extra_details["ai_enriched"] = True

    mock_ai.enrich_remediation.side_effect = fake_enrich

    targets = [
        RepoTarget(org_name="org1", repo_name="repo-a", target_personas={"all"}),
        RepoTarget(org_name="org1", repo_name="repo-b", target_personas={"all"}),
    ]

    config = {
        "enabled_checks": {"codeowners_validation": True},
        "policy_thresholds": {},
    }

    results = run_audit(
        inspector=mock_inspector,
        targets=targets,
        advisory_config=config,
        ai_engine=mock_ai,
        max_workers=2,
    )

    assert len(results) >= 2
    assert mock_ai.enrich_remediation.call_count >= 2
    repo_results = [r for r in results if r.repo_name in ("repo-a", "repo-b") and r.check_id == "codeowners_validation"]
    assert len(repo_results) == 2
    for r in repo_results:
        assert r.extra_details.get("ai_enriched") is True
        assert "# Auto Enriched In Background" in r.remediation_snippet_after



def test_main_route_version_flags(monkeypatch) -> None:
    """Verify 'version', '--version', and '-V' route to run_version_cli cleanly."""
    from unittest.mock import MagicMock
    from src import main as main_mod

    for flag in ("version", "--version", "-V"):
        monkeypatch.setattr(sys, "argv", ["repo360", flag, "--check"])
        mock_version = MagicMock(return_value=0)
        monkeypatch.setattr("src.upgrade.run_version_cli", mock_version)
        assert main_mod.main() == 0
        assert mock_version.called


def test_main_route_health_includes_commit_metadata(monkeypatch, capsys) -> None:
    """Verify 'repo360 health' output includes installed version and commit metadata."""
    import json
    from unittest.mock import MagicMock
    from src import main as main_mod

    monkeypatch.setattr(sys, "argv", ["repo360", "health"])
    monkeypatch.setattr("src.credentials.resolve_runtime_credentials", lambda: {"app_id": "123", "private_key": "pem"})
    monkeypatch.setattr("src.auth.github_app_auth.GitHubAppAuth", lambda **kw: MagicMock(discover_installations=lambda: [{"id": 1}]))
    monkeypatch.setattr("src.ai.init_ai_engine", lambda: MagicMock(is_available=lambda: True))
    monkeypatch.setattr("src.upgrade.get_installation_details", lambda: {
        "version": "1.0.0",
        "commit_short": "cdfeb814",
        "commit_source": "direct_url.json",
    })

    rc = main_mod.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "HEALTHY"
    assert payload["version"] == "1.0.0"
    assert payload["installed_commit"] == "cdfeb814"
    assert payload["commit_source"] == "direct_url.json"
