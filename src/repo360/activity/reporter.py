"""Excel reporter for Repo360 user activity analysis."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from openpyxl import Workbook
from openpyxl.formatting.rule import DataBarRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..common.concurrency import AI_NOT_ENABLED_MESSAGE, is_ai_disabled, is_ai_live
from .models import (
    DATE_FMT,
    LOCAL_TZ,
    LOCAL_TZ_NAME,
    METRIC_LABELS,
    ORG_HEADERS,
    REPO_HEADERS,
    STATUS_IDX,
    STATUSES,
    TOTAL_IDX,
    USER_HEADERS,
    ActivityConfig,
    resolve_reporting_window,
)

logger = logging.getLogger("repo360.reporter")
NAVY = "1F3864"
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FONT = Font(bold=True, color="FFFFFF")
BAND_FILL = PatternFill("solid", fgColor="F2F2F2")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
LEFT_NOWRAP = Alignment(horizontal="left", vertical="center", wrap_text=False)
CENTERED_VALUES = frozenset({"Yes", "No", "Unknown", "None", "Complete", "Partial", "N/A", "Online", "Offline", "Idle", "Busy"})

STATUS_STYLES = {
    "Active": ("C6EFCE", "006100"),
    "Moderate": ("FFEB9C", "9C5700"),
    "Low": ("FCE4D6", "C65911"),
    "Inactive": ("FFC7CE", "9C0006"),
}

EMPTY_CELL_MESSAGES = {
    "Average Commits per Active Month": "No months",
    f"First Activity ({LOCAL_TZ_NAME})": "No activity",
    f"Last Activity ({LOCAL_TZ_NAME})": "No activity",
    "Days Since Last Activity": "No activity",
}
_EMPTY_CELL_MESSAGE_VALUES = tuple(dict.fromkeys(EMPTY_CELL_MESSAGES.values()))
STATUS_STYLE_OBJECTS = {
    status: (PatternFill("solid", fgColor=fill), Font(bold=True, color=font))
    for status, (fill, font) in STATUS_STYLES.items()
}

LEFT_HEADERS = {
    "Metric",
    "Scope",
    "Category",
    "Definition",
    "Issue Description",
    "Target Item",
    "What this means",
    "AI Flow Diagnostic & Advisory",
    "AI Strategic Flow Diagnostic & Advisory",
    "Methodology & Compliance Scope",
    "Operational Guidance",
    "API / Permission Gap Detail",
    "Error / Permission Detail",
}

NOWRAP_HEADERS = {
    "Active Repositories",
    "Affected Repositories",
    "Repositories Served",
    "Repos Served",
    "Repository List",
    "Job URL",
    "Job Link",
    "Alert URL",
    "Audit / Commit URL",
    "Official Documentation URL",
    "Documentation URL",
    "HTML URL",
    "URL",
    "Run URL",
    "Workflow URL",
    "PR URL",
    "Commit URL",
    "Repository URL",
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
    f"Created ({LOCAL_TZ_NAME})",
    f"First Activity ({LOCAL_TZ_NAME})",
    f"Last Activity ({LOCAL_TZ_NAME})",
    f"First Seen ({LOCAL_TZ_NAME})",
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


EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_CELL_CHARS = 32_767
_TRUNCATION_SUFFIX = " ...[truncated]"
AUTOFIT_SAMPLE_ROWS = 2000


def clamp_cell_text(value: str) -> str:
    """Truncate text to Excel's per-cell character limit so workbooks never open as corrupted."""
    if len(value) <= EXCEL_MAX_CELL_CHARS:
        return value
    return value[: EXCEL_MAX_CELL_CHARS - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def safe_cell(value: Any, header: Optional[str] = None) -> Any:
    """Sanitize spreadsheet cell values against formula injection, enforce Excel's cell size limit, and fill defaults."""
    if (value is None or value == "") and header in EMPTY_CELL_MESSAGES:
        value = EMPTY_CELL_MESSAGES[header]
    if isinstance(value, str):
        value = clamp_cell_text(value)
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


class CellStyler:
    """Apply openpyxl styles through a per-combination cache.

    openpyxl hashes every style object on each assignment, which dominates runtime on
    large sheets. Each unique (font, fill, border, alignment) combination is resolved once
    on a real cell and its compact style array is copied onto later cells. Pass long-lived
    style objects (module or instance constants): the cache is keyed by object identity,
    and one styler must only be used within a single workbook.
    """

    def __init__(self) -> None:
        self._cache: Dict[Tuple[int, int, int, int], Tuple[Any, Tuple[Any, ...]]] = {}

    def apply(self, cell, font=None, fill=None, border=None, alignment=None) -> None:
        key = (id(font), id(fill), id(border), id(alignment))
        cached = self._cache.get(key)
        if cached is not None:
            cell._style = copy(cached[0])
            return
        if font is not None:
            cell.font = font
        if fill is not None:
            cell.fill = fill
        if border is not None:
            cell.border = border
        if alignment is not None:
            cell.alignment = alignment
        # Holding the style objects keeps their ids stable for the cache key
        self._cache[key] = (copy(cell._style), (font, fill, border, alignment))


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

    capacity = EXCEL_MAX_ROWS - top - 1
    omitted = 0
    if len(rows) > capacity:
        omitted = len(rows) - capacity
        rows = rows[:capacity]
        logger.warning(
            "Sheet '%s' exceeds Excel's row limit; %d rows omitted from the workbook.", ws.title, omitted
        )

    styler = CellStyler()
    for row_idx, row in enumerate(rows, start=top + 1):
        band_fill = BAND_FILL if (row_idx - top) % 2 == 0 else None
        for col_idx, value in enumerate(row, start=1):
            header = headers[col_idx - 1] if col_idx - 1 < len(headers) else ""
            cell = ws.cell(row=row_idx, column=col_idx, value=safe_cell(value, header))
            val_str = str(value).strip() if value is not None else ""
            h_lower = header.lower()
            val_lower = val_str.lower()
            is_url_or_link = (
                "url" in h_lower
                or "link" in h_lower
                or val_lower.startswith(("http://", "https://", "ftp://"))
            )
            if header in NOWRAP_HEADERS or is_url_or_link:
                alignment = LEFT_NOWRAP
            elif header in LEFT_HEADERS:
                alignment = LEFT
            else:
                alignment = CENTER
            styler.apply(cell, fill=band_fill, border=BORDER, alignment=alignment)

        if status_col is not None and row[status_col] in STATUS_STYLE_OBJECTS:
            status_fill, status_font = STATUS_STYLE_OBJECTS[row[status_col]]
            styler.apply(
                ws.cell(row=row_idx, column=status_col + 1),
                font=status_font,
                fill=status_fill,
                border=BORDER,
                alignment=CENTER,
            )

    last_row = top + len(rows)
    if omitted:
        notice = ws.cell(
            row=last_row + 1,
            column=1,
            value=f"{omitted:,} additional rows omitted: Excel supports at most {EXCEL_MAX_ROWS:,} rows per sheet.",
        )
        notice.font = Font(italic=True, color="9C0006")
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
    url_cols: Set[int] = set()
    # Widths are capped at 48 (or 64 for URLs/links), so a bounded sample keeps autofit O(1) for very large sheets
    sample_end = min(ws.max_row, skip_rows + AUTOFIT_SAMPLE_ROWS)
    for row in ws.iter_rows(min_row=skip_rows + 1, max_row=sample_end):
        for cell in row:
            if cell.value is not None:
                val_str = str(cell.value)
                widths[cell.column] = max(widths[cell.column], len(val_str))
                val_lower = val_str.lower()
                if "url" in val_lower or "link" in val_lower or val_lower.startswith(("http://", "https://", "ftp://")):
                    url_cols.add(cell.column)
    for col, width in widths.items():
        max_col_w = 64 if col in url_cols else 48
        ws.column_dimensions[get_column_letter(col)].width = min(max(width + 3, 12), max_col_w)


def finalize_sheet(ws, header_row: int, last_row: int, columns: int, freeze: str) -> None:
    """Configure auto-filters, freeze panes, and print setup for professional delivery."""
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(columns)}{max(last_row, header_row)}"
    ws.freeze_panes = freeze
    ws.row_dimensions[header_row].height = 28
    ws.page_setup.orientation = "landscape"
    for r in range(header_row + 1, last_row + 1):
        ws.row_dimensions[r].height = 20
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

    summary = wb.active
    summary.title = "Executive Summary"
    summary["A1"] = "Repo360 Contributor & Platform Activity Intelligence"
    summary["A1"].font = Font(bold=True, size=16, color=NAVY)

    activity_dates: List[Any] = []
    first_act_idx = (
        USER_HEADERS.index(f"First Activity ({LOCAL_TZ_NAME})")
        if f"First Activity ({LOCAL_TZ_NAME})" in USER_HEADERS
        else -1
    )
    member_idx = (
        USER_HEADERS.index("Org Member")
        if "Org Member" in USER_HEADERS
        else -1
    )
    if first_act_idx >= 0:
        for r in user_rows:
            if member_idx >= 0 and len(r) > member_idx and r[member_idx] not in ("Yes", "Unknown"):
                continue
            if len(r) > first_act_idx and r[first_act_idx]:
                activity_dates.append(r[first_act_idx])
    repo_act_idx = (
        REPO_HEADERS.index(f"Last Activity ({LOCAL_TZ_NAME})")
        if f"Last Activity ({LOCAL_TZ_NAME})" in REPO_HEADERS
        else -1
    )
    if repo_act_idx >= 0:
        for r in repo_rows:
            if len(r) > repo_act_idx and r[repo_act_idx]:
                activity_dates.append(r[repo_act_idx])

    since_local, end_local = since.astimezone(LOCAL_TZ), end.astimezone(LOCAL_TZ)
    duration_str = f"{execution_duration:.1f}s" if execution_duration else "N/A"
    window_str = resolve_reporting_window(cfg.lookback_days, since, end, activity_dates)
    summary["A2"] = (
        f"Reporting Window: {window_str} "
        f"| Generated: {end_local:{DATE_FMT} %H:%M} {LOCAL_TZ_NAME} | Duration: {duration_str}"
    )
    summary["A2"].font = Font(italic=True, color="595959")

    status_counts = Counter(row[STATUS_IDX] for row in user_rows)
    total_activities = sum(row[TOTAL_IDX] for row in user_rows)
    active_contributors = sum(1 for r in user_rows if r[TOTAL_IDX] > 0)
    total_commits = sum(row[TOTAL_IDX + 1] for row in user_rows) if user_rows else 0
    prs_opened = sum(row[TOTAL_IDX + 2] for row in user_rows) if user_rows else 0
    prs_merged = sum(row[TOTAL_IDX + 3] for row in user_rows) if user_rows else 0
    org_names = ", ".join(str(r[0]) for r in org_rows) if org_rows else "Monitored Fleet"

    if is_ai_disabled(ai_engine):
        advisory_title = "AI Contributor Cadence & Collaboration Summary (AI Not Enabled)"
        advisory_text = AI_NOT_ENABLED_MESSAGE
    elif is_ai_live(ai_engine):
        advisory_title = "AI Contributor Cadence & Collaboration Summary"
        advisory_text = ai_engine.generate_activity_insights(
            org_name=org_names,
            total_contributors=len(user_rows),
            active_contributors=active_contributors,
            total_activities=total_activities,
            total_commits=total_commits,
            prs_opened=prs_opened,
            prs_merged=prs_merged,
        )
    else:
        advisory_title = "Contributor Cadence & Collaboration Summary (Deterministic Policy)"
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

    open_prs_total = None
    open_issues_total = None
    closed_issues_total = None
    total_issues_count = None
    if management_tables and "Repository Inventory" in management_tables:
        inv_headers, inv_rows = management_tables["Repository Inventory"]
        if "Open PRs" in inv_headers:
            open_prs_idx = inv_headers.index("Open PRs")
            open_prs_total = sum(r[open_prs_idx] for r in inv_rows if isinstance(r[open_prs_idx], int))
        if "Open Issues" in inv_headers:
            open_iss_idx = inv_headers.index("Open Issues")
            open_issues_total = sum(r[open_iss_idx] for r in inv_rows if isinstance(r[open_iss_idx], int))
        if "Closed Issues" in inv_headers:
            closed_iss_idx = inv_headers.index("Closed Issues")
            closed_issues_total = sum(r[closed_iss_idx] for r in inv_rows if isinstance(r[closed_iss_idx], int))
        if "Total Issues" in inv_headers:
            tot_iss_idx = inv_headers.index("Total Issues")
            total_issues_count = sum(r[tot_iss_idx] for r in inv_rows if isinstance(r[tot_iss_idx], int))

    kpis = [
        ["Total Duration", duration_str],
        ["Organizations Scanned", len(org_rows)],
        ["Repositories Scanned", repos_scanned],
        ["Active Contributors", active_contributors],
        ["Total Users Reported", len(user_rows)],
        *([f"{s} Users", status_counts[s]] for s in STATUSES),
        ["Total Activities", total_activities],
        *([label, sum(row[TOTAL_IDX + 1 + i] for row in user_rows)] for i, label in enumerate(METRIC_LABELS)),
    ]
    if open_prs_total is not None:
        kpis.append(["Currently Open PRs", open_prs_total])
    if open_issues_total is not None:
        kpis.append(["Open Issues Backlog", open_issues_total])
    if closed_issues_total is not None:
        kpis.append(["Closed Issues", closed_issues_total])
    if total_issues_count is not None:
        kpis.append(["Total Issues Tracked", total_issues_count])
    kpis.append(["Unprocessed Items", len(failures)])
    curr_row = write_table(summary, 4, ("Metric", "Value"), kpis) + 2

    curr_row = section_title(summary, curr_row, advisory_title)
    summary.cell(curr_row, 1, safe_cell(advisory_text))
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

    users_ws = wb.create_sheet("User Activity")
    last = write_table(users_ws, 1, USER_HEADERS, user_rows, status_col=STATUS_IDX, bar_col=TOTAL_IDX)
    finalize_sheet(users_ws, 1, last, len(USER_HEADERS), "C2")

    repo_ws = wb.create_sheet("Repository Detail")
    last_repo = write_table(repo_ws, 1, REPO_HEADERS, repo_rows, bar_col=REPO_HEADERS.index("Total Activities"))
    finalize_sheet(repo_ws, 1, last_repo, len(REPO_HEADERS), "D2")

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
