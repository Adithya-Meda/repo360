"""Comprehensive tests verifying the standardized AI recommendations disclaimer note in Executive Summary across all features."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import openpyxl

from repo360.activity.models import ActivityConfig
from repo360.activity.reporter import write_activity_workbook
from repo360.bypass.collector import build_bypass_rows
from repo360.bypass.models import BypassConfig, RepoBypassProfile
from repo360.bypass.reporter import write_bypass_workbook
from repo360.common.ai_sheet import AI_DISCLAIMER_NOTE as NOTE_AI_SHEET
from repo360.common.concurrency import AI_DISCLAIMER_NOTE as NOTE_CONCURRENCY
from repo360.dora.collector import build_dora_rows
from repo360.dora.models import DORAConfig, DeploymentRecord, RepoDORAMetrics
from repo360.dora.reporter import write_dora_workbook
from repo360.finops.collector import build_finops_rows
from repo360.finops.models import FinOpsConfig, RepoStorageProfile
from repo360.finops.reporter import write_finops_workbook
from repo360.checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
from repo360.reporters.excel_reporter import ExcelReporter
from repo360.runners.models import RunnersConfig
from repo360.runners.reporter import write_runners_workbook


def test_ai_disclaimer_constant_consistency():
    """Verify AI disclaimer constant is uniform and consistent across modules."""
    expected = (
        "Note: AI recommendations are advisory and may contain inaccuracies. "
        "Review and validate all suggestions before applying or configuring them."
    )
    assert NOTE_AI_SHEET == expected
    assert NOTE_CONCURRENCY == expected


def _get_sheet_values(wb, sheet_name):
    ws = wb[sheet_name]
    return [
        str(cell.value)
        for row in ws.iter_rows()
        for cell in row
        if cell.value is not None
    ]


def test_activity_executive_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify Activity feature includes AI disclaimer note in Executive Summary."""
    out_file = tmp_path / "activity.xlsx"
    now = datetime.now(timezone.utc)
    write_activity_workbook(
        out_file,
        ActivityConfig(),
        now - timedelta(days=30),
        now,
        [],
        [],
        [],
        [],
        1,
        execution_duration=1.0,
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive Summary")
    assert NOTE_AI_SHEET in values


def test_bypass_executive_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify Bypass feature includes AI disclaimer note in Executive Summary."""
    out_file = tmp_path / "bypass.xlsx"
    now = datetime.now(timezone.utc)
    profiles = [
        RepoBypassProfile(
            org="test-org",
            repo="test-repo",
        )
    ]
    push_rows, ruleset_rows, org_rows, actor_rows = build_bypass_rows(profiles, ["test-org"])
    write_bypass_workbook(
        out_file,
        BypassConfig(),
        now - timedelta(days=30),
        now,
        push_rows,
        ruleset_rows,
        org_rows,
        actor_rows,
        [],
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive Summary")
    assert NOTE_AI_SHEET in values


def test_dora_executive_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify DORA feature includes AI disclaimer note in Executive Summary."""
    out_file = tmp_path / "dora.xlsx"
    now = datetime.now(timezone.utc)
    metrics = [
        RepoDORAMetrics(
            org="test-org",
            repo="test-repo",
            signal_mode="GitHub Deployments",
            deployments=[
                DeploymentRecord(
                    "Deployment",
                    "dep-1",
                    "production",
                    "production",
                    "SUCCESS",
                    "main",
                    now - timedelta(days=2),
                )
            ],
        )
    ]
    repo_rows, org_rows, dep_rows, rec_rows = build_dora_rows(metrics, ["test-org"], window_days=30)
    write_dora_workbook(
        out_file,
        DORAConfig(),
        now - timedelta(days=30),
        now,
        repo_rows,
        org_rows,
        dep_rows,
        rec_rows,
        [],
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive Summary")
    assert NOTE_AI_SHEET in values


def test_finops_executive_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify FinOps feature includes AI disclaimer note in Executive Summary."""
    out_file = tmp_path / "finops.xlsx"
    now = datetime.now(timezone.utc)
    profiles = [
        RepoStorageProfile(
            org="test-org",
            repo="test-repo",
            visibility="private",
            disk_size_kb=1024,
            artifacts=[],
        )
    ]
    repo_rows, org_rows, art_rows, cp_rows = build_finops_rows(profiles, {}, ["test-org"])
    write_finops_workbook(
        out_file,
        FinOpsConfig(),
        now,
        repo_rows,
        org_rows,
        art_rows,
        cp_rows,
        [],
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive Summary")
    assert NOTE_AI_SHEET in values


def test_runners_executive_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify Runners feature includes AI disclaimer note in Executive Summary."""
    out_file = tmp_path / "runners.xlsx"
    now = datetime.now(timezone.utc)
    write_runners_workbook(
        out_file,
        RunnersConfig(),
        now - timedelta(days=30),
        now,
        [],
        [],
        [],
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive Summary")
    assert NOTE_AI_SHEET in values


def test_excel_reporter_summary_has_ai_disclaimer(tmp_path: Path):
    """Verify standard ExcelReporter audit report includes AI disclaimer note in Executive_Summary."""
    out_file = str(tmp_path / "audit.xlsx")
    mock_results = [
        CheckResult(
            check_id="branch_protection",
            check_name="Branch Protection Rules",
            persona=Persona.CONTRIBUTOR,
            category="Security",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.HIGH,
            org_name="test-org",
            repo_name="test-repo",
            current_state="Disabled",
            recommended_state="Enabled",
            explanation="No branch protection configured.",
            remediation_snippet_after="Configure branch protection.",
            extra_details={"ai_synthesized": True},
        )
    ]
    reporter = ExcelReporter()
    reporter.generate(
        mock_results,
        out_file,
        executive_summary="AI analyzed governance gaps across repositories.",
        ai_enabled=True,
    )
    wb = openpyxl.load_workbook(out_file)
    values = _get_sheet_values(wb, "Executive_Summary")
    assert NOTE_AI_SHEET in values
