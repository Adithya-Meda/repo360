"""Unit and integration tests for Repo360 Actions self-hosted runners analytics."""

from datetime import datetime, timezone
import pytest

from src.runners.models import (
    ORG_RUNNER_HEADERS,
    RUNNER_HEADERS,
    RunnerInfo,
    RunnerJobMetrics,
    RunnersConfig,
)
from src.runners.collector import build_runner_rows
from src.runners.reporter import write_runners_workbook


def test_runner_job_metrics_calculations():
    m = RunnerJobMetrics()
    m.successful_jobs = 8
    m.failed_jobs = 2
    m.timed_out_jobs = 0
    m.total_jobs = 10
    m.total_duration_seconds = 1200.0
    m.job_durations = [60.0, 120.0, 180.0, 240.0, 600.0]
    m.total_queue_seconds = 250.0
    m.queue_durations = [10.0, 20.0, 40.0, 80.0, 100.0]

    assert m.completed_jobs == 10
    assert m.success_rate == 80.0
    assert m.failure_rate == 20.0
    assert m.total_compute_hours == 0.33
    assert m.avg_duration_minutes == 4.0
    assert m.min_duration_minutes == 1.0
    assert m.max_duration_minutes == 10.0
    assert m.median_duration_minutes == 3.0
    assert m.avg_queue_seconds == 50.0
    assert m.p95_queue_seconds == 100.0
    assert m.queue_sla_breaches == 2
    assert m.queue_sla_breach_rate == 40.0


def test_runner_job_metrics_empty():
    m = RunnerJobMetrics()
    assert m.completed_jobs == 0
    assert m.success_rate == "N/A"
    assert m.failure_rate == "N/A"
    assert m.avg_duration_minutes == "N/A"
    assert m.median_duration_minutes == "N/A"
    assert m.min_duration_minutes == "N/A"
    assert m.max_duration_minutes == "N/A"
    assert m.avg_queue_seconds == "N/A"
    assert m.p95_queue_seconds == "N/A"
    assert m.queue_sla_breaches == 0
    assert m.queue_sla_breach_rate == "N/A"


def test_build_runner_rows():
    runner = RunnerInfo(
        id=101,
        name="linux-runner-1",
        os="Linux",
        status="Online",
        busy=True,
        labels=("self-hosted", "x64", "linux"),
        runner_group_name="Core-Compute",
        org="fintech-org",
        arch="x64",
        runner_version="2.319.0",
        ephemeral=False,
    )
    metrics = RunnerJobMetrics()
    metrics.successful_jobs = 5
    metrics.total_jobs = 5
    metrics.total_duration_seconds = 600.0
    metrics.repos_served.add("payment-service")

    runner_rows, org_runner_rows = build_runner_rows(
        runners=[runner],
        runner_metrics={("fintech-org", "linux-runner-1"): metrics},
        org_runner_metrics={"fintech-org": metrics},
        orgs=["fintech-org"],
    )

    assert len(runner_rows) == 1
    r_row = runner_rows[0]
    assert r_row[0] == "fintech-org"
    assert r_row[1] == 101
    assert r_row[2] == "linux-runner-1"
    assert r_row[4] == "Core-Compute"
    assert r_row[9] == "Online"
    assert r_row[10] == "Busy"
    assert r_row[13] == 5
    assert r_row[18] == 100.0

    assert len(org_runner_rows) == 1
    o_row = org_runner_rows[0]
    assert o_row[0] == "fintech-org"
    assert o_row[1] == 1
    assert o_row[4] == 1
    assert o_row[6] == 100.0
    assert o_row[7] == 1

    _, multi_org_rows = build_runner_rows(
        runners=[runner],
        runner_metrics={("fintech-org", "linux-runner-1"): metrics},
        org_runner_metrics={"fintech-org": metrics},
        orgs=["fintech-org", "empty-org"],
    )
    assert len(multi_org_rows) == 2
    org_names = [row[0] for row in multi_org_rows]
    assert "fintech-org" in org_names
    assert "empty-org" in org_names
    empty_row = next(row for row in multi_org_rows if row[0] == "empty-org")
    assert empty_row[1] == 0
    assert empty_row[6] == "N/A"


