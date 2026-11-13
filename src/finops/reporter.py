"""Excel reporter for Repo360 FinOps storage & artifact profiling."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..common.timezones import DATE_FMT, IST, LOCAL_TZ, LOCAL_TZ_NAME
from ..activity.reporter import (
    NAVY,
    autofit,
    finalize_sheet,
    safe_cell,
    section_title,
    write_empty_table,
    write_table,
)
from ..common.concurrency import AI_NOT_ENABLED_MESSAGE, is_ai_disabled
from .models import (
    FINOPS_ARTIFACT_HEADERS,
    FINOPS_BILLING_HEADERS,
    FINOPS_CACHE_PACKAGE_HEADERS,
    FINOPS_ORG_HEADERS,
    FINOPS_REPO_HEADERS,
    FINOPS_SUMMARY_HEADERS,
    FinOpsConfig,
    OrgBillingSummary,
    _round_mb,
)

OPTIMIZATION_STYLES = {
    "Optimized": ("C6EFCE", "006100"),
    "Moderate Waste": ("FFEB9C", "9C5700"),
    "High Waste": ("FFC7CE", "9C0006"),
}

SAVINGS_STYLES = {
    "Low": ("C6EFCE", "006100"),
    "Moderate": ("FFEB9C", "9C5700"),
    "High": ("FFC7CE", "9C0006"),
}


def write_finops_workbook(
    path: Path,
    cfg: FinOpsConfig,
    now: datetime,
    repo_rows: List[list],
    org_rows: List[list],
    artifact_rows: List[list],
    cache_package_rows: List[list],
    failures: List[Tuple[str, str]],
    execution_duration: Optional[float] = None,
    ai_engine: Optional[Any] = None,
    billing_rows: Optional[List[list]] = None,
    org_billing: Optional[Dict[str, OrgBillingSummary]] = None,
) -> None:
    """Generate high-fidelity executive FinOps workbook for Actions and repository storage."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    now_local = now.astimezone(LOCAL_TZ)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"

    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 FinOps Storage & Workflow Artifact Profiler"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    summary["A2"] = (
        f"Generated: {now_local:{DATE_FMT} %H:%M} {LOCAL_TZ_NAME} | "
        f"Organizations: {len(org_rows)} | Repositories: {len(repo_rows)} | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    total_disk_mb = _round_mb(sum(r[2] for r in org_rows if len(r) > 2 and isinstance(r[2], (int, float))))
    total_active_artifacts = sum(r[3] for r in org_rows if len(r) > 3 and isinstance(r[3], int))
    total_artifact_mb = _round_mb(sum(r[4] for r in org_rows if len(r) > 4 and isinstance(r[4], (int, float))))
    total_90d_count = sum(r[5] for r in org_rows if len(r) > 5 and isinstance(r[5], int))
    total_caches = sum(r[6] for r in org_rows if len(r) > 6 and isinstance(r[6], int))
    total_cache_mb = _round_mb(sum(r[7] for r in org_rows if len(r) > 7 and isinstance(r[7], (int, float))))
    total_packages = sum(r[8] for r in org_rows if len(r) > 8 and isinstance(r[8], int))
    total_package_mb = _round_mb(
        sum(
            r[4]
            for r in cache_package_rows
            if len(r) > 4 and str(r[2]).startswith("Package (") and isinstance(r[4], (int, float))
        )
    )
    if any(len(r) > 13 for r in org_rows):
        total_lfs_mb = _round_mb(sum(r[13] for r in org_rows if len(r) > 13 and isinstance(r[13], (int, float))))
    else:
        total_lfs_mb = _round_mb(
            sum(
                r[4]
                for r in cache_package_rows
                if len(r) > 4 and str(r[2]) == "Git LFS Object" and isinstance(r[4], (int, float))
            )
        )
    if any(len(r) > 14 for r in org_rows):
        total_release_mb = _round_mb(sum(r[14] for r in org_rows if len(r) > 14 and isinstance(r[14], (int, float))))
    else:
        total_release_mb = _round_mb(
            sum(
                r[4]
                for r in cache_package_rows
                if len(r) > 4 and str(r[2]) == "Release Asset" and isinstance(r[4], (int, float))
            )
        )
    total_90d_policy_repos = sum(r[12] for r in org_rows if len(r) > 12 and isinstance(r[12], int))
    total_footprint_mb = _round_mb(sum(r[9] for r in org_rows if len(r) > 9 and isinstance(r[9], (int, float))))
    total_waste_mb = _round_mb(sum(r[10] for r in org_rows if len(r) > 10 and isinstance(r[10], (int, float))))

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Monitored", len(org_rows)],
        ["Repositories Profiled", len(repo_rows)],
        ["Repositories with 90-Day Default Policy", total_90d_policy_repos],
        ["Repository Disk Footprint (MB)", total_disk_mb],
        ["Git LFS Storage Tracked (MB)", total_lfs_mb],
        ["Release Asset Storage Tracked (MB)", total_release_mb],
        ["Active Workflow Artifacts", total_active_artifacts],
        ["Active Artifact Storage (MB)", total_artifact_mb],
        ["Inefficient 90-Day Retention Artifacts", total_90d_count],
        ["Actions Caches Tracked", total_caches],
        ["Actions Cache Storage (MB)", total_cache_mb],
        ["Packages & Containers Tracked", total_packages],
        ["Package & Container Storage (MB)", total_package_mb],
        ["Total Storage Footprint (MB)", total_footprint_mb],
        ["Estimated Storage Waste / Savings Potential (MB)", total_waste_mb],
        ["Collection / Permission Gaps", len(failures)],
    ]
    if org_billing:
        has_billing = any(b.items for b in org_billing.values())
        if has_billing:
            compute_mins = round(sum(b.actions_compute_minutes for b in org_billing.values()), 2)
            gross_usd = round(sum(b.gross_amount_usd for b in org_billing.values()), 4)
            net_usd = round(sum(b.net_amount_usd for b in org_billing.values()), 4)
            kpis.extend([
                ["Actions Compute Minutes (Current Period)", compute_mins],
                ["Billing Gross Usage Value (USD)", gross_usd],
                ["Billing Net Billed Spend (USD)", net_usd],
            ])
            ytd_mins = round(sum(b.ytd_compute_minutes for b in org_billing.values()), 2)
            ytd_gross = round(sum(b.ytd_gross_amount_usd for b in org_billing.values()), 4)
            if ytd_mins > 0 or ytd_gross > 0:
                kpis.extend([
                    ["Actions Compute Minutes (YTD)", ytd_mins],
                    ["Billing Gross Usage Value (YTD USD)", ytd_gross],
                ])

    curr_row = write_table(summary, 4, ("Metric", "Fleet Aggregate"), kpis) + 2

    if is_ai_disabled(ai_engine):
        curr_row = section_title(summary, curr_row, "AI Cost & Storage Advisory (AI Not Enabled)")
        summary.cell(curr_row, 1, AI_NOT_ENABLED_MESSAGE)
        summary.cell(curr_row, 1).font = Font(name="Calibri", size=10, italic=True, color="595959")
        summary.row_dimensions[curr_row].height = 24
        curr_row += 2

    curr_row = section_title(summary, curr_row, "Organization Storage & Optimization Overview")
    curr_row = write_table(summary, curr_row, FINOPS_ORG_HEADERS, org_rows, bar_col=9) + 2

    status_col_zero = FINOPS_REPO_HEADERS.index("Optimization Status")
    waste_candidates = [
        r for r in repo_rows if len(r) > status_col_zero and r[status_col_zero] in ("High Waste", "Moderate Waste")
    ]
    if waste_candidates:
        curr_row = section_title(summary, curr_row, "Priority Cost & Storage Optimization Candidates")
        write_table(summary, curr_row, FINOPS_REPO_HEADERS, waste_candidates[:15])

    autofit(summary, skip_rows=2)
    summary.sheet_view.showGridLines = False

    repo_ws = wb.create_sheet("Storage by Repository")
    if repo_rows:
        last_repo = write_table(repo_ws, 1, FINOPS_REPO_HEADERS, repo_rows)
        finalize_sheet(repo_ws, 1, last_repo, len(FINOPS_REPO_HEADERS), "C2")

        status_col_idx = FINOPS_REPO_HEADERS.index("Optimization Status") + 1
        policy_col_idx = FINOPS_REPO_HEADERS.index("Repo Policy (Days)") + 1
        for r_idx in range(2, last_repo + 1):
            cell = repo_ws.cell(r_idx, status_col_idx)
            val = str(cell.value).strip()
            if val in OPTIMIZATION_STYLES:
                fill, font_col = OPTIMIZATION_STYLES[val]
                cell.fill = PatternFill("solid", fgColor=fill)
                cell.font = Font(bold=True, color=font_col)
            policy_cell = repo_ws.cell(r_idx, policy_col_idx)
            if isinstance(policy_cell.value, (int, float)) and policy_cell.value >= 85:
                policy_cell.fill = PatternFill("solid", fgColor="FFEB9C")
                policy_cell.font = Font(bold=True, color="9C5700")
    else:
        write_empty_table(repo_ws, 1, FINOPS_REPO_HEADERS, "No repositories found on target organizations.")

    art_ws = wb.create_sheet("Artifact Inventory")
    if artifact_rows:
        last_art = write_table(art_ws, 1, FINOPS_ARTIFACT_HEADERS, artifact_rows)
        finalize_sheet(art_ws, 1, last_art, len(FINOPS_ARTIFACT_HEADERS), "D2")
    else:
        write_empty_table(
            art_ws,
            1,
            FINOPS_ARTIFACT_HEADERS,
            "No workflow artifacts found on target repositories.",
        )

    cache_ws = wb.create_sheet("Cache & Package Inventory")
    if cache_package_rows:
        last_cp = write_table(cache_ws, 1, FINOPS_CACHE_PACKAGE_HEADERS, cache_package_rows)
        finalize_sheet(cache_ws, 1, last_cp, len(FINOPS_CACHE_PACKAGE_HEADERS), "D2")
    else:
        write_empty_table(
            cache_ws,
            1,
            FINOPS_CACHE_PACKAGE_HEADERS,
            "No Actions caches or packages found on target organizations.",
        )

    if billing_rows:
        bill_ws = wb.create_sheet("Billing & Compute Usage")
        last_bill = write_table(bill_ws, 1, FINOPS_BILLING_HEADERS, billing_rows)
        finalize_sheet(bill_ws, 1, last_bill, len(FINOPS_BILLING_HEADERS), "C2")

    if failures:
        gap_ws = wb.create_sheet("Collection & Permission Gaps")
        gap_rows = [[target, msg] for target, msg in failures]
        last_gap = write_table(gap_ws, 1, ("Target Scope", "API / Permission Gap Detail"), gap_rows)
        finalize_sheet(gap_ws, 1, last_gap, 2, "B2")

    wb.save(path)
