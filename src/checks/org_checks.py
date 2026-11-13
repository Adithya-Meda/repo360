"""Organization Admin governance and security checks."""

from __future__ import annotations

from typing import Any, Dict
import requests

from ._helpers import get_permission_gap, is_http_forbidden, make_permission_skipped_result
from .base import BaseCheck, CheckResult, CheckSeverity, CheckStatus, Persona
from ..collector.repo_inspector import RepoInspector


class OrgRulesetsCheck(BaseCheck):
    """Verifies that organization-level repository rulesets are enforced."""

    check_id = "rulesets_adoption"
    check_name = "Organization Rulesets Enforcement"
    persona = Persona.ORG_ADMIN
    category = "Governance & Compliance"
    severity = CheckSeverity.HIGH

    def evaluate(
        self, inspector: RepoInspector, org: str, thresholds: Dict[str, Any]
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/enterprise-cloud@latest/organizations/managing-organization-settings/managing-rulesets-for-repositories-in-your-organization"
        try:
            rulesets = inspector.get_org_rulesets(org)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(self, org, "HTTP 403 Forbidden on /orgs/{org}/rulesets.", doc_url)
            raise

        perm_err = get_permission_gap(inspector, org, "org_rulesets")
        if perm_err and not rulesets:
            return make_permission_skipped_result(self, org, perm_err, doc_url)

        active_rulesets = [r for r in rulesets if r.get("enforcement") == "active"]
        evaluate_rulesets = [r for r in rulesets if r.get("enforcement") == "evaluate"]

        if active_rulesets:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state=f"{len(active_rulesets)} actively enforced organization ruleset(s) detected.",
                recommended_state="Enforce centralized organization rulesets across all repositories.",
                explanation="Organization rulesets centrally protect default branches and prevent unauthorized policy bypasses.",
                documentation_url=doc_url,
            )

        if evaluate_rulesets:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=CheckSeverity.MEDIUM,
                org_name=org,
                repo_name="Organization-Wide",
                current_state=f"{len(evaluate_rulesets)} organization ruleset(s) found in 'evaluate' (dry-run) mode; none actively enforced.",
                recommended_state="Switch tested organization rulesets from 'Evaluate' to 'Active' enforcement.",
                explanation="Rulesets in evaluate mode log policy matches without blocking non-compliant pushes or merges.",
                remediation_snippet_before="Enforcement status: Evaluate (non-blocking)",
                remediation_snippet_after="Enforcement status: Active",
                documentation_url=doc_url,
            )

        org_data = inspector.get_org_info(org)
        plan_name = ""
        if isinstance(org_data, dict):
            plan_name = str((org_data.get("plan") or {}).get("name") or "").lower()
        is_enterprise = "enterprise" in plan_name

        if isinstance(org_data, dict) and plan_name and not is_enterprise:
            return make_permission_skipped_result(
                self,
                org,
                f"Organization-level rulesets require GitHub Enterprise Cloud (current organization plan: '{plan_name}'). Individual repository branch protections are evaluated per repo.",
                doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.FAIL,
            severity=self.severity,
            org_name=org,
            repo_name="Organization-Wide",
            current_state="No active organization-level rulesets found.",
            recommended_state="Create centralized organization rulesets targeting default and release branches.",
            explanation="Without org-level rulesets, branch security depends on disparate repository configurations which creates compliance drift.",
            remediation_snippet_before="Legacy individual repo branch protection or unmanaged branches.",
            remediation_snippet_after="Organization Settings -> Repository Rulesets -> New ruleset: Enforce on 'All repositories' with required pull request reviews & linear history.",
            remediation_command="gh api --method POST /orgs/{org}/rulesets -f name='Standard Branch Governance'",
            documentation_url=doc_url,
        )


