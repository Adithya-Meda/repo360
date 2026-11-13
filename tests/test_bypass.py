"""Unit and integration tests for Repo360 Push Protection & Ruleset Bypass Ledger."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import requests
from openpyxl import load_workbook

from src.bypass.models import (
    BypassConfig,
    PushProtectionBypassRecord,
    RepoBypassProfile,
    RulesetBypassRecord,
    classify_bypass_risk,
)
from src.bypass.collector import (
    build_bypass_rows,
    collapse_plan_limitations,
    collect_repo_push_bypasses,
    collect_repo_ruleset_bypasses,
    profile_repo_bypasses,
)
from src.bypass.reporter import write_bypass_workbook


def test_bypass_record_models():
    now = datetime.now(timezone.utc)
    push_rec = PushProtectionBypassRecord(
        org="fintech-org",
        repo="payment-service",
        alert_number=42,
        secret_type="GitHub Personal Access Token",
        actor="dev-user-1",
        timestamp=now,
        resolution="used_in_tests",
        resolution_comment="Test token with dummy scopes",
        push_protection_bypassed=True,
        html_url="https://github.com/fintech-org/payment-service/security/secret-scanning/42",
    )
    assert push_rec.alert_number == 42
    assert push_rec.actor == "dev-user-1"
    assert push_rec.push_protection_bypassed is True

    rule_rec = RulesetBypassRecord(
        org="fintech-org",
        repo="payment-service",
        rule_suite_id=1001,
        ref="refs/heads/main",
        actor="lead-architect",
        evaluation_result="bypass",
        bypassed_rules="Required linear history, Pull request",
        commit_sha="a1b2c3d4e5",
        timestamp=now,
    )
    assert rule_rec.rule_suite_id == 1001
    assert rule_rec.actor == "lead-architect"


def test_repo_bypass_profile_aggregation():
    profile = RepoBypassProfile(org="fintech-org", repo="payment-service")
    profile.push_bypasses.append(
        PushProtectionBypassRecord(
            org="fintech-org",
            repo="payment-service",
            alert_number=1,
            secret_type="AWS Access Key",
            actor="engineer-a",
        )
    )
    profile.ruleset_bypasses.append(
        RulesetBypassRecord(
            org="fintech-org",
            repo="payment-service",
            rule_suite_id=99,
            ref="main",
            actor="engineer-b",
        )
    )

    assert profile.total_bypasses == 2
    assert profile.unique_actors == {"engineer-a", "engineer-b"}


def test_build_bypass_rows():
    now = datetime.now(timezone.utc)
    profile = RepoBypassProfile(org="fintech-org", repo="auth-service")
    profile.push_bypasses.append(
        PushProtectionBypassRecord(
            org="fintech-org",
            repo="auth-service",
            alert_number=10,
            secret_type="Slack Webhook URL",
            actor="dev-ops-user",
            timestamp=now,
            resolution="false_positive",
            resolution_comment="Mock webhook",
            push_protection_bypassed=True,
        )
    )
    profile.ruleset_bypasses.append(
        RulesetBypassRecord(
            org="fintech-org",
            repo="auth-service",
            rule_suite_id=505,
            ref="release/v2",
            actor="dev-ops-user",
            evaluation_result="bypass",
            bypassed_rules="Required reviews",
            commit_sha="abcdef1234",
            timestamp=now,
        )
    )

    push_rows, ruleset_rows, org_rows, actor_rows = build_bypass_rows(
        profiles=[profile],
        orgs=["fintech-org"],
    )

    assert len(push_rows) == 1
    assert push_rows[0][0] == "fintech-org"
    assert push_rows[0][1] == "auth-service"
    assert push_rows[0][3] == "Slack Webhook URL"
    assert push_rows[0][4] == "dev-ops-user"

    assert len(ruleset_rows) == 1
    assert ruleset_rows[0][0] == "fintech-org"
    assert ruleset_rows[0][2] == 505
    assert ruleset_rows[0][4] == "dev-ops-user"

    assert len(org_rows) == 1
    assert org_rows[0][0] == "fintech-org"
    assert org_rows[0][2] == 1
    assert org_rows[0][3] == 1
    assert org_rows[0][4] == 2
    assert org_rows[0][5] == 1

    assert len(actor_rows) == 1
    assert actor_rows[0][1] == "dev-ops-user"
    assert actor_rows[0][2] == 1
    assert actor_rows[0][3] == 1
    assert actor_rows[0][4] == 2


def test_write_bypass_workbook(tmp_path: Path):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=90)
    profile = RepoBypassProfile(org="demo-org", repo="demo-repo")

    push_rows, ruleset_rows, org_rows, actor_rows = build_bypass_rows(
        profiles=[profile],
        orgs=["demo-org"],
    )

    out_file = tmp_path / "bypass_test.xlsx"
    cfg = BypassConfig(output_dir=str(tmp_path), lookback_days=90)

    write_bypass_workbook(
        path=out_file,
        cfg=cfg,
        since=since,
        end=now,
        push_rows=push_rows,
        ruleset_rows=ruleset_rows,
        org_rows=org_rows,
        actor_rows=actor_rows,
        failures=[],
        execution_duration=1.8,
    )

    assert out_file.exists()
    wb = load_workbook(out_file)
    sheet_names = wb.sheetnames
    assert "Executive Summary" in sheet_names
    assert "Push Protection Bypasses" in sheet_names
    assert "Ruleset Bypasses" in sheet_names
    assert "Actor Accountability Ledger" in sheet_names


def test_bypass_hardened_accuracy_and_403_handling(tmp_path: Path):
    """Verify closed non-bypass alerts are excluded, 403 surfaces as unaudited, and repo dedup is org-scoped."""
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    mock_client = MagicMock()
    mock_client.paginate.return_value = [
        {
            "number": 101,
            "secret_type_display_name": "GitHub Token",
            "push_protection_bypassed": False,
            "resolution": "revoked",
            "resolved_by": {"login": "security-reviewer"},
            "resolved_at": ts_str,
            "created_at": ts_str,
        },
        {
            "number": 102,
            "secret_type_display_name": "AWS Secret Key",
            "push_protection_bypassed": True,
            "push_protection_bypassed_by": {"login": "BypassActor"},
            "push_protection_bypassed_at": ts_str,
            "resolution": None,
            "created_at": ts_str,
        },
    ]
    recs = collect_repo_push_bypasses(mock_client, "org-a", "api", now - timedelta(days=7))
    assert len(recs) == 1
    assert recs[0].alert_number == 102
    assert recs[0].actor == "BypassActor"
    assert recs[0].ai_risk_assessment == "High Risk"

    err_resp = requests.Response()
    err_resp.status_code = 403
    blocked_client = MagicMock()
    blocked_client.paginate.side_effect = requests.HTTPError(response=err_resp)
    blocked_profile = profile_repo_bypasses(
        blocked_client, "locked-org", "secret-repo", now - timedelta(days=7), {"pushed_at": ts_str}
    )
    assert len(blocked_profile.errors) >= 1

    p1 = RepoBypassProfile(org="org-a", repo="api", push_bypasses=recs)
    p2 = RepoBypassProfile(
        org="org-b",
        repo="api",
        ruleset_bypasses=[
            RulesetBypassRecord("org-b", "api", 55, "refs/heads/main", "bypassactor", timestamp=now)
        ],
    )
    push_rows, rule_rows, org_rows, actor_rows = build_bypass_rows(
        [p1, p2, blocked_profile], ["org-a", "org-b", "locked-org"]
    )
    locked_org_row = next(r for r in org_rows if r[0] == "locked-org")
    assert locked_org_row[7] == "Unknown (Partial)"

    assert classify_bypass_risk(0, 21) == "High"
    assert classify_bypass_risk(0, 20) == "Medium"

    out_file = tmp_path / "bypass_hardened.xlsx"
    write_bypass_workbook(
        path=out_file,
        cfg=BypassConfig(output_dir=str(tmp_path), lookback_days=30),
        since=now - timedelta(days=30),
        end=now,
        push_rows=push_rows,
        ruleset_rows=rule_rows,
        org_rows=org_rows,
        actor_rows=actor_rows,
        failures=[("locked-org/secret-repo", err) for err in blocked_profile.errors],
        execution_duration=0.5,
    )
    wb = load_workbook(out_file)
    assert "Unaudited & Permission Gaps" in wb.sheetnames
    exec_text = {
        ws_row[0].value: ws_row[1].value
        for ws_row in wb["Executive Summary"].iter_rows()
        if len(ws_row) > 1 and ws_row[0].value is not None
    }
    assert exec_text["Repositories with Bypasses"] == 2

    zero_file = tmp_path / "bypass_partial.xlsx"
    write_bypass_workbook(
        path=zero_file,
        cfg=BypassConfig(output_dir=str(tmp_path), lookback_days=30),
        since=now - timedelta(days=30),
        end=now,
        push_rows=[],
        ruleset_rows=[],
        org_rows=[["locked-org", 1, 0, 0, 0, 0, "None", "Unknown (Partial)"]],
        actor_rows=[],
        failures=[("locked-org/secret-repo", "403 Forbidden")],
    )
    wb_zero = load_workbook(zero_file)
    zero_text = "\n".join(str(c.value) for row in wb_zero["Executive Summary"].iter_rows() for c in row if c.value)
    assert "Partial Coverage" in zero_text
    assert "Satisfactory (No Bypasses)" not in zero_text


def test_bypass_plan_tier_limitation_handling(tmp_path: Path):
    """Verify GitHub Free plan 403 paywall is detected as plan limitation, not false permission error."""
    now = datetime.now(timezone.utc)

    err_resp = requests.Response()
    err_resp.status_code = 403
    err_resp._content = b'{"message":"Upgrade to GitHub Pro or make this repository public to enable this feature."}'
    blocked_client = MagicMock()
    blocked_client.paginate.side_effect = requests.HTTPError(response=err_resp)

    # 1. Profile collection tags error as PLAN_LIMITATION_RULESET_MSG, NOT administration:read
    errors: list[str] = []
    recs = collect_repo_ruleset_bypasses(blocked_client, "FreeOrg", "priv-repo1", now - timedelta(days=7), errors_out=errors)
    assert len(recs) == 0
    assert len(errors) == 1
    assert "GitHub Plan limitation" in errors[0]
    assert "grant administration:read" not in errors[0]

    # 2. Simulate 3 private repos in FreeOrg hitting plan limitations
    p1 = RepoBypassProfile(org="FreeOrg", repo="repo1", errors=[errors[0]])
    p2 = RepoBypassProfile(org="FreeOrg", repo="repo2", errors=[errors[0]])
    p3 = RepoBypassProfile(org="FreeOrg", repo="repo3", errors=[errors[0]])

    cleaned_profiles, failures = collapse_plan_limitations([p1, p2, p3])
    # Collapsed into exactly 1 org-scoped entry
    assert len(failures) == 1
    assert failures[0][0] == "FreeOrg (Private Repositories)"
    assert "GitHub Plan limitation" in failures[0][1]
    # Repo profiles cleaned of plan limitations
    assert p1.errors == []
    assert p2.errors == []
    assert p3.errors == []

    # 3. Org risk classification with 0 bypasses remains Low (not Unknown/Partial)
    push_rows, ruleset_rows, org_rows, actor_rows = build_bypass_rows(cleaned_profiles, ["FreeOrg"])
    assert len(org_rows) == 1
    assert org_rows[0][0] == "FreeOrg"
    assert org_rows[0][7] == "Low"

    # 4. Excel report generates clean posture and separate KPI
    out_file = tmp_path / "bypass_free_plan.xlsx"
    write_bypass_workbook(
        path=out_file,
        cfg=BypassConfig(output_dir=str(tmp_path), lookback_days=30),
        since=now - timedelta(days=30),
        end=now,
        push_rows=push_rows,
        ruleset_rows=ruleset_rows,
        org_rows=org_rows,
        actor_rows=actor_rows,
        failures=failures,
        execution_duration=1.2,
    )
    wb = load_workbook(out_file)
    exec_ws = wb["Executive Summary"]
    kpis = {r[0].value: r[1].value for r in exec_ws.iter_rows() if len(r) > 1 and r[0].value}

    assert kpis["Unaudited / API Permission Gaps"] == 0
    assert kpis["GitHub Plan Tier Limitations"] == 1
    assert "Plan-Restricted" in kpis["Enterprise Compliance Posture"]
    assert "Satisfactory" in kpis["Enterprise Compliance Posture"]

    # Gaps sheet has the collapsed org-level entry
    assert "Unaudited & Permission Gaps" in wb.sheetnames
    gap_ws = wb["Unaudited & Permission Gaps"]
    gap_rows = list(gap_ws.iter_rows(values_only=True))
    assert len(gap_rows) == 2  # header + 1 row
    assert gap_rows[1][0] == "FreeOrg (Private Repositories)"
    assert "GitHub Plan limitation" in gap_rows[1][1]