def test_write_runners_workbook_and_csv(tmp_path):
    runner = RunnerInfo(
        id=202,
        name="k8s-pod-runner",
        os="Linux",
        status="Online",
        busy=False,
        labels=("self-hosted", "k8s"),
        runner_group_name="Autoscaling",
        org="acme-corp",
        arch="x64",
        runner_version="2.320.0",
        ephemeral=True,
    )
    metrics = RunnerJobMetrics()
    metrics.successful_jobs = 12
    metrics.total_jobs = 12
    metrics.total_duration_seconds = 3600.0

    runner_rows, org_runner_rows = build_runner_rows(
        runners=[runner],
        runner_metrics={("acme-corp", "k8s-pod-runner"): metrics},
        org_runner_metrics={"acme-corp": metrics},
        orgs=["acme-corp"],
    )

    cfg = RunnersConfig(output_dir=str(tmp_path))
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    xlsx_path = tmp_path / "runners.xlsx"

    write_runners_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=now,
        end=now,
        runner_rows=runner_rows,
        org_runner_rows=org_runner_rows,
        failures=[],
        execution_duration=2.0,
    )

    assert xlsx_path.is_file()
    assert xlsx_path.stat().st_size > 1000

    from openpyxl import load_workbook
    wb = load_workbook(xlsx_path)
    summary = wb["Executive Summary"]
    assert summary["B5"].value == "2.0s"
    assert summary["B5"].alignment.horizontal == "center"
    online_row_val = None
    for r in range(5, 16):
        metric_name = summary.cell(row=r, column=1).value
        assert summary.cell(row=r, column=1).alignment.horizontal == "left"
        if metric_name == "Online Runners":
            online_row_val = summary.cell(row=r, column=2).value
            assert summary.cell(row=r, column=2).alignment.horizontal == "center"
    assert online_row_val == 1