class OrgSecretScanningPushProtectionCheck(BaseCheck):
    """Verifies that Secret Scanning Push Protection is active across the organization."""

    check_id = "secret_scanning_push_protection"
    check_name = "Organization Secret Scanning Push Protection"
    persona = Persona.ORG_ADMIN
    category = "Security"
    severity = CheckSeverity.CRITICAL

    def evaluate(
        self, inspector: RepoInspector, org: str, thresholds: Dict[str, Any]
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/enterprise-cloud@latest/code-security/secret-scanning/push-protection-for-repositories-and-organizations"
        try:
            org_data = inspector.get_org_info(org)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(self, org, "HTTP 403 Forbidden on /orgs/{org}.", doc_url)
            raise

        perm_err = get_permission_gap(inspector, org, "org_info")
        if perm_err or org_data is None:
            return make_permission_skipped_result(
                self,
                org,
                perm_err or "Organization metadata unavailable.",
                doc_url,
            )

        security_features = org_data.get("security_and_analysis")
        if security_features is None and org_data:
            # Public /orgs/{org} omits security_and_analysis unless Administration: Read + GHAS are accessible
            return make_permission_skipped_result(
                self,
                org,
                "'security_and_analysis' omitted by GitHub API (requires Organization Administration Read and Secret Scanning plan entitlement).",
                doc_url,
            )
        security_features = security_features or {}
        secret_scanning = (security_features.get("secret_scanning") or {}).get("status")
        push_protection = (security_features.get("secret_scanning_push_protection") or {}).get("status")

        if push_protection == "enabled":
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Secret Scanning Push Protection is enabled org-wide.",
                recommended_state="Maintain Secret Scanning Push Protection enabled for all existing and new repositories.",
                explanation="Push protection blocks developers from accidentally pushing secrets, credentials, and API keys before they reach git history.",
                documentation_url=doc_url,
            )

        if secret_scanning == "enabled":
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Secret Scanning alert detection is enabled, but Push Protection (blocking) is disabled.",
                recommended_state="Enable Secret Scanning Push Protection so exposed secrets are blocked prior to push.",
                explanation="Alert-only secret scanning notifies after secrets have already landed in remote commits; push protection prevents exposure upstream.",
                remediation_snippet_before="Secret scanning: enabled | Push protection: disabled",
                remediation_snippet_after="Organization Settings -> Code security -> Secret scanning Push protection: Enabled for new & existing repositories.",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.FAIL,
            severity=self.severity,
            org_name=org,
            repo_name="Organization-Wide",
            current_state="Secret scanning push protection is not enabled org-wide.",
            recommended_state="Enable Secret Scanning & Push Protection automatically for all repositories.",
            explanation="Exposing secrets in Git history requires costly credential revocation and poses high breach risk.",
            remediation_snippet_before="Push protection: Disabled (developers can push raw API keys/tokens)",
            remediation_snippet_after="Organization Settings -> Code security and analysis -> Secret scanning & Push protection: Checked and enforced for new repos.",
            remediation_command="gh api --method PATCH /orgs/{org} -F 'secret_scanning_push_protection_custom_link=https://wiki.internal/security'",
            documentation_url=doc_url,
        )


