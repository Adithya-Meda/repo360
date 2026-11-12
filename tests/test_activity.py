"""Unit and integration tests for Repo360 user activity scraper."""

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from src.activity.models import (
    Activity,
    ActivityConfig,
    fmt_timestamp,
    is_bot,
    parse_ts,
)
from src.activity.collector import (
    build_org_rows,
    build_rows,
    classify,
)
from src.activity.reporter import write_activity_workbook


def test_activity_model_methods():
    act = Activity()
    now = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    act.add("commits", now)
    act.add("prs_opened", now)
    act.add("reviews", now)

    assert act.commits == 1
    assert act.prs_opened == 1
    assert act.reviews == 1
    assert act.total() == 3
    assert len(act.active_days) == 1
    assert act.first_active == now
    assert act.last_active == now

    act2 = Activity()
    later = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    act2.add("commits", later)
    act.merge(act2)

    assert act.commits == 2
    assert act.total() == 4
    assert len(act.active_days) == 2
    assert act.last_active == later


def test_activity_classification():
    cfg = ActivityConfig(active_min_days=8, moderate_min_days=3)
    assert classify(10, cfg) == "Active"
    assert classify(8, cfg) == "Active"
    assert classify(5, cfg) == "Moderate"
    assert classify(3, cfg) == "Moderate"
    assert classify(1, cfg) == "Low"
    assert classify(0, cfg) == "Inactive"


def test_is_bot_detection():
    assert is_bot({"type": "Bot", "login": "some-app"}) is True
    assert is_bot({"type": "User", "login": "app[bot]"}) is True
    assert is_bot({"type": "User", "login": "dependabot"}) is True
    assert is_bot({"type": "User", "login": "alice"}) is False


def test_build_rows_and_org_rows():
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    act = Activity()
    act.add("commits", now)

    results = {("acme-org", "service-a"): {"alice": act}}
    members = {"acme-org": {"alice", "bob"}}
    cfg = ActivityConfig(active_min_days=8, moderate_min_days=3, include_inactive_members=True)

    user_rows, repo_rows = build_rows(
        results=results,
        members=members,
        cfg=cfg,
        end=now,
        teams_by_user={("acme-org", "alice"): {"backend-team"}},
        org_team_complete={"acme-org": True},
    )

    assert len(user_rows) == 2
    alice_row = next(r for r in user_rows if r[1] == "alice")
    bob_row = next(r for r in user_rows if r[1] == "bob")

    assert alice_row[2] == "Low"
    assert alice_row[4] == 1
    assert alice_row[17] == "Yes"
    assert alice_row[18] == "backend-team"
    assert len(alice_row) == 19

    assert bob_row[2] == "Inactive"
    assert bob_row[4] == 0

    assert len(repo_rows) == 1
    assert repo_rows[0][1] == "service-a"
    assert repo_rows[0][2] == "alice"

    org_rows = build_org_rows(user_rows, results)
    assert len(org_rows) == 1
    assert org_rows[0][0] == "acme-org"
    assert org_rows[0][1] == 1
    assert org_rows[0][2] == 1

    multi_org_rows = build_org_rows(user_rows, results, all_orgs=["acme-org", "dormant-org"])
    assert len(multi_org_rows) == 2
    assert multi_org_rows[0][0] == "acme-org"
    assert multi_org_rows[1][0] == "dormant-org"
    assert multi_org_rows[1][1] == 0
    assert multi_org_rows[1][2] == 0


def test_write_activity_workbook_and_csv(tmp_path):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    act = Activity()
    act.add("commits", now)
    cfg = ActivityConfig(output_dir=str(tmp_path))

    user_rows, repo_rows = build_rows(
        results={("acme-org", "service-a"): {"alice": act}},
        members={"acme-org": {"alice"}},
        cfg=cfg,
        end=now,
        teams_by_user={},
        org_team_complete={"acme-org": True},
    )
    org_rows = build_org_rows(user_rows, {("acme-org", "service-a"): {"alice": act}})

    xlsx_path = tmp_path / "activity.xlsx"
    write_activity_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=now,
        end=now,
        user_rows=user_rows,
        repo_rows=repo_rows,
        org_rows=org_rows,
        failures=[],
        repos_scanned=1,
        execution_duration=1.5,
    )

    assert xlsx_path.is_file()
    assert xlsx_path.stat().st_size > 1000

    from openpyxl import load_workbook
    wb = load_workbook(xlsx_path)
    summary = wb["Executive Summary"]
    assert summary["B5"].value == "1.5s"
    assert summary["B5"].alignment.horizontal == "center"

    for r in range(5, 20):
        val = summary.cell(row=r, column=1).value
        if val in ("Commits", "Active Users", "Low Users", "PR Reviews", "Total Duration"):
            assert summary.cell(row=r, column=1).alignment.horizontal == "left"


