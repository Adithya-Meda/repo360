"""Unit and integration tests for Repo360 DORA & Flow Metrics."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from openpyxl import load_workbook

from src.dora.models import (
    DORAConfig,
    DeploymentRecord,
    RecoveryIncident,
    RepoDORAMetrics,
    compute_composite_tier,
    get_cfr_tier,
    get_df_tier,
    get_lttc_tier,
    get_mttr_tier,
)
from src.dora.collector import (
    build_dora_rows,
    calculate_recovery_incidents,
)
from src.dora.reporter import write_dora_workbook


def test_dora_tier_classifiers():
    assert get_df_tier(10.0) == "Elite"
    assert get_df_tier(3.0) == "High"
    assert get_df_tier(0.5) == "Medium"
    assert get_df_tier(0.1) == "Low"

    assert get_lttc_tier(0.5) == "Elite"
    assert get_lttc_tier(48.0) == "High"
    assert get_lttc_tier(300.0) == "Medium"
    assert get_lttc_tier(800.0) == "Low"
    assert get_lttc_tier(None) == "N/A"

    assert get_cfr_tier(2.0) == "Elite"
    assert get_cfr_tier(10.0) == "High"
    assert get_cfr_tier(15.0) == "Medium"
    assert get_cfr_tier(25.0) == "Low"
    assert get_cfr_tier(40.0) == "Low"

    assert get_mttr_tier(0.5) == "Elite"
    assert get_mttr_tier(12.0) == "High"
    assert get_mttr_tier(72.0) == "Medium"
    assert get_mttr_tier(200.0) == "Low"
    assert get_mttr_tier(None) == "N/A"

    assert compute_composite_tier(["Elite", "Elite", "High", "Elite"]) == "Elite"
    assert compute_composite_tier(["High", "High", "Medium", "High"]) == "High"
    assert compute_composite_tier(["Low", "Low", "Medium", "Low"]) == "Low"
    assert compute_composite_tier(
        ["Low", "Elite", "Elite", "Elite"], df_tier="Low", lttc_tier="Elite"
    ) == "Medium"
    assert compute_composite_tier(
        ["High", "N/A", "Elite", "Elite"], df_tier="High", lttc_tier="N/A"
    ) == "Medium"
    assert compute_composite_tier(
        ["Elite", "N/A", "Elite", "Elite"], df_tier="Elite", lttc_tier="N/A"
    ) == "High"


def test_recovery_incident_calculation():
    now = datetime.now(timezone.utc)
    ev_success_1 = DeploymentRecord(
        event_type="Deployment",
        event_id="dep-1",
        name="Deploy 1",
        environment="production",
        status="SUCCESS",
        ref="main",
        created_at=now - timedelta(hours=10),
    )
    ev_fail = DeploymentRecord(
        event_type="Deployment",
        event_id="dep-2",
        name="Deploy 2",
        environment="production",
        status="FAILURE",
        ref="main",
        created_at=now - timedelta(hours=6),
    )
    ev_restore = DeploymentRecord(
        event_type="Deployment",
        event_id="dep-3",
        name="Deploy 3 (Hotfix)",
        environment="production",
        status="SUCCESS",
        ref="hotfix",
        created_at=now - timedelta(hours=4),
    )

    incidents = calculate_recovery_incidents([ev_success_1, ev_fail, ev_restore])
    assert len(incidents) == 1
    inc = incidents[0]
    assert "dep-2" in inc.failed_event
    assert "dep-3" in inc.restored_event
    assert inc.mttr_hours == 2.0


def test_repo_dora_metrics_aggregation():
    now = datetime.now(timezone.utc)
    m = RepoDORAMetrics(org="fintech-org", repo="payment-gateway")

    for i in range(9):
        m.deployments.append(
            DeploymentRecord(
                event_type="Deployment",
                event_id=f"dep-{i}",
                name="Deploy",
                environment="production",
                status="SUCCESS",
                ref="main",
                created_at=now - timedelta(days=i),
                lead_time_hours=2.0 + i,
            )
        )
    m.deployments.append(
        DeploymentRecord(
            event_type="Deployment",
            event_id="dep-fail",
            name="Deploy Fail",
            environment="production",
            status="FAILURE",
            ref="main",
            created_at=now - timedelta(days=10),
            lead_time_hours=4.0,
        )
    )
    m.incidents.append(
        RecoveryIncident(
            failed_event="dep-fail",
            failure_time=now - timedelta(days=10),
            restored_event="dep-8",
            restored_time=now - timedelta(days=8),
            mttr_hours=48.0,
        )
    )

    assert m.total_deploys == 10
    assert m.successful_deploys == 9
    assert m.failed_deploys == 1
    assert m.change_failure_rate_pct == 10.0
    assert m.deployment_frequency_per_week(70) == 0.9
    assert m.median_lead_time_hours is not None
    assert m.median_mttr_hours == 48.0

    overall, df_tier, lttc_tier, cfr_tier, mttr_tier = m.get_tiers(70)
    assert df_tier == "Medium"
    assert cfr_tier == "High"
    assert mttr_tier == "Medium"


def test_build_dora_rows():
    now = datetime.now(timezone.utc)
    m = RepoDORAMetrics(org="fintech-org", repo="auth-service")
    m.deployments.append(
        DeploymentRecord(
            event_type="Release",
            event_id="v1.0.0",
            name="v1.0.0 Release",
            environment="production",
            status="SUCCESS",
            ref="v1.0.0",
            created_at=now,
            lead_time_hours=1.5,
        )
    )

    repo_rows, org_rows, deployment_rows, recovery_rows = build_dora_rows(
        metrics_list=[m],
        orgs=["fintech-org"],
        window_days=90,
    )

    assert len(repo_rows) == 1
    assert repo_rows[0][0] == "fintech-org"
    assert repo_rows[0][1] == "auth-service"
    assert repo_rows[0][3] == 1
    assert repo_rows[0][4] == 1

    assert len(org_rows) == 1
    assert org_rows[0][0] == "fintech-org"
    assert org_rows[0][2] == 1
    assert org_rows[0][4] == 1

    assert len(deployment_rows) == 1
    assert deployment_rows[0][2] == "Release"
    assert deployment_rows[0][3] == "v1.0.0"


def test_write_dora_workbook(tmp_path: Path):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=90)
    m = RepoDORAMetrics(org="demo-org", repo="demo-repo")

    repo_rows, org_rows, deployment_rows, recovery_rows = build_dora_rows(
        metrics_list=[m],
        orgs=["demo-org"],
        window_days=90,
    )

    out_file = tmp_path / "dora_test.xlsx"
    cfg = DORAConfig(output_dir=str(tmp_path), lookback_days=90)

    write_dora_workbook(
        path=out_file,
        cfg=cfg,
        since=since,
        end=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=deployment_rows,
        recovery_rows=recovery_rows,
        failures=[],
        execution_duration=2.0,
    )

    assert out_file.exists()
    wb = load_workbook(out_file)
    sheet_names = wb.sheetnames
    assert "Executive Summary" in sheet_names
    assert "Repository DORA Scorecard" in sheet_names
    assert "Deployment & Release Ledger" in sheet_names
    assert "Recovery & MTTR Ledger" in sheet_names


from unittest.mock import MagicMock
import requests

from src.dora.models import (
    compute_composite_tier,
    compute_fleet_dora_summary,
    get_lttc_tier,
    get_rework_tier,
)
from src.dora.collector import (
    _apply_native_incident_and_rework_signals,
    _deduplicate_release_and_workflow_events,
    _enrich_lead_times_from_commits,
    _resolve_deployment_status,
    collect_deploy_workflow_runs,
    collect_repo_deployments,
    profile_repo_dora,
)
from src.dora.cli import print_dora_scorecard


def test_inactive_repos_are_na_and_stability_guardrails():
    assert get_lttc_tier(None) == "N/A"
    assert get_lttc_tier(12.0) == "Elite"
    assert get_lttc_tier(24.0) == "High"
    assert get_lttc_tier(168.0) == "High"
    assert get_lttc_tier(720.0) == "Medium"
    assert get_lttc_tier(721.0) == "Low"

    assert get_rework_tier(None) == "N/A"
    assert get_rework_tier(2.0) == "Elite"
    assert get_rework_tier(5.0) == "High"
    assert get_rework_tier(12.0) == "Medium"
    assert get_rework_tier(20.0) == "Low"

    assert compute_composite_tier(["N/A", "N/A"]) == "N/A"
    assert compute_composite_tier(["Elite", "Elite", "Low", "Elite"], cfr_tier="Low") == "Medium"
    assert compute_composite_tier(["Elite", "High", "Elite", "High"], rework_tier="Low") == "Medium"

    idle = RepoDORAMetrics(org="acme", repo="docs-only")
    assert idle.get_tiers(90) == ("N/A", "N/A", "N/A", "N/A", "N/A")
    assert idle.change_failure_rate_pct is None
    assert idle.rework_rate_pct is None

    now = datetime.now(timezone.utc)
    elite_deployments = [
        DeploymentRecord(
            event_type="Deployment",
            event_id=str(i),
            name="prod-api",
            environment="production",
            status="SUCCESS",
            ref="main",
            created_at=now - timedelta(hours=i * 12),
            lead_time_hours=4.0,
        )
        for i in range(1, 16)
    ]
    elite_repo = RepoDORAMetrics(org="acme", repo="payments-api", deployments=elite_deployments)

    repo_rows, org_rows, _, _ = build_dora_rows([elite_repo, idle], ["acme"], window_days=7)
    assert repo_rows[1][1] == "docs-only"
    assert repo_rows[1][2] == "N/A"
    assert repo_rows[1][8] == "N/A"
    assert repo_rows[1][10] == "N/A"
    assert repo_rows[1][17] == "N/A"

    assert org_rows[0][1] == 2
    assert org_rows[0][2] == 1
    assert org_rows[0][3] == "Elite"
    assert org_rows[0][5] == 15.0
    assert org_rows[0][15] == 1


def test_workflow_filtering_ignores_ci_pr_and_non_prod_branches():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)

    def _ts(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    runs_payload = [
        {
            "id": 101,
            "name": "CI/CD",
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "head_branch": "main",
            "conclusion": "failure",
            "created_at": _ts(now - timedelta(hours=5)),
            "updated_at": _ts(now - timedelta(hours=5)),
            "head_sha": "sha101",
            "head_commit": {"message": "deploy documentation fix", "timestamp": _ts(now - timedelta(hours=6))},
        },
        {
            "id": 102,
            "name": "Deploy Production",
            "path": ".github/workflows/deploy-prod.yml",
            "event": "pull_request",
            "head_branch": "feature/test",
            "conclusion": "failure",
            "created_at": _ts(now - timedelta(hours=4)),
            "updated_at": _ts(now - timedelta(hours=4)),
            "head_sha": "sha102",
            "head_commit": {"message": "wip pr check", "timestamp": _ts(now - timedelta(hours=5))},
        },
        {
            "id": 103,
            "name": "Deploy Staging",
            "path": ".github/workflows/deploy-staging.yml",
            "event": "push",
            "head_branch": "main",
            "conclusion": "success",
            "created_at": _ts(now - timedelta(hours=3)),
            "updated_at": _ts(now - timedelta(hours=3)),
            "head_sha": "sha103",
            "head_commit": {"message": "staging deploy", "timestamp": _ts(now - timedelta(hours=4))},
        },
        {
            "id": 104,
            "name": "Deploy Production",
            "path": ".github/workflows/deploy-prod.yml",
            "event": "push",
            "head_branch": "main",
            "conclusion": "success",
            "created_at": _ts(now - timedelta(hours=1)),
            "updated_at": _ts(now - timedelta(hours=1)),
            "head_sha": "sha104",
            "head_commit": {"message": "hotfix: restore webhook signature", "timestamp": _ts(now - timedelta(hours=2))},
        },
    ]

    mock_client = MagicMock()
    mock_client.paginate_enveloped.return_value = runs_payload

    prod_runs = collect_deploy_workflow_runs(
        mock_client, "acme", "api", since, now, default_branch="main", include_non_prod=False
    )
    assert len(prod_runs) == 1
    assert prod_runs[0].event_id == "104"
    assert prod_runs[0].is_rework is True

    all_env_runs = collect_deploy_workflow_runs(
        mock_client, "acme", "api", since, now, default_branch="main", include_non_prod=True
    )
    assert [r.event_id for r in all_env_runs] == ["103", "104"]


def test_profile_repo_dora_prod_env_filter_and_commit_compare_lead_time():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=30)

    def _ts(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    graphql_nodes = [
        {
            "id": "gql-stg",
            "environment": "staging",
            "state": "ACTIVE",
            "ref": {"name": "main"},
            "commitOid": "sha0",
            "createdAt": _ts(now - timedelta(hours=60)),
            "updatedAt": _ts(now - timedelta(hours=60)),
            "commit": {"authoredDate": _ts(now - timedelta(hours=62)), "messageHeadline": "stg"},
            "associatedPullRequests": {"nodes": []},
            "latestStatus": {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=60)), "updatedAt": _ts(now - timedelta(hours=60))},
        },
        {
            "id": "gql-p1",
            "environment": "production",
            "state": "ACTIVE",
            "ref": {"name": "main"},
            "commitOid": "sha1",
            "createdAt": _ts(now - timedelta(hours=48)),
            "updatedAt": _ts(now - timedelta(hours=48)),
            "commit": {"authoredDate": _ts(now - timedelta(hours=52)), "messageHeadline": "feat: v1"},
            "associatedPullRequests": {"nodes": []},
            "latestStatus": {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=48)), "updatedAt": _ts(now - timedelta(hours=48))},
        },
        {
            "id": "gql-p2",
            "environment": "production",
            "state": "ACTIVE",
            "ref": {"name": "main"},
            "commitOid": "sha2",
            "createdAt": _ts(now - timedelta(hours=10)),
            "updatedAt": _ts(now - timedelta(hours=10)),
            "commit": {"authoredDate": _ts(now - timedelta(hours=14)), "messageHeadline": "feat: v2"},
            "associatedPullRequests": {"nodes": []},
            "latestStatus": {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=10)), "updatedAt": _ts(now - timedelta(hours=10))},
        },
    ]

    mock_client = MagicMock()
    mock_client.graphql.return_value = {
        "data": {
            "repository": {
                "deployments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": graphql_nodes,
                }
            }
        }
    }

    def _fake_paginate(org, endpoint, params=None):
        return []

    def _fake_paginate_enveloped(org, endpoint, key, params=None):
        return []

    def _fake_get(org, endpoint, params=None):
        if endpoint == "/repos/acme/service/compare/sha1...sha2":
            return {
                "commits": [
                    {"commit": {"author": {"date": _ts(now - timedelta(hours=18))}, "message": "feat: part 1"}},
                    {"commit": {"author": {"date": _ts(now - timedelta(hours=14))}, "message": "feat: part 2"}},
                ]
            }
        return []

    mock_client.paginate.side_effect = _fake_paginate
    mock_client.paginate_enveloped.side_effect = _fake_paginate_enveloped
    mock_client.get.side_effect = _fake_get

    metrics = profile_repo_dora(
        mock_client,
        "acme",
        "service",
        since,
        now,
        {"default_branch": "main", "pushed_at": _ts(now)},
        cfg=DORAConfig(lookback_days=30),
    )

    assert metrics.total_deploys == 2
    assert all(d.environment == "production" for d in metrics.deployments)
    assert metrics.deployments[0].lead_time_hours == 4.0
    assert metrics.deployments[0].lead_time_source == "commit_timestamp"
    assert metrics.deployments[1].lead_time_hours == 6.0
    assert metrics.deployments[1].lead_time_source == "commit_to_deploy"
    assert metrics.median_lead_time_hours == 5.0
    assert metrics.get_tiers(30)[2] == "Elite"


def test_stream_scoped_mttr_open_incidents_and_native_issue_pr_signals():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    events = [
        DeploymentRecord(
            event_type="Deployment",
            event_id="api-1",
            name="Deploy API",
            environment="production",
            status="FAILURE",
            ref="main",
            created_at=now - timedelta(hours=10),
            stream_key="deploy:deploy api:production",
        ),
        DeploymentRecord(
            event_type="Deployment",
            event_id="web-1",
            name="Deploy Web",
            environment="production",
            status="SUCCESS",
            ref="main",
            created_at=now - timedelta(hours=9),
            stream_key="deploy:deploy web:production",
        ),
        DeploymentRecord(
            event_type="Deployment",
            event_id="api-2",
            name="Deploy API",
            environment="production",
            status="SUCCESS",
            ref="main",
            created_at=now - timedelta(hours=6),
            commit_sha="sha-api-2",
            stream_key="deploy:deploy api:production",
        ),
        DeploymentRecord(
            event_type="Deployment",
            event_id="web-2",
            name="Deploy Web",
            environment="production",
            status="FAILURE",
            ref="main",
            created_at=now - timedelta(hours=2),
            stream_key="deploy:deploy web:production",
        ),
    ]

    incidents = calculate_recovery_incidents(events)
    assert len(incidents) == 2
    resolved = [i for i in incidents if i.status == "Resolved"]
    open_incs = [i for i in incidents if i.status == "Open"]
    assert len(resolved) == 1
    assert resolved[0].failed_event == "Deployment api-1"
    assert resolved[0].restored_event == "Deployment api-2"
    assert resolved[0].mttr_hours == 4.0
    assert len(open_incs) == 1
    assert open_incs[0].failed_event == "Deployment web-2"
    assert open_incs[0].mttr_hours is None

    metrics = RepoDORAMetrics(org="acme", repo="platform", deployments=events, incidents=incidents)
    rework_prs = [
        {"number": 44, "title": "hotfix: fix api session", "merge_commit_sha": "sha-api-2", "merged_at": now - timedelta(hours=7)},
    ]
    incident_issues = [
        {
            "number": 88,
            "title": "API outage post-web-1",
            "created_at": now - timedelta(hours=8),
            "closed_at": now - timedelta(hours=5),
        }
    ]
    _apply_native_incident_and_rework_signals(metrics, rework_prs, incident_issues)
    # Failure on api-1 (T-10h) overlaps rework PR and issue within 48h window
    assert metrics.deployments[2].is_rework is True
    assert metrics.rework_deploys == 1
    assert metrics.unlinked_prod_incidents == 0
    assert metrics.open_incidents_count == 1
    assert any(inc.source == "GitHub Incident Issue" and inc.mttr_hours == 3.0 for inc in metrics.incidents)


def test_org_median_df_dedup_permission_gap_and_workbook(tmp_path: Path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    rel = DeploymentRecord(
        event_type="Release",
        event_id="v2.0.0",
        name="Release v2.0.0",
        environment="production",
        status="SUCCESS",
        ref="v2.0.0",
        created_at=now - timedelta(hours=2),
    )
    wf_dup = DeploymentRecord(
        event_type="Workflow Run",
        event_id="999",
        name="Release Pipeline",
        environment="production",
        status="SUCCESS",
        ref="v2.0.0",
        created_at=now - timedelta(hours=1, minutes=50),
    )
    deduped = _deduplicate_release_and_workflow_events([rel], [wf_dup])
    assert len(deduped) == 1
    assert deduped[0].event_type == "Release"

    mock_client = MagicMock()
    mock_client.graphql.return_value = {}
    resp_403 = requests.Response()
    resp_403.status_code = 403
    mock_client.paginate.side_effect = requests.HTTPError(response=resp_403)
    dep_status = {}
    deps = collect_repo_deployments(mock_client, "acme", "locked-repo", now - timedelta(days=7), now, status_out=dep_status)
    assert deps == []
    assert dep_status.get("deployments_forbidden") is True

    r_fast = RepoDORAMetrics(
        org="acme",
        repo="fast",
        deployments=[
            DeploymentRecord("Deployment", str(i), "prod", "production", "SUCCESS", "main", now - timedelta(hours=i * 6), lead_time_hours=2.0)
            for i in range(14)
        ],
    )
    r_slow = RepoDORAMetrics(
        org="acme",
        repo="slow",
        deployments=[
            DeploymentRecord("Deployment", "s1", "prod", "production", "FAILURE", "main", now - timedelta(days=3), lead_time_hours=10.0),
            DeploymentRecord("Deployment", "s2", "prod", "production", "SUCCESS", "main", now - timedelta(days=2), lead_time_hours=10.0, is_rework=True),
        ],
        incidents=[
            RecoveryIncident("Deployment s1", now - timedelta(days=3), "Pending Restoration", None, None, status="Open", source="Deployment Stream", environment="production")
        ],
    )
    r_gap = RepoDORAMetrics(org="acme", repo="locked-repo", signal_mode="Permission Gap (deployments:read)", deployments_api_forbidden=True)

    repo_rows, org_rows, dep_rows, rec_rows = build_dora_rows([r_fast, r_slow, r_gap], ["acme"], window_days=7)
    assert org_rows[0][2] == 2
    assert org_rows[0][4] == 16
    assert org_rows[0][5] == 7.5
    assert org_rows[0][15] == 1
    assert org_rows[0][16] == 1

    fleet = compute_fleet_dora_summary(repo_rows, lookback_days=7)
    assert fleet["active_repos"] == 2
    assert fleet["inactive_repos"] == 1
    assert fleet["permission_gap_repos"] == 1
    assert fleet["open_incidents"] == 1
    assert fleet["total_df_weekly"] == 15.0

    out_file = tmp_path / "dora_compliance.xlsx"
    cfg = DORAConfig(output_dir=str(tmp_path), lookback_days=7)
    write_dora_workbook(
        path=out_file,
        cfg=cfg,
        since=now - timedelta(days=7),
        end=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=dep_rows,
        recovery_rows=rec_rows,
        failures=[],
        execution_duration=1.5,
    )
    print_dora_scorecard(repo_rows, org_rows, dep_rows, rec_rows, out_file, 1.5, 7)

    wb = load_workbook(out_file)
    summary_ws = wb["Executive Summary"]
    summary_text = "\n".join(
        str(cell.value) for row in summary_ws.iter_rows() for cell in row if cell.value is not None
    )
    assert "Native GitHub Signal Methodology & Scope" in summary_text
    assert "Permission Gap Notice" in summary_text
    rec_ws = wb["Recovery & MTTR Ledger"]
    assert rec_ws.cell(2, 8).value == "Open"


def test_run_dora_cli_passes_production_scope_flags(monkeypatch, tmp_path: Path):
    import src.dora.cli as dora_cli

    captured = {}

    def _fake_collect(client, orgs, targets, since, end, cfg, metadata_cache):
        captured["cfg"] = cfg
        return [RepoDORAMetrics(org="acme", repo="svc")]

    monkeypatch.setattr(dora_cli, "resolve_runtime_credentials", lambda: {"app_id": "1", "private_key": "k"})
    monkeypatch.setattr(dora_cli, "GitHubAppAuth", lambda **kwargs: MagicMock())
    monkeypatch.setattr(dora_cli, "ResilientGitHubClient", lambda **kwargs: MagicMock())
    monkeypatch.setattr(dora_cli, "discover_app_organizations", lambda **kwargs: ["acme"])
    monkeypatch.setattr(dora_cli, "discover_app_repositories", lambda **kwargs: ([("acme", "svc")], {}))
    monkeypatch.setattr(dora_cli, "collect_dora_data", _fake_collect)

    rc = dora_cli.run_dora_cli([
        "--org", "acme",
        "--output-dir", str(tmp_path),
        "--lookback-days", "30",
        "--prod-envs", "prod,live-eu",
        "--incident-labels", "sev0,outage",
        "--include-non-prod",
        "--require-ai",
        "no",
    ])
    assert rc == 0
    cfg = captured["cfg"]
    assert cfg.prod_environments == ("prod", "live-eu")
    assert cfg.incident_labels == ("sev0", "outage")
    assert cfg.include_non_prod is True


def test_dora_excludes_in_progress_and_inactive_and_skips_failed_lttc():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=90)

    def _ts(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Superseded INACTIVE with prior SUCCESS stays SUCCESS; empty statuses stay IN_PROGRESS
    superseded_state, superseded_ts = _resolve_deployment_status(
        [
            {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=10))},
            {"state": "INACTIVE", "createdAt": _ts(now - timedelta(hours=1))},
        ]
    )
    assert superseded_state == "SUCCESS"
    assert superseded_ts == now - timedelta(hours=10)

    never_live_state, _ = _resolve_deployment_status(
        [
            {"state": "PENDING", "createdAt": _ts(now - timedelta(hours=5))},
            {"state": "INACTIVE", "createdAt": _ts(now - timedelta(hours=1))},
        ]
    )
    assert never_live_state == "INACTIVE"
    assert _resolve_deployment_status([])[0] == "IN_PROGRESS"

    stale_commit = _ts(now - timedelta(days=70))
    fresh_commit = _ts(now - timedelta(minutes=2))
    nodes = [
        {
            "databaseId": 100 + i,
            "environment": "production",
            "ref": {"name": "main"},
            "commitOid": f"fail-sha-{i}",
            "createdAt": _ts(now - timedelta(hours=20 - i)),
            "commit": {"authoredDate": stale_commit, "committedDate": stale_commit, "messageHeadline": "old"},
            "associatedPullRequests": {"nodes": []},
            "statuses": {"nodes": [{"state": "FAILURE", "createdAt": _ts(now - timedelta(hours=20 - i))}]},
            "latestStatus": {"state": "FAILURE", "createdAt": _ts(now - timedelta(hours=20 - i))},
        }
        for i in range(3)
    ] + [
        {
            "databaseId": 200 + i,
            "environment": "production",
            "ref": {"name": "main"},
            "commitOid": f"pend-sha-{i}",
            "createdAt": _ts(now - timedelta(minutes=1)),
            "commit": {"authoredDate": fresh_commit, "committedDate": fresh_commit, "messageHeadline": "wip"},
            "associatedPullRequests": {"nodes": []},
            "statuses": {"nodes": [{"state": "IN_PROGRESS", "createdAt": _ts(now - timedelta(minutes=1))}]},
            "latestStatus": {"state": "IN_PROGRESS", "createdAt": _ts(now - timedelta(minutes=1))},
        }
        for i in range(4)
    ]

    mock_client = MagicMock()
    mock_client.graphql.return_value = {
        "data": {"repository": {"deployments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}
    }
    mock_client.paginate.return_value = []
    mock_client.paginate_enveloped.return_value = []
    mock_client.get.return_value = []

    metrics = profile_repo_dora(
        mock_client,
        "acme",
        "unstable-app",
        since,
        now,
        {"default_branch": "main", "pushed_at": _ts(now)},
        cfg=DORAConfig(lookback_days=90),
    )

    assert metrics.in_progress_deploys == 4
    assert metrics.total_deploys == 3
    assert metrics.successful_deploys == 0
    assert metrics.failed_deploys == 3
    assert metrics.deployment_frequency_per_week(90) == 0.0
    assert metrics.change_failure_rate_pct == 100.0
    assert metrics.median_lead_time_hours is None
    assert all(d.lead_time_hours is None for d in metrics.deployments)
    overall, df_t, lttc_t, cfr_t, _ = metrics.get_tiers(90)
    assert lttc_t == "N/A"
    assert cfr_t == "Low"
    assert df_t == "Low"
    assert overall == "Low"


def test_dora_pre_window_baseline_and_same_sha_redeploy():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=30)

    def _ts(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Oldest pre-window node provides baseline_refs; Node 2 re-runs same sha-new1 48h later (no_code_change)
    nodes = [
        {
            "databaseId": 3,
            "environment": "production",
            "ref": {"name": "main"},
            "commitOid": "sha-new1",
            "createdAt": _ts(now - timedelta(hours=2)),
            "commit": {"authoredDate": _ts(now - timedelta(hours=70)), "committedDate": _ts(now - timedelta(hours=50))},
            "associatedPullRequests": {"nodes": []},
            "statuses": {"nodes": [{"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=2))}]},
            "latestStatus": {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=2))},
        },
        {
            "databaseId": 2,
            "environment": "production",
            "ref": {"name": "main"},
            "commitOid": "sha-new1",
            "createdAt": _ts(now - timedelta(hours=50)),
            "commit": {"authoredDate": _ts(now - timedelta(hours=70)), "committedDate": _ts(now - timedelta(hours=54))},
            "associatedPullRequests": {"nodes": []},
            "statuses": {"nodes": [{"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=50))}]},
            "latestStatus": {"state": "SUCCESS", "createdAt": _ts(now - timedelta(hours=50))},
        },
        {
            "databaseId": 1,
            "environment": "production",
            "ref": {"name": "main"},
            "commitOid": "sha-prewindow",
            "createdAt": _ts(since - timedelta(days=2)),
            "commit": {"authoredDate": _ts(since - timedelta(days=3))},
            "associatedPullRequests": {"nodes": []},
            "statuses": {
                "nodes": [
                    {"state": "SUCCESS", "createdAt": _ts(since - timedelta(days=2))},
                    {"state": "INACTIVE", "createdAt": _ts(now - timedelta(hours=50))},
                ]
            },
            "latestStatus": {"state": "INACTIVE", "createdAt": _ts(now - timedelta(hours=50))},
        },
    ]

    mock_client = MagicMock()
    mock_client.graphql.return_value = {
        "data": {"repository": {"deployments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}
    }
    mock_client.paginate.return_value = []
    mock_client.paginate_enveloped.return_value = []

    def _fake_get(org, endpoint, params=None):
        if endpoint == "/repos/acme/app/compare/sha-prewindow...sha-new1":
            return {"commits": [{"commit": {"author": {"date": _ts(now - timedelta(hours=54))}, "message": "feat: new"}}]}
        return []

    mock_client.get.side_effect = _fake_get

    metrics = profile_repo_dora(
        mock_client,
        "acme",
        "app",
        since,
        now,
        {"default_branch": "main", "pushed_at": _ts(now)},
        cfg=DORAConfig(lookback_days=30),
    )

    assert len(metrics.deployments) == 2
    assert metrics.deployments[0].lead_time_hours == 4.0
    assert metrics.deployments[0].lead_time_source == "commit_to_deploy"
    assert metrics.deployments[1].lead_time_hours is None
    assert metrics.deployments[1].lead_time_source == "no_code_change"
    assert metrics.median_lead_time_hours == 4.0

    zero_diff_client = MagicMock()
    zero_diff_client.get.return_value = {"status": "identical", "ahead_by": 0, "commits": []}
    ev_identical = DeploymentRecord("Release", "v1.0.3", "v1.0.3", "production", "SUCCESS", "main", now, commit_sha="v1.0.3")
    _enrich_lead_times_from_commits(
        zero_diff_client, "acme", "app", [ev_identical], {}, ("hotfix",), baseline_refs={"release:production": "v1.0.2"}
    )
    assert ev_identical.lead_time_hours is None
    assert ev_identical.lead_time_source == "no_code_change"


def test_fleet_and_single_org_summaries_match_and_track_incomplete_deploys(tmp_path: Path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    repo_small = RepoDORAMetrics(
        org="single-org",
        repo="small-svc",
        deployments=[
            DeploymentRecord("Deployment", "1", "prod", "production", "SUCCESS", "main", now - timedelta(hours=5), lead_time_hours=0.1)
        ],
    )
    repo_heavy = RepoDORAMetrics(
        org="single-org",
        repo="heavy-svc",
        deployments=[
            DeploymentRecord("Deployment", f"h{i}", "prod", "production", "SUCCESS", "main", now - timedelta(hours=i + 1), lead_time_hours=6.0)
            for i in range(9)
        ] + [
            DeploymentRecord("Deployment", "hf", "prod", "production", "FAILURE", "main", now - timedelta(hours=20)),
            DeploymentRecord("Deployment", "hp", "prod", "production", "IN_PROGRESS", "main", now),
        ],
        incidents=[
            RecoveryIncident("Deployment hf", now - timedelta(hours=20), "Deployment h1", now - timedelta(hours=18), 2.0, "Resolved", "Deployment Stream", "production")
        ],
    )

    repo_rows, org_rows, dep_rows, rec_rows = build_dora_rows(
        [repo_small, repo_heavy],
        orgs=["single-org"],
        window_days=30,
    )
    fleet = compute_fleet_dora_summary(
        repo_rows,
        lookback_days=30,
        deployment_rows=dep_rows,
        recovery_rows=rec_rows,
    )

    assert fleet["median_lttc_hours"] == org_rows[0][7] == 6.0
    assert fleet["median_mttr_hours"] == org_rows[0][11] == 2.0
    assert fleet["total_deploys"] == 11
    assert fleet["total_successful"] == 10
    assert fleet["total_failed_deploys"] == 1
    assert fleet["incomplete_deploys"] == 1
    assert fleet["total_df_weekly"] == round(10 / (30.0 / 7.0), 2)

    out_file = tmp_path / "dora_unified.xlsx"
    write_dora_workbook(
        path=out_file,
        cfg=DORAConfig(output_dir=str(tmp_path), lookback_days=30),
        since=now - timedelta(days=30),
        end=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=dep_rows,
        recovery_rows=rec_rows,
        failures=[],
        execution_duration=1.2,
    )
    wb = load_workbook(out_file)
    exec_text = "\n".join(str(c.value) for row in wb["Executive Summary"].iter_rows() for c in row if c.value is not None)
    assert "Pending / Incomplete Deployments (Excluded)" in exec_text


def test_composite_tier_enterprise_guardrails():
    assert (
        compute_composite_tier(
            ["Elite", "Elite", "Elite", "Low"],
            cfr_tier="Elite",
            df_tier="Elite",
            lttc_tier="Elite",
            mttr_tier="Low",
        )
        == "Medium"
    )
    assert (
        compute_composite_tier(
            ["Elite", "Elite", "Elite", "N/A"],
            cfr_tier="Elite",
            df_tier="Elite",
            lttc_tier="Elite",
            mttr_tier="N/A",
            has_open_incidents=True,
        )
        == "Medium"
    )
    assert (
        compute_composite_tier(
            ["Elite", "Elite", "Elite", "Elite"],
            cfr_tier="Elite",
            df_tier="Elite",
            lttc_tier="Elite",
            mttr_tier="Elite",
            code_change_deploys=0,
        )
        == "Medium"
    )
    assert (
        compute_composite_tier(
            ["Elite", "Elite", "Elite", "Elite"],
            cfr_tier="Elite",
            df_tier="Elite",
            lttc_tier="Elite",
            mttr_tier="Elite",
            successful_deploys=2,
            code_change_deploys=2,
            window_days=90,
        )
        == "Medium"
    )
    assert (
        compute_composite_tier(
            ["High", "Elite", "Elite", "Elite"],
            cfr_tier="Elite",
            df_tier="High",
            lttc_tier="Elite",
            mttr_tier="Elite",
            successful_deploys=2,
            code_change_deploys=2,
            window_days=7,
        )
        == "Elite"
    )


def test_no_op_rebuilds_retry_collapse_and_stalled_deploys(tmp_path: Path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    deploys_a = [
        DeploymentRecord("Deployment", "c1", "prod", "production", "SUCCESS", "main", now - timedelta(days=20), lead_time_hours=4.0, commit_sha="sha-a1", lead_time_source="commit_to_deploy", completed_at=now - timedelta(days=20)),
        DeploymentRecord("Deployment", "c2", "prod", "production", "SUCCESS", "main", now - timedelta(days=15), lead_time_hours=6.0, commit_sha="sha-a2", lead_time_source="pr_to_deploy", completed_at=now - timedelta(days=15)),
    ] + [
        DeploymentRecord("Deployment", f"noop-{i}", "prod", "production", "SUCCESS", "main", now - timedelta(days=14 - i), lead_time_hours=None, commit_sha="sha-a2", lead_time_source="no_code_change", completed_at=now - timedelta(days=14 - i))
        for i in range(8)
    ] + [
        DeploymentRecord("Deployment", f"retry-{j}", "prod", "production", "FAILURE", "main", now - timedelta(hours=10 - j), commit_sha="bad-sha-1", completed_at=now - timedelta(hours=10 - j))
        for j in range(3)
    ] + [
        DeploymentRecord("Deployment", "fix-1", "prod", "production", "SUCCESS", "main", now - timedelta(hours=5), lead_time_hours=2.0, commit_sha="fix-sha-2", is_rework=True, lead_time_source="commit_to_deploy", completed_at=now - timedelta(hours=5)),
    ]
    repo_a = RepoDORAMetrics(
        org="acme",
        repo="svc-a",
        deployments=deploys_a,
        incidents=[RecoveryIncident("Deployment retry-0", now - timedelta(hours=10), "Deployment fix-1", now - timedelta(hours=5), 5.0, "Resolved", "Deployment Stream", "production")],
    )

    assert repo_a.total_deploys == 14
    assert repo_a.successful_deploys == 11
    assert repo_a.code_change_deploys == 3
    assert repo_a.no_op_deploys == 8
    assert repo_a.redundant_failure_retries == 2
    assert repo_a.cfr_denominator == 12
    assert repo_a.effective_failed_changes == 1
    assert repo_a.change_failure_rate_pct == round((1 / 12) * 100, 2)
    assert repo_a.deployment_frequency_per_week(90) == round(3 / (90.0 / 7.0), 2)
    assert repo_a.lttc_coverage_pct == 100.0

    distinct_fail_repo = RepoDORAMetrics(
        org="acme",
        repo="distinct-fails",
        deployments=[
            DeploymentRecord("Deployment", "d1", "prod", "production", "FAILURE", "main", now - timedelta(hours=3), commit_sha="s1"),
            DeploymentRecord("Deployment", "d2", "prod", "production", "FAILURE", "main", now - timedelta(hours=2), commit_sha="s2"),
        ],
    )
    assert distinct_fail_repo.redundant_failure_retries == 0
    assert distinct_fail_repo.cfr_denominator == 2
    assert distinct_fail_repo.effective_failed_changes == 2

    repo_b = RepoDORAMetrics(
        org="acme",
        repo="cron-only",
        deployments=[
            DeploymentRecord("Deployment", f"n{k}", "prod", "production", "SUCCESS", "main", now - timedelta(days=k + 2), commit_sha="same", lead_time_source="no_code_change", completed_at=now - timedelta(days=k + 2))
            for k in range(11)
        ] + [
            DeploymentRecord("Deployment", "stalled-1", "prod", "production", "IN_PROGRESS", "main", now - timedelta(hours=40), commit_sha="pend")
        ],
        stalled_deploys_count=1,
    )
    assert repo_b.code_change_deploys == 0
    assert repo_b.deployment_frequency_per_week(90) == 0.0
    assert repo_b.get_tiers(90)[0] == "Medium"

    repo_rows, org_rows, dep_rows, rec_rows = build_dora_rows([repo_a, repo_b], ["acme"], window_days=90)
    assert repo_rows[0][21] == 3
    assert repo_rows[0][22] == 8
    assert repo_rows[0][23] == 100.0
    assert repo_rows[0][25] == 1
    assert repo_rows[0][27] == 2
    assert "Only same-SHA/zero-diff rebuilds" in repo_rows[1][14]
    assert dep_rows[-1][12] == "Pending"

    fleet = compute_fleet_dora_summary(repo_rows, 90, deployment_rows=dep_rows, recovery_rows=rec_rows)
    assert fleet["code_change_deploys"] == 3
    assert fleet["no_op_deploys"] == 19
    assert fleet["collapsed_retries"] == 2
    assert fleet["stalled_deploys"] == 1
    assert fleet["cfr_denominator"] == 23
    assert fleet["effective_failed"] == 1
    assert fleet["cfr_overall"] == round((1 / 23) * 100, 2)
    assert fleet["pr_lttc_count"] == 1
    assert fleet["commit_lttc_count"] == 2
    assert fleet["lttc_coverage_pct"] == 100.0

    out_xlsx = tmp_path / "enterprise_dora.xlsx"
    write_dora_workbook(
        path=out_xlsx,
        cfg=DORAConfig(output_dir=str(tmp_path), lookback_days=90),
        since=now - timedelta(days=90),
        end=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=dep_rows,
        recovery_rows=rec_rows,
        failures=[("acme/cron-only", "HTTP 403 on /deployments API")],
        execution_duration=1.1,
    )
    wb = load_workbook(out_xlsx)
    exec_text = "\n".join(str(c.value) for row in wb["Executive Summary"].iter_rows() for c in row if c.value is not None)
    for label in (
        "Code-Change Deployments (Unique Changes)",
        "No-Op / Same-SHA Redeploys (Excluded DF)",
        "Collapsed Workflow Failure Retries (CFR)",
        "Stalled Deployments (>24h IN_PROGRESS)",
        "LTTC Signal Coverage (%)",
        "Collection Errors & Permission Gaps",
    ):
        assert label in exec_text


def test_squash_merge_single_commit_pr_lead_time_and_url_quoting():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    def _ts(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    ev1 = DeploymentRecord("Release", "release/v1.0", "v1.0", "production", "SUCCESS", "main", now - timedelta(hours=24), commit_sha="release/v1.0")
    ev2 = DeploymentRecord("Release", "release/v1.1", "v1.1", "production", "SUCCESS", "main", now, commit_sha="release/v1.1")

    seen_endpoints: list[str] = []

    def _fake_get(org, endpoint, params=None):
        seen_endpoints.append(endpoint)
        if endpoint == "/repos/acme/squash-app/compare/release%2Fv1.0...release%2Fv1.1":
            return {
                "commits": [
                    {"commit": {"author": {"date": _ts(now - timedelta(minutes=6))}, "message": "feat: big PR (#42)"}}
                ]
            }
        if endpoint == "/repos/acme/squash-app/commits/release%2Fv1.1/pulls":
            return [{"created_at": _ts(now - timedelta(hours=18)), "title": "feat: big PR", "head": {"ref": "feat/big"}}]
        return []

    mock_client = MagicMock()
    mock_client.get.side_effect = _fake_get

    _enrich_lead_times_from_commits(
        mock_client, "acme", "squash-app", [ev1, ev2], {}, ("hotfix", "revert", "rollback")
    )

    assert "/repos/acme/squash-app/compare/release%2Fv1.0...release%2Fv1.1" in seen_endpoints
    assert "/repos/acme/squash-app/commits/release%2Fv1.1/pulls" in seen_endpoints
    assert ev2.lead_time_hours == 18.0
    assert ev2.lead_time_source == "pr_to_deploy"


def test_run_dora_cli_forwards_permission_and_api_failures(monkeypatch, tmp_path: Path):
    from src.dora.cli import run_dora_cli

    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    captured = {}

    monkeypatch.setattr("src.dora.cli.resolve_runtime_credentials", lambda: {"app_id": "123", "private_key": "pem"})
    monkeypatch.setattr("src.dora.cli.GitHubAppAuth", lambda **kw: MagicMock())
    monkeypatch.setattr("src.dora.cli.ResilientGitHubClient", lambda **kw: MagicMock())
    monkeypatch.setattr("src.dora.cli.discover_app_organizations", lambda **kw: ["acme"])
    monkeypatch.setattr("src.dora.cli.discover_app_repositories", lambda **kw: ([("acme", "locked")], {}))

    sample_metric = RepoDORAMetrics(
        org="acme",
        repo="locked",
        signal_mode="Permission Gap (deployments:read)",
        deployments_api_forbidden=True,
        errors=["Issues API forbidden (HTTP 403) on acme/locked"],
        deployments=[
            DeploymentRecord("Workflow Run", "1", "Deploy", "production", "IN_PROGRESS", "main", now - timedelta(hours=36))
        ],
        stalled_deploys_count=1,
    )
    monkeypatch.setattr("src.dora.cli.collect_dora_data", lambda *a, **kw: [sample_metric])

    orig_write = write_dora_workbook

    def _spy_write(**kwargs):
        captured["failures"] = kwargs["failures"]
        captured["path"] = kwargs["path"]
        return orig_write(**kwargs)

    monkeypatch.setattr("src.dora.cli.write_dora_workbook", _spy_write)
    rc = run_dora_cli(["--output-dir", str(tmp_path), "--lookback-days", "30", "--require-ai", "no"])
    assert rc == 0
    assert len(captured["failures"]) == 2
    assert any("deployments: read" in reason for _, reason in captured["failures"])
    assert any("Issues API forbidden" in reason for _, reason in captured["failures"])
    wb = load_workbook(captured["path"])
    exec_text = "\n".join(str(c.value) for row in wb["Executive Summary"].iter_rows() for c in row if c.value is not None)
    assert "Collection Errors & Permission Gaps" in exec_text
    assert "Issues API forbidden (HTTP 403) on acme/locked" in exec_text


def test_build_dora_rows_passes_grounded_ai_context_and_stalled_suffix():
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    seen_kwargs = []

    class _FakeAI:
        def diagnose_dora_flow(self, **kwargs):
            seen_kwargs.append(kwargs)
            return "Reduce direct-push failures using required CI status checks."

    m = RepoDORAMetrics(
        org="acme",
        repo="unstable",
        signal_mode="GitHub Deployments",
        deployments=[
            DeploymentRecord("Deployment", "f1", "prod", "production", "FAILURE", "main", now - timedelta(days=2), commit_sha="s1"),
            DeploymentRecord("Deployment", "f2", "prod", "production", "FAILURE", "main", now - timedelta(days=1), commit_sha="s1"),
            DeploymentRecord("Deployment", "p1", "prod", "production", "IN_PROGRESS", "main", now - timedelta(hours=36), commit_sha="s2"),
        ],
        stalled_deploys_count=1,
    )

    repo_rows, _, _, _ = build_dora_rows([m], ["acme"], window_days=90, ai_engine=_FakeAI())
    assert len(seen_kwargs) == 1
    assert seen_kwargs[0]["signal_mode"] == "GitHub Deployments"
    assert seen_kwargs[0]["no_op_deploys"] == 0
    assert seen_kwargs[0]["stalled_deploys"] == 1
    assert seen_kwargs[0]["collapsed_retries"] == 1
    assert seen_kwargs[0]["total_deploys"] == 2
    assert "1 stalled IN_PROGRESS deploy >24h." in repo_rows[0][14]


def test_incident_issue_resolved_within_lookback_window():
    """Verify incident issues created before lookback window but resolved within lookback are counted."""
    from src.dora.collector import _collect_incident_issues
    from src.dora.models import DORAConfig

    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=30)  # 2026-10-11

    mock_client = MagicMock()
    # Issue #101 created before since (2026-09-15), closed during window (2026-10-25)
    # Issue #102 created and closed within window
    mock_client.paginate.return_value = [
        {
            "number": 101,
            "title": "Outage on payment gateway",
            "created_at": "2026-09-15T10:00:00Z",
            "closed_at": "2026-10-25T14:00:00Z",
            "state": "closed",
            "labels": [{"name": "incident"}],
        },
        {
            "number": 102,
            "title": "Database connection spike",
            "created_at": "2026-10-15T10:00:00Z",
            "closed_at": "2026-10-15T12:00:00Z",
            "state": "closed",
            "labels": [{"name": "bug"}],
        },
    ]

    cfg = DORAConfig(incident_labels=("incident", "bug"))
    incidents = _collect_incident_issues(mock_client, "acme", "pay-svc", since, now, cfg)
    assert len(incidents) == 2
    assert incidents[0]["number"] == 101
    assert incidents[0]["closed_at"] == datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc)
    assert incidents[1]["number"] == 102




