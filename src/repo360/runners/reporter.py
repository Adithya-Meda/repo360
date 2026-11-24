"""Excel reporter for Actions self-hosted runners analytics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..common.timezones import DATE_FMT, LOCAL_TZ, LOCAL_TZ_NAME, resolve_reporting_window
from ..activity.reporter import (
    NAVY,
    autofit,
    finalize_sheet,
    safe_cell,
    section_title,
    write_empty_table,
    write_table,
)
from ..common.concurrency import AI_NOT_ENABLED_MESSAGE, is_ai_disabled, is_ai_live
from ..common.ai_sheet import AI_DISCLAIMER_NOTE, AI_REC_HEADERS, render_ai_recommendations_sheet
from ..common.text import pluralize
from .models import ORG_RUNNER_HEADERS, RUNNER_HEADERS, RUNNER_JOB_HEADERS, RunnersConfig


def build_runner_ai_recommendations(
    runner_rows: Sequence[Sequence[Any]],
    org_runner_rows: Sequence[Sequence[Any]],
    total_sla_breaches: int,
    failures: Sequence[Tuple[str, str]],
    all_truncations: Optional[Sequence[Any]] = None,
    ai_engine: Optional[Any] = None,
) -> List[List[str]]:
    """Compile prioritized, executive-grade AI infrastructure recommendations for runner fleet."""
    rows: List[List[str]] = []

    # 1. Queue SLA Breaches & Autoscaling
    if total_sla_breaches > 0:
        rows.append([
            "High",
            "Queue Latency",
            "Runner Fleet Capacity",
            f"{total_sla_breaches:,} workflow jobs breached the 60-second queue wait SLA during peak concurrency.",
            "Deploy auto-scaling runner pools (e.g., Actions Runner Controller on Kubernetes) to dynamically scale instances based on queue depth.",
            "Eliminates job queue bottlenecks, stabilizes CI build start times, and accelerates developer feedback.",
        ])
    else:
        rows.append([
            "Low",
            "Queue Latency",
            "Runner Fleet Capacity",
            "Self-hosted runner queue wait times are within healthy operating boundaries (<60s across jobs).",
            "Maintain current runner pool sizing and review concurrency metrics during peak sprint milestones.",
            "Guarantees predictable, low-latency workflow execution without provisioning excess idle compute.",
        ])

    # 2. Offline Runner Hygiene
    offline_runners = [str(r[2]) for r in runner_rows if len(r) > 9 and str(r[9]).strip().lower() == "offline"]
    if offline_runners:
        sample_str = ", ".join(offline_runners[:3])
        more = f" (+{len(offline_runners) - 3} more)" if len(offline_runners) > 3 else ""
        rows.append([
            "Medium",
            "Fleet Hygiene",
            f"{len(offline_runners)} Offline Runners",
            f"{len(offline_runners)} registered self-hosted runners are currently in 'Offline' status ({sample_str}{more}).",
            "Decommission dormant or unreachable runner registrations via GitHub REST API to eliminate orphaned agent records.",
            "Declutters pool inventory, prevents job routing errors, and tightens runner credential security.",
        ])
    elif runner_rows:
        rows.append([
            "Low",
            "Fleet Hygiene",
            "Registered Runners",
            "100% of registered self-hosted runners are currently online and responsive.",
            "Continue periodic health heartbeat monitoring and automated ephemeral provisioning.",
            "Ensures optimal runner pool availability and resource utilization.",
        ])

    # 3. Execution Failures & Job Timeouts
    timed_out = sum(r[17] for r in runner_rows if len(r) > 17 and isinstance(r[17], int))
    failed = sum(r[15] for r in runner_rows if len(r) > 15 and isinstance(r[15], int))
    if timed_out > 0:
        rows.append([
            "High",
            "Job Reliability",
            f"{timed_out} Timed Out Jobs",
            f"{timed_out} workflow jobs timed out on self-hosted runners ({failed} total failures).",
            "Configure granular 'timeout-minutes' on individual job steps, inspect long-running test suites, and clean disk caches.",
            "Prevents hung agent processes from exhausting runner worker threads and consuming compute quotas.",
        ])
    elif failed > 0:
        rows.append([
            "Medium",
            "Job Reliability",
            f"{failed} Failed Jobs",
            f"{failed} workflow jobs ended in failure across self-hosted runner infrastructure.",
            "Audit tool dependencies, container daemon availability, and runner workspace cleanup hooks.",
            "Improves build success consistency and avoids redundant workflow rerun cycles.",
        ])

    # 4. Ephemeral Runner Isolation Architecture
    static_count = sum(1 for r in runner_rows if len(r) > 8 and "ephemeral" not in str(r[8]).lower())
    if static_count > 0:
        rows.append([
            "Optimization",
            "Build Isolation",
            "Runner Architecture",
            f"{static_count} self-hosted runners use persistent/static host provisioning.",
            "Migrate runner fleet to ephemeral infrastructure where runner containers or VMs are destroyed after each job.",
            "Guarantees clean build environments, eliminates inter-job secret or artifact leakage, and simplifies patch management.",
        ])

    return rows


def write_runners_workbook(
    path: Path,
    cfg: RunnersConfig,
    since: datetime,
    end: datetime,
    runner_rows: List[list],
    org_runner_rows: List[list],
    failures: List[Tuple[str, str]],
    execution_duration: Optional[float] = None,
    ai_engine: Optional[Any] = None,
    org_runner_metrics: Optional[Dict[str, Any]] = None,
    job_rows: Optional[List[list]] = None,
    truncation_notes: Optional[List[Tuple[str, str]]] = None,
) -> None:
    """Generate executive Excel workbook for self-hosted runner fleet utilization."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 Actions Runner Fleet Performance & Utilization Report"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    since_local, end_local = since.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"

    total_runners = len(runner_rows)
    online_count = sum(1 for r in runner_rows if len(r) > 9 and str(r[9]).capitalize() == "Online")
    offline_count = sum(1 for r in runner_rows if len(r) > 9 and str(r[9]).capitalize() == "Offline")
    registered_count = online_count + offline_count
    busy_count = sum(1 for r in runner_rows if len(r) > 10 and str(r[10]).capitalize() == "Busy")
    ephemeral_count = sum(1 for r in runner_rows if len(r) > 8 and "Ephemeral" in str(r[8]))

    fleet_metrics: Optional[Any] = None
    if org_runner_metrics:
        from .models import RunnerJobMetrics
        fleet_metrics = RunnerJobMetrics()
        for om in org_runner_metrics.values():
            fleet_metrics.merge(om)

    activity_dates: List[Any] = []
    if fleet_metrics and fleet_metrics.first_job_time:
        activity_dates.append(fleet_metrics.first_job_time)
    for r in runner_rows:
        if len(r) > 27 and r[27] and r[27] not in ("-", "None", "No activity"):
            activity_dates.append(r[27])
    if job_rows:
        for j in job_rows:
            if len(j) > 12 and j[12] and j[12] not in ("-", "None", "No activity"):
                activity_dates.append(j[12])

    window_str = resolve_reporting_window(cfg.lookback_days, since, end, activity_dates)
    summary["A2"] = (
        f"Reporting Window: {window_str} | Generated: {end_local:{DATE_FMT} %H:%M} {LOCAL_TZ_NAME} | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    if fleet_metrics is not None:
        total_jobs_run = fleet_metrics.total_jobs
        total_compute_hours = fleet_metrics.total_compute_hours
        total_sla_breaches = fleet_metrics.queue_sla_breaches
        avg_queue_wait_raw = fleet_metrics.avg_queue_seconds
        p95_queue_wait_raw = fleet_metrics.p95_queue_seconds
        fleet_success_rate = fleet_metrics.success_rate
    else:
        source_rows = org_runner_rows if org_runner_rows else []
        if source_rows:
            total_jobs_run = sum(o[10] for o in source_rows if len(o) > 10 and isinstance(o[10], int))
            total_compute_hours = sum(o[16] for o in source_rows if len(o) > 16 and isinstance(o[16], (int, float)))
            total_sla_breaches = sum(o[19] for o in source_rows if len(o) > 19 and isinstance(o[19], int))
            weighted_avg_pairs = [
                (float(o[17]), int(o[10]))
                for o in source_rows
                if len(o) > 17 and isinstance(o[17], (int, float)) and isinstance(o[10], int) and o[10] > 0
            ]
            p95_vals = [float(o[18]) for o in source_rows if len(o) > 18 and isinstance(o[18], (int, float))]
            total_succ = sum(o[11] for o in source_rows if len(o) > 11 and isinstance(o[11], int))
            total_dec = sum(
                (o[11] if isinstance(o[11], int) else 0)
                + (o[12] if isinstance(o[12], int) else 0)
                + (o[14] if isinstance(o[14], int) else 0)
                for o in source_rows if len(o) > 14
            )
            fleet_success_rate = round(total_succ / total_dec * 100, 2) if total_dec else "N/A"
        else:
            total_jobs_run = sum(r[13] for r in runner_rows if len(r) > 13 and isinstance(r[13], int))
            total_compute_hours = sum(r[19] for r in runner_rows if len(r) > 19 and isinstance(r[19], (int, float)))
            total_sla_breaches = sum(r[26] for r in runner_rows if len(r) > 26 and isinstance(r[26], int))
            weighted_avg_pairs = [
                (float(r[24]), int(r[13]))
                for r in runner_rows
                if len(r) > 24 and isinstance(r[24], (int, float)) and isinstance(r[13], int) and r[13] > 0
            ]
            p95_vals = [float(r[25]) for r in runner_rows if len(r) > 25 and isinstance(r[25], (int, float))]
            total_succ = sum(r[14] for r in runner_rows if len(r) > 14 and isinstance(r[14], int))
            total_dec = sum(
                (r[14] if isinstance(r[14], int) else 0)
                + (r[15] if isinstance(r[15], int) else 0)
                + (r[17] if isinstance(r[17], int) else 0)
                for r in runner_rows if len(r) > 17
            )
            fleet_success_rate = round(total_succ / total_dec * 100, 2) if total_dec else "N/A"
        avg_queue_wait_raw = (
            round(sum(w * n for w, n in weighted_avg_pairs) / sum(n for _, n in weighted_avg_pairs), 2)
            if weighted_avg_pairs
            else "N/A"
        )
        p95_queue_wait_raw = round(max(p95_vals), 2) if p95_vals else "N/A"

    availability_display: Any = (
        round((online_count / registered_count * 100), 1)
        if registered_count
        else ("N/A (Ephemeral Only)" if total_runners else "N/A")
    )
    availability_pct = float(availability_display) if isinstance(availability_display, (int, float)) else 100.0
    avg_queue_wait_s = float(avg_queue_wait_raw) if isinstance(avg_queue_wait_raw, (int, float)) else 0.0
    p95_queue_wait_s = float(p95_queue_wait_raw) if isinstance(p95_queue_wait_raw, (int, float)) else 0.0

    if is_ai_disabled(ai_engine):
        advisory_title = "AI Fleet Advisory & Autoscaling Guidance (AI Not Enabled)"
        advisory_text = AI_NOT_ENABLED_MESSAGE
    elif is_ai_live(ai_engine):
        advisory_title = "AI Fleet Advisory & Autoscaling Guidance"
        advisory_text = ai_engine.generate_runner_fleet_advisory(
            total_runners=total_runners,
            availability_pct=availability_pct,
            busy_count=busy_count,
            avg_queue_wait_s=avg_queue_wait_s,
            p95_queue_wait_s=p95_queue_wait_s,
            sla_breaches=total_sla_breaches,
            ephemeral_count=ephemeral_count,
        )
    else:
        advisory_title = "Fleet Advisory & Autoscaling Guidance (Deterministic Policy)"
        advisory_text = (
            ai_engine.generate_runner_fleet_advisory(
                total_runners=total_runners,
                availability_pct=availability_pct,
                busy_count=busy_count,
                avg_queue_wait_s=avg_queue_wait_s,
                p95_queue_wait_s=p95_queue_wait_s,
                sla_breaches=total_sla_breaches,
                ephemeral_count=ephemeral_count,
            )
            if ai_engine is not None
            else (
                f"Queue latency breaches 60s SLA ({total_sla_breaches} jobs delayed, P95: {p95_queue_wait_s:.0f}s). Adopt Actions Runner Controller (ARC) with HorizontalRunnerAutoscaler to dynamically expand runner capacity during peak build windows."
                if (total_sla_breaches > 0 or p95_queue_wait_s > 60)
                else (
                    f"Persistent runner sprawl detected ({total_runners - ephemeral_count} non-ephemeral). Migrate to ephemeral container runner scale sets to ensure reproducible CI environments and prevent security cross-contamination."
                    if (ephemeral_count < (total_runners / 2) and total_runners > 0)
                    else "Runner fleet capacity is well-provisioned. Availability and queue latency remain within target SLAs."
                )
            )
        )

    real_failures = [f for f in failures if "truncated" not in str(f[0]).lower() and "truncated" not in str(f[1]).lower()]
    all_truncations = list(truncation_notes or []) + [f for f in failures if "truncated" in str(f[0]).lower() or "truncated" in str(f[1]).lower()]

    hosted_pools_count = sum(1 for r in runner_rows if len(r) > 9 and str(r[9]).capitalize() == "Hosted")
    self_hosted_count = total_runners - hosted_pools_count

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Monitored", len(org_runner_rows)],
        ["Discovered Self-Hosted Runners", self_hosted_count],
    ]
    if hosted_pools_count > 0:
        kpis.append(["GitHub-Hosted Runner Pools", hosted_pools_count])
    kpis.extend([
        ["Online Runners", online_count],
        ["Offline / Dormant Runners", offline_count],
        ["Fleet Availability (%)", availability_display],
        ["Currently Busy Runners", busy_count],
        ["Ephemeral (Autoscaled) Runners", ephemeral_count],
        ["Total Workflow Jobs Executed", total_jobs_run],
        ["Fleet Success Rate (%)", fleet_success_rate],
        ["Total Compute Consumed (Hours)", round(total_compute_hours, 2)],
        [
            "Avg / P95 Queue Wait (s)",
            f"{avg_queue_wait_raw}s / {p95_queue_wait_raw}s"
            if isinstance(avg_queue_wait_raw, (int, float))
            else "N/A",
        ],
        ["Queue SLA Breaches (>60s wait)", total_sla_breaches],
        ["Unprocessed Items / Errors", len(real_failures)],
    ])
    curr_row = write_table(summary, 4, ("Metric", "Fleet Aggregate"), kpis) + 2

    curr_row = section_title(summary, curr_row, advisory_title)
    summary.cell(curr_row, 1, safe_cell(advisory_text))
    summary.cell(curr_row, 1).font = Font(name="Calibri", size=10, italic=True)
    summary.row_dimensions[curr_row].height = 28
    curr_row += 1
    summary.cell(
        curr_row,
        1,
        "See dedicated 'AI Recommendations' sheet for actionable runner capacity, queue SLA, and fleet hygiene advisories.",
    )
    summary.cell(curr_row, 1).font = Font(name="Calibri", size=9, bold=True, color=NAVY)
    curr_row += 1
    summary.cell(curr_row, 1, AI_DISCLAIMER_NOTE)
    summary.cell(curr_row, 1).font = Font(name="Calibri", size=9, italic=True, color="595959")
    summary.row_dimensions[curr_row].height = 20
    curr_row += 2


    if real_failures:
        curr_row = section_title(summary, curr_row, "Items Requiring Review / Failures")
        curr_row = write_table(summary, curr_row, ("Target Item", "Issue Description"), [list(item) for item in real_failures]) + 2

    if all_truncations:
        curr_row = section_title(summary, curr_row, "Operational Notes / Run History Limits")
        note_summary = [
            [
                f"{pluralize(len(all_truncations), 'Repository', 'Repositories', include_count=True)} Reached Inspection Limit",
                f"Inspected latest {cfg.max_runner_runs_per_repo} runs per repo; earlier runs exist in window (increase --max-runs for full capture).",
            ]
        ]
        curr_row = write_table(summary, curr_row, ("Scope", "Operational Guidance"), note_summary) + 2

    autofit(summary, skip_rows=2)
    summary.sheet_view.showGridLines = False

    ai_rows = build_runner_ai_recommendations(
        runner_rows=runner_rows,
        org_runner_rows=org_runner_rows,
        total_sla_breaches=total_sla_breaches,
        failures=failures,
        all_truncations=all_truncations,
        ai_engine=ai_engine,
    )
    render_ai_recommendations_sheet(wb, ai_rows, title="AI Recommendations", index=1)

    org_ws = wb.create_sheet("Org Runner Summary")
    last_org = write_table(org_ws, 1, ORG_RUNNER_HEADERS, org_runner_rows)
    finalize_sheet(org_ws, 1, last_org, len(ORG_RUNNER_HEADERS), "B2")

    runner_ws = wb.create_sheet("Runner Fleet Detail")
    if runner_rows:
        last_runner = write_table(runner_ws, 1, RUNNER_HEADERS, runner_rows)
        finalize_sheet(runner_ws, 1, last_runner, len(RUNNER_HEADERS), "D2")

        status_col_idx = RUNNER_HEADERS.index("Status") + 1
        for r_idx in range(2, last_runner + 1):
            cell = runner_ws.cell(r_idx, status_col_idx)
            val = str(cell.value).capitalize()
            if val == "Online":
                cell.fill = PatternFill("solid", fgColor="C6EFCE")
                cell.font = Font(bold=True, color="006100")
            elif val == "Offline":
                cell.fill = PatternFill("solid", fgColor="FFC7CE")
                cell.font = Font(bold=True, color="9C0006")
            elif val == "Ephemeral":
                cell.fill = PatternFill("solid", fgColor="D9E1F2")
                cell.font = Font(bold=True, color="1F4E78")
            elif val == "Hosted":
                cell.fill = PatternFill("solid", fgColor="E2EFDA")
                cell.font = Font(bold=True, color="375623")
    else:
        write_empty_table(
            runner_ws,
            1,
            RUNNER_HEADERS,
            "No self-hosted runners found on target organizations.",
        )

    # Workflow Job History Detail Worksheet
    job_ws = wb.create_sheet("Workflow Job History")
    if job_rows:
        last_job = write_table(job_ws, 1, RUNNER_JOB_HEADERS, job_rows)
        finalize_sheet(job_ws, 1, last_job, len(RUNNER_JOB_HEADERS), "D2")

        concl_col_idx = RUNNER_JOB_HEADERS.index("Conclusion") + 1
        sla_col_idx = RUNNER_JOB_HEADERS.index("Queue SLA Breached (>60s)") + 1
        for r_idx in range(2, last_job + 1):
            c_cell = job_ws.cell(r_idx, concl_col_idx)
            c_val = str(c_cell.value).capitalize()
            if c_val == "Success":
                c_cell.fill = PatternFill("solid", fgColor="C6EFCE")
                c_cell.font = Font(bold=True, color="006100")
            elif c_val in ("Failure", "Timed_out"):
                c_cell.fill = PatternFill("solid", fgColor="FFC7CE")
                c_cell.font = Font(bold=True, color="9C0006")
            elif c_val == "Cancelled":
                c_cell.fill = PatternFill("solid", fgColor="E2EFDA")
                c_cell.font = Font(bold=True, color="375623")

            s_cell = job_ws.cell(r_idx, sla_col_idx)
            if str(s_cell.value).strip().lower() == "yes":
                s_cell.fill = PatternFill("solid", fgColor="FFEB9C")
                s_cell.font = Font(bold=True, color="9C5700")
    else:
        write_empty_table(
            job_ws,
            1,
            RUNNER_JOB_HEADERS,
            "No self-hosted workflow jobs executed within the lookback window.",
        )

    wb.save(path)