def test_runners_hardened_isolation_ephemeral_and_pooled_metrics(tmp_path):
    from datetime import timedelta

    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    since = now - timedelta(days=30)

    jm = RunnerJobMetrics(successful_jobs=4, failed_jobs=1, cancelled_jobs=5, total_jobs=10)
    assert jm.completed_jobs == 10
    assert jm.decisive_jobs == 5
    assert jm.success_rate == 80.0
    assert jm.failure_rate == 20.0

    class _ForbiddenResp:
        status_code = 403

    class _ForbiddenErr(Exception):
        response = _ForbiddenResp()

    class _FakeClient:
        def paginate_enveloped(self, org, endpoint, key, params=None):
            if endpoint.endswith("/actions/runner-groups"):
                return iter([{"id": 1, "name": "Default"}])
            if endpoint == "/orgs/org-a/actions/runners":
                return iter([{"id": 10, "name": "shared-runner", "os": "Linux", "status": "online", "busy": False, "labels": []}])
            if endpoint == "/orgs/org-b/actions/runners":
                return iter([{"id": 20, "name": "shared-runner", "os": "Linux", "status": "offline", "busy": False, "labels": []}])
            if endpoint == "/repos/org-a/repo-1/actions/runners":
                raise _ForbiddenErr("forbidden")
            if endpoint == "/repos/org-a/repo-1/actions/runs":
                # Include 4 GitHub-hosted runs first to verify scanning doesn't stop after 3 runs
                return iter([
                    {"id": 1001, "created_at": "2026-10-19T12:00:00Z"},
                    {"id": 1002, "created_at": "2026-10-18T12:00:00Z"},
                    {"id": 1003, "created_at": "2026-10-17T12:00:00Z"},
                    {"id": 1004, "created_at": "2026-10-16T12:00:00Z"},
                ])
            if "/actions/runs/100" in endpoint and endpoint.endswith("/jobs"):
                run_num = int(endpoint.split("/runs/")[1].split("/")[0])
                if run_num in (1001, 1002, 1003):
                    return iter([{
                        "labels": ["ubuntu-latest"],
                        "runner_id": 9000 + run_num,
                        "runner_name": f"GitHub Actions {run_num}",
                        "conclusion": "success",
                        "created_at": "2026-10-19T12:00:00Z",
                        "started_at": "2026-10-19T12:00:10Z",
                        "completed_at": "2026-10-19T12:01:10Z",
                    }])
                return iter([
                    {"labels": ["self-hosted"], "runner_id": None, "runner_name": None, "conclusion": "skipped"},
                    {
                        "labels": ["self-hosted"],
                        "runner_id": None,
                        "runner_name": None,
                        "conclusion": "cancelled",
                        "created_at": "2026-10-16T12:00:00Z",
                    },
                    {
                        "labels": ["self-hosted", "k8s"],
                        "runner_id": 777,
                        "runner_name": "arc-eph-pod-1",
                        "os": "Linux",
                        "arch": "x64",
                        "conclusion": "success",
                        "created_at": "2026-10-16T12:00:00Z",
                        "started_at": "2026-10-16T12:01:40Z",
                        "completed_at": "2026-10-16T12:03:40Z",
                    },
                ])
            return iter([])

    from src.runners.collector import collect_runners_and_metrics

    failures = []
    cfg = RunnersConfig(lookback_days=30, max_runner_runs_per_repo=20, output_dir=str(tmp_path))
    runners, runner_metrics, org_metrics = collect_runners_and_metrics(
        client=_FakeClient(),
        orgs=["org-a", "org-b"],
        targets=[("org-a", "repo-1")],
        since=since,
        end=now,
        cfg=cfg,
        failures=failures,
    )

    names_by_org = sorted((r.org, r.name, r.status) for r in runners)
    assert ("org-a", "shared-runner", "Online") in names_by_org
    assert ("org-b", "shared-runner", "Offline") in names_by_org
    assert ("org-a", "arc-eph-pod-1", "Ephemeral") in names_by_org
    assert not any(r.name == "self-hosted-runner" for r in runners)

    assert any("403 Forbidden" in msg and "org-a/repo-1" in target for target, msg in failures)

    assert runner_metrics[("org-a", "arc-eph-pod-1")].total_jobs == 1
    assert org_metrics["org-a"].total_jobs == 2
    assert org_metrics["org-a"].cancelled_jobs == 1
    assert org_metrics["org-a"].success_rate == 100.0

    eph_only = RunnerInfo(id=501, name="arc-1", os="Linux", status="Ephemeral", busy=False, labels=("self-hosted",), org="eph-org", ephemeral=True)
    r_rows, o_rows = build_runner_rows([eph_only], {}, {}, orgs=["eph-org"])
    assert o_rows[0][5] == 0
    assert o_rows[0][6] == "N/A (Ephemeral Only)"

    # Workbook uses pooled P95 rather than mean of per-runner P95s
    m_small = RunnerJobMetrics(total_jobs=1, successful_jobs=1, total_queue_seconds=200.0, queue_durations=[200.0])
    m_big = RunnerJobMetrics(total_jobs=10, successful_jobs=10, total_queue_seconds=50.0, queue_durations=[5.0] * 10)
    pooled_rows, pooled_org_rows = build_runner_rows(
        [
            RunnerInfo(id=1, name="r1", os="Linux", status="Online", busy=False, labels=("self-hosted",), org="pool-org"),
            RunnerInfo(id=2, name="r2", os="Linux", status="Online", busy=False, labels=("self-hosted",), org="pool-org"),
        ],
        {("pool-org", "r1"): m_small, ("pool-org", "r2"): m_big},
        {"pool-org": RunnerJobMetrics()},
        orgs=["pool-org"],
    )
    pooled_om = RunnerJobMetrics()
    pooled_om.merge(m_small)
    pooled_om.merge(m_big)
    pooled_xlsx = tmp_path / "pooled.xlsx"
    write_runners_workbook(
        path=pooled_xlsx,
        cfg=cfg,
        since=since,
        end=now,
        runner_rows=pooled_rows,
        org_runner_rows=pooled_org_rows,
        failures=[],
        execution_duration=1.2,
        org_runner_metrics={"pool-org": pooled_om},
    )
    from openpyxl import load_workbook
    wb_pooled = load_workbook(pooled_xlsx)
    ws_sum = wb_pooled["Executive Summary"]
    kpi_map = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(5, 22)}
    # Pooled avg = 250/11 = 22.73s; pooled P95 index ceil(0.95*11)-1 = 10 -> 200.0s
    assert kpi_map["Avg / P95 Queue Wait (s)"] == "22.73s / 200.0s"
