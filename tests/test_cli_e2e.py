"""End-to-end tests for the activity, bypass, finops, and runners CLI orchestrators.

Each test drives the real ``run_*_cli`` entry point with GitHub discovery and collection
boundaries mocked, so argument parsing, credential gating, row building, workbook writing,
and scorecard printing all execute without any network I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import openpyxl
import pytest
import requests

from repo360.activity import cli as activity_cli
from repo360.activity import reporter as activity_reporter
from repo360.activity.models import Activity, RepositoryReport
from repo360.auth.github_app_auth import GitHubAppAuth
from repo360.bypass import cli as bypass_cli
from repo360.bypass.models import PushProtectionBypassRecord, RepoBypassProfile, RulesetBypassRecord
from repo360.finops import cli as finops_cli
from repo360.finops.models import (
    ArtifactRecord,
    BillingUsageRecord,
    CacheRecord,
    OrgBillingSummary,
    PackageRecord,
    RepoStorageProfile,
)
from repo360.runners import cli as runners_cli
from repo360.runners.models import RunnerInfo, RunnerJobMetrics, RunnerJobRecord

NOW = datetime.now(timezone.utc)
ORG = "acme"
TARGETS = [(ORG, "api"), (ORG, "web")]
METADATA = {
    (ORG, "api"): {"name": "api", "visibility": "private", "default_branch": "main", "size": 120},
    (ORG, "web"): {"name": "web", "visibility": "public", "default_branch": "main", "size": 80},
}

CLI_CASES = [
    pytest.param(activity_cli, "run_activity_cli", id="activity"),
    pytest.param(bypass_cli, "run_bypass_cli", id="bypass"),
    pytest.param(finops_cli, "run_finops_cli", id="finops"),
    pytest.param(runners_cli, "run_runners_cli", id="runners"),
]


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f"Unexpected network call: args={args!r}")

    monkeypatch.setattr(requests.Session, "request", _fail)
    monkeypatch.setattr(requests, "request", _fail)


@pytest.fixture(autouse=True)
def _small_autofit_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    # autofit materialises every row up to the sample size; tiny test sheets don't need 2000.
    monkeypatch.setattr(activity_reporter, "AUTOFIT_SAMPLE_ROWS", 50)


@pytest.fixture
def app_credentials(monkeypatch: pytest.MonkeyPatch, test_rsa_key_pem: str) -> None:
    monkeypatch.setenv("REPO360_APP_ID", "12345")
    monkeypatch.setenv("REPO360_APP_PRIVATE_KEY", test_rsa_key_pem)


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    orgs: Optional[List[str]] = None,
    targets: Optional[List[Tuple[str, str]]] = None,
    permissions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Mock org/repo discovery and installation permissions; return captured call kwargs."""
    calls: Dict[str, Any] = {}

    def _discover(**kwargs: Any) -> List[str]:
        calls["discover"] = kwargs
        return list(orgs if orgs is not None else [ORG])

    def _resolve(**kwargs: Any) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]]]:
        calls["resolve"] = kwargs
        return list(targets if targets is not None else TARGETS), dict(METADATA)

    monkeypatch.setattr(module, "discover_app_organizations", _discover)
    monkeypatch.setattr(module, "resolve_scan_targets", _resolve)
    monkeypatch.setattr(
        GitHubAppAuth,
        "get_installation_permissions",
        lambda self, org: dict(permissions or {}),
    )
    return calls


def _single_workbook(out_dir: Path) -> openpyxl.Workbook:
    files = list(out_dir.glob("*.xlsx"))
    assert len(files) == 1, files
    return openpyxl.load_workbook(files[0])


def _workbook_text(wb: openpyxl.Workbook) -> str:
    return "\n".join(
        str(value)
        for ws in wb.worksheets
        for row in ws.iter_rows(values_only=True)
        for value in row
        if value is not None
    )


