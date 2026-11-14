"""Excel reporter for Repo360 Push Protection & Ruleset Bypass Ledger."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from ..common.timezones import DATE_FMT, LOCAL_TZ, LOCAL_TZ_NAME
from ..activity.reporter import (
    NAVY,
    autofit,
    finalize_sheet,
    section_title,
    write_empty_table,
    write_table,
)
from ..common.concurrency import AI_NOT_ENABLED_MESSAGE, is_ai_disabled
from ..common.text import pluralize
from .models import (
    BYPASS_ACTOR_HEADERS,
    BYPASS_ORG_HEADERS,
    BYPASS_PUSH_PROTECTION_HEADERS,
    BYPASS_RULESET_HEADERS,
    BypassConfig,
    classify_bypass_risk,
    is_plan_limitation,
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
    ai_engine: Optional[Any] = None,
) -> None:
    """Generate high-fidelity executive compliance workbook for security and policy bypasses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    since_local, end_local = since.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"

    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 Consolidated Push Protection & Ruleset Bypass Ledger"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    window_str = (
        f"Reporting Window: Full History (All-Time) as of {end_local:{DATE_FMT}}"
        if cfg.lookback_days == 0
        else f"Reporting Window: {since_local:{DATE_FMT}} to {end_local:{DATE_FMT}} ({cfg.lookback_days} days)"
    )
    summary["A2"] = (
        f"{window_str} | "
        f"Generated: {end_local:{DATE_FMT} %H:%M} {LOCAL_TZ_NAME} | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    total_push = len(push_rows)
    total_ruleset = len(ruleset_rows)
    total_org_ruleset = sum(1 for r in ruleset_rows if len(r) > 5 and str(r[5]).lower() == "organization")
    total_repo_ruleset = total_ruleset - total_org_ruleset
    total_incidents = total_push + total_ruleset
    unique_actors = len(actor_rows)
    repos_with_bypasses = len(
        {(str(r[0]).casefold(), str(r[1]).casefold()) for r in push_rows}
        | {(str(r[0]).casefold(), str(r[1]).casefold()) for r in ruleset_rows}
    )

    real_failures = [f for f in failures if not is_plan_limitation(f[1])]
    plan_limitations = [f for f in failures if is_plan_limitation(f[1])]

    risk_tier = classify_bypass_risk(total_push, total_incidents)
    if risk_tier == "High":
        compliance_posture = "Critical Concern (High Bypass Volume)"
    elif risk_tier == "Medium":
        compliance_posture = "Review Required (Moderate Bypasses)"
    elif real_failures:
        compliance_posture = f"Partial Coverage ({pluralize(len(real_failures), 'Unaudited / Permission Gap', include_count=True)})"
    elif total_incidents > 0:
        compliance_posture = "Low Risk (Minor Policy Overrides Detected)"
    elif plan_limitations:
        compliance_posture = "Satisfactory (No Bypasses; Branch Rulesets Plan-Restricted on Private Repos)"
    else:
        compliance_posture = "Satisfactory (No Bypasses)"

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Audited", len(org_rows)],
        ["Repositories with Bypasses", repos_with_bypasses],
        ["Total Bypass Incidents", total_incidents],
        ["Push Protection Secret Bypasses", total_push],
        ["Total Ruleset Policy Bypasses", total_ruleset],
        ["  ↳ Organization Rulesets", total_org_ruleset],
        ["  ↳ Repository Rulesets", total_repo_ruleset],
        ["Unique Bypassing Actors", unique_actors],
        ["Unaudited / API Permission Gaps", len(real_failures)],
    ]
    if plan_limitations:
        kpis.append(["GitHub Plan Tier Limitations", len(plan_limitations)])
    kpis.append(["Enterprise Compliance Posture", compliance_posture])
    curr_row = write_table(summary, 4, ("Metric", "Fleet Aggregate"), kpis) + 2

    if is_ai_disabled(ai_engine):
        curr_row = section_title(summary, curr_row, "AI Security Advisory (AI Not Enabled)")
        summary.cell(curr_row, 1, AI_NOT_ENABLED_MESSAGE)
        summary.cell(curr_row, 1).font = Font(name="Calibri", size=10, italic=True, color="595959")
        summary.row_dimensions[curr_row].height = 24
        curr_row += 2

    curr_row = section_title(summary, curr_row, "Organization Bypass & Compliance Summary")
    curr_row = write_table(summary, curr_row, BYPASS_ORG_HEADERS, org_rows, bar_col=6) + 2

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
            elif "AI Not Enabled" in val:
                cell.fill = PatternFill("solid", fgColor="E0E0E0")
                cell.font = Font(italic=True, color="595959")
    else:
        write_empty_table(
            push_ws,
            1,
            BYPASS_PUSH_PROTECTION_HEADERS,
            "No secret scanning push protection bypasses detected within the lookback window.",
        )

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
