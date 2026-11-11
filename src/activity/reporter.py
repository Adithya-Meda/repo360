"""Excel reporter for Repo360 user activity analysis."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..common.concurrency import is_ai_live
from .models import (
    DATE_FMT,
    IST,
    METRIC_LABELS,
    METRICS,
    ORG_HEADERS,
    REPO_HEADERS,
    STATUS_IDX,
    STATUSES,
    TOTAL_IDX,
    USER_HEADERS,
    ActivityConfig,
)

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
NAVY = "1F3864"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(bold=True, color="FFFFFF")
BAND_FILL = PatternFill("solid", fgColor="F2F2F2")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")

STATUS_STYLES = {
    "Active": ("C6EFCE", "006100"),
    "Moderate": ("FFEB9C", "9C5700"),
    "Low": ("FCE4D6", "C65911"),
    "Inactive": ("FFC7CE", "9C0006"),
}

EMPTY_CELL_MESSAGES = {
    "Average Commits per Active Month": "No months",
    "First Activity (IST)": "No activity",
    "Last Activity (IST)": "No activity",
    "Days Since Last Activity": "No activity",
}

LEFT_HEADERS = {
    "Metric",
    "Scope",
    "Category",
    "Organization",
    "Repository",
    "User",
    "Definition",
    "Issue Description",
    "Target Item",
    "Teams",
    "CODEOWNERS Path",
    "What this means",
    "Topic",
    "Runner Name",
    "Last Job Name",
    "Labels",
    "Group",
}

CENTER_HEADERS = {
    "Value",
    "Fleet Aggregate",
    "Activity Status",
    "Org Member",
    "Active Days",
    "Repositories Contributed",
    "Rank",
    "Coverage",
    "Protected Branches",
    "CODEOWNERS Present",
    "New in Period",
    "Archived",
    "Activity Last 7 Days",
    "Visibility",
    "Created (IST)",
    "First Activity (IST)",
    "Last Activity (IST)",
    "First Seen (IST)",
    "Days Since Last Activity",
    "Average Commits per Active Month",
    "PR Cohort Merge Rate (%)",
    "Avg First Review Hours",
    "First Review Sample PRs",
    "Status",
    "Busy",
    "Ephemeral",
    "JIT",
    "OS",
    "Architecture",
    "Runner Version",
}


def safe_cell(value: Any, header: Optional[str] = None) -> Any:
    """Sanitize spreadsheet cell values against formula injection and fill defaults."""
    if (value is None or value == "") and header in EMPTY_CELL_MESSAGES:
        value = EMPTY_CELL_MESSAGES[header]
    if isinstance(value, str):
        val_strip = value.strip()
        if val_strip in ("-", "--", "---", "N/A", "None", ""):
            return value
        if value.startswith(("=", "@", "\t", "\r")):
            return f"'{value}"
        if value.startswith(("+", "-")) and len(val_strip) > 1 and not val_strip.lstrip("+-").replace(".", "", 1).isdigit():
            return f"'{value}"
    return value


def write_empty_table(ws, top: int, headers: Tuple[str, ...], message: str) -> None:
    """Write table headers and a clean, elegant empty-state notice without trailing dashes."""
    for col_idx, header in enumerate(headers, start=1):
        cell = ws.cell(row=top, column=col_idx, value=header)
        cell.fill, cell.font, cell.border = HEADER_FILL, HEADER_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    notice_row = top + 1
    cell = ws.cell(row=notice_row, column=1, value=message)
    cell.font = Font(italic=True, color="595959")
    cell.alignment = Alignment(horizontal="left", vertical="center")
    cell.border = BORDER

    for col_idx in range(2, len(headers) + 1):
        c = ws.cell(row=notice_row, column=col_idx, value="")
        c.border = BORDER

    ws.auto_filter.ref = f"A{top}:{get_column_letter(len(headers))}{top}"
    ws.row_dimensions[top].height = 28
    ws.row_dimensions[notice_row].height = 22
    ws.page_setup.orientation = "landscape"
    autofit(ws)


def write_table(
    ws,
    top: int,
    headers: Tuple[str, ...],
    rows: List[list],
    status_col: Optional[int] = None,
    bar_col: Optional[int] = None,
) -> int:
    """Write styled data table to worksheet with header fill, borders, and zebra striping."""
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=top, column=col, value=header)
        cell.fill, cell.font, cell.border = HEADER_FILL, HEADER_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row_idx, row in enumerate(rows, start=top + 1):
        banded = (row_idx - top) % 2 == 0
        for col_idx, value in enumerate(row, start=1):
            header = headers[col_idx - 1] if col_idx - 1 < len(headers) else ""
            cell = ws.cell(row=row_idx, column=col_idx, value=safe_cell(value, header))
            cell.border = BORDER
            if banded:
                cell.fill = BAND_FILL
            val_str = str(value).strip() if value is not None else ""
            if header in LEFT_HEADERS:
                cell.alignment = Alignment(horizontal="left", vertical="center")
            elif (
                isinstance(value, (int, float))
                or header in CENTER_HEADERS
                or val_str in ("Yes", "No", "Unknown", "None", "Complete", "Partial", "N/A", "Online", "Offline", "Idle", "Busy")
                or value in EMPTY_CELL_MESSAGES.values()
            ):
                cell.alignment = CENTER

        if status_col is not None and row[status_col] in STATUS_STYLES:
            fill, font = STATUS_STYLES[row[status_col]]
            cell = ws.cell(row=row_idx, column=status_col + 1)
            cell.fill, cell.font, cell.alignment = (
                PatternFill("solid", fgColor=fill),
                Font(bold=True, color=font),
                CENTER,
            )

    last_row = top + len(rows)
    if bar_col is not None and rows:
        letter = get_column_letter(bar_col + 1)
        ws.conditional_formatting.add(
            f"{letter}{top + 1}:{letter}{last_row}",
            DataBarRule(start_type="min", end_type="max", color="5B9BD5"),
        )
    return last_row


def autofit(ws, skip_rows: int = 0) -> None:
    """Automatically fit column widths based on content length."""
    widths: Dict[int, int] = defaultdict(int)
    for row in ws.iter_rows(min_row=skip_rows + 1):
        for cell in row:
            if cell.value is not None:
                widths[cell.column] = max(widths[cell.column], len(str(cell.value)))
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max(width + 3, 12), 48)


def finalize_sheet(ws, header_row: int, last_row: int, columns: int, freeze: str) -> None:
    """Configure auto-filters, freeze panes, and print setup for professional delivery."""
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(columns)}{max(last_row, header_row)}"
    ws.freeze_panes = freeze
    ws.row_dimensions[header_row].height = 28
    ws.page_setup.orientation = "landscape"
    autofit(ws)


def section_title(ws, row: int, title: str) -> int:
    """Insert section header banner."""
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = Font(bold=True, size=12, color=NAVY)
    return row + 1


def write_activity_workbook(
    path: Path,
    cfg: ActivityConfig,
    since: datetime,
    end: datetime,
    user_rows: List[list],
    repo_rows: List[list],
    org_rows: List[list],
    failures: List[Tuple[str, str]],
    repos_scanned: int,
    management_tables: Optional[Dict[str, Tuple[Tuple[str, ...], List[list]]]] = None,
    execution_duration: Optional[float] = None,
    ai_engine: Optional[Any] = None,
) -> None:
    """Generate high-fidelity executive Excel workbook for user activity and adoption trends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()

    # Sheet 1: Executive Summary
    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 Contributor & Platform Activity Intelligence"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    since_ist, end_ist = since.astimezone(IST), end.astimezone(IST)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"
    summary["A2"] = (
        f"Reporting Window: {since_ist:{DATE_FMT}} to {end_ist:{DATE_FMT}} ({cfg.lookback_days} days) "
        f"| Generated: {end_ist:{DATE_FMT} %H:%M} IST | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    status_counts = Counter(row[STATUS_IDX] for row in user_rows)
    total_activities = sum(row[TOTAL_IDX] for row in user_rows)
    active_contributors = sum(1 for r in user_rows if r[TOTAL_IDX] > 0)
    total_commits = sum(row[TOTAL_IDX + 1] for row in user_rows) if user_rows else 0
    prs_opened = sum(row[TOTAL_IDX + 2] for row in user_rows) if user_rows else 0
    prs_merged = sum(row[TOTAL_IDX + 3] for row in user_rows) if user_rows else 0
    org_names = ", ".join(str(r[0]) for r in org_rows) if org_rows else "Monitored Fleet"

    advisory_text = (
        ai_engine.generate_activity_insights(
            org_name=org_names,
            total_contributors=len(user_rows),
            active_contributors=active_contributors,
            total_activities=total_activities,
            total_commits=total_commits,
            prs_opened=prs_opened,
            prs_merged=prs_merged,
        )
        if ai_engine is not None
        else f"Engineering activity across {org_names}: {active_contributors} active contributors ({total_activities} actions, {prs_merged} PRs merged). Maintain balanced code review assignments to prevent reviewer fatigue."
    )

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Scanned", len(org_rows)],
        ["Repositories Scanned", repos_scanned],
        ["Active Contributors", active_contributors],
        ["Total Users Reported", len(user_rows)],
        *([f"{s} Users", status_counts[s]] for s in STATUSES),
        ["Total Activities", total_activities],
        *([label, sum(row[TOTAL_IDX + 1 + i] for row in user_rows)] for i, label in enumerate(METRIC_LABELS)),
        ["Unprocessed Items", len(failures)],
    ]
    curr_row = write_table(summary, 4, ("Metric", "Value"), kpis) + 2

    advisory_title = (
        "AI Contributor Cadence & Collaboration Summary"
        if is_ai_live(ai_engine)
        else "Contributor Cadence & Collaboration Summary (Deterministic Policy)"
    )
    curr_row = section_title(summary, curr_row, advisory_title)
    summary.cell(curr_row, 1, advisory_text)
    summary.cell(curr_row, 1).font = Font(name="Calibri", size=10, italic=True)
    summary.row_dimensions[curr_row].height = 28
    curr_row += 2

    curr_row = section_title(summary, curr_row, "Organization Governance & Activity Overview")
    curr_row = write_table(summary, curr_row, ORG_HEADERS, org_rows, bar_col=len(ORG_HEADERS) - 1) + 2

    curr_row = section_title(summary, curr_row, "Activity Threshold Definitions")
    definitions = [
        ["Active", f"Contributed on {cfg.active_min_days} or more distinct days"],
        ["Moderate", f"Contributed on {cfg.moderate_min_days} to {cfg.active_min_days - 1} distinct days"],
        ["Low", f"Contributed on 1 to {cfg.moderate_min_days - 1} distinct days"],
        ["Inactive", "Organization member or assigned seat with zero recorded contributions"],
    ]
    curr_row = write_table(summary, curr_row, ("Status", "Definition"), definitions, status_col=0) + 2

    if failures:
        curr_row = section_title(summary, curr_row, "Items Requiring Review / Failures")
        write_table(summary, curr_row, ("Target Item", "Issue Description"), [list(item) for item in failures])

    autofit(summary, skip_rows=2)
    summary.sheet_view.showGridLines = False

    # Sheet 2: User Activity
    users_ws = wb.create_sheet("User Activity")
    last = write_table(users_ws, 1, USER_HEADERS, user_rows, status_col=STATUS_IDX, bar_col=TOTAL_IDX)
    finalize_sheet(users_ws, 1, last, len(USER_HEADERS), "C2")

    # Sheet 3: Repository Detail
    repo_ws = wb.create_sheet("Repository Detail")
    last_repo = write_table(repo_ws, 1, REPO_HEADERS, repo_rows, bar_col=REPO_HEADERS.index("Total Activities"))
    finalize_sheet(repo_ws, 1, last_repo, len(REPO_HEADERS), "D2")

    # Executive Management Sheets
    if management_tables:
        for title, (headers, rows) in management_tables.items():
            sheet = wb.create_sheet(title)
            last = write_table(sheet, 1, headers, rows)
            freeze = "C2" if title == "Repository Inventory" else ("D2" if title == "Top Active Repositories" else "B2")
            finalize_sheet(sheet, 1, last, len(headers), freeze)
            sheet.sheet_view.showGridLines = False

            if title == "Repository Inventory":
                colors = {
                    "Yes": STATUS_STYLES["Active"],
                    "No": STATUS_STYLES["Inactive"],
                    "None": STATUS_STYLES["Inactive"],
                    "Unknown": STATUS_STYLES["Moderate"],
                    "Complete": STATUS_STYLES["Active"],
                    "Partial": STATUS_STYLES["Moderate"],
                }
                for col_name in ("Protected Branches", "CODEOWNERS Present", "Coverage"):
                    if col_name in headers:
                        col_idx = headers.index(col_name) + 1
                        for r_idx in range(2, last + 1):
                            cell = sheet.cell(row=r_idx, column=col_idx)
                            val = cell.value
                            status = val if col_name != "Protected Branches" or val in {"None", "Unknown"} else "Yes"
                            if status in colors:
                                fill, font = colors[status]
                                cell.fill = PatternFill("solid", fgColor=fill)
                                cell.font = Font(bold=True, color=font)
                if "Protected Branches" in headers:
                    sheet.column_dimensions[get_column_letter(headers.index("Protected Branches") + 1)].width = 38
                if "Total Activities" in headers and rows:
                    col_letter = get_column_letter(headers.index("Total Activities") + 1)
                    sheet.conditional_formatting.add(
                        f"{col_letter}2:{col_letter}{last}",
                        DataBarRule(start_type="min", end_type="max", color="5B9BD5"),
                    )

            elif title == "Top Active Repositories" and rows:
                if "Total Activities" in headers:
                    col_letter = get_column_letter(headers.index("Total Activities") + 1)
                    sheet.conditional_formatting.add(
                        f"{col_letter}2:{col_letter}{last}",
                        DataBarRule(start_type="min", end_type="max", color="5B9BD5"),
                    )

            elif title == "Management KPIs":
                desc_letter = get_column_letter(len(headers))
                sheet.column_dimensions[desc_letter].width = 95
                val_col_idx = headers.index("Value") + 1 if "Value" in headers else None
                for r_idx in range(2, last + 1):
                    for c_idx in range(1, len(headers) + 1):
                        cell = sheet.cell(row=r_idx, column=c_idx)
                        h_align = "center" if c_idx == val_col_idx else ("left" if c_idx == len(headers) else None)
                        cell.alignment = Alignment(horizontal=h_align, vertical="center", wrap_text=True)

    wb.save(path)
