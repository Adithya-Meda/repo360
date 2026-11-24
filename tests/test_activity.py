"""Unit and integration tests for Repo360 user activity scraper."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from repo360.activity.models import (
    Activity,
    ActivityConfig,
    is_bot,
)
from repo360.activity.collector import (
    build_org_rows,
    build_rows,
    classify,
)
from repo360.activity.reporter import write_activity_workbook


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
    from repo360.activity.models import USER_HEADERS
    assert alice_row[USER_HEADERS.index("Org Member")] == "Yes"
    assert alice_row[USER_HEADERS.index("Teams")] == "backend-team"
    assert len(alice_row) == len(USER_HEADERS)

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
    from repo360.activity.collector import _format_error
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
    from repo360.activity.collector import build_management_tables
    from repo360.activity.models import (
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
    assert inv_rows[0][inv_headers.index("Total PRs")] == 1
    assert inv_rows[0][inv_headers.index("Open PRs")] == 1
    assert inv_rows[0][inv_headers.index("Closed PRs")] == 0
    assert inv_rows[0][inv_headers.index("Merged PRs")] == 0
    assert inv_rows[0][inv_headers.index("Closed (Unmerged) PRs")] == 0
    assert inv_rows[0][inv_headers.index("PR Cohort Merge Rate (%)")] == 80.0
    assert inv_rows[0][inv_headers.index("Avg First Review Hours")] == 3.0
    assert inv_rows[0][inv_headers.index("Coverage")] == "Complete"

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
    kpi_metric_names = {r[2] for r in kpi_rows}
    assert "Currently open pull requests" in kpi_metric_names
    assert "Open issue backlog" in kpi_metric_names
    assert "Closed issues" in kpi_metric_names
    assert "Total issues tracked" in kpi_metric_names
    assert "Pull request discussion comments" in kpi_metric_names
    assert "Issue discussion comments" in kpi_metric_names

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

    ws_exec = wb["Executive Summary"]
    exec_metric_names = [ws_exec.cell(row=r, column=1).value for r in range(5, 30)]
    assert "Currently Open PRs" in exec_metric_names
    assert "Open Issues Backlog" in exec_metric_names
    assert "Closed Issues" in exec_metric_names
    assert "Total Issues Tracked" in exec_metric_names


def test_activity_hardened_dormancy_partial_errors_reviews_and_normalization():
    from datetime import timedelta
    from repo360.activity.collector import (
        build_management_tables,
        collect_repository,
        fetch_team_access,
    )

    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    # Month bucketing follows the configured local timezone: 2026-10-31 20:00 UTC == 2026-11-01 01:30 in Asia/Kolkata
    with patch("repo360.activity.models.LOCAL_TZ", ZoneInfo("Asia/Kolkata")):
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
    from repo360.activity.models import USER_HEADERS
    assert alice_r[USER_HEADERS.index("Org Member")] == "Yes"
    assert alice_r[USER_HEADERS.index("Teams")] == "Platform"
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
    inv_headers, inv_row = mgmt["Repository Inventory"][0], mgmt["Repository Inventory"][1][0]
    assert inv_row[11] == "N/A"
    assert inv_row[inv_headers.index("Coverage")] == "Partial"
    kpi_rows = mgmt["Management KPIs"][1]
    rolling_30 = next(r for r in kpi_rows if r[2] == "Rolling 30-day active users")
    assert rolling_30[3] >= 3

    teams_by_user, team_complete = fetch_team_access(client, ["acme-org"], exclude_bots=True)
    assert team_complete["acme-org"] is False
    assert "sec" in teams_by_user[("acme-org", "alice")]


def test_activity_commit_unlinked_author_fallback():
    """Verify that commits where author is None fall back to committer login."""
    from repo360.activity.collector import collect_repo_events
    from repo360.activity.models import RepositoryReport

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    mock_client = MagicMock()
    mock_client.paginate.side_effect = [
        # commits endpoint
        [
            {
                "author": None,  # unlinked git author
                "committer": {"login": "verified-committer", "type": "User"},
                "commit": {
                    "author": {"name": "Unlinked Dev", "date": "2026-10-15T12:00:00Z"},
                    "committer": {"name": "GitHub Web", "date": "2026-10-15T12:00:00Z"},
                },
            }
        ],
        [],  # issues
        [],  # issue comments
        [],  # review comments
    ]
    report = RepositoryReport()
    report.metadata = {"has_pull_requests": False, "has_issues": False}

    users = collect_repo_events(
        client=mock_client,
        org="demo-org",
        repo="demo-repo",
        since=now - timedelta(days=7),
        exclude_bots=True,
        end=now,
        report=report,
    )
    assert "verified-committer" in users
    assert users["verified-committer"].commits == 1


def test_activity_all_time_lookback_window_reporting(tmp_path):
    """Verify that lookback_days=0 calculates dynamic reporting window from earliest activity date, never showing 1970."""
    from openpyxl import load_workbook
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    inception = datetime(2023, 4, 15, 10, 0, tzinfo=timezone.utc)
    act = Activity()
    act.add("commits", inception)
    act.add("commits", now)

    cfg = ActivityConfig(output_dir=str(tmp_path), lookback_days=0)
    user_rows, repo_rows = build_rows(
        results={("acme-org", "service-a"): {"alice": act}},
        members={"acme-org": {"alice"}},
        cfg=cfg,
        end=now,
        teams_by_user={},
        org_team_complete={"acme-org": True},
    )
    org_rows = build_org_rows(user_rows, {("acme-org", "service-a"): {"alice": act}})

    xlsx_path = tmp_path / "activity_all_time.xlsx"
    write_activity_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=datetime(1970, 1, 1, tzinfo=timezone.utc),
        end=now,
        user_rows=user_rows,
        repo_rows=repo_rows,
        org_rows=org_rows,
        failures=[],
        repos_scanned=1,
        execution_duration=1.0,
    )

    wb = load_workbook(xlsx_path)
    ws = wb["Executive Summary"]
    a2_val = str(ws["A2"].value)

    assert "1970" not in a2_val
    assert "(0 days)" not in a2_val
    assert "Full History" in a2_val
    assert "15-04-2023" in a2_val
    assert "24-11-2026" in a2_val


def test_repository_inventory_pr_metrics_breakdown():
    """Verify exact PR state breakdown: Total (23), Open (11), Closed (12), Merged (11), Closed Unmerged (1)."""
    from repo360.activity.collector import build_management_tables, collect_pull_requests_graphql
    from repo360.activity.models import REPO_INVENTORY_HEADERS, TOP_REPOS_HEADERS, RepositoryReport

    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    since = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    # 11 Open PRs, 11 Merged PRs, 1 Closed Unmerged PR = 23 Total PRs
    mock_pr_nodes = []
    # 11 Open
    for i in range(1, 12):
        mock_pr_nodes.append({
            "id": f"PR_OPEN_{i}",
            "number": i,
            "createdAt": "2026-09-01T10:00:00Z",
            "mergedAt": None,
            "closedAt": None,
            "updatedAt": "2026-10-01T10:00:00Z",
            "state": "OPEN",
            "author": {"login": f"dev_{i}"},
            "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        })
    # 11 Merged
    for i in range(12, 23):
        mock_pr_nodes.append({
            "id": f"PR_MERGED_{i}",
            "number": i,
            "createdAt": "2026-09-02T10:00:00Z",
            "mergedAt": "2026-09-10T12:00:00Z",
            "closedAt": "2026-09-10T12:00:00Z",
            "updatedAt": "2026-09-10T12:00:00Z",
            "state": "MERGED",
            "author": {"login": f"dev_{i}"},
            "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        })
    # 1 Closed (Unmerged)
    mock_pr_nodes.append({
        "id": "PR_CLOSED_23",
        "number": 23,
        "createdAt": "2026-09-03T10:00:00Z",
        "mergedAt": None,
        "closedAt": "2026-09-05T10:00:00Z",
        "updatedAt": "2026-09-05T10:00:00Z",
        "state": "CLOSED",
        "author": {"login": "dev_abandoned"},
        "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    })

    class MockClient:
        def graphql(self, org, query, variables):
            return {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": mock_pr_nodes,
                        }
                    }
                }
            }

    report = RepositoryReport(
        metadata={
            "created_at": "2026-01-01T00:00:00Z",
            "visibility": "private",
            "archived": False,
            "default_branch": "main",
        },
        protected="Yes",
        protected_branches=("main",),
        activity_complete=True,
    )

    events = []
    def record_fn(author, metric, ts):
        if ts:
            events.append((author.get("login") if author else None, metric, datetime.fromisoformat(ts)))

    success = collect_pull_requests_graphql(
        client=MockClient(),
        org="CanarysPlayground",
        repo="TaskManager-DemoApp",
        collected_since=since,
        since=since,
        end=now,
        exclude_bots=True,
        report=report,
        record_fn=record_fn,
    )

    assert success is True
    assert report.prs_total == 23
    assert report.prs_open == 11
    assert report.prs_closed == 12
    assert report.prs_merged == 11
    assert report.prs_closed_unmerged == 1

    report.events = events
    tables = build_management_tables(
        reports={("CanarysPlayground", "TaskManager-DemoApp"): report},
        members={"CanarysPlayground": set()},
        since=since,
        end=now,
        organization_status={"CanarysPlayground": True},
    )

    inv_headers, inv_rows = tables["Repository Inventory"]
    assert inv_headers == REPO_INVENTORY_HEADERS
    inv_row = inv_rows[0]

    assert inv_row[inv_headers.index("Total PRs")] == 23
    assert inv_row[inv_headers.index("Open PRs")] == 11
    assert inv_row[inv_headers.index("Closed PRs")] == 12
    assert inv_row[inv_headers.index("Merged PRs")] == 11
    assert inv_row[inv_headers.index("Closed (Unmerged) PRs")] == 1

    top_headers, top_rows = tables["Top Active Repositories"]
    assert top_headers == TOP_REPOS_HEADERS
    top_row = top_rows[0]

    assert top_row[top_headers.index("Total PRs")] == 23
    assert top_row[top_headers.index("Open PRs")] == 11
    assert top_row[top_headers.index("Closed PRs")] == 12
    assert top_row[top_headers.index("Merged PRs")] == 11
    assert top_row[top_headers.index("Closed (Unmerged) PRs")] == 1


def test_repository_inventory_issue_and_comment_breakdown():
    """Verify issues breakdown (Total=20, Open=15, Closed=5, Active=0) and isolated PR/Issue comments."""
    from repo360.activity.collector import build_management_tables, collect_repo_events
    from repo360.activity.models import REPO_INVENTORY_HEADERS, TOP_REPOS_HEADERS, RepositoryReport

    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    since = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

    # 15 Open issues, 5 Closed issues created before 'since' (no activity in window -> Active=0)
    # 8 PR conversation comments on /issues/comments
    # 4 Issue comments on /issues/comments
    # 0 PR review inline comments on /pulls/comments

    class MockIssueClient:
        def paginate(self, org, endpoint, params=None):
            if endpoint.endswith("/commits"):
                return iter([])
            if endpoint.endswith("/issues"):
                return iter([])  # no issues updated in since_param window
            if endpoint.endswith("/issues/comments"):
                comments = []
                for i in range(1, 9):
                    comments.append({
                        "id": 100 + i,
                        "html_url": f"https://github.com/CanarysPlayground/TaskManager-DemoApp/pull/1#issuecomment-{i}",
                        "created_at": "2026-09-15T10:00:00Z",
                        "user": {"login": f"reviewer_{i}"},
                    })
                for i in range(1, 5):
                    comments.append({
                        "id": 200 + i,
                        "html_url": f"https://github.com/CanarysPlayground/TaskManager-DemoApp/issues/2#issuecomment-{i}",
                        "created_at": "2026-09-16T10:00:00Z",
                        "user": {"login": f"user_{i}"},
                    })
                return iter(comments)
            if endpoint.endswith("/pulls/comments"):
                return iter([])
            return iter([])

        def graphql(self, org, query, variables=None):
            return {
                "data": {
                    "repository": {
                        "openIssues": {"totalCount": 15},
                        "closedIssues": {"totalCount": 5},
                        "totalIssues": {"totalCount": 20},
                        "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                    }
                }
            }

    report = RepositoryReport(
        metadata={
            "created_at": "2026-01-01T00:00:00Z",
            "visibility": "private",
            "archived": False,
            "default_branch": "main",
            "has_pull_requests": True,
            "has_issues": True,
        },
        protected="Yes",
        protected_branches=("main",),
        activity_complete=True,
    )

    from repo360.activity.collector import collect_pull_requests_graphql

    events = []
    def record_fn(author, metric, ts):
        if ts:
            events.append((author.get("login") if author else None, metric, datetime.fromisoformat(ts)))

    collect_pull_requests_graphql(
        client=MockIssueClient(),
        org="CanarysPlayground",
        repo="TaskManager-DemoApp",
        collected_since=since,
        since=since,
        end=now,
        exclude_bots=True,
        report=report,
        record_fn=record_fn,
    )

    assert report.issues_total == 20
    assert report.issues_open == 15
    assert report.issues_closed == 5

    users = collect_repo_events(
        client=MockIssueClient(),
        org="CanarysPlayground",
        repo="TaskManager-DemoApp",
        since=since,
        exclude_bots=True,
        end=now,
        report=report,
    )

    assert report.issues_active == 0

    tables = build_management_tables(
        reports={("CanarysPlayground", "TaskManager-DemoApp"): report},
        members={"CanarysPlayground": set()},
        since=since,
        end=now,
        organization_status={"CanarysPlayground": True},
    )

    inv_headers, inv_rows = tables["Repository Inventory"]
    inv_row = inv_rows[0]

    assert inv_row[inv_headers.index("Total Issues")] == 20
    assert inv_row[inv_headers.index("Open Issues")] == 15
    assert inv_row[inv_headers.index("Closed Issues")] == 5
    assert inv_row[inv_headers.index("Active Issues")] == 0

    assert inv_row[inv_headers.index("PR Comments")] == 8
    assert inv_row[inv_headers.index("Issue Comments")] == 4
    assert inv_row[inv_headers.index("PR Review Comments")] == 0

    top_headers, top_rows = tables["Top Active Repositories"]
    top_row = top_rows[0]

    assert top_row[top_headers.index("Total Issues")] == 20
    assert top_row[top_headers.index("Open Issues")] == 15
    assert top_row[top_headers.index("Closed Issues")] == 5
    assert top_row[top_headers.index("Active Issues")] == 0
    assert top_row[top_headers.index("PR Comments")] == 8
    assert top_row[top_headers.index("Issue Comments")] == 4
    assert top_row[top_headers.index("PR Review Comments")] == 0




