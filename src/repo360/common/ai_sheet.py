"""Shared rendering utility for dedicated AI Recommendations worksheets."""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

AI_REC_HEADERS: Tuple[str, ...] = (
    "Priority",
    "Category",
    "Scope",
    "Finding",
    "Recommended Action",
    "Expected Impact",
)

AI_DISCLAIMER_NOTE: str = (
    "Note: AI recommendations can make mistakes. Review before using or setting them up."
)

PRIORITY_STYLES = {
    "High": ("FFC7CE", "9C0006"),
    "Medium": ("FFEB9C", "9C6500"),
    "Low": ("C6EFCE", "006100"),
    "Optimization": ("BDD7EE", "1F497D"),
}

NAVY = "1F497D"
BORDER_GRAY = "D9D9D9"


def render_ai_recommendations_sheet(
    wb: Any,
    ai_rows: Sequence[Sequence[Any]],
    title: str = "AI Recommendations",
    index: Optional[int] = 1,
) -> Any:
    """Create and style a standardized, executive-grade AI Recommendations worksheet.

    Args:
        wb: Target openpyxl Workbook.
        ai_rows: Tabular data rows conforming to AI_REC_HEADERS.
        title: Worksheet title (default: 'AI Recommendations').
        index: Sheet index position (default: 1, placed immediately after Executive Summary).

    Returns:
        The styled openpyxl Worksheet instance.
    """
    if title in wb.sheetnames:
        ws = wb[title]
    else:
        ws = wb.create_sheet(title, index=index)

    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "C2"

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=NAVY)
    thin_border = Border(
        left=Side(style="thin", color=BORDER_GRAY),
        right=Side(style="thin", color=BORDER_GRAY),
        top=Side(style="thin", color=BORDER_GRAY),
        bottom=Side(style="thin", color=BORDER_GRAY),
    )

    ws.row_dimensions[1].height = 26
    for c_idx, h in enumerate(AI_REC_HEADERS, start=1):
        cell = ws.cell(row=1, column=c_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border

    rows_to_render = list(ai_rows)
    if not rows_to_render:
        rows_to_render = [[
            "Low",
            "General Governance",
            "Fleet Scope",
            "All audited resources meet governance and operational benchmarks.",
            "Maintain current engineering practices and monitor telemetry regularly.",
            "Ensures continuous operational stability and compliance.",
        ]]

    prio_col_idx = AI_REC_HEADERS.index("Priority") + 1

    for r_idx, row_data in enumerate(rows_to_render, start=2):
        ws.row_dimensions[r_idx].height = 36
        for c_idx in range(1, len(AI_REC_HEADERS) + 1):
            val = row_data[c_idx - 1] if c_idx - 1 < len(row_data) else ""
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.font = Font(name="Calibri", size=10)
            cell.border = thin_border

            if c_idx == prio_col_idx:
                prio_str = str(val).strip()
                if prio_str in PRIORITY_STYLES:
                    fill_c, font_c = PRIORITY_STYLES[prio_str]
                    cell.fill = PatternFill("solid", fgColor=fill_c)
                    cell.font = Font(name="Calibri", size=10, bold=True, color=font_c)
                else:
                    cell.font = Font(name="Calibri", size=10, bold=True)
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif c_idx in (3, 4, 5, 6):
                cell.alignment = Alignment(wrap_text=True, vertical="center")
            else:
                cell.alignment = Alignment(vertical="center")

    col_widths = {
        "Priority": 15,
        "Category": 24,
        "Scope": 28,
        "Finding": 48,
        "Recommended Action": 64,
        "Expected Impact": 48,
    }
    for col_name, width in col_widths.items():
        if col_name in AI_REC_HEADERS:
            c_letter = get_column_letter(AI_REC_HEADERS.index(col_name) + 1)
            ws.column_dimensions[c_letter].width = width

    return ws