def test_format_error_permission_mapping():
    from src.activity.collector import _format_error
    import requests

    resp_403 = requests.Response()
    resp_403.status_code = 403
    http_err_403 = requests.exceptions.HTTPError(response=resp_403)

    msg_pulls = _format_error("Pulls", http_err_403)
    assert "403 Forbidden" in msg_pulls
    assert "pull_requests: read" in msg_pulls

    msg_issues = _format_error("Issues", http_err_403)
    assert "403 Forbidden" in msg_issues
    assert "issues: read" in msg_issues

    resp_404 = requests.Response()
    resp_404.status_code = 404
    http_err_404 = requests.exceptions.HTTPError(response=resp_404)
    msg_404 = _format_error("Pulls", http_err_404)
    assert "404 Not Found" in msg_404

    generic_err = RuntimeError("Network timeout")
    msg_generic = _format_error("Commits", generic_err)
    assert "Commits: Network timeout" in msg_generic


def test_build_management_tables_and_sheets(tmp_path):
    from openpyxl import load_workbook
    from src.activity.collector import build_management_tables
    from src.activity.models import (
        MGMT_KPI_HEADERS,
        REPO_INVENTORY_HEADERS,
        TOP_REPOS_HEADERS,
        RepositoryReport,
    )

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    since = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    report_a = RepositoryReport(
        metadata={
            "created_at": "2026-08-01T00:00:00Z",
            "visibility": "private",
            "archived": False,
            "default_branch": "main",
        },
        protected="Yes",
        protected_branches=("main",),
        codeowners="Yes",
        codeowners_path=".github/CODEOWNERS",
        activity_complete=True,
        cohort_opened=10,
        cohort_merged=8,
        review_hours=[2.5, 3.5],
        events=[
            ("alice", "commits", now),
            ("alice", "prs_opened", now),
            ("bob", "reviews", now),
        ],
    )

    report_b = RepositoryReport(
        metadata={
            "created_at": "2025-01-01T00:00:00Z",
            "visibility": "public",
            "archived": False,
            "default_branch": "master",
        },
        protected="No",
        protected_branches=(),
        codeowners="No",
        codeowners_path="Not found",
        activity_complete=True,
        cohort_opened=0,
        cohort_merged=0,
        review_hours=[],
        events=[],
    )

    reports = {
        ("test-org", "repo-active"): report_a,
        ("test-org", "repo-idle"): report_b,
    }
    members = {"test-org": {"alice", "charlie"}}
    org_status = {"test-org": True}

    tables = build_management_tables(
        reports=reports,
        members=members,
        since=since,
        end=now,
        organization_status=org_status,
    )

    assert "Repository Inventory" in tables
    assert "Top Active Repositories" in tables
    assert "Management KPIs" in tables

    inv_headers, inv_rows = tables["Repository Inventory"]
    assert inv_headers == REPO_INVENTORY_HEADERS
    assert len(inv_rows) == 2
    assert inv_rows[0][0] == "test-org"
    assert inv_rows[0][1] == "repo-active"
    assert inv_rows[0][2] == "private"
    assert inv_rows[0][4] == "Yes"
    assert inv_rows[0][7] == "main"
    assert inv_rows[0][8] == "Yes"
    assert inv_rows[0][11] == 3
    assert inv_rows[0][12] == 2
    assert inv_rows[0][14] == 1
    assert inv_rows[0][20] == 80.0
    assert inv_rows[0][21] == 3.0
    assert inv_rows[0][23] == "Complete"

    top_headers, top_rows = tables["Top Active Repositories"]
    assert top_headers == TOP_REPOS_HEADERS
    assert len(top_rows) == 2
    assert top_rows[0][1] == 1
    assert top_rows[0][2] == "repo-active"
    assert top_rows[0][3] == 3

    kpi_headers, kpi_rows = tables["Management KPIs"]
    assert kpi_headers == MGMT_KPI_HEADERS
    categories = {r[1] for r in kpi_rows}
    assert "User Adoption" in categories
    assert "Repository Landscape" in categories
    assert "Developer Activity" in categories
    assert "Collaboration" in categories
    assert "Security & Governance" in categories
    assert "Coverage" in categories

    cfg = ActivityConfig(output_dir=str(tmp_path))
    xlsx_path = tmp_path / "activity_with_management.xlsx"
    write_activity_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=since,
        end=now,
        user_rows=[],
        repo_rows=[],
        org_rows=[],
        failures=[],
        repos_scanned=2,
        management_tables=tables,
    )

    assert xlsx_path.is_file()
    wb = load_workbook(xlsx_path)
    sheet_names = wb.sheetnames
    assert "Executive Summary" in sheet_names
    assert "User Activity" in sheet_names
    assert "Repository Detail" in sheet_names
    assert "Repository Inventory" in sheet_names
    assert "Top Active Repositories" in sheet_names
    assert "Management KPIs" in sheet_names


