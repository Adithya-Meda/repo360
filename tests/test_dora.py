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
    # Deployment Frequency
    assert get_df_tier(10.0) == "Elite"
    assert get_df_tier(3.0) == "High"
    assert get_df_tier(0.5) == "Medium"
    assert get_df_tier(0.1) == "Low"

    # Lead Time for Changes
    assert get_lttc_tier(0.5) == "Elite"
    assert get_lttc_tier(48.0) == "High"
    assert get_lttc_tier(300.0) == "Medium"
    assert get_lttc_tier(800.0) == "Low"
    assert get_lttc_tier(None) == "N/A"

    # Change Failure Rate
    assert get_cfr_tier(2.0) == "Elite"
    assert get_cfr_tier(10.0) == "High"
    assert get_cfr_tier(25.0) == "Medium"
    assert get_cfr_tier(40.0) == "Low"

    # MTTR
    assert get_mttr_tier(0.5) == "Elite"
    assert get_mttr_tier(12.0) == "High"
    assert get_mttr_tier(72.0) == "Medium"
    assert get_mttr_tier(200.0) == "Low"
    assert get_mttr_tier(None) == "N/A"

    # Composite Tier
    assert compute_composite_tier(["Elite", "Elite", "High", "Elite"]) == "Elite"
    assert compute_composite_tier(["High", "High", "Medium", "High"]) == "High"
    assert compute_composite_tier(["Low", "Low", "Medium", "Low"]) == "Low"


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

    # Add 10 deployments: 9 success, 1 failure
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
    assert m.deployment_frequency_per_week(70) == 1.0
    assert m.median_lead_time_hours is not None
    assert m.median_mttr_hours == 48.0

    overall, df_tier, lttc_tier, cfr_tier, mttr_tier = m.get_tiers(70)
    assert df_tier == "High"
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
    assert repo_rows[0][3] == 1  # Total deploys
    assert repo_rows[0][4] == 1  # Successful

    assert len(org_rows) == 1
    assert org_rows[0][0] == "fintech-org"
    assert org_rows[0][2] == 1  # Active deploy repos
    assert org_rows[0][4] == 1  # Total deploys

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

    # Org aggregation excludes inactive docs-only repo from active DF/tiers
    assert org_rows[0][1] == 2  # Repositories Scanned
    assert org_rows[0][2] == 1  # Active Deploy Repos
    assert org_rows[0][3] == "Elite"
    assert org_rows[0][5] == 15.0  # Median active repo /week
    assert org_rows[0][15] == 1  # Inactive Repos


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
    assert metrics.deployments[1].lead_time_hours == 6.0  # median of 8.0h and 4.0h
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
    assert resolved[0].mttr_hours == 4.0  # NOT 1.0h from Deploy Web stream
    assert len(open_incs) == 1
    assert open_incs[0].failed_event == "Deployment web-2"
    assert open_incs[0].mttr_hours is None

    # Link rework PR & overlapping incident issue after web-1 SUCCESS without double-counting CFR
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
    # Failure on api-1 (T-10h) is already within 48h window of rework PR (T-7h) and issue (T-8h), so no double-count
    assert metrics.deployments[2].is_rework is True
    assert metrics.rework_deploys == 1
    assert metrics.unlinked_prod_incidents == 0
    assert metrics.open_incidents_count == 1
    assert any(inc.source == "GitHub Incident Issue" and inc.mttr_hours == 3.0 for inc in metrics.incidents)


def test_org_median_df_dedup_permission_gap_and_workbook(tmp_path: Path):
    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Deduplicate Release & matching workflow run on tag within 30 minutes
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

    # Permission gap 403 detection
    mock_client = MagicMock()
    mock_client.graphql.return_value = {}
    resp_403 = requests.Response()
    resp_403.status_code = 403
    mock_client.paginate.side_effect = requests.HTTPError(response=resp_403)
    dep_status = {}
    deps = collect_repo_deployments(mock_client, "acme", "locked-repo", now - timedelta(days=7), now, status_out=dep_status)
    assert deps == []
    assert dep_status.get("deployments_forbidden") is True

    # Org-level median DF across active repos (14/wk & 2/wk -> median 8.0/wk), ignoring 0-deploy repo
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
    assert org_rows[0][2] == 2  # Active Deploy Repos
    assert org_rows[0][4] == 16  # Total Deployments
    assert org_rows[0][5] == 8.0  # Median of 14.0 and 2.0
    assert org_rows[0][15] == 1  # Inactive Repos
    assert org_rows[0][16] == 1  # Open Incidents

    fleet = compute_fleet_dora_summary(repo_rows, lookback_days=7)
    assert fleet["active_repos"] == 2
    assert fleet["inactive_repos"] == 1
    assert fleet["permission_gap_repos"] == 1
    assert fleet["open_incidents"] == 1

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
        "--no-ai",
    ])
    assert rc == 0
    cfg = captured["cfg"]
    assert cfg.prod_environments == ("prod", "live-eu")
    assert cfg.incident_labels == ("sev0", "outage")
    assert cfg.include_non_prod is True
