"""Unit tests for individual Org, Repo, and Contributor checks."""

from unittest.mock import MagicMock
import pytest
import requests

from src.checks.base import CheckStatus
from src.checks.org_checks import (
    OrgDefaultWorkflowPermissionsCheck,
    OrgRulesetsCheck,
    OrgRunnerGroupsCheck,
    OrgSamlSsoEnforcementCheck,
    OrgSecretScanningPushProtectionCheck,
)
from src.checks.repo_checks import (
    BranchRulesetsCheck,
    CodeownersCheck,
    DependabotConfigCheck,
    EnvironmentProtectionGatesCheck,
    WorkflowConcurrencyGroupsCheck,
    WorkflowShaPinningCheck,
)
from src.checks.contributor_checks import (
    CopilotInstructionsCheck,
    DevcontainersCheck,
    IssueTemplatesCheck,
    PreCommitHooksCheck,
    PullRequestTemplatesCheck,
    SetupDocumentationCheck,
)


# --- Org Checks ---

def test_org_rulesets_check() -> None:
    inspector = MagicMock()
    check = OrgRulesetsCheck()

    inspector.get_org_rulesets.return_value = [{"name": "Standard Ruleset", "enforcement": "active"}]
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    inspector.get_org_rulesets.return_value = [{"name": "Dry Run Ruleset", "enforcement": "evaluate"}]
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.WARN

    inspector.get_org_rulesets.return_value = []
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.FAIL

    resp_403 = requests.Response()
    resp_403.status_code = 403
    inspector.get_org_rulesets.side_effect = requests.HTTPError(response=resp_403)
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.SKIPPED


def test_org_secret_scanning_check() -> None:
    inspector = MagicMock()
    check = OrgSecretScanningPushProtectionCheck()

    # Pass - push protection explicitly enabled
    inspector.get_org_info.return_value = {
        "security_and_analysis": {
            "secret_scanning_push_protection": {"status": "enabled"}
        }
    }
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    # Warn - secret scanning alert-only without push protection (no false PASS)
    inspector.get_org_info.return_value = {
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        }
    }
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.WARN

    # Fail
    inspector.get_org_info.return_value = {}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.FAIL

    # Omitted security_and_analysis on non-null org payload -> SKIPPED
    inspector.get_org_info.return_value = {"login": "org"}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.SKIPPED


def test_org_runner_groups_check() -> None:
    inspector = MagicMock()
    check = OrgRunnerGroupsCheck()

    # Pass - no runner groups
    inspector.get_org_runner_groups.return_value = []
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    # Warn - unrestricted visibility on custom group
    inspector.get_org_runner_groups.return_value = [{"name": "custom-group", "visibility": "all", "default": False}]
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.WARN

    # Warn - unrestricted visibility on default group when self-hosted runners exist
    inspector.get_org_runner_groups.return_value = [{"name": "Default", "visibility": "all", "default": True}]
    inspector.get_org_self_hosted_runners_count.return_value = 3
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.WARN

    # Pass - restricted visibility
    inspector.get_org_self_hosted_runners_count.return_value = 0
    inspector.get_org_runner_groups.return_value = [{"name": "custom-group", "visibility": "selected", "default": False}]
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    # Skipped - 403 Permission Gap
    inspector.get_org_runner_groups.return_value = []
    inspector.get_permission_error.return_value = "HTTP 403 Forbidden"
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.SKIPPED


def test_org_default_workflow_permissions_check() -> None:
    inspector = MagicMock()
    check = OrgDefaultWorkflowPermissionsCheck()

    inspector.get_org_workflow_permissions.return_value = {"default_workflow_permissions": "read"}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    inspector.get_org_workflow_permissions.return_value = {
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": True,
    }
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.WARN

    inspector.get_org_workflow_permissions.return_value = {"default_workflow_permissions": "write"}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.FAIL

    inspector.get_org_workflow_permissions.return_value = None
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.SKIPPED


def test_org_saml_sso_enforcement_check() -> None:
    inspector = MagicMock()
    check = OrgSamlSsoEnforcementCheck()

    inspector.get_org_saml_sso.return_value = {"samlIdentityProvider": {"ssoUrl": "https://idp.example.com/sso"}}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.PASS

    inspector.get_org_info.return_value = {"plan": {"name": "enterprise"}}
    inspector.get_org_saml_sso.return_value = {"samlIdentityProvider": None, "requiresTwoFactorAuthentication": True}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.FAIL

    inspector.get_org_saml_sso.return_value = {"error_403": True}
    assert check.evaluate(inspector, "org", {}).status == CheckStatus.SKIPPED


# --- Repo Checks ---

def test_codeowners_check_variations() -> None:
    inspector = MagicMock()
    check = CodeownersCheck()

    # 1. Full catch-all rule -> PASS
    inspector.get_file_content.return_value = "* @fintech-core/maintainers\n/api/ @team-api"
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS

    # 2. Specific rules without catch-all -> WARN
    inspector.get_file_content.return_value = "/api/ @team-api"
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # 3. Missing file -> FAIL
    inspector.get_file_content.return_value = None
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.FAIL

    # 4. Comment/bare wildcard without owner handle -> WARN
    inspector.get_file_content.return_value = "# * @org/team\n*\n/api/ @team-api"
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN


def test_branch_rulesets_check() -> None:
    inspector = MagicMock()
    check = BranchRulesetsCheck()
    thresholds = {"minimum_approvals": 2}

    # Fail - no protection or rulesets
    inspector.get_repo_rulesets.return_value = []
    inspector.get_repo_branch_rules.return_value = []
    inspector.get_branch_protection.return_value = None
    res_fail = check.evaluate(inspector, "org", "repo", "main", thresholds)
    assert res_fail.status == CheckStatus.FAIL

    # Evaluate-only ruleset without enforcement -> WARN (not PASS)
    inspector.get_repo_rulesets.return_value = [{"name": "eval", "enforcement": "evaluate"}]
    res_eval = check.evaluate(inspector, "org", "repo", "main", thresholds)
    assert res_eval.status == CheckStatus.WARN

    # Handles null required_pull_request_reviews cleanly -> WARN
    inspector.get_repo_rulesets.return_value = []
    inspector.get_branch_protection.return_value = {"required_pull_request_reviews": None}
    assert check.evaluate(inspector, "org", "repo", "main", thresholds).status == CheckStatus.WARN

    # Warn - approvals below threshold
    inspector.get_branch_protection.return_value = {
        "required_pull_request_reviews": {"required_approving_review_count": 1}
    }
    res_warn = check.evaluate(inspector, "org", "repo", "main", thresholds)
    assert res_warn.status == CheckStatus.WARN

    # Pass - classic protection meets threshold
    inspector.get_branch_protection.return_value = {
        "required_pull_request_reviews": {"required_approving_review_count": 2}
    }
    res_pass = check.evaluate(inspector, "org", "repo", "main", thresholds)
    assert res_pass.status == CheckStatus.PASS

    # Pass - modern effective branch rules (from rulesets) satisfy full policy thresholds
    inspector.get_branch_protection.return_value = None
    inspector.get_repo_rulesets.return_value = [{"name": "org-std", "enforcement": "active"}]
    inspector.get_repo_branch_rules.return_value = [
        {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
        {"type": "required_linear_history"},
        {"type": "non_fast_forward"},
        {"type": "deletion"},
    ]
    full_thresholds = {
        "minimum_approvals": 2,
        "require_linear_history": True,
        "disallow_force_pushes": True,
        "disallow_deletions": True,
    }
    assert check.evaluate(inspector, "org", "repo", "main", full_thresholds).status == CheckStatus.PASS


def test_environment_protection_gates_check() -> None:
    inspector = MagicMock()
    check = EnvironmentProtectionGatesCheck()

    inspector.get_environments.return_value = []
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.SKIPPED

    inspector.get_environments.return_value = [{"name": "production", "protection_rules": []}]
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.FAIL

    inspector.get_environments.return_value = [
        {"name": "production", "protection_rules": [{"type": "required_reviewers"}]}
    ]
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS


def test_workflow_sha_pinning_check() -> None:
    inspector = MagicMock()
    check = WorkflowShaPinningCheck()

    # No workflows -> SKIPPED
    inspector.list_workflows.return_value = []
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.SKIPPED

    # Unpinned action -> WARN
    inspector.list_workflows.return_value = [{"name": "CI", "path": ".github/workflows/ci.yml"}]
    inspector.get_file_content.return_value = "steps:\n  - uses: actions/checkout@v4"
    res_unpinned = check.evaluate(inspector, "org", "repo", "main", {})
    assert res_unpinned.status == CheckStatus.WARN

    # Pinned action -> PASS (including comment containing './' and uppercase hex)
    inspector.get_file_content.return_value = (
        "steps:\n  - uses: actions/checkout@B4FFDE65F46336AB88EB53BE808477A3936BAE11 # v4.1.1 ./local"
    )
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS

    # Comment with './' must NOT bypass unpinned tag detection
    inspector.get_file_content.return_value = (
        "steps:\n  - uses: actions/checkout@v4 # TODO: migrate from ./old"
    )
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Reusable workflow unpinned -> WARN
    inspector.get_file_content.return_value = (
        "jobs:\n  build:\n    uses: acme/shared-ci/.github/workflows/ci.yml@main"
    )
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN


def test_workflow_concurrency_check() -> None:
    inspector = MagicMock()
    check = WorkflowConcurrencyGroupsCheck()

    inspector.list_workflows.return_value = [{"name": "Build", "path": ".github/workflows/build.yml"}]

    # Missing concurrency -> WARN
    inspector.get_file_content.return_value = "name: Build\non: push\n# concurrency: commented"
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Has concurrency -> PASS
    inspector.get_file_content.return_value = "name: Build\nconcurrency:\n  group: main\n  cancel-in-progress: true"
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS


def test_dependabot_config_check() -> None:
    inspector = MagicMock()
    check = DependabotConfigCheck()

    inspector.check_file_exists.return_value = True
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS

    inspector.check_file_exists.return_value = False
    assert check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.FAIL


# --- Contributor Checks ---

def test_contributor_checks() -> None:
    inspector = MagicMock()

    # DevContainers
    dev_check = DevcontainersCheck()
    inspector.check_file_exists.return_value = True
    assert dev_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert dev_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Copilot Instructions
    copilot_check = CopilotInstructionsCheck()
    inspector.check_file_exists.return_value = True
    assert copilot_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert copilot_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Issue Templates
    issue_check = IssueTemplatesCheck()
    inspector.check_file_exists.return_value = True
    assert issue_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert issue_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # PR Templates
    pr_check = PullRequestTemplatesCheck()
    inspector.check_file_exists.return_value = True
    assert pr_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert pr_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Pre-commit
    pc_check = PreCommitHooksCheck()
    inspector.check_file_exists.return_value = True
    assert pc_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert pc_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN

    # Setup Documentation
    doc_check = SetupDocumentationCheck()
    inspector.check_file_exists.return_value = True
    assert doc_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.PASS
    inspector.check_file_exists.return_value = False
    assert doc_check.evaluate(inspector, "org", "repo", "main", {}).status == CheckStatus.WARN
