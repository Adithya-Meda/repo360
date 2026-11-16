"""Advisory and governance check registry."""

from .base import BaseCheck, CheckResult, CheckSeverity, CheckStatus, Persona
from .org_checks import (
    OrgDefaultWorkflowPermissionsCheck,
    OrgRulesetsCheck,
    OrgRunnerGroupsCheck,
    OrgSamlSsoEnforcementCheck,
    OrgSecretScanningPushProtectionCheck,
)
from .repo_checks import (
    BranchRulesetsCheck,
    CodeownersCheck,
    DependabotConfigCheck,
    EnvironmentProtectionGatesCheck,
    WorkflowConcurrencyGroupsCheck,
    WorkflowShaPinningCheck,
)
from .contributor_checks import (
    CopilotInstructionsCheck,
    DevcontainersCheck,
    IssueTemplatesCheck,
    PreCommitHooksCheck,
    PullRequestTemplatesCheck,
    SetupDocumentationCheck,
)

ALL_ORG_CHECKS = [
    OrgRulesetsCheck,
    OrgSecretScanningPushProtectionCheck,
    OrgSamlSsoEnforcementCheck,
    OrgRunnerGroupsCheck,
    OrgDefaultWorkflowPermissionsCheck,
]

ALL_REPO_CHECKS = [
    CodeownersCheck,
    BranchRulesetsCheck,
    EnvironmentProtectionGatesCheck,
    WorkflowShaPinningCheck,
    WorkflowConcurrencyGroupsCheck,
    DependabotConfigCheck,
]

ALL_CONTRIBUTOR_CHECKS = [
    DevcontainersCheck,
    CopilotInstructionsCheck,
    IssueTemplatesCheck,
    PullRequestTemplatesCheck,
    PreCommitHooksCheck,
    SetupDocumentationCheck,
]

__all__ = [
    "BaseCheck",
    "CheckResult",
    "CheckSeverity",
    "CheckStatus",
    "Persona",
    "ALL_ORG_CHECKS",
    "ALL_REPO_CHECKS",
    "ALL_CONTRIBUTOR_CHECKS",
]