class OrgRunnerGroupsCheck(BaseCheck):
    """Audits self-hosted runner groups to prevent unrestricted repository access."""

    check_id = "runner_groups_governance"
    check_name = "Runner Groups Least Privilege Isolation"
    persona = Persona.ORG_ADMIN
    category = "Infrastructure & CI/CD"
    severity = CheckSeverity.MEDIUM

    def evaluate(
        self, inspector: RepoInspector, org: str, thresholds: Dict[str, Any]
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/enterprise-cloud@latest/actions/hosting-your-own-runners/managing-self-hosted-runners/managing-access-to-self-hosted-runners-using-groups"
        try:
            runner_groups = inspector.get_org_runner_groups(org)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden on /orgs/{org}/actions/runner-groups.", doc_url
                )
            raise

        perm_err = get_permission_gap(inspector, org, "org_runner_groups")
        if perm_err and not runner_groups:
            return make_permission_skipped_result(self, org, perm_err, doc_url)

        if not runner_groups:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="No unmanaged self-hosted runner groups detected (using GitHub-hosted runners).",
                recommended_state="Ensure all self-hosted runner groups restrict visibility to approved repositories.",
                explanation="Restricting runner groups prevents untrusted public or internal forks from executing arbitrary commands on internal build infrastructure.",
                documentation_url=doc_url,
            )

        runner_count_fn = getattr(inspector, "get_org_self_hosted_runners_count", None)
        raw_runner_count = runner_count_fn(org) if callable(runner_count_fn) else None
        has_org_self_hosted = isinstance(raw_runner_count, int) and raw_runner_count > 0

        unrestricted_groups = []
        for g in runner_groups:
            if g.get("visibility") != "all":
                continue
            is_default = bool(g.get("default"))
            allows_public = bool(g.get("allows_public_repositories"))
            has_inline_runners = bool(g.get("runners"))
            if not is_default or allows_public or has_inline_runners or has_org_self_hosted:
                unrestricted_groups.append(g.get("name") or "Default")

        if unrestricted_groups:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state=f"Runner groups accessible to all repositories: {', '.join(unrestricted_groups)}",
                recommended_state="Restrict runner groups to explicit repository lists and disallow public repository execution.",
                explanation="Unrestricted runner groups allow any repository to consume build compute and potentially access internal network credentials.",
                remediation_snippet_before="Runner Group Visibility: All repositories",
                remediation_snippet_after="Runner Group Visibility: Selected repositories only (and Disallow public repositories)",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.PASS,
            severity=self.severity,
            org_name=org,
            repo_name="Organization-Wide",
            current_state="Self-hosted runner groups adhere to restricted visibility.",
            recommended_state="Maintain repository isolation on runner groups.",
            explanation="Runner groups are isolated to approved repositories.",
            documentation_url=doc_url,
        )


class OrgDefaultWorkflowPermissionsCheck(BaseCheck):
    """Verifies default GITHUB_TOKEN permissions are read-only across the org."""

    check_id = "default_workflow_permissions"
    check_name = "Default GITHUB_TOKEN Least Privilege"
    persona = Persona.ORG_ADMIN
    category = "Security"
    severity = CheckSeverity.HIGH

    def evaluate(
        self, inspector: RepoInspector, org: str, thresholds: Dict[str, Any]
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/enterprise-cloud@latest/actions/security-guides/automatic-token-authentication#modifying-the-permissions-for-the-github_token"
        wf_getter = getattr(inspector, "get_org_workflow_permissions", None)
        try:
            perms = wf_getter(org) if callable(wf_getter) else inspector.get_org_actions_permissions(org)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden on /orgs/{org}/actions/permissions/workflow.", doc_url
                )
            raise

        perm_err = get_permission_gap(inspector, org, "org_workflow_permissions")
        if perm_err or not isinstance(perms, dict) or "default_workflow_permissions" not in perms:
            return make_permission_skipped_result(
                self,
                org,
                perm_err or "Default workflow permissions unavailable (requires Organization Administration Read).",
                doc_url,
            )

        default_perm = perms.get("default_workflow_permissions")
        can_approve_prs = bool(perms.get("can_approve_pull_request_reviews", False))

        if default_perm == "read" and not can_approve_prs:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Default workflow permission is Read-only and PR approvals by Actions are disabled.",
                recommended_state="Enforce read-only default GITHUB_TOKEN permissions org-wide.",
                explanation="Default read-only permissions prevent workflow compromise from automatically granting write access to repositories.",
                documentation_url=doc_url,
            )

        if default_perm == "read" and can_approve_prs:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=CheckSeverity.MEDIUM,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Default workflow permission is Read-only, but GitHub Actions can approve pull requests.",
                recommended_state="Disable 'Allow GitHub Actions to create and approve pull requests' unless strictly required.",
                explanation="Allowing Actions tokens to approve pull requests weakens human peer-review separation of duties.",
                remediation_snippet_before="can_approve_pull_request_reviews: true",
                remediation_snippet_after="Organization Settings -> Actions -> General -> Uncheck 'Allow GitHub Actions to create and approve pull requests'",
                remediation_command="gh api --method PUT /orgs/{org}/actions/permissions/workflow -F default_workflow_permissions='read' -F can_approve_pull_request_reviews=false",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.FAIL,
            severity=self.severity,
            org_name=org,
            repo_name="Organization-Wide",
            current_state=f"Default workflow permission is set to '{default_perm}' (elevated write scope).",
            recommended_state="Change organization default workflow permission to Read-only.",
            explanation="Read/write tokens allow any compromised workflow dependency to push code or alter releases.",
            remediation_snippet_before="Workflow permissions: Read and write permissions",
            remediation_snippet_after="Organization Settings -> Actions -> General -> Workflow permissions: 'Read repository contents and packages permissions'",
            remediation_command="gh api --method PUT /orgs/{org}/actions/permissions/workflow -F default_workflow_permissions='read'",
            documentation_url="https://docs.github.com/en/enterprise-cloud@latest/actions/security-guides/automatic-token-authentication",
        )