# ---------------------------------------------------------------------------
# Shared behaviour across all four CLIs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("module", "entry"), CLI_CASES)
def test_missing_credentials_fails_without_output(module: Any, entry: str, tmp_path: Path, capsys) -> None:
    rc = getattr(module, entry)(["--output-dir", str(tmp_path)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "[FAIL] Missing GitHub App credentials." in out
    assert "REPO360_APP_ID and REPO360_APP_PRIVATE_KEY" in out
    assert not list(tmp_path.glob("*.xlsx"))


@pytest.mark.parametrize(("module", "entry"), CLI_CASES)
@pytest.mark.parametrize(
    ("org_arg", "noun"),
    [("acme", "organization."), ("", "organizations.")],
    ids=["single-org", "all-orgs"],
)
def test_no_installations_fails(
    module: Any, entry: str, org_arg: str, noun: str, app_credentials, monkeypatch, tmp_path: Path, capsys
) -> None:
    calls = _patch_discovery(monkeypatch, module, orgs=[])

    rc = getattr(module, entry)(["--output-dir", str(tmp_path), "--org", org_arg])

    assert rc == 1
    out = capsys.readouterr().out
    assert f"No active GitHub App installations found for the specified {noun}" in out
    assert calls["discover"]["org_filter"] == ([org_arg] if org_arg else None)
    assert "resolve" not in calls
    assert not list(tmp_path.glob("*.xlsx"))


@pytest.mark.parametrize(("module", "entry"), CLI_CASES)
@pytest.mark.parametrize(
    "bad_args",
    [["--workers", "99"], ["--workers", "abc"], ["--require-ai", "maybe"]],
    ids=["workers-too-high", "workers-not-int", "require-ai-invalid"],
)
def test_common_argument_validation_exits_2(module: Any, entry: str, bad_args: List[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        getattr(module, entry)(["--output-dir", str(tmp_path), *bad_args])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("module", "entry", "bad_args"),
    [
        (activity_cli, "run_activity_cli", ["--lookback-days", "-1"]),
        (activity_cli, "run_activity_cli", ["--active-min-days", "0"]),
        (activity_cli, "run_activity_cli", ["--moderate-min-days", "366"]),
        (activity_cli, "run_activity_cli", ["--active-min-days", "3", "--moderate-min-days", "5"]),
        (bypass_cli, "run_bypass_cli", ["--lookback-days", "-5"]),
        (finops_cli, "run_finops_cli", ["--lookback-days", "30"]),
        (runners_cli, "run_runners_cli", ["--lookback-days", "-1"]),
        (runners_cli, "run_runners_cli", ["--max-runs", "1001"]),
    ],
    ids=[
        "activity-negative-lookback",
        "activity-active-min-zero",
        "activity-moderate-over-year",
        "activity-moderate-gt-active",
        "bypass-negative-lookback",
        "finops-has-no-lookback",
        "runners-negative-lookback",
        "runners-max-runs-too-high",
    ],
)
def test_command_specific_argument_validation_exits_2(
    module: Any, entry: str, bad_args: List[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        getattr(module, entry)(["--output-dir", str(tmp_path), *bad_args])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------


def _activity_report(*events: Tuple[str, str, int]) -> RepositoryReport:
    report = RepositoryReport(metadata={"visibility": "private", "default_branch": "main"}, activity_complete=True)
    for login, metric, days_ago in events:
        report.users.setdefault(login, Activity()).add(metric, NOW - timedelta(days=days_ago))
    return report


def _patch_activity_collectors(
    monkeypatch: pytest.MonkeyPatch,
    failing_repo: Optional[str] = None,
    partial_repo: Optional[str] = None,
) -> None:
    def _collect(client, org, repo, since, end, exclude_bots, metadata=None, *args, **kwargs):
        if repo == failing_repo:
            raise RuntimeError("simulated GraphQL outage")
        events = [("alice", "commits", d) for d in range(1, 10)] + [("bob", "prs_opened", 2)]
        report = _activity_report(*events)
        if repo == partial_repo:
            report.errors.append("PR timeline truncated by API (HTTP 502)")
        return report

    def _members(client, orgs, exclude_bots, failures) -> Dict[str, Set[str]]:
        return {org: {"alice", "bob", "carol"} for org in orgs}

    def _teams(client, orgs, exclude_bots):
        return {(ORG, "alice"): {"platform"}}, {org: True for org in orgs}

    monkeypatch.setattr(activity_cli, "collect_repository", _collect)
    monkeypatch.setattr(activity_cli, "fetch_members", _members)
    monkeypatch.setattr(activity_cli, "fetch_team_access", _teams)


def test_activity_happy_path_writes_workbook(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    calls = _patch_discovery(monkeypatch, activity_cli, permissions={"pull_requests": "none", "issues": "none"})
    _patch_activity_collectors(monkeypatch)

    rc = activity_cli.run_activity_cli(
        ["--output-dir", str(tmp_path), "--org", f" {ORG} , ", "--workers", "2", "--include-archived"]
    )

    assert rc == 0
    assert calls["discover"]["org_filter"] == [ORG]
    assert calls["resolve"]["include_archived"] is True
    wb = _single_workbook(tmp_path)
    assert {"User Activity", "Repository Detail"} <= set(wb.sheetnames)
    users = [row[1] for row in wb["User Activity"].iter_rows(min_row=2, values_only=True) if row[1]]
    assert {"alice", "bob", "carol"} <= set(users)
    out = capsys.readouterr().out
    assert "missing: pull_requests: read, issues: read" in out
    assert "CONTRIBUTOR & USER ACTIVITY SCORECARD" in out
    assert "[SUCCESS] Activity analysis completed" in out


def test_activity_repo_failure_is_recorded_and_run_completes(
    app_credentials, monkeypatch, tmp_path: Path, capsys
) -> None:
    _patch_discovery(monkeypatch, activity_cli, permissions={"pull_requests": "read", "issues": "write"})
    _patch_activity_collectors(monkeypatch, failing_repo="web", partial_repo="api")

    rc = activity_cli.run_activity_cli(["--output-dir", str(tmp_path), "--lookback-days", "0"])

    assert rc == 0
    text = _workbook_text(_single_workbook(tmp_path))
    assert f"{ORG}/web" in text
    assert "simulated GraphQL outage" in text
    assert f"{ORG}/api" in text
    assert "PR timeline truncated by API (HTTP 502)" in text
    out = capsys.readouterr().out
    assert "2 items (see workbook Summary)" in out
    assert "[WARN]" not in out


# ---------------------------------------------------------------------------
# bypass
# ---------------------------------------------------------------------------


def _bypass_profiles(targets: List[Tuple[str, str]]) -> List[RepoBypassProfile]:
    api = RepoBypassProfile(
        org=ORG,
        repo="api",
        push_bypasses=[
            PushProtectionBypassRecord(
                org=ORG, repo="api", alert_number=7, secret_type="github_personal_access_token",
                actor="mallory", timestamp=NOW - timedelta(days=2), resolution="used_in_tests",
            )
        ],
        ruleset_bypasses=[
            RulesetBypassRecord(
                org=ORG, repo="api", rule_suite_id=99, ref="refs/heads/main", actor="mallory",
                timestamp=NOW - timedelta(days=1), ruleset_scope="Organization", ruleset_name="protect-main",
            )
        ],
    )
    web = RepoBypassProfile(
        org=ORG,
        repo="web",
        errors=[
            "Secret scanning alerts: 403 Forbidden (missing secret_scanning_alerts permission)",
            "Organization rule suites API requires GitHub Team plan or higher",
        ],
    )
    by_repo = {"api": api, "web": web}
    return [by_repo[repo] for _, repo in targets]


@pytest.mark.parametrize(
    ("lookback", "window_text"),
    [("0", "Full Lifetime (All-Time)"), ("60", "up to 60d"), ("7", "7 Days")],
)
def test_bypass_happy_path_writes_workbook_and_records_gaps(
    lookback: str, window_text: str, app_credentials, monkeypatch, tmp_path: Path, capsys
) -> None:
    _patch_discovery(monkeypatch, bypass_cli, permissions={"secret_scanning_alerts": "none"})
    captured: Dict[str, Any] = {}

    def _collect(client, orgs, targets, since, cfg, metadata_cache, ai_engine=None):
        captured.update(orgs=orgs, since=since, lookback=cfg.lookback_days)
        return _bypass_profiles(targets)

    monkeypatch.setattr(bypass_cli, "collect_bypass_data", _collect)

    rc = bypass_cli.run_bypass_cli(["--output-dir", str(tmp_path), "--lookback-days", lookback])

    assert rc == 0
    assert captured["orgs"] == [ORG]
    assert captured["lookback"] == int(lookback)
    if lookback == "0":
        assert captured["since"].year == 1970
    wb = _single_workbook(tmp_path)
    assert {"Push Protection Bypasses", "Ruleset Bypasses", "Unaudited & Permission Gaps"} <= set(wb.sheetnames)
    gap_text = _workbook_text(wb)
    assert "missing secret_scanning_alerts permission" in gap_text
    out = capsys.readouterr().out
    assert "missing: secret_scanning_alerts: read" in out
    assert window_text in out
    assert "1 overrides" in out
    assert "Unaudited / Permission Gaps" in out
    assert "GitHub Plan Limitations" in out
    assert "[SUCCESS] Bypass ledger generated across 1 organization" in out


def test_bypass_clean_run_has_no_gap_sheet(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_discovery(monkeypatch, bypass_cli, targets=[(ORG, "web")], permissions={"secret_scanning_alerts": "read"})
    monkeypatch.setattr(
        bypass_cli,
        "collect_bypass_data",
        lambda client, orgs, targets, since, cfg, metadata_cache, ai_engine=None: [RepoBypassProfile(org=ORG, repo="web")],
    )

    rc = bypass_cli.run_bypass_cli(["--output-dir", str(tmp_path)])

    assert rc == 0
    assert "Unaudited & Permission Gaps" not in _single_workbook(tmp_path).sheetnames
    out = capsys.readouterr().out
    assert "[WARN]" not in out
    assert "0 overrides" in out


# ---------------------------------------------------------------------------
# finops
# ---------------------------------------------------------------------------


def _finops_collect(client, orgs, targets, cfg, metadata_cache, failures_out=None, billing_out=None):
    profiles = [
        RepoStorageProfile(
            org=ORG,
            repo="api",
            disk_size_kb=20480,
            artifacts=[
                ArtifactRecord(
                    id=1, name="build", size_in_bytes=5 * 1024 * 1024,
                    created_at=NOW - timedelta(days=3), expires_at=NOW + timedelta(days=87),
                )
            ],
            caches=[
                CacheRecord(
                    id=2, key="npm-cache", size_in_bytes=2 * 1024 * 1024,
                    created_at=NOW - timedelta(days=40), last_accessed_at=NOW - timedelta(days=35),
                )
            ],
        ),
        RepoStorageProfile(org=ORG, repo="web", disk_size_kb=1024, errors=["Actions caches: 403 Forbidden"]),
    ]
    packages = {ORG: [PackageRecord(id=3, name="web-image", package_type="container", size_in_bytes=1024 * 1024)]}
    if failures_out is not None:
        failures_out.append((ORG, "Packages listing: 403 Forbidden"))
    if billing_out is not None:
        billing_out[ORG] = OrgBillingSummary(
            org=ORG,
            period="2026-11",
            actions_compute_minutes=120.0,
            items=[
                BillingUsageRecord(
                    org=ORG, scope="organization", period="2026-11", product="actions",
                    sku="Actions Linux", quantity=120.0, unit_type="minutes", gross_amount=0.96, net_amount=0.96,
                )
            ],
        )
    return profiles, packages


def test_finops_happy_path_writes_workbook_with_gaps(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_discovery(monkeypatch, finops_cli, permissions={"packages": "none", "actions": "none"})
    monkeypatch.setattr(finops_cli, "collect_finops_data", _finops_collect)

    rc = finops_cli.run_finops_cli(["--output-dir", str(tmp_path), "--org", ORG])

    assert rc == 0
    wb = _single_workbook(tmp_path)
    assert {
        "Storage by Repository",
        "Artifact Inventory",
        "Cache & Package Inventory",
        "Billing & Compute Usage",
        "Collection & Permission Gaps",
    } <= set(wb.sheetnames)
    gaps = [row for row in wb["Collection & Permission Gaps"].iter_rows(values_only=True)]
    gap_text = " ".join(str(v) for row in gaps for v in row if v is not None)
    assert "Packages listing: 403 Forbidden" in gap_text
    assert f"{ORG}/web" in gap_text and "Actions caches: 403 Forbidden" in gap_text
    out = capsys.readouterr().out
    assert "missing permission: packages: read" in out
    assert "missing permission: actions: read" in out
    assert "2 signal checks skipped" in out
    assert "[SUCCESS] FinOps profiling completed across 2 repositories" in out


def test_finops_clean_run_has_no_gap_sheet(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_discovery(monkeypatch, finops_cli, targets=[(ORG, "api")], permissions={"packages": "read", "actions": "read"})
    monkeypatch.setattr(
        finops_cli,
        "collect_finops_data",
        lambda client, orgs, targets, cfg, metadata_cache, failures_out=None, billing_out=None: (
            [RepoStorageProfile(org=ORG, repo="api", disk_size_kb=512)],
            {},
        ),
    )

    rc = finops_cli.run_finops_cli(["--output-dir", str(tmp_path)])

    assert rc == 0
    sheets = _single_workbook(tmp_path).sheetnames
    assert "Collection & Permission Gaps" not in sheets
    assert "Billing & Compute Usage" not in sheets
    out = capsys.readouterr().out
    assert "[WARN]" not in out
    assert "Collection / Permission Gaps" not in out


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------


def _runners_collect(client, orgs, targets, since, end, cfg, failures, metadata_cache=None,
                     job_records_out=None, truncation_notes_out=None):
    runners = [
        RunnerInfo(id=1, name="build-01", os="Linux", status="online", busy=True, labels=("self-hosted", "linux"), org=ORG),
        RunnerInfo(id=2, name="build-02", os="Linux", status="offline", busy=False, labels=("self-hosted",), org=ORG),
    ]
    started = NOW - timedelta(days=1)
    metrics = RunnerJobMetrics(
        total_jobs=2,
        successful_jobs=1,
        failed_jobs=1,
        total_duration_seconds=600.0,
        total_queue_seconds=95.0,
        job_durations=[300.0, 300.0],
        queue_durations=[5.0, 90.0],
        repos_served={"api"},
        first_job_time=started,
        last_job_time=started + timedelta(minutes=10),
    )
    failures.append((f"{ORG}/web", "Workflow runs: 403 Forbidden"))
    if job_records_out is not None:
        job_records_out.append(
            RunnerJobRecord(
                org=ORG, repo="api", workflow_name="CI", run_id=10, job_id=11, job_name="build",
                runner_id=1, runner_name="build-01", runner_type="Persistent (Static VM)", conclusion="success",
                duration_seconds=300.0, queue_seconds=5.0, started_at=started, completed_at=started + timedelta(minutes=5),
            )
        )
    if truncation_notes_out is not None:
        truncation_notes_out.append((f"{ORG}/api", "Workflow run history truncated at --max-runs 5"))
    org_metrics = RunnerJobMetrics()
    org_metrics.merge(metrics)
    return runners, {(ORG, "build-01"): metrics}, {ORG: org_metrics}


def test_runners_happy_path_writes_workbook_with_failures(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_discovery(monkeypatch, runners_cli, permissions={"organization_self_hosted_runners": "none"})
    captured: Dict[str, Any] = {}

    def _collect(*args: Any, **kwargs: Any):
        captured["cfg"] = args[5]
        return _runners_collect(*args, **kwargs)

    monkeypatch.setattr(runners_cli, "collect_runners_and_metrics", _collect)

    rc = runners_cli.run_runners_cli(
        ["--output-dir", str(tmp_path), "--max-runs", "5", "--lookback-days", "0", "--no-github-hosted"]
    )

    assert rc == 0
    cfg = captured["cfg"]
    assert cfg.max_runner_runs_per_repo == 5
    assert cfg.lookback_days == 0
    assert cfg.include_github_hosted is False
    wb = _single_workbook(tmp_path)
    assert {"Org Runner Summary", "Runner Fleet Detail", "Workflow Job History"} <= set(wb.sheetnames)
    text = _workbook_text(wb)
    assert "Workflow runs: 403 Forbidden" in text
    assert "build-01" in text
    out = capsys.readouterr().out
    assert "missing: organization_self_hosted_runners: read" in out
    assert "ACTIONS RUNNER FLEET SCORECARD" in out
    assert "Warnings / Errors       :\x1b[0m \x1b[93m1 items" in out
    assert "[SUCCESS] Runner analysis completed across 2 runners" in out


def test_runners_empty_fleet_completes(app_credentials, monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_discovery(monkeypatch, runners_cli, targets=[], permissions={"organization_self_hosted_runners": "read"})
    monkeypatch.setattr(
        runners_cli,
        "collect_runners_and_metrics",
        lambda *args, **kwargs: ([], {}, {}),
    )

    rc = runners_cli.run_runners_cli(["--output-dir", str(tmp_path)])

    assert rc == 0
    _single_workbook(tmp_path)
    out = capsys.readouterr().out
    assert "No accessible repositories discovered" in out
    assert "Warnings / Errors" not in out
    assert "[SUCCESS] Runner analysis completed across 0 runners" in out
