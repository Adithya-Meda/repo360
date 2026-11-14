"""Production-Grade Multi-Tab Excel Workbook Generator."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .base_reporter import BaseReporter
from ..checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
from ..common.concurrency import AI_NOT_ENABLED_MESSAGE
from ..common.text import pluralize


class ExcelReporter(BaseReporter):
    """Generates structured Excel audit reports with executive summaries and playbooks."""

    def __init__(self) -> None:
        self.font_title = Font(name="Calibri", size=16, bold=True, color="1F497D")
        self.font_subtitle = Font(name="Calibri", size=11, italic=True, color="595959")
        self.font_section = Font(name="Calibri", size=13, bold=True, color="1F497D")
        self.font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        self.font_data = Font(name="Calibri", size=11)
        self.font_bold = Font(name="Calibri", size=11, bold=True)
        self.font_italic = Font(name="Calibri", size=10, italic=True, color="595959")
        self.font_code = Font(name="Consolas", size=10)
        self.font_ai_badge = Font(name="Calibri", size=10, bold=True, color="FFFFFF")

        self.fill_header = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
        self.fill_ai_header = PatternFill(start_color="4B0082", end_color="4B0082", fill_type="solid")
        self.fill_ai_badge = PatternFill(start_color="6F42C1", end_color="6F42C1", fill_type="solid")
        self.fill_pass = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        self.fill_warn = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        self.fill_fail = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        self.fill_skip = PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid")
        self.fill_zebra = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
        self.fill_sev_high = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
        self.fill_sev_low = PatternFill(start_color="E8EEF5", end_color="E8EEF5", fill_type="solid")
        self.font_muted = Font(name="Calibri", size=11, color="7A828E")

        thin = Side(border_style="thin", color="D9D9D9")
        self.border_cell = Border(left=thin, right=thin, top=thin, bottom=thin)

    def _apply_status_style(self, cell, status: CheckStatus) -> None:
        """Apply color-coded badge style to status cell."""
        if status == CheckStatus.PASS:
            cell.fill = self.fill_pass
        elif status == CheckStatus.WARN:
            cell.fill = self.fill_warn
        elif status == CheckStatus.FAIL:
            cell.fill = self.fill_fail
        else:
            cell.fill = self.fill_skip
        cell.font = self.font_bold
        cell.alignment = Alignment(horizontal="center", vertical="top")

    def _apply_severity_style(self, cell, severity: CheckSeverity) -> None:
        """Apply subtle color-coded badge styling to severity cells."""
        if severity == CheckSeverity.CRITICAL:
            cell.fill = self.fill_fail
        elif severity == CheckSeverity.HIGH:
            cell.fill = self.fill_sev_high
        elif severity == CheckSeverity.MEDIUM:
            cell.fill = self.fill_warn
        else:
            cell.fill = self.fill_sev_low
        cell.font = self.font_bold
        cell.alignment = Alignment(horizontal="center", vertical="top")

    @staticmethod
    def _estimate_row_height(
        values: List[Any],
        col_widths: Dict[int, float],
        min_height: float = 20.0,
        max_height: float = 220.0,
    ) -> float:
        """Estimate neat row height accounting for newlines and wrapped character lengths."""
        max_lines = 1
        for col_idx, val in enumerate(values, start=1):
            text = str(val or "")
            if not text or text == "-":
                continue
            width = max(col_widths.get(col_idx, 30.0) - 3.0, 12.0)
            lines = 0
            for raw_line in text.splitlines() or [text]:
                lines += max(1, (len(raw_line) + int(width) - 1) // int(width))
            if lines > max_lines:
                max_lines = lines
        return min(max(min_height, max_lines * 15.0 + 6.0), max_height)

    def _auto_adjust_columns(
        self,
        sheet,
        max_sample_rows: int = 100,
        col_max_widths: Dict[int, int] | None = None,
        skip_merged_rows: bool = True,
    ) -> Dict[int, float]:
        """Autofit column widths in O(1) memory while ignoring merged footer/banner rows."""
        sampled_rows = list(sheet.iter_rows(max_row=max_sample_rows))
        if not sampled_rows:
            return {}

        merged_coords: set[tuple[int, int]] = set()
        if skip_merged_rows:
            for rng in sheet.merged_cells.ranges:
                for r in range(rng.min_row, rng.max_row + 1):
                    for c in range(rng.min_col, rng.max_col + 1):
                        merged_coords.add((r, c))

        col_max_widths = col_max_widths or {}
        computed: Dict[int, float] = {}
        num_cols = len(sampled_rows[0])
        for col_idx in range(num_cols):
            col_num = col_idx + 1
            col_letter = get_column_letter(col_num)
            cap = col_max_widths.get(col_num, 45)
            max_len = 0
            for row in sampled_rows:
                cell = row[col_idx]
                if (cell.row, cell.column) in merged_coords:
                    continue
                val = str(cell.value or "")
                first_line = val.split("\n")[0]
                if len(first_line) > max_len:
                    max_len = min(len(first_line), cap)
            width = max(max_len + 4, 12)
            sheet.column_dimensions[col_letter].width = width
            computed[col_num] = float(width)
        return computed

    def generate(
        self,
        results: List[CheckResult],
        output_path: str,
        executive_summary: str = "",
        model_name: str = "",
        ai_enabled: Optional[bool] = None,
    ) -> str:
        """Build and write the Excel workbook."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        wb = Workbook()

        ws_summary = wb.active
        ws_summary.title = "Executive_Summary"
        self._build_summary_sheet(
            ws_summary,
            results,
            executive_summary,
            model_name=model_name,
            ai_enabled=ai_enabled,
        )

        ws_playbook = wb.create_sheet(title="Fleet_Playbook")
        self._build_fleet_playbook_sheet(ws_playbook, results)

        ws_ai = wb.create_sheet(title="AI_Recommendations")
        self._build_ai_recommendations_sheet(ws_ai, results, ai_enabled=ai_enabled)

        ws_org = wb.create_sheet(title="Org_Admin_Findings")
        self._build_findings_sheet(
            ws_org,
            [r for r in results if r.persona == Persona.ORG_ADMIN],
            is_org=True,
        )

        ws_repo = wb.create_sheet(title="Repo_Admin_Findings")
        self._build_findings_sheet(
            ws_repo,
            [r for r in results if r.persona == Persona.REPO_ADMIN],
            is_org=False,
        )

        ws_contrib = wb.create_sheet(title="Contributor_Findings")
        self._build_findings_sheet(
            ws_contrib,
            [r for r in results if r.persona == Persona.CONTRIBUTOR],
            is_org=False,
        )

        wb.save(output_path)
        return output_path

    def _build_summary_sheet(
        self,
        ws,
        results: List[CheckResult],
        executive_summary: str = "",
        model_name: str = "",
        ai_enabled: Optional[bool] = None,
    ) -> None:
        ws.views.sheetView[0].showGridLines = True
        ws["B2"] = "Repo360 Platform Governance & Intelligence Summary"
        ws["B2"].font = self.font_title

        total = len(results)
        passed = sum(1 for r in results if r.status == CheckStatus.PASS)
        warned = sum(1 for r in results if r.status == CheckStatus.WARN)
        failed = sum(1 for r in results if r.status == CheckStatus.FAIL)
        skipped = sum(1 for r in results if r.status == CheckStatus.SKIPPED)
        ai_count = 0 if ai_enabled is False else sum(1 for r in results if self.is_ai_result(r))

        effective = total - skipped
        if effective > 0:
            compliance_str = f"{(passed / effective) * 100:.1f}%"
        elif total == 0:
            compliance_str = "100.0%"
        else:
            compliance_str = "N/A (All Skipped)"

        metrics = [
            ("Audit Execution Timestamp", self.get_local_timestamp()),
            ("Total Findings Evaluated", total),
            ("Passed Checks (PASS)", passed),
            ("Warning Risks (WARN)", warned),
            ("Failed Checks (FAIL)", failed),
            ("Skipped / Not Applicable", skipped),
            ("Overall Compliance Score", compliance_str),
            ("AI-Tailored Remediations Synthesized", ai_count),
        ]

        headers_overview = ["Audit Metric", "Value"]
        for col_idx, h in enumerate(headers_overview, start=2):
            cell = ws.cell(row=5, column=col_idx, value=h)
            cell.font = self.font_header
            cell.fill = self.fill_header
            cell.alignment = Alignment(horizontal="left", vertical="center")

        for row_idx, (metric, val) in enumerate(metrics, start=6):
            c1 = ws.cell(row=row_idx, column=2, value=metric)
            c2 = ws.cell(row=row_idx, column=3, value=val)
            c1.font = self.font_data
            c2.font = self.font_bold
            c1.border = self.border_cell
            c2.border = self.border_cell
            c2.alignment = Alignment(horizontal="right", vertical="center")

            if metric == "AI-Tailored Remediations Synthesized" and ai_count > 0:
                c2.fill = self.fill_ai_badge
                c2.font = self.font_ai_badge

        org_metrics = self.build_organization_metrics(results)
        org_headers = [
            "Organization Name",
            "Repositories Audited",
            "Total Checks",
            "Controls Present (PASS)",
            "Controls Missing (FAIL)",
            "Review Items (WARN)",
            "Critical / High Risks",
            "Org Compliance Score",
            "Posture Status",
        ]

        for col_idx, h in enumerate(org_headers, start=5):
            cell = ws.cell(row=5, column=col_idx, value=h)
            cell.font = self.font_header
            cell.fill = self.fill_header
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        org_start_row = 6
        for r_offset, org_info in enumerate(org_metrics):
            row_idx = org_start_row + r_offset
            score_display = (
                "N/A"
                if org_info["status"] == "NOT EVALUATED"
                else f"{org_info['compliance_score']:.1f}%"
            )
            row_vals = [
                org_info["org_name"],
                org_info["repo_count"],
                org_info["total"],
                org_info["passed"],
                org_info["failed"],
                org_info["warned"],
                org_info["critical_high"],
                score_display,
                org_info["status"],
            ]
            for col_idx, val in enumerate(row_vals, start=5):
                cell = ws.cell(row=row_idx, column=col_idx, value=self.sanitize_spreadsheet_value(val))
                cell.font = self.font_data
                cell.border = self.border_cell
                cell.alignment = Alignment(horizontal="center", vertical="center")

                if col_idx == 5:
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                    cell.font = self.font_bold
                elif col_idx == 8:
                    cell.fill = self.fill_pass
                    cell.font = self.font_bold
                elif col_idx == 9 and org_info["failed"] > 0:
                    cell.fill = self.fill_fail
                    cell.font = self.font_bold
                elif col_idx == 10 and org_info["warned"] > 0:
                    cell.fill = self.fill_warn
                    cell.font = self.font_bold
                elif col_idx == 12:
                    if org_info["status"] == "NOT EVALUATED":
                        cell.fill = self.fill_warn
                    else:
                        score_val = org_info["compliance_score"]
                        cell.fill = self.fill_pass if score_val >= 80 else (self.fill_warn if score_val >= 50 else self.fill_fail)
                    cell.font = self.font_bold
                elif col_idx == 13:
                    if org_info["status"] == "COMPLIANT":
                        cell.fill = self.fill_pass
                    elif org_info["status"] in ("REVIEW NEEDED", "NOT EVALUATED"):
                        cell.fill = self.fill_warn
                    else:
                        cell.fill = self.fill_fail
                    cell.font = self.font_bold

        org_end_row = max(13, org_start_row + len(org_metrics))

        is_det_summary = model_name == "Deterministic Policy Engine"
        if ai_enabled is False and not executive_summary:
            narrative_title = "AI Executive Risk & Governance Narrative (AI Not Enabled)"
            narrative_text = AI_NOT_ENABLED_MESSAGE
            narrative_banner_row = org_end_row + 2

            header_cell = ws.cell(
                row=narrative_banner_row,
                column=2,
                value=narrative_title,
            )
            header_cell.font = self.font_header
            header_cell.fill = self.fill_ai_header
            header_cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.merge_cells(f"B{narrative_banner_row}:M{narrative_banner_row}")
            ws.row_dimensions[narrative_banner_row].height = 26
            for c in range(2, 14):
                ws.cell(row=narrative_banner_row, column=c).fill = self.fill_ai_header
                ws.cell(row=narrative_banner_row, column=c).border = self.border_cell

            narrative_start_row = narrative_banner_row + 1
            narrative_end_row = narrative_start_row + 1
            c_narrative = ws.cell(
                row=narrative_start_row,
                column=2,
                value=self.sanitize_spreadsheet_value(narrative_text),
            )
            c_narrative.font = self.font_italic
            c_narrative.alignment = Alignment(wrap_text=True, vertical="center")
            ws.merge_cells(f"B{narrative_start_row}:M{narrative_end_row}")
            for r in range(narrative_start_row, narrative_end_row + 1):
                ws.row_dimensions[r].height = 20
                for c in range(2, 14):
                    ws.cell(row=r, column=c).border = self.border_cell
            notice_row = narrative_end_row + 2
        elif executive_summary:
            if is_det_summary:
                narrative_title = "Executive Risk & Governance Narrative (Deterministic Policy Summary)"
            else:
                active_model = model_name or self.infer_ai_model(results, "Configured LLM")
                narrative_title = f"AI Executive Risk & Governance Narrative (Synthesized via {active_model})"
            narrative_banner_row = org_end_row + 2

            header_cell = ws.cell(
                row=narrative_banner_row,
                column=2,
                value=narrative_title,
            )
            header_cell.font = self.font_header
            header_cell.fill = self.fill_ai_header
            header_cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.merge_cells(f"B{narrative_banner_row}:M{narrative_banner_row}")
            ws.row_dimensions[narrative_banner_row].height = 26
            for c in range(2, 14):
                ws.cell(row=narrative_banner_row, column=c).fill = self.fill_ai_header
                ws.cell(row=narrative_banner_row, column=c).border = self.border_cell

            lines = executive_summary.splitlines()
            visual_lines = 0
            for l in lines:
                l_str = l.strip()
                if not l_str:
                    visual_lines += 1
                else:
                    visual_lines += max(1, (len(l_str) + 125) // 126)

            needed_rows = max(12, visual_lines + 2)
            narrative_start_row = narrative_banner_row + 1
            narrative_end_row = narrative_start_row + needed_rows - 1

            c_narrative = ws.cell(
                row=narrative_start_row,
                column=2,
                value=self.sanitize_spreadsheet_value(executive_summary),
            )
            c_narrative.font = self.font_data
            c_narrative.alignment = Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(f"B{narrative_start_row}:M{narrative_end_row}")

            for r in range(narrative_start_row, narrative_end_row + 1):
                ws.row_dimensions[r].height = 18
                for c in range(2, 14):
                    ws.cell(row=r, column=c).border = self.border_cell

            notice_row = narrative_end_row + 2
        else:
            notice_row = org_end_row + 2

        if ai_enabled is not False and (ai_count > 0 or (executive_summary and not is_det_summary)):
            c_notice = ws.cell(
                row=notice_row,
                column=2,
                value="AI Safety Notice: All AI-synthesized remediation snippets and summaries are advisory only. Review and validate in non-production environments before deployment.",
            )
            c_notice.font = self.font_italic
            c_notice.alignment = Alignment(wrap_text=True, vertical="center")
            ws.merge_cells(f"B{notice_row}:M{notice_row}")
            ws.row_dimensions[notice_row].height = 24

        self._auto_adjust_columns(ws, col_max_widths={2: 36, 3: 18, 5: 26, 13: 24})

        for col_char in ("D", "E", "F", "G", "H", "I", "J", "K", "L", "M"):
            if ws.column_dimensions[col_char].width < 14:
                ws.column_dimensions[col_char].width = 14

    def _build_fleet_playbook_sheet(self, ws, results: List[CheckResult]) -> None:
        """Build consolidated fleet-wide playbook grouping common remediations across repositories."""
        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 26

        headers = [
            "Category",
            "Remediation Pattern / Check Name",
            "Severity",
            "Detected Tech Stack",
            "Target Configuration File",
            "Affected Repos Count",
            "Affected Repositories List",
            "Canonical Recommended Configuration / Code",
            "Official Documentation URL",
        ]

        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = self.font_header
            cell.fill = self.fill_header
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        clusters = self.build_remediation_clusters(results)

        if not clusters:
            ws.cell(
                row=2,
                column=1,
                value="All evaluated repositories met compliance standards. No fleet remediations required.",
            )
            ws.cell(row=2, column=1).font = self.font_italic
            ws.cell(row=2, column=1).alignment = Alignment(horizontal="center", vertical="center")
            ws.merge_cells("A2:I2")
            ws.row_dimensions[2].height = 24
            self._auto_adjust_columns(ws)
            return

        row_values_cache: List[tuple[int, List[Any], CheckSeverity]] = []
        for row_idx, c in enumerate(clusters, start=2):
            repos_str = self.format_repo_list(c["affected_repos"], max_display=6)
            values: List[Any] = [
                self.sanitize_spreadsheet_value(c["category"]),
                self.sanitize_spreadsheet_value(c["check_name"]),
                c["severity"].value,
                self.sanitize_spreadsheet_value(c["detected_stack"]),
                self.sanitize_spreadsheet_value(c["target_file"]),
                pluralize(c["affected_count"], "repository", "repositories", include_count=True),
                self.sanitize_spreadsheet_value(repos_str),
                self.sanitize_spreadsheet_value(c["remediation_code"]),
                self.sanitize_spreadsheet_value(c["documentation_url"]),
            ]
            row_values_cache.append((row_idx, values, c["severity"]))
            use_zebra = row_idx % 2 == 1

            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = self.font_data
                cell.border = self.border_cell
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if use_zebra and col_idx != 3:
                    cell.fill = self.fill_zebra

                if col_idx == 3:
                    self._apply_severity_style(cell, c["severity"])
                elif col_idx == 6:
                    cell.alignment = Alignment(horizontal="center", vertical="top")
                    cell.font = self.font_bold
                elif col_idx == 8:
                    cell.font = self.font_code

        col_widths = self._auto_adjust_columns(
            ws,
            col_max_widths={1: 22, 2: 34, 3: 13, 4: 34, 5: 34, 6: 18, 7: 44, 8: 68, 9: 48},
        )
        for row_idx, values, _ in row_values_cache:
            ws.row_dimensions[row_idx].height = self._estimate_row_height(
                values, col_widths, min_height=24.0, max_height=240.0
            )
        ws.auto_filter.ref = f"A1:I{len(clusters) + 1}"

    def _build_ai_recommendations_sheet(
        self,
        ws,
        results: List[CheckResult],
        ai_enabled: Optional[bool] = None,
    ) -> None:
        """Build dedicated worksheet consolidating AI-synthesized remediations without repetitive clutter."""
        from ..ai.engine import AIEngine

        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 26

        headers = [
            "Repository",
            "Category",
            "Finding / Check",
            "Severity",
            "Status",
            "Detected Tech Stack",
            "AI Model Engine",
            "Recommendation Source",
            "Target Configuration File",
            "AI-Synthesized Remediation Code / Config",
            "Why AI Recommends This",
            "Official Documentation URL",
        ]

        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = self.font_header
            cell.fill = self.fill_ai_header
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        if ai_enabled is False:
            ws.cell(
                row=2,
                column=1,
                value=AI_NOT_ENABLED_MESSAGE,
            )
            ws.cell(row=2, column=1).font = self.font_italic
            ws.cell(row=2, column=1).alignment = Alignment(horizontal="center", vertical="center")
            ws.merge_cells("A2:L2")
            ws.row_dimensions[2].height = 28
            self._auto_adjust_columns(ws)
            return

        ai_findings = [r for r in results if self.is_ai_result(r)]
        if ai_findings:
            eligible_fallback = [
                r
                for r in results
                if not self.is_ai_result(r)
                and r.status in (CheckStatus.WARN, CheckStatus.FAIL)
                and r.check_id in AIEngine.SUPPORTED_REMEDIATION_CHECKS
                and (r.remediation_snippet_after or r.recommended_state)
            ]
            ai_findings = ai_findings + eligible_fallback
        else:
            actionable = [
                r
                for r in results
                if r.status in (CheckStatus.WARN, CheckStatus.FAIL)
                and (r.remediation_snippet_after or r.recommended_state)
            ]
            scoped = [r for r in actionable if r.check_id in AIEngine.SUPPORTED_REMEDIATION_CHECKS]
            ai_findings = scoped if scoped else actionable

        if not ai_findings:
            ws.cell(
                row=2,
                column=1,
                value="All evaluated checks met compliance standards. No remediations required.",
            )
            ws.cell(row=2, column=1).font = self.font_italic
            ws.cell(row=2, column=1).alignment = Alignment(horizontal="center", vertical="center")
            ws.merge_cells("A2:L2")
            ws.row_dimensions[2].height = 24
            self._auto_adjust_columns(ws)
            return

        # Key by (check_id, snippet, is_ai) so stack-specific AI rows stay separate from baseline rows
        grouped_ai: Dict[tuple[str, str, bool], Dict[str, Any]] = {}
        for f in ai_findings:
            raw_code = f.remediation_snippet_after or f.recommended_state or ""
            norm_code = " ".join(raw_code.split())
            f_is_ai = self.is_ai_result(f)
            key = (f.check_id, norm_code, f_is_ai)
            if key not in grouped_ai:
                grouped_ai[key] = {
                    "repos": [],
                    "finding": f,
                    "stacks": [],
                    "is_ai": f_is_ai,
                }
            if f.repo_name and f.repo_name not in grouped_ai[key]["repos"]:
                grouped_ai[key]["repos"].append(f.repo_name)
            stack_item = f.extra_details.get("detected_stack")
            if stack_item:
                grouped_ai[key]["stacks"].append(stack_item)

        severity_order = {
            CheckSeverity.CRITICAL: 0,
            CheckSeverity.HIGH: 1,
            CheckSeverity.MEDIUM: 2,
            CheckSeverity.LOW: 3,
            CheckSeverity.INFO: 4,
        }
        sorted_items = sorted(
            grouped_ai.values(),
            key=lambda item: (
                0 if item["is_ai"] else 1,
                severity_order.get(item["finding"].severity, 9),
                item["finding"].category,
                item["finding"].check_name,
                self.format_detected_stacks(item["stacks"]),
            ),
        )

        seen_explanations: set[tuple[str, str]] = set()
        seen_docs: set[tuple[str, str]] = set()
        row_values_cache: List[tuple[int, List[Any]]] = []
        has_any_ai = False

        for row_idx, item in enumerate(sorted_items, start=2):
            f = item["finding"]
            row_is_ai = bool(item["is_ai"])
            if row_is_ai:
                has_any_ai = True
            repos_display = self.format_repo_list(item["repos"], max_display=5)
            stack_fallback = "Detected Framework" if row_is_ai else "General Platform"
            stack_display = self.format_detected_stacks(
                item["stacks"] or f.extra_details.get("detected_stack", ""),
                fallback=stack_fallback,
                max_items=6,
            )
            model_name = (
                (f.extra_details.get("ai_model") or "Configured LLM")
                if row_is_ai
                else "Deterministic Policy Engine"
            )
            source_label = "AI Tailored" if row_is_ai else "Standard Policy (Fallback)"
            target_file = self.infer_target_file(f.check_id)
            remediation_code = f.remediation_snippet_after or f.recommended_state

            raw_expl = (f.explanation or "").strip()
            expl_key = (f.check_id, " ".join(raw_expl.split()))
            if raw_expl and expl_key not in seen_explanations:
                seen_explanations.add(expl_key)
                expl_val = raw_expl
            else:
                expl_val = "-"

            raw_doc = (f.documentation_url or "").strip()
            doc_key = (f.check_id, raw_doc)
            if raw_doc and doc_key not in seen_docs:
                seen_docs.add(doc_key)
                doc_val = raw_doc
            else:
                doc_val = "-"

            values: List[Any] = [
                self.sanitize_spreadsheet_value(repos_display),
                self.sanitize_spreadsheet_value(f.category),
                self.sanitize_spreadsheet_value(f.check_name),
                f.severity.value,
                f.status.value,
                self.sanitize_spreadsheet_value(stack_display),
                self.sanitize_spreadsheet_value(model_name),
                self.sanitize_spreadsheet_value(source_label),
                self.sanitize_spreadsheet_value(target_file),
                self.sanitize_spreadsheet_value(remediation_code),
                self.sanitize_spreadsheet_value(expl_val),
                self.sanitize_spreadsheet_value(doc_val),
            ]
            row_values_cache.append((row_idx, values))
            use_zebra = row_idx % 2 == 1

            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = self.font_data
                cell.border = self.border_cell
                cell.alignment = Alignment(vertical="top", wrap_text=True)

                if use_zebra and col_idx not in (4, 5, 8):
                    cell.fill = self.fill_zebra

                if col_idx == 4:
                    self._apply_severity_style(cell, f.severity)
                elif col_idx == 5:
                    self._apply_status_style(cell, f.status)
                elif col_idx == 8:
                    if row_is_ai:
                        cell.fill = self.fill_ai_badge
                        cell.font = self.font_ai_badge
                    else:
                        cell.fill = self.fill_warn
                        cell.font = self.font_bold
                    cell.alignment = Alignment(horizontal="center", vertical="top")
                elif col_idx == 10:
                    cell.font = self.font_code
                elif val == "-":
                    cell.font = self.font_muted
                    cell.alignment = Alignment(horizontal="center", vertical="top")

        col_widths = self._auto_adjust_columns(
            ws,
            col_max_widths={1: 36, 2: 20, 3: 32, 4: 13, 5: 12, 6: 34, 7: 26, 8: 30, 9: 34, 10: 68, 11: 44, 12: 44},
        )
        for row_idx, values in row_values_cache:
            ws.row_dimensions[row_idx].height = self._estimate_row_height(
                values, col_widths, min_height=24.0, max_height=240.0
            )
        ws.auto_filter.ref = f"A1:L{len(sorted_items) + 1}"

        safety_row = len(sorted_items) + 3
        notice_msg = (
            "AI Safety Notice: AI-synthesized remediation snippets and configuration files are advisory only. Validate in a lower environment prior to production merge."
            if has_any_ai
            else "Policy Advisory Notice: Generated using built-in deterministic GitHub governance templates because live LLM synthesis was unavailable or rate-limited."
        )
        c_safe = ws.cell(
            row=safety_row,
            column=1,
            value=notice_msg,
        )
        c_safe.font = self.font_italic
        c_safe.alignment = Alignment(vertical="center")
        ws.merge_cells(f"A{safety_row}:L{safety_row}")
        ws.row_dimensions[safety_row].height = 22

    def _build_findings_sheet(self, ws, findings: List[CheckResult], is_org: bool) -> None:
        """Build compact findings worksheet with clean status formatting and minimal vertical repetition."""
        ws.views.sheetView[0].showGridLines = True
        ws.freeze_panes = "A2"
        ws.row_dimensions[1].height = 24

        headers = [
            "Organization",
            "Target Resource",
            "Category",
            "Check Name",
            "Status",
            "Severity",
            "Target Configuration File",
            "Current State",
            "Recommended State",
            "Explanation & Remediation",
            "Documentation URL",
        ]

        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = self.font_header
            cell.fill = self.fill_header
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        if not findings:
            ws.cell(
                row=2,
                column=1,
                value="No findings recorded for this category.",
            )
            ws.cell(row=2, column=1).font = self.font_italic
            ws.cell(row=2, column=1).alignment = Alignment(horizontal="center", vertical="center")
            ws.merge_cells("A2:K2")
            ws.row_dimensions[2].height = 24
            self._auto_adjust_columns(ws)
            return

        seen_explanations: set[tuple[str, str]] = set()
        seen_docs: set[tuple[str, str]] = set()
        row_values_cache: List[tuple[int, List[Any], CheckStatus]] = []
        generic_pass_states = {
            "",
            "control verified present and compliant.",
            "verified compliant.",
            "compliant",
        }

        for row_idx, f in enumerate(findings, start=2):
            target_file = self.infer_target_file(f.check_id)

            if f.status == CheckStatus.PASS:
                raw_state = (f.current_state or "").strip()
                current_state_val = (
                    "Compliant"
                    if raw_state.casefold() in generic_pass_states
                    else raw_state
                )
                rec_state_val = "-"
                expl_val = "-"
                doc_val = "-"
            elif f.status == CheckStatus.SKIPPED:
                current_state_val = (f.current_state or "").strip() or "Skipped / Not Applicable"
                rec_state_val = "-"
                expl_val = "-"
                doc_val = "-"
            else:
                current_state_val = f.current_state
                rec_state_val = f.recommended_state

                raw_expl = (f.explanation or "").strip()
                expl_key = (f.check_id, " ".join(raw_expl.split()))
                if raw_expl and expl_key not in seen_explanations:
                    seen_explanations.add(expl_key)
                    expl_val = raw_expl
                else:
                    expl_val = "-"

                raw_doc = (f.documentation_url or "").strip()
                doc_key = (f.check_id, raw_doc)
                if raw_doc and doc_key not in seen_docs:
                    seen_docs.add(doc_key)
                    doc_val = raw_doc
                else:
                    doc_val = "-"

            values: List[Any] = [
                self.sanitize_spreadsheet_value(f.org_name),
                self.sanitize_spreadsheet_value(f.repo_name),
                self.sanitize_spreadsheet_value(f.category),
                self.sanitize_spreadsheet_value(f.check_name),
                f.status.value,
                f.severity.value,
                self.sanitize_spreadsheet_value(target_file),
                self.sanitize_spreadsheet_value(current_state_val),
                self.sanitize_spreadsheet_value(rec_state_val),
                self.sanitize_spreadsheet_value(expl_val),
                self.sanitize_spreadsheet_value(doc_val),
            ]
            row_values_cache.append((row_idx, values, f.status))
            use_zebra = row_idx % 2 == 1

            for col_idx, val in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = self.font_data
                cell.border = self.border_cell
                cell.alignment = Alignment(vertical="top", wrap_text=True)

                if use_zebra and col_idx not in (5, 6):
                    cell.fill = self.fill_zebra

                if col_idx == 5:
                    self._apply_status_style(cell, f.status)
                elif col_idx == 6:
                    if f.status in (CheckStatus.WARN, CheckStatus.FAIL):
                        self._apply_severity_style(cell, f.severity)
                    else:
                        if use_zebra:
                            cell.fill = self.fill_zebra
                        cell.alignment = Alignment(horizontal="center", vertical="top")
                elif val == "-":
                    cell.font = self.font_muted
                    cell.alignment = Alignment(horizontal="center", vertical="top")

        col_widths = self._auto_adjust_columns(
            ws,
            col_max_widths={1: 20, 2: 28, 3: 22, 4: 34, 5: 12, 6: 13, 7: 32, 8: 38, 9: 40, 10: 52, 11: 42},
        )
        for row_idx, values, status in row_values_cache:
            min_h = 20.0 if status in (CheckStatus.PASS, CheckStatus.SKIPPED) else 22.0
            ws.row_dimensions[row_idx].height = self._estimate_row_height(
                values, col_widths, min_height=min_h, max_height=140.0
            )
        ws.auto_filter.ref = f"A1:K{len(findings) + 1}"
