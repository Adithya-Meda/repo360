"""Excel reporter for Repo360 Push Protection & Ruleset Bypass Ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..activity.models import DATE_FMT, IST
from ..activity.reporter import (
    NAVY,
    autofit,
    finalize_sheet,
    safe_cell,
    section_title,
    write_empty_table,
    write_table,
)
from .models import (
    BYPASS_ACTOR_HEADERS,
    BYPASS_ORG_HEADERS,
    BYPASS_PUSH_PROTECTION_HEADERS,
    BYPASS_RULESET_HEADERS,
    BypassConfig,
    classify_bypass_risk,
)

RISK_TIER_STYLES = {
    "High": ("FFC7CE", "9C0006"),
    "Medium": ("FFEB9C", "9C5700"),
    "Unknown (Partial)": ("FFEB9C", "9C5700"),
    "Low": ("C6EFCE", "006100"),
}


def write_bypass_workbook(
    path: Path,
    cfg: BypassConfig,
    since: datetime,
    end: datetime,
    push_rows: List[list],
    ruleset_rows: List[list],
    org_rows: List[list],
    actor_rows: List[list],
    failures: List[Tuple[str, str]],
    execution_duration: Optional[float] = None,
) -> None:
    """Generate high-fidelity executive compliance workbook for security and policy bypasses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    since_ist, end_ist = since.astimezone(IST), end.astimezone(IST)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"

    # Sheet 1: Executive Summary
    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 Consolidated Push Protection & Ruleset Bypass Ledger"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    summary["A2"] = (
        f"Reporting Window: {since_ist:{DATE_FMT}} to {end_ist:{DATE_FMT}} ({cfg.lookback_days} days) | "
        f"Generated: {end_ist:{DATE_FMT} %H:%M} IST | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    total_push = len(push_rows)
    total_ruleset = len(ruleset_rows)
    total_incidents = total_push + total_ruleset
    unique_actors = len(actor_rows)
    repos_with_bypasses = len(
        {(str(r[0]).casefold(), str(r[1]).casefold()) for r in push_rows}
        | {(str(r[0]).casefold(), str(r[1]).casefold()) for r in ruleset_rows}
    )

    risk_tier = classify_bypass_risk(total_push, total_incidents)
    if risk_tier == "High":
        compliance_posture = "Critical Concern (High Bypass Volume)"
    elif risk_tier == "Medium":
        compliance_posture = "Review Required (Moderate Bypasses)"
    elif failures:
        compliance_posture = f"Partial Coverage ({len(failures)} Unaudited / Permission Gap(s))"
    else:
        compliance_posture = "Satisfactory (No Bypasses)"

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Audited", len(org_rows)],
        ["Repositories with Bypasses", repos_with_bypasses],
        ["Total Bypass Incidents", total_incidents],
        ["Push Protection Secret Bypasses", total_push],
        ["Branch Ruleset Policy Bypasses", total_ruleset],
        ["Unique Bypassing Actors", unique_actors],
        ["Unaudited / API Permission Gaps", len(failures)],
        ["Enterprise Compliance Posture", compliance_posture],
    ]
    curr_row = write_table(summary, 4, ("Metric", "Fleet Aggregate"), kpis) + 2

    curr_row = section_title(summary, curr_row, "Organization Bypass & Compliance Summary")
    curr_row = write_table(summary, curr_row, BYPASS_ORG_HEADERS, org_rows, bar_col=4) + 2

    if actor_rows:
        curr_row = section_title(summary, curr_row, "Top Bypassing Actors Across Enterprise")
        write_table(summary, curr_row, BYPASS_ACTOR_HEADERS, actor_rows[:15])

    autofit(summary, skip_rows=2)
    summary.sheet_view.showGridLines = False

    if failures:
        gap_ws = wb.create_sheet("Unaudited & Permission Gaps")
        gap_rows = [[target, msg] for target, msg in failures]
        last_gap = write_table(gap_ws, 1, ("Target Scope", "API / Permission Gap Detail"), gap_rows)
        finalize_sheet(gap_ws, 1, last_gap, 2, "B2")

    # Sheet 2: Push Protection Bypasses
    push_ws = wb.create_sheet("Push Protection Bypasses")
    if push_rows:
        last_push = write_table(push_ws, 1, BYPASS_PUSH_PROTECTION_HEADERS, push_rows)
        finalize_sheet(push_ws, 1, last_push, len(BYPASS_PUSH_PROTECTION_HEADERS), "D2")

        risk_col_idx = BYPASS_PUSH_PROTECTION_HEADERS.index("AI Risk Assessment") + 1
        for r_idx in range(2, last_push + 1):
            cell = push_ws.cell(r_idx, risk_col_idx)
            val = str(cell.value).strip()
            if "Critical" in val or "High" in val:
                cell.fill = PatternFill("solid", fgColor="FFC7CE")
                cell.font = Font(bold=True, color="9C0006")
            elif "Moderate" in val:
                cell.fill = PatternFill("solid", fgColor="FFEB9C")
                cell.font = Font(bold=True, color="9C5700")
            elif "Low" in val:
                cell.fill = PatternFill("solid", fgColor="C6EFCE")
                cell.font = Font(bold=True, color="006100")
    else:
        write_empty_table(
            push_ws,
            1,
            BYPASS_PUSH_PROTECTION_HEADERS,
            "No secret scanning push protection bypasses detected within the lookback window.",
        )

    # Sheet 3: Ruleset Bypasses
    rule_ws = wb.create_sheet("Ruleset Bypasses")
    if ruleset_rows:
        last_rule = write_table(rule_ws, 1, BYPASS_RULESET_HEADERS, ruleset_rows)
        finalize_sheet(rule_ws, 1, last_rule, len(BYPASS_RULESET_HEADERS), "C2")
    else:
        write_empty_table(
            rule_ws,
            1,
            BYPASS_RULESET_HEADERS,
            "No branch ruleset bypasses detected within the lookback window.",
        )

    # Sheet 4: Actor Accountability Ledger
    actor_ws = wb.create_sheet("Actor Accountability Ledger")
    if actor_rows:
        last_act = write_table(actor_ws, 1, BYPASS_ACTOR_HEADERS, actor_rows)
        finalize_sheet(actor_ws, 1, last_act, len(BYPASS_ACTOR_HEADERS), "C2")
    else:
        write_empty_table(
            actor_ws,
            1,
            BYPASS_ACTOR_HEADERS,
            "No bypassing actors identified within the lookback window.",
        )

    wb.save(path)