class OrgSamlSsoEnforcementCheck(BaseCheck):
    """Verifies SAML Single Sign-On (or enterprise 2FA baseline) for organization access."""

    check_id = "saml_sso_enforcement"
    check_name = "Organization SAML SSO Enforcement"
    persona = Persona.ORG_ADMIN
    category = "Identity & Access Security"
    severity = CheckSeverity.HIGH

    def evaluate(
        self, inspector: RepoInspector, org: str, thresholds: Dict[str, Any]
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/enterprise-cloud@latest/organizations/managing-saml-single-sign-on-for-your-organization/about-identity-and-access-management-with-saml-single-sign-on"
        saml_getter = getattr(inspector, "get_org_saml_sso", None)
        saml_info = saml_getter(org) if callable(saml_getter) else None
        org_data = inspector.get_org_info(org)

        if not isinstance(saml_info, dict):
            perm_err = get_permission_gap(inspector, org, "org_saml_sso")
            return make_permission_skipped_result(
                self,
                org,
                perm_err or "SAML SSO identity provider inspection requires GitHub Enterprise Cloud GraphQL access.",
                doc_url,
            )

        if saml_info.get("error_403"):
            return make_permission_skipped_result(
                self,
                org,
                get_permission_gap(inspector, org, "org_saml_sso") or "GraphQL SAML SSO query returned HTTP 403 Forbidden.",
                doc_url,
            )

        idp = saml_info.get("samlIdentityProvider") or {}
        if isinstance(idp, dict) and idp.get("ssoUrl"):
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Organization SAML Single Sign-On (SSO) identity provider is active.",
                recommended_state="Maintain SAML SSO enforcement and periodic SCIM/access reviews.",
                explanation="SAML SSO centralizes authentication, enforces corporate IdP policies, and revokes GitHub org access upon offboarding.",
                documentation_url=doc_url,
            )

        plan_name = ""
        if isinstance(org_data, dict):
            plan_name = str((org_data.get("plan") or {}).get("name") or "").lower()
        requires_2fa = saml_info.get("requiresTwoFactorAuthentication")
        if requires_2fa is None and isinstance(org_data, dict):
            requires_2fa = org_data.get("two_factor_requirement_enabled")
        is_enterprise = "enterprise" in plan_name

        if is_enterprise:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.FAIL,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="Enterprise organization has no active SAML SSO identity provider configured.",
                recommended_state="Configure and enforce SAML SSO with your corporate IdP in Organization Settings.",
                explanation="Without SAML SSO on GitHub Enterprise, employee offboarding and conditional access policies cannot be enforced centrally.",
                remediation_snippet_before="SAML SSO: Not configured",
                remediation_snippet_after="Organization Settings -> Authentication security -> Enable SAML authentication",
                documentation_url=doc_url,
            )

        if requires_2fa is False:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name="Organization-Wide",
                current_state="SAML SSO not configured (non-Enterprise plan) and mandatory 2FA is disabled.",
                recommended_state="Require two-factor authentication org-wide and evaluate Enterprise Cloud for SAML SSO.",
                explanation="Without SAML SSO or mandatory 2FA, organization members rely only on single-factor GitHub credentials.",
                remediation_snippet_before="two_factor_requirement_enabled: false",
                remediation_snippet_after="Organization Settings -> Authentication security -> Require two-factor authentication for everyone",
                documentation_url=doc_url,
            )

        return make_permission_skipped_result(
            self,
            org,
            "Organization is not on GitHub Enterprise Cloud (SAML SSO gated to GHEC; mandatory 2FA is active).",
            doc_url,
        )