def test_activity_hardened_dormancy_partial_errors_reviews_and_normalization():
    from datetime import timedelta
    from src.activity.collector import (
        build_management_tables,
        collect_repository,
        fetch_team_access,
    )

    # 2026-10-31 20:00 UTC == 2026-11-01 01:30 IST
    boundary_act = Activity()
    boundary_act.add("commits", datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc))
    assert (2026, 11) in boundary_act.commit_months

    end = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    since_7d = end - timedelta(days=7)
    u_rows, r_rows = build_rows(
        results={("Acme-Org", "svc"): {"Alice": boundary_act}},
        members={"acme-org": {"alice", "bob"}},
        cfg=ActivityConfig(),
        end=end,
        teams_by_user={("acme-org", "ALICE"): {"Platform"}},
        org_team_complete={"acme-org": True},
    )
    assert len(u_rows) == 2
    alice_r = next(r for r in u_rows if r[1].lower() == "alice")
    assert alice_r[17] == "Yes"
    assert alice_r[18] == "Platform"
    o_rows = build_org_rows(u_rows, {("Acme-Org", "svc"): {"Alice": boundary_act}}, all_orgs=["acme-org"])
    assert len(o_rows) == 1

    class _ForbiddenResp:
        status_code = 403

    class _ForbiddenErr(Exception):
        response = _ForbiddenResp()

    class _HardenedClient:
        def __init__(self):
            self.gql_calls = []

        def get(self, org, endpoint, params=None):
            if endpoint.endswith("/rules/branches/main"):
                return [{"type": "pull_request"}]
            if endpoint.endswith("/git/trees/main"):
                return {"tree": []}
            return {}

        def paginate(self, org, endpoint, params=None):
            if endpoint.endswith("/commits"):
                return iter([{
                    "author": {"login": "alice"},
                    "commit": {"author": {"date": "2026-10-05T10:00:00Z"}},  # 15 days ago (>7d since, <30d rolling)
                }])
            if endpoint.endswith("/issues"):
                raise _ForbiddenErr("issues forbidden")
            if endpoint == "/orgs/acme-org/teams":
                return iter([{"slug": "sec"}, {"slug": "infra"}])
            if endpoint == "/orgs/acme-org/teams/sec/members":
                return iter([{"login": "alice"}])
            if endpoint == "/orgs/acme-org/teams/infra/members":
                raise RuntimeError("team forbidden")
            return iter([])

        def graphql(self, org, query, variables=None):
            self.gql_calls.append(variables or {})
            if variables and "revCursor" in variables:
                return {
                    "data": {
                        "node": {
                            "reviews": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"author": {"login": "carol"}, "submittedAt": "2026-10-18T10:00:00Z", "state": "APPROVED"}],
                            }
                        }
                    }
                }
            if variables and variables.get("cursor") is None:
                return {
                    "data": {
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{
                                    "id": "PR_123",
                                    "number": 1,
                                    "createdAt": "2026-10-16T10:00:00Z",
                                    "mergedAt": None,
                                    "updatedAt": "2026-10-18T10:00:00Z",
                                    "author": {"login": "alice"},
                                    "reviews": {
                                        "pageInfo": {"hasNextPage": True, "endCursor": "rev_c1"},
                                        "nodes": [{"author": {"login": "bob"}, "submittedAt": "2026-10-17T10:00:00Z", "state": "COMMENTED"}],
                                    },
                                }],
                            }
                        }
                    }
                }
            return {}

    client = _HardenedClient()
    meta_15d = {
        "size": 100,
        "pushed_at": "2026-10-05T10:00:00Z",
        "updated_at": "2026-10-05T10:00:00Z",
        "default_branch": "main",
        "has_pull_requests": True,
        "has_issues": True,
        "archived": False,
        "visibility": "private",
    }
    report = collect_repository(
        client=client,
        org="acme-org",
        repo="svc",
        since=since_7d,
        end=end,
        exclude_bots=True,
        cached_metadata=meta_15d,
    )
    assert any(ev[0] == "alice" and ev[1] == "commits" for ev in report.events)
    assert ("carol", "reviews", datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)) in report.events
    assert any("revCursor" in call for call in client.gql_calls)
    assert report.protected == "Yes"
    assert "main (Ruleset)" in report.protected_branches
    assert report.activity_complete is False
    assert any("Issues: 403 Forbidden" in e for e in report.errors)

    mgmt = build_management_tables(
        reports={("acme-org", "svc"): report},
        members={"acme-org": {"alice"}},
        since=since_7d,
        end=end,
        organization_status={"acme-org": False},
    )
    inv_row = mgmt["Repository Inventory"][1][0]
    assert inv_row[11] == "N/A"
    assert inv_row[23] == "Partial"
    kpi_rows = mgmt["Management KPIs"][1]
    rolling_30 = next(r for r in kpi_rows if r[2] == "Rolling 30-day active users")
    assert rolling_30[3] >= 3

    teams_by_user, team_complete = fetch_team_access(client, ["acme-org"], exclude_bots=True)
    assert team_complete["acme-org"] is False
    assert "sec" in teams_by_user[("acme-org", "alice")]
