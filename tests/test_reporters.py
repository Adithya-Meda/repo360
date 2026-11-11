"""Unit tests for Excel report generator."""

import os
import tempfile
import openpyxl
import pytest

from src.reporters.excel_reporter import ExcelReporter
from tests.conftest import create_mock_results
from src.validator import RepoTarget


@pytest.fixture
def mock_results() -> list:
    targets = [
        RepoTarget(
            org_name="fintech-core",
            repo_name="payment-gateway",
            target_personas={"all"},
        )
    ]
    return create_mock_results(targets)


def test_excel_reporter_generation(mock_results: list) -> None:
    """Verify Excel report generates properly structured workbook with dedicated AI sheet."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
        temp_path = f.name

    try:
        reporter = ExcelReporter()
        executive_summary = "High risk gaps identified in branch rulesets and token security."
        saved_path = reporter.generate(mock_results, temp_path, executive_summary)
        assert os.path.exists(saved_path)

        # Inspect generated workbook
        wb = openpyxl.load_workbook(saved_path)
        expected_sheets = {
            "Executive_Summary",
            "Fleet_Playbook",
            "AI_Recommendations",
            "Org_Admin_Findings",
            "Repo_Admin_Findings",
            "Contributor_Findings",
        }
        assert expected_sheets.issubset(set(wb.sheetnames))

        # 1. Verify Executive Summary
        ws_summary = wb["Executive_Summary"]
        assert ws_summary["B2"].value == "Repo360 Platform Governance & Intelligence Summary"
        # Check that AI KPI row exists
        metric_labels = [ws_summary.cell(row=r, column=2).value for r in range(5, 15)]
        assert "Audit Execution Timestamp" in metric_labels
        assert "AI-Tailored Remediations Synthesized" in metric_labels

        # 2. Verify Consolidated Fleet Playbook Sheet
        ws_playbook = wb["Fleet_Playbook"]
        playbook_headers = [cell.value for cell in ws_playbook[1]]
        assert "Category" in playbook_headers
        assert "Remediation Pattern / Check Name" in playbook_headers
        assert "Affected Repos Count" in playbook_headers
        assert "Affected Repositories List" in playbook_headers
        assert "Canonical Recommended Configuration / Code" in playbook_headers
        assert ws_playbook.max_row >= 2

        # 3. Verify Dedicated AI Recommendations Sheet
        ws_ai = wb["AI_Recommendations"]
        ai_headers = [cell.value for cell in ws_ai[1]]
        assert "Repository" in ai_headers
        assert "Detected Tech Stack" in ai_headers
        assert "AI Model Engine" in ai_headers
        assert "Recommendation Source" in ai_headers
        assert "AI-Synthesized Remediation Code / Config" in ai_headers
        assert "Target Configuration File" in ai_headers
        assert "CLI Remediation Command" not in ai_headers

        # Check that rows were populated
        assert ws_ai.max_row >= 2
        # Verify AI data row
        row2_values = [cell.value for cell in ws_ai[2]]
        assert any("payment-gateway" in str(v) for v in row2_values)

        # 3. Verify Findings Sheets do not clutter with Recommendation Source or Remediation Code (After)
        ws_contrib = wb["Contributor_Findings"]
        contrib_headers = [cell.value for cell in ws_contrib[1]]
        assert "Recommendation Source" not in contrib_headers
        assert "Target Configuration File" in contrib_headers
        assert "Remediation Code (After)" not in contrib_headers
        assert "Recommended State" in contrib_headers
        assert "Explanation & Remediation" in contrib_headers

        ws_repo = wb["Repo_Admin_Findings"]
        repo_headers = [cell.value for cell in ws_repo[1]]
        assert "Recommendation Source" not in repo_headers
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_formula_injection_defense() -> None:
    """Verify spreadsheet formula injection (CWE-1236) elements are neutralized with single quotes in Excel."""
    from src.checks.base import CheckResult, CheckStatus, CheckSeverity, Persona

    injected_result = CheckResult(
        check_id="test_injection",
        check_name="=1+1",
        persona=Persona.REPO_ADMIN,
        category="+Security",
        status=CheckStatus.FAIL,
        severity=CheckSeverity.HIGH,
        current_state="-old",
        recommended_state="@calc",
        explanation="=cmd|'/C calc'!A0",
        remediation_command="-rm -rf /",
        documentation_url="https://example.com",
        org_name="=MALICIOUS_ORG",
        repo_name="@malicious_repo",
    )

    # Test Excel
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
        xlsx_path = f.name
    try:
        ExcelReporter().generate([injected_result], xlsx_path)
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb["Repo_Admin_Findings"]
        row2 = [cell.value for cell in ws[2]]
        assert "'=MALICIOUS_ORG" in row2
        assert "'@malicious_repo" in row2
        assert "'+Security" in row2
    finally:
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)


def test_local_timestamp_dd_mm_yyyy_format() -> None:
    """Verify local timestamp conforms to DD-MM-YYYY format."""
    import re
    from src.reporters.base_reporter import BaseReporter

    ts = BaseReporter.get_local_timestamp()
    # Expect: DD-MM-YYYY HH:MM:SS TZ (e.g. 04-11-2026 19:10:00 IST)
    match = re.match(r"^(\d{2})-(\d{2})-(\d{4})\s+\d{2}:\d{2}:\d{2}(?:\s+[A-Za-z0-9_-]+)?$", ts)
    assert match is not None, f"Timestamp '{ts}' does not match DD-MM-YYYY pattern."
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    assert 1 <= day <= 31
    assert 1 <= month <= 12
    assert year >= 2024


def test_format_detected_stacks_and_repo_list() -> None:
    """Verify tech stack deduplication and bounded repository list formatting."""
    from src.reporters.base_reporter import BaseReporter

    cleaned = BaseReporter.format_detected_stacks(
        ["C#, C#, C# / .NET Core, Docker Compose, General Software", "C# / .NET Core"]
    )
    assert cleaned == "C# / .NET Core, Docker Compose"

    only_placeholder = BaseReporter.format_detected_stacks(["General Software", "General Platform"])
    assert only_placeholder == "General Software"

    repos = [f"repo-{idx}" for idx in range(1, 9)] + ["repo-1"]
    capped = BaseReporter.format_repo_list(repos, max_display=6)
    assert capped == "repo-1, repo-2, repo-3, repo-4, repo-5, repo-6 (+2 more — see Findings tabs)"


def test_excel_deduplication_and_clean_rendering() -> None:
    """Verify cross-sheet separation, findings vertical deduplication, and PASS/SKIPPED cleanup."""
    from src.checks.base import CheckResult, CheckSeverity, CheckStatus, Persona

    canonical_cfg = '{"name": "Standard Baseline DevContainer"}'
    ai_py_cfg = '{"name": "Python FastAPI AI Tailored DevContainer"}'
    ai_dotnet_cfg = '{"name": "DotNet Core AI Tailored DevContainer"}'

    results = [
        CheckResult(
            check_id="devcontainers",
            check_name="DevContainer Configuration",
            persona=Persona.REPO_ADMIN,
            category="Developer Ergonomics",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.MEDIUM,
            current_state="Missing .devcontainer/devcontainer.json",
            recommended_state=canonical_cfg,
            explanation="Standardize local development environments.",
            remediation_snippet_after=ai_py_cfg,
            documentation_url="https://docs.example.com/devcontainers",
            org_name="acme",
            repo_name="api-one",
            extra_details={
                "detected_stack": "Python, General Software",
                "ai_enriched": True,
                "ai_model": "gemini-3.5-flash-lite",
                "baseline_remediation": canonical_cfg,
            },
        ),
        CheckResult(
            check_id="devcontainers",
            check_name="DevContainer Configuration",
            persona=Persona.REPO_ADMIN,
            category="Developer Ergonomics",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.MEDIUM,
            current_state="Missing .devcontainer/devcontainer.json",
            recommended_state=canonical_cfg,
            explanation="Standardize local development environments.",
            remediation_snippet_after=ai_dotnet_cfg,
            documentation_url="https://docs.example.com/devcontainers",
            org_name="acme",
            repo_name="api-two",
            extra_details={
                "detected_stack": "C#, C# / .NET Core, Python / FastAPI",
                "ai_enriched": True,
                "ai_model": "gemini-3.5-flash-lite",
                "baseline_remediation": canonical_cfg,
            },
        ),
        CheckResult(
            check_id="codeowners_validation",
            check_name="CODEOWNERS Validation",
            persona=Persona.REPO_ADMIN,
            category="Governance",
            status=CheckStatus.PASS,
            severity=CheckSeverity.HIGH,
            current_state="Control verified present and compliant.",
            recommended_state="Maintain baseline CODEOWNERS.",
            explanation="Verified compliant with CODEOWNERS policy.",
            documentation_url="https://docs.example.com/codeowners",
            org_name="acme",
            repo_name="api-one",
        ),
        CheckResult(
            check_id="workflow_sha_pinning",
            check_name="GitHub Actions SHA Pinning",
            persona=Persona.CONTRIBUTOR,
            category="CI/CD Supply Chain",
            status=CheckStatus.SKIPPED,
            severity=CheckSeverity.HIGH,
            current_state="No workflow YAML files to inspect.",
            recommended_state="Pin third-party actions to full 40-char SHAs.",
            explanation="Prevents supply chain tag hijacking.",
            documentation_url="https://docs.example.com/sha-pinning",
            org_name="acme",
            repo_name="api-one",
        ),
    ]

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
        temp_path = f.name

    try:
        ExcelReporter().generate(results, temp_path)
        wb = openpyxl.load_workbook(temp_path)

        # 1. Fleet_Playbook keeps canonical baseline snippet & clean tech stack
        ws_fleet = wb["Fleet_Playbook"]
        assert ws_fleet.max_row == 2
        assert ws_fleet["D2"].value == "C# / .NET Core, Python / FastAPI"
        assert ws_fleet["H2"].value == canonical_cfg
        assert ws_fleet.auto_filter.ref == "A1:I2"

        # 2. AI_Recommendations splits distinct stack snippets while deduplicating repetitive explanation/URL
        ws_ai = wb["AI_Recommendations"]
        assert ws_ai.max_row == 5  # Header + 2 distinct stack rows + blank + AI Safety Notice
        assert ws_ai["F2"].value == "C# / .NET Core, Python / FastAPI"
        assert ws_ai["F3"].value == "Python"
        assert ws_ai["G2"].value == "gemini-3.5-flash-lite"
        assert ws_ai["H2"].value == "AI Tailored"
        assert ws_ai["J2"].value == ai_dotnet_cfg
        assert ws_ai["J3"].value == ai_py_cfg
        assert ws_ai["K2"].value == "Standardize local development environments."
        assert ws_ai["L2"].value == "https://docs.example.com/devcontainers"
        assert ws_ai["K3"].value == "-"
        assert ws_ai["L3"].value == "-"

        # 3. Repo_Admin_Findings: first FAIL shows full explanation/URL, second shows "-", PASS shows clean "-"
        ws_repo = wb["Repo_Admin_Findings"]
        assert ws_repo.auto_filter.ref == "A1:K4"
        assert "(Refer to Fleet Playbook" not in str(ws_repo["J2"].value)
        assert ws_repo["J2"].value == "Standardize local development environments."
        assert ws_repo["K2"].value == "https://docs.example.com/devcontainers"
        assert ws_repo["I3"].value == canonical_cfg
        assert ws_repo["J3"].value == "-"
        assert ws_repo["K3"].value == "-"
        assert ws_repo["H4"].value == "Compliant"
        assert ws_repo["I4"].value == "-"
        assert ws_repo["J4"].value == "-"
        assert ws_repo["K4"].value == "-"

        # 4. Contributor_Findings: SKIPPED shows concise "-" in remediation/doc columns
        ws_contrib = wb["Contributor_Findings"]
        assert ws_contrib["H2"].value == "No workflow YAML files to inspect."
        assert ws_contrib["I2"].value == "-"
        assert ws_contrib["J2"].value == "-"
        assert ws_contrib["K2"].value == "-"
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_finops_and_dora_baseline_rows_suppress_repetitive_advice() -> None:
    """Verify healthy FinOps profiles and zero-deploy DORA repos emit concise '-' instead of filler."""
    from unittest.mock import MagicMock
    from src.finops.models import RepoStorageProfile
    from src.finops.collector import build_finops_rows
    from src.dora.models import RepoDORAMetrics
    from src.dora.collector import build_dora_rows

    ai_spy = MagicMock()

    healthy_profile = RepoStorageProfile(org="acme", repo="clean-svc", visibility="private", disk_size_kb=46080)
    repo_rows, _, _, _ = build_finops_rows([healthy_profile], {}, ["acme"], ai_engine=ai_spy)
    assert repo_rows[0][-1] == "-"
    ai_spy.generate_finops_advisory.assert_not_called()

    zero_deploy_metrics = RepoDORAMetrics(org="acme", repo="docs-only")
    dora_rows, _, _, _ = build_dora_rows([zero_deploy_metrics], ["acme"], window_days=30, ai_engine=ai_spy)
    assert dora_rows[0][14] == "-"
    ai_spy.diagnose_dora_flow.assert_not_called()


def test_audit_score_consistency_excludes_skipped(capsys) -> None:
    """Verify terminal scorecard and BaseReporter exclude SKIPPED checks consistently."""
    from src.checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
    from src.main import print_terminal_scorecard
    from src.reporters.base_reporter import BaseReporter

    results = [
        CheckResult(
            check_id="c1",
            check_name="Check 1",
            persona=Persona.ORG_ADMIN,
            category="Security",
            status=CheckStatus.PASS,
            severity=CheckSeverity.HIGH,
            org_name="acme",
            repo_name="repo1",
            current_state="OK",
            recommended_state="OK",
            explanation="OK",
            documentation_url="https://docs.example.com",
        ),
        CheckResult(
            check_id="c2",
            check_name="Check 2",
            persona=Persona.REPO_ADMIN,
            category="CI/CD",
            status=CheckStatus.SKIPPED,
            severity=CheckSeverity.INFO,
            org_name="acme",
            repo_name="repo1",
            current_state="Skipped",
            recommended_state="N/A",
            explanation="Skipped",
            documentation_url="https://docs.example.com",
        ),
        CheckResult(
            check_id="c3",
            check_name="Check 3",
            persona=Persona.ORG_ADMIN,
            category="Security",
            status=CheckStatus.SKIPPED,
            severity=CheckSeverity.INFO,
            org_name="locked-org",
            repo_name="Organization-Wide",
            current_state="403 Forbidden",
            recommended_state="Grant perms",
            explanation="Skipped due to 403",
            documentation_url="https://docs.example.com",
        ),
    ]
    org_metrics = BaseReporter.build_organization_metrics(results)
    acme = next(m for m in org_metrics if m["org_name"] == "acme")
    locked = next(m for m in org_metrics if m["org_name"] == "locked-org")
    assert acme["compliance_score"] == 100.0
    assert acme["status"] == "COMPLIANT"
    assert locked["status"] == "NOT EVALUATED"

    print_terminal_scorecard(
        results[:2],
        [RepoTarget(org_name="acme", repo_name="repo1", target_personas={"all"})],
        {"xlsx": "out.xlsx"},
    )
    out = capsys.readouterr().out
    assert "100.0%" in out
    assert "SKIPPED" in out


def test_hybrid_ai_and_fallback_recommendations_and_truthful_scorecard(capsys) -> None:
    """Verify partial AI + fallback rows both render truthfully without dropping checks or mislabeling."""
    from datetime import datetime, timezone
    from pathlib import Path
    from src.ai.engine import AIEngine
    from src.activity.models import ActivityConfig
    from src.activity.reporter import write_activity_workbook
    from src.checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
    from src.main import print_terminal_scorecard
    from src.runners.models import RunnersConfig
    from src.runners.reporter import write_runners_workbook

    results = [
        CheckResult(
            check_id="copilot_instructions",
            check_name="Copilot Instructions",
            persona=Persona.CONTRIBUTOR,
            category="AI Readiness",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.LOW,
            org_name="acme",
            repo_name="repo-ai",
            current_state="Missing",
            recommended_state="Baseline instructions",
            explanation="Standardize guidance.",
            remediation_snippet_after="# AI tailored Python snippet",
            extra_details={
                "ai_enriched": True,
                "ai_model": "gemini-3.5-flash-lite",
                "detected_stack": "Python",
            },
        ),
        CheckResult(
            check_id="dependabot_configuration",
            check_name="Dependabot Configuration",
            persona=Persona.REPO_ADMIN,
            category="Supply Chain",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.HIGH,
            org_name="acme",
            repo_name="repo-fallback",
            current_state="Missing dependabot.yml",
            recommended_state="Enable Dependabot updates",
            explanation="Automate dependency patches.",
            remediation_snippet_after="version: 2\nupdates:\n  - package-ecosystem: pip",
        ),
    ]

    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f:
        temp_path = f.name

    try:
        ExcelReporter().generate(
            results,
            temp_path,
            executive_summary=AIEngine.build_deterministic_executive_summary(results, "acme"),
            model_name="Deterministic Policy Engine",
        )
        wb = openpyxl.load_workbook(temp_path)
        ws_sum = wb["Executive_Summary"]
        banner_values = [ws_sum.cell(row=r, column=2).value for r in range(1, ws_sum.max_row + 1)]
        assert any(
            "Deterministic Policy Summary" in str(v) for v in banner_values if v is not None
        )
        assert not any("Synthesized via Gemini" in str(v) for v in banner_values if v is not None)

        ws_ai = wb["AI_Recommendations"]
        sources = {ws_ai[f"A{r}"].value: (ws_ai[f"G{r}"].value, ws_ai[f"H{r}"].value) for r in range(2, ws_ai.max_row + 1)}
        assert "repo-ai" in sources
        assert "repo-fallback" in sources
        assert sources["repo-ai"] == ("gemini-3.5-flash-lite", "AI Tailored")
        assert sources["repo-fallback"] == ("Deterministic Policy Engine", "Standard Policy (Fallback)")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    throttled_engine = AIEngine(provider="gemini", api_key="test-key", model_name="gemini-3.5-flash", enabled=True)
    throttled_engine.trip_circuit_breaker(60.0, reason="429")
    print_terminal_scorecard(
        results,
        [RepoTarget(org_name="acme", repo_name="repo-ai", target_personas={"all"})],
        {"xlsx": "out.xlsx"},
        ai_engine=throttled_engine,
    )
    out = capsys.readouterr().out
    assert "RATE LIMITED / FALLBACK" in out
    assert "1 AI, remainder deterministic" in out

    now = datetime.now(timezone.utc)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f1, tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as f2:
        run_xlsx, act_xlsx = Path(f1.name), Path(f2.name)
    try:
        write_runners_workbook(
            run_xlsx,
            RunnersConfig(),
            now,
            now,
            [],
            [],
            [],
            ai_engine=throttled_engine,
        )
        write_activity_workbook(
            act_xlsx,
            ActivityConfig(),
            now,
            now,
            [],
            [],
            [],
            [],
            1,
            ai_engine=throttled_engine,
        )
        wb_run = openpyxl.load_workbook(run_xlsx)
        wb_act = openpyxl.load_workbook(act_xlsx)
        run_titles = [c.value for row in wb_run["Executive Summary"].iter_rows(values_only=False) for c in row if c.value]
        act_titles = [c.value for row in wb_act["Executive Summary"].iter_rows(values_only=False) for c in row if c.value]
        assert "Fleet Advisory & Autoscaling Guidance (Deterministic Policy)" in run_titles
        assert "Contributor Cadence & Collaboration Summary (Deterministic Policy)" in act_titles
    finally:
        for p in (run_xlsx, act_xlsx):
            if p.exists():
                p.unlink()
