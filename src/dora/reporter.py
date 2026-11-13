"""Excel reporter for Repo360 DORA & Flow Metrics."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..common.timezones import DATE_FMT, IST, LOCAL_TZ, LOCAL_TZ_NAME
from ..activity.reporter import (
    NAVY,
    autofit,
    finalize_sheet,
    section_title,
    write_empty_table,
    write_table,
)
from ..common.concurrency import AI_NOT_ENABLED_MESSAGE, is_ai_disabled
from .models import (
    DORA_DEPLOYMENT_HEADERS,
    DORA_ORG_HEADERS,
    DORA_RECOVERY_HEADERS,
    DORA_REPO_HEADERS,
    DORAConfig,
    compute_fleet_dora_summary,
)

DORA_TIER_STYLES = {
    "Elite": ("C6EFCE", "006100"),
    "High": ("E2EFDA", "375623"),
    "Medium": ("FFF2CC", "833C0C"),
    "Low": ("FFC7CE", "9C0006"),
    "N/A": ("F2F2F2", "595959"),
}

STATUS_STYLES = {
    "SUCCESS": ("C6EFCE", "006100"),
    "FAILURE": ("FFC7CE", "9C0006"),
    "IN_PROGRESS": ("FFF2CC", "833C0C"),
    "INACTIVE": ("F2F2F2", "595959"),
    "Resolved": ("C6EFCE", "006100"),
    "Open": ("FFC7CE", "9C0006"),
}


def _apply_cell_badge(
    ws,
    start_row: int,
    last_row: int,
    col_idx: int,
    styles: Dict[str, Tuple[str, str]],
) -> None:
    """Apply semantic fill and bold font styling to cells matching badge keys."""
    for r_idx in range(start_row, last_row + 1):
        cell = ws.cell(r_idx, col_idx)
        val = str(cell.value).strip()
        if val in styles:
            fill, font_col = styles[val]
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.font = Font(bold=True, color=font_col)


def write_dora_workbook(
    path: Path,
    cfg: DORAConfig,
    since: datetime,
    end: datetime,
    repo_rows: List[list],
    org_rows: List[list],
    deployment_rows: List[list],
    recovery_rows: List[list],
    failures: List[Tuple[str, str]],
    execution_duration: Optional[float] = None,
    ai_engine: Optional[Any] = None,
) -> None:
    """Generate executive DORA delivery and recovery performance workbook."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    since_local, end_local = since.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"

    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 DORA & Engineering Delivery Flow Metrics"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    summary["A2"] = (
        f"Reporting Window: {since_local:{DATE_FMT}} to {end_local:{DATE_FMT}} ({cfg.lookback_days} days) | "
        f"Generated: {end_local:{DATE_FMT} %H:%M} {LOCAL_TZ_NAME} | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    summary_data = compute_fleet_dora_summary(
        repo_rows,
        cfg.lookback_days,
        deployment_rows=deployment_rows,
        recovery_rows=recovery_rows,
    )
    tier_counts = summary_data["tier_counts"]
    cfr_str = f"{summary_data['cfr_overall']}%" if summary_data["cfr_overall"] is not None else "N/A"
    rework_str = (
        f"{summary_data['rework_overall']}%" if summary_data["rework_overall"] is not None else "N/A"
    )
    df_median_val = (
        summary_data["median_df_weekly"] if summary_data["median_df_weekly"] is not None else "N/A"
    )
    lttc_median_val = (
        summary_data["median_lttc_hours"] if summary_data["median_lttc_hours"] is not None else "N/A"
    )
    lttc_cov_str = (
        f"{summary_data['lttc_coverage_pct']}%"
        if summary_data.get("lttc_coverage_pct") is not None
        else "N/A"
    )
    mttr_median_val = (
        summary_data["median_mttr_hours"] if summary_data["median_mttr_hours"] is not None else "N/A"
    )

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Monitored", len(org_rows)],
        ["Repositories Scanned", summary_data["total_repos"]],
        ["Active Deploying Repositories", summary_data["active_repos"]],
        ["Inactive Repositories (N/A)", summary_data["inactive_repos"]],
        ["Fleet Overall DORA Performance Tier", summary_data["fleet_tier"]],
        ["Total Completed Deployments & Releases", summary_data["total_deploys"]],
        ["Successful Deployments", summary_data["total_successful"]],
        ["Code-Change Deployments (Unique Changes)", summary_data["code_change_deploys"]],
        ["Failed Deployments (Direct)", summary_data["total_failed_deploys"]],
        ["Effective Failed Changes (Inc. Incidents/Hotfixes)", summary_data["effective_failed"]],
        ["Open Unresolved Incidents", summary_data["open_incidents"]],
        ["Fleet Median Deployment Frequency (/week)", df_median_val],
        ["Fleet Total Deployment Throughput (/week)", summary_data["total_df_weekly"]],
        ["Fleet Median Lead Time for Changes (Hours)", lttc_median_val],
        ["LTTC Signal Coverage (%)", lttc_cov_str],
        ["Fleet Change Failure Rate (%)", cfr_str],
        ["Fleet Median Failed Deployment Recovery Time (Hours)", mttr_median_val],
        ["Fleet Deployment Rework Rate (%)", rework_str],
        ["Elite Tier Repositories", tier_counts.get("Elite", 0)],
        ["High Tier Repositories", tier_counts.get("High", 0)],
        ["Medium Tier Repositories", tier_counts.get("Medium", 0)],
        ["Low Tier Repositories", tier_counts.get("Low", 0)],
        ["Inactive / N/A Repositories", tier_counts.get("N/A", 0)],
    ]
    if summary_data.get("no_op_deploys", 0) > 0:
        kpis.insert(
            9,
            ["No-Op / Same-SHA Redeploys (Excluded DF)", summary_data["no_op_deploys"]],
        )
    if summary_data.get("collapsed_retries", 0) > 0:
        kpis.insert(
            12 if summary_data.get("no_op_deploys", 0) > 0 else 11,
            ["Collapsed Workflow Failure Retries (CFR)", summary_data["collapsed_retries"]],
        )
    if summary_data.get("incomplete_deploys", 0) > 0:
        kpis.append(
            ["Pending / Incomplete Deployments (Excluded)", summary_data["incomplete_deploys"]],
        )
    if summary_data.get("stalled_deploys", 0) > 0:
        kpis.append(
            ["Stalled Deployments (>24h IN_PROGRESS)", summary_data["stalled_deploys"]],
        )
    if summary_data["permission_gap_repos"] > 0:
        kpis.append(["Deployments API Permission Gap Repos", summary_data["permission_gap_repos"]])

    curr_row = write_table(summary, 4, ("Metric", "Fleet Aggregate"), kpis) + 2

    curr_row = section_title(summary, curr_row, "Organization DORA Performance Overview")
    org_last_row = write_table(summary, curr_row, DORA_ORG_HEADERS, org_rows, bar_col=4)
    curr_row = org_last_row + 2

    env_scope = (
        "All environments & branches (--include-non-prod)"
        if cfg.include_non_prod
        else f"Production environments ({', '.join(cfg.prod_environments)}) & default/release branches"
    )
    pr_lttc_count = summary_data.get("pr_lttc_count", 0)
    commit_lttc_count = summary_data.get("commit_lttc_count", 0)
    methodology_rows = [
        ["Production Scope", env_scope],
        [
            "Deployment Frequency",
            "Unique value-delivering code-change deployments per week per active repo (same-SHA/zero-diff cron rebuilds excluded; 0-change or <3 multi-week repos capped at Medium).",
        ],
        [
            "Lead Time for Changes",
            "Hours from commit authored/pushed or associated PR creation (prioritized on single-commit squash merges) to completion (<24h Elite; skips failed/in-progress/no-diff runs).",
        ],
        [
            "Change Failure Rate",
            "Unique failed production changes divided by deduplicated completed deployments (consecutive same-SHA retry bursts collapsed; <=5% Elite, <=10% High, <=20% Medium, >20% Low).",
        ],
        [
            "Recovery Time (MTTR)",
            f"Stream-scoped failure-to-next-success recovery plus resolved incident issues (timestamps in {LOCAL_TZ_NAME}; Ledger includes Completed/Updated timestamp). Open unresolved incidents cap tier at Medium.",
        ],
        [
            "Deployment Rework Rate",
            "Percentage of deduplicated deployments matched to revert, rollback, hotfix, or emergency remediation work.",
        ],
        [
            "Telemetry Coverage",
            (
                f"LTTC coverage: {lttc_cov_str} "
                f"({pr_lttc_count} PR-linked squash/PR cycle, {commit_lttc_count} commit-linked); "
                "repositories without labeled GitHub incident issues rely on pipeline-only recovery signals."
            ),
        ],
    ]
    if summary_data["permission_gap_repos"] > 0:
        methodology_rows.append(
            [
                "Permission Gap Notice",
                (
                    f"{summary_data['permission_gap_repos']} repository(ies) returned 403 on /deployments; "
                    "grant GitHub App 'deployments: read' permission via 'repo360 setup' for full deployment signals."
                ),
            ]
        )
    else:
        methodology_rows.append(
            [
                "Native GitHub Signals",
                "Uses GitHub Deployments, Releases, Production Workflow Runs, Pull Requests, and Incident Issues only.",
            ]
        )

    curr_row = section_title(summary, curr_row, "Native GitHub Signal Methodology & Scope")
    if is_ai_disabled(ai_engine):
        methodology_rows.append(["AI Flow Advisory", AI_NOT_ENABLED_MESSAGE])
    curr_row = write_table(summary, curr_row, ("Metric", "Methodology & Compliance Scope"), methodology_rows) + 2

    if failures:
        curr_row = section_title(summary, curr_row, "Collection Errors & Permission Gaps")
        failure_rows = [[target, reason] for target, reason in failures]
        curr_row = write_table(
            summary, curr_row, ("Target Repository", "Error / Permission Detail"), failure_rows
        ) + 2

    autofit(summary, skip_rows=2)
    summary.sheet_view.showGridLines = False

    repo_ws = wb.create_sheet("Repository DORA Scorecard")
    if repo_rows:
        last_repo = write_table(repo_ws, 1, DORA_REPO_HEADERS, repo_rows)
        finalize_sheet(repo_ws, 1, last_repo, len(DORA_REPO_HEADERS), "C2")

        for header_name in ("DORA Tier", "CFR Tier", "Rework Tier"):
            col_idx = DORA_REPO_HEADERS.index(header_name) + 1
            _apply_cell_badge(repo_ws, 2, last_repo, col_idx, DORA_TIER_STYLES)
    else:
        write_empty_table(repo_ws, 1, DORA_REPO_HEADERS, "No repositories found on target organizations.")

    dep_ws = wb.create_sheet("Deployment & Release Ledger")
    if deployment_rows:
        last_dep = write_table(dep_ws, 1, DORA_DEPLOYMENT_HEADERS, deployment_rows)
        finalize_sheet(dep_ws, 1, last_dep, len(DORA_DEPLOYMENT_HEADERS), "D2")

        status_col_idx = DORA_DEPLOYMENT_HEADERS.index("Status") + 1
        _apply_cell_badge(dep_ws, 2, last_dep, status_col_idx, STATUS_STYLES)
    else:
        write_empty_table(
            dep_ws,
            1,
            DORA_DEPLOYMENT_HEADERS,
            "No deployment or release events recorded within the lookback window.",
        )

    rec_ws = wb.create_sheet("Recovery & MTTR Ledger")
    if recovery_rows:
        last_rec = write_table(rec_ws, 1, DORA_RECOVERY_HEADERS, recovery_rows)
        finalize_sheet(rec_ws, 1, last_rec, len(DORA_RECOVERY_HEADERS), "C2")

        rec_status_col_idx = DORA_RECOVERY_HEADERS.index("Status") + 1
        _apply_cell_badge(rec_ws, 2, last_rec, rec_status_col_idx, STATUS_STYLES)
    else:
        write_empty_table(
            rec_ws,
            1,
            DORA_RECOVERY_HEADERS,
            "No failure recovery incidents recorded within the lookback window.",
        )

    wb.save(path)
