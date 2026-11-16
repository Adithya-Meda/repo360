"""Mock result generator for offline verification and local testing."""

from __future__ import annotations

from typing import List

from .checks.base import CheckResult, CheckSeverity, CheckStatus, Persona
from .validator import RepoTarget


def create_mock_results(targets: List[RepoTarget]) -> List[CheckResult]:
    """Generate representative mock audit findings for verification."""
    results: List[CheckResult] = []

    results.append(
        CheckResult(
            check_id="rulesets_adoption",
            check_name="Organization Rulesets Enforcement",
            persona=Persona.ORG_ADMIN,
            category="Governance & Compliance",
            status=CheckStatus.FAIL,
            severity=CheckSeverity.HIGH,
            org_name="fintech-core",
            repo_name="[Organization-Wide]",
            current_state="No active organization-level rulesets found.",
            recommended_state="Create centralized organization rulesets targeting default and release branches.",
            explanation="Without org-level rulesets, branch security depends on disparate repository configurations.",
            remediation_snippet_before="Legacy individual repo branch protection or unmanaged branches.",
            remediation_snippet_after="Organization Settings -> Repository Rulesets -> New ruleset: Enforce on 'All repositories'.",
            remediation_command="gh api --method POST /orgs/fintech-core/rulesets -f name='Standard Branch Governance'",
            documentation_url="https://docs.github.com/en/enterprise-cloud@latest/organizations/managing-organization-settings/managing-rulesets-for-repositories-in-your-organization",
        )
    )

    results.append(
        CheckResult(
            check_id="secret_scanning_push_protection",
            check_name="Organization Secret Scanning Push Protection",
            persona=Persona.ORG_ADMIN,
            category="Security",
            status=CheckStatus.PASS,
            severity=CheckSeverity.CRITICAL,
            org_name="fintech-core",
            repo_name="[Organization-Wide]",
            current_state="Secret Scanning & Push Protection enabled org-wide.",
            recommended_state="Maintain push protection enabled org-wide.",
            explanation="Push protection blocks developers from accidentally pushing secrets, credentials, and API keys before they reach git history.",
            documentation_url="https://docs.github.com/en/enterprise-cloud@latest/code-security/secret-scanning/push-protection-for-repositories-and-organizations",
        )
    )

    for target in targets:
        results.append(
            CheckResult(
                check_id="codeowners_validation",
                check_name="CODEOWNERS Configuration",
                persona=Persona.REPO_ADMIN,
                category="Governance & Maintainability",
                status=CheckStatus.PASS,
                severity=CheckSeverity.HIGH,
                org_name=target.org_name,
                repo_name=target.repo_name,
                current_state="Valid CODEOWNERS file with root wildcard rule present.",
                recommended_state="Maintain CODEOWNERS with active team handles.",
                explanation="CODEOWNERS guarantees that changes are reviewed and approved by designated domain maintainers.",
                remediation_snippet_before="/src/api/ @team-api",
                remediation_snippet_after="* @fintech-core/maintainers\n/src/api/ @team-api",
                documentation_url="https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners",
            )
        )

        results.append(
            CheckResult(
                check_id="workflow_sha_pinning",
                check_name="GitHub Actions Immutable SHA Pinning",
                persona=Persona.REPO_ADMIN,
                category="Supply Chain Security",
                status=CheckStatus.WARN,
                severity=CheckSeverity.HIGH,
                org_name=target.org_name,
                repo_name=target.repo_name,
                current_state="Unpinned third-party actions detected: actions/checkout@v4; actions/setup-python@v5",
                recommended_state="Pin actions to immutable 40-character commit SHAs with comments.",
                explanation="Mutable tags (@v4) can be retargeted or hijacked by malicious actors to inject supply-chain attacks.",
                remediation_snippet_before="- uses: actions/checkout@v4",
                remediation_snippet_after="- uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1",
                documentation_url="https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#using-third-party-actions",
            )
        )

        results.append(
            CheckResult(
                check_id="devcontainers",
                check_name="DevContainers Standardized Onboarding",
                persona=Persona.CONTRIBUTOR,
                category="Developer Ergonomics (DX)",
                status=CheckStatus.WARN,
                severity=CheckSeverity.MEDIUM,
                org_name=target.org_name,
                repo_name=target.repo_name,
                current_state="Missing .devcontainer/devcontainer.json.",
                recommended_state="Provide a .devcontainer configuration for reproducible local and Codespaces development.",
                explanation="Junior developers frequently lose hours debugging local toolchains (Node/Python/Docker versions); DevContainers eliminate 'works on my machine' drift.",
                remediation_snippet_before="[No devcontainer setup - manual developer machine installation required]",
                remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"\n}",
                documentation_url="https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers",
                extra_details={
                    "ai_enriched": True,
                    "detected_stack": "Python 3.12, Docker",
                    "ai_model": "gemini-3.5-flash",
                },
            )
        )

        results.append(
            CheckResult(
                check_id="copilot_instructions",
                check_name="Repository Copilot Instructions",
                persona=Persona.CONTRIBUTOR,
                category="AI Enablement & DX",
                status=CheckStatus.PASS,
                severity=CheckSeverity.LOW,
                org_name=target.org_name,
                repo_name=target.repo_name,
                current_state="Custom .github/copilot-instructions.md found.",
                recommended_state="Add .github/copilot-instructions.md with coding conventions and anti-patterns.",
                explanation="Contextual instructions teach AI agents team-specific testing practices and architecture conventions.",
                remediation_snippet_before="[No custom instructions]",
                remediation_snippet_after="# Copilot Instructions\n- Always use pytest fixtures.\n- Never hardcode credentials.",
                documentation_url="https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot",
            )
        )

    return results
