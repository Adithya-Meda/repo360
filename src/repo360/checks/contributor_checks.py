"""Contributor and Fresher Developer Experience (DX) checks."""

from __future__ import annotations

from typing import Any, Dict

from ._helpers import (
    check_org_default_contributing,
    check_org_default_issue_templates,
    check_org_default_pr_template,
)
from .base import BaseCheck, CheckResult, CheckSeverity, CheckStatus, Persona
from ..collector.repo_inspector import RepoInspector


class DevcontainersCheck(BaseCheck):
    """Verifies existence of DevContainers for one-click environment onboarding."""

    check_id = "devcontainers"
    check_name = "DevContainers Standardized Onboarding"
    persona = Persona.CONTRIBUTOR
    category = "Developer Ergonomics (DX)"
    severity = CheckSeverity.MEDIUM

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers"
        has_devcontainer = (
            inspector.check_file_exists(
                org, repo, ".devcontainer/devcontainer.json", default_branch
            )
            or inspector.check_file_exists(
                org, repo, ".devcontainer.json", default_branch
            )
            or inspector.check_file_exists(
                org, repo, ".devcontainer", default_branch
            )
        )

        if has_devcontainer:
            content = (
                inspector.get_file_content(org, repo, ".devcontainer/devcontainer.json", default_branch)
                or inspector.get_file_content(org, repo, ".devcontainer.json", default_branch)
            )
            if isinstance(content, str) and not content.strip():
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=".devcontainer/devcontainer.json is empty or invalid. Please review.",
                    recommended_state="Provide a valid .devcontainer configuration with base image and settings.",
                    explanation="An empty or malformed devcontainer file cannot be used by Codespaces or VS Code.",
                    documentation_url=doc_url,
                    remediation_snippet_before="{}",
                    remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"\n}",
                )
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="DevContainer configuration found.",
                recommended_state="Maintain base container image and extensions.",
                explanation="DevContainers allow new contributors and juniors to spin up a fully configured environment in seconds.",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="Missing .devcontainer/devcontainer.json.",
            recommended_state="Provide a .devcontainer configuration for reproducible local and Codespaces development.",
            explanation="Junior developers frequently lose hours debugging local toolchains (Node/Python/Docker versions); DevContainers eliminate 'works on my machine' drift.",
            remediation_snippet_before="[No devcontainer setup - manual developer machine installation required]",
            remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\",\n  \"customizations\": {\n    \"vscode\": {\n      \"extensions\": [\"ms-python.python\", \"charliermarsh.ruff\"]\n    }\n  }\n}",
            documentation_url="https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers",
        )


class CopilotInstructionsCheck(BaseCheck):
    """Verifies that repository-specific Copilot Instructions exist."""

    check_id = "copilot_instructions"
    check_name = "Repository Copilot Instructions"
    persona = Persona.CONTRIBUTOR
    category = "AI Enablement & DX"
    severity = CheckSeverity.LOW

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot"
        has_instructions = inspector.check_file_exists(
            org, repo, ".github/copilot-instructions.md", default_branch
        )

        if has_instructions:
            content = inspector.get_file_content(org, repo, ".github/copilot-instructions.md", default_branch)
            if isinstance(content, str) and not content.strip():
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=".github/copilot-instructions.md exists but is empty. Please review.",
                    recommended_state="Add project architecture guidelines and testing conventions to copilot-instructions.md.",
                    explanation="An empty instructions file provides no architectural context to AI coding assistants.",
                    remediation_snippet_before="[Empty copilot-instructions.md file]",
                    remediation_snippet_after="# Copilot Instructions\n- Always use pytest fixtures for unit tests.\n- Never hardcode secrets or mock network calls in production paths.\n- All database transactions must use the UnitOfWork pattern.",
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
                repo_name=repo,
                current_state="Custom .github/copilot-instructions.md found.",
                recommended_state="Update prompt guidelines as tech stack evolves.",
                explanation="Instructs GitHub Copilot to generate code adhering to your team's specific patterns, frameworks, and testing styles.",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="Missing .github/copilot-instructions.md.",
            recommended_state="Add .github/copilot-instructions.md with coding conventions and anti-patterns.",
            explanation="Providing contextual instructions teaches AI agents team-specific testing practices and architecture conventions, helping junior engineers write idiomatic code.",
            remediation_snippet_before="[No custom instructions - Copilot uses generic internet defaults]",
            remediation_snippet_after="# Copilot Instructions\n- Always use pytest fixtures for unit tests.\n- Never hardcode secrets or mock network calls in production paths.\n- All database transactions must use the UnitOfWork pattern.",
            documentation_url="https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot",
        )


class IssueTemplatesCheck(BaseCheck):
    """Verifies that structured Issue Templates exist."""

    check_id = "issue_templates"
    check_name = "Standardized Issue Templates"
    persona = Persona.CONTRIBUTOR
    category = "Developer Ergonomics (DX)"
    severity = CheckSeverity.LOW

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        has_templates = (
            inspector.check_file_exists(org, repo, ".github/ISSUE_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, ".github/ISSUE_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, ".github/issue_template.md", default_branch)
            or inspector.check_file_exists(org, repo, "ISSUE_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, "ISSUE_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, "issue_template.md", default_branch)
            or inspector.check_file_exists(org, repo, "docs/ISSUE_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, "docs/ISSUE_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, "docs/issue_template.md", default_branch)
        )

        if has_templates:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="Issue templates configured.",
                recommended_state="Keep issue forms updated with triage labels.",
                explanation="Structured issue templates prompt contributors for exact reproduction steps and environment details.",
                documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository",
            )

        if repo != ".github" and check_org_default_issue_templates(inspector, org):
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="Issue templates configured (inherited from organization '.github' repository).",
                recommended_state="Maintain centralized issue templates in the organization '.github' repository or provide repository-specific overrides.",
                explanation="Structured issue templates prompt contributors for exact reproduction steps and environment details (inherited from organization-level default templates).",
                documentation_url="https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file",
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="No issue templates found in repository or organization '.github' repository.",
            recommended_state="Add issue forms (.github/ISSUE_TEMPLATE/*.yml) for bug reports and feature requests, or configure default templates in the organization '.github' repository.",
            explanation="Unstructured issue filing creates back-and-forth communication delays between contributors and maintainers.",
            remediation_snippet_before="[Blank issue submission box without guidance]",
            remediation_snippet_after="name: Bug Report\ndescription: File a bug\nbody:\n  - type: textarea\n    attributes:\n      label: Steps to Reproduce",
            documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository",
        )


class PullRequestTemplatesCheck(BaseCheck):
    """Verifies that PR templates guide contributors on PR readiness."""

    check_id = "pull_request_templates"
    check_name = "Pull Request Template"
    persona = Persona.CONTRIBUTOR
    category = "Developer Ergonomics (DX)"
    severity = CheckSeverity.LOW

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        has_template = (
            inspector.check_file_exists(org, repo, ".github/PULL_REQUEST_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, ".github/pull_request_template.md", default_branch)
            or inspector.check_file_exists(org, repo, ".github/PULL_REQUEST_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, ".github/pull_request_template", default_branch)
            or inspector.check_file_exists(org, repo, "PULL_REQUEST_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, "pull_request_template.md", default_branch)
            or inspector.check_file_exists(org, repo, "PULL_REQUEST_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, "pull_request_template", default_branch)
            or inspector.check_file_exists(org, repo, "docs/PULL_REQUEST_TEMPLATE.md", default_branch)
            or inspector.check_file_exists(org, repo, "docs/pull_request_template.md", default_branch)
            or inspector.check_file_exists(org, repo, "docs/PULL_REQUEST_TEMPLATE", default_branch)
            or inspector.check_file_exists(org, repo, "docs/pull_request_template", default_branch)
        )

        if has_template:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="Pull Request template configured.",
                recommended_state="Include testing checklists in PR template.",
                explanation="PR templates ensure that developers verify test coverage, security considerations, and changelogs before requesting review.",
                documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository",
            )

        if repo != ".github" and check_org_default_pr_template(inspector, org):
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="Pull Request template configured (inherited from organization '.github' repository).",
                recommended_state="Maintain centralized PR checklist in the organization '.github' repository or provide repository-specific overrides.",
                explanation="PR templates ensure that developers verify test coverage, security considerations, and changelogs before requesting review (inherited from organization-level default template).",
                documentation_url="https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file",
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="No PR template found in repository or organization '.github' repository.",
            recommended_state="Create '.github/PULL_REQUEST_TEMPLATE.md' with PR hygiene checklist, or configure a default PR template in the organization '.github' repository.",
            explanation="Provides junior engineers with an explicit checklist (tests written, documentation updated, secrets checked) prior to PR review.",
            remediation_snippet_before="[Empty PR description field]",
            remediation_snippet_after="## Description\n...\n## Checklist\n- [ ] Unit tests pass locally\n- [ ] No secrets or debug statements committed\n- [ ] Documentation updated",
            documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository",
        )


class PreCommitHooksCheck(BaseCheck):
    """Verifies that pre-commit configurations prevent bad commits locally."""

    check_id = "pre_commit_hooks"
    check_name = "Pre-commit Local Quality Hooks"
    persona = Persona.CONTRIBUTOR
    category = "Developer Ergonomics (DX)"
    severity = CheckSeverity.LOW

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://pre-commit.com/"
        has_precommit = (
            inspector.check_file_exists(
                org, repo, ".pre-commit-config.yaml", default_branch
            )
            or inspector.check_file_exists(
                org, repo, ".pre-commit-config.yml", default_branch
            )
        )

        if has_precommit:
            content = (
                inspector.get_file_content(org, repo, ".pre-commit-config.yaml", default_branch)
                or inspector.get_file_content(org, repo, ".pre-commit-config.yml", default_branch)
            )
            if isinstance(content, str) and ("repos:" not in content or not content.strip()):
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=".pre-commit-config.yaml exists but defines no active hooks. Please review.",
                    recommended_state="Configure linter/formatter hooks (e.g., ruff, black, eslint) in .pre-commit-config.yaml.",
                    explanation="A pre-commit configuration without hook repositories does not enforce any quality gates.",
                    remediation_snippet_before=content.strip() or "[Empty pre-commit config]",
                    remediation_snippet_after="repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n    hooks:\n      - id: trailing-whitespace\n      - id: end-of-file-fixer\n      - id: check-added-large-files",
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
                repo_name=repo,
                current_state=".pre-commit-config.yaml found.",
                recommended_state="Keep pre-commit hook versions updated.",
                explanation="Pre-commit catches formatting, linting, and secret leakage errors immediately on the developer's machine.",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="Missing .pre-commit-config.yaml file.",
            recommended_state="Add .pre-commit-config.yaml with basic lint and secret checks.",
            explanation="Without pre-commit hooks, juniors discover trivial formatting or whitespace failures only after pushing and waiting for CI.",
            remediation_snippet_before="[No local git pre-commit hook configuration]",
            remediation_snippet_after="repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n    hooks:\n      - id: trailing-whitespace\n      - id: end-of-file-fixer\n      - id: check-added-large-files",
            documentation_url="https://pre-commit.com/",
        )


class SetupDocumentationCheck(BaseCheck):
    """Verifies onboarding documentation (README setup instructions & CONTRIBUTING guide)."""

    check_id = "setup_documentation"
    check_name = "Local Setup & Contributing Documentation"
    persona = Persona.CONTRIBUTOR
    category = "Developer Ergonomics (DX)"
    severity = CheckSeverity.LOW

    SETUP_KEYWORDS = ("install", "setup", "getting started", "quickstart", "prerequisites", "running locally", "usage")

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/setting-guidelines-for-repository-contributors"
        has_readme = any(
            inspector.check_file_exists(org, repo, path, default_branch)
            for path in ("README.md", "README.rst", "readme.md")
        )
        has_contributing = any(
            inspector.check_file_exists(org, repo, path, default_branch)
            for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "SETUP.md")
        )
        inherited_contributing = False
        if not has_contributing and repo != ".github" and check_org_default_contributing(inspector, org):
            has_contributing = True
            inherited_contributing = True

        if has_contributing and has_readme:
            state_msg = (
                "README.md and CONTRIBUTING.md present (CONTRIBUTING inherited from organization '.github' repository)."
                if inherited_contributing
                else "README.md and CONTRIBUTING.md / setup documentation present."
            )
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state=state_msg,
                recommended_state="Keep onboarding setup commands and architecture overview updated.",
                explanation="Clear setup and contribution docs accelerate engineer onboarding and reduce support toil.",
                documentation_url=doc_url,
            )

        if has_readme:
            readme_content = (
                inspector.get_file_content(org, repo, "README.md", default_branch)
                or inspector.get_file_content(org, repo, "README.rst", default_branch)
                or ""
            ).lower()
            if any(kw in readme_content for kw in self.SETUP_KEYWORDS):
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.PASS,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state="README contains setup/getting-started documentation.",
                    recommended_state="Consider extracting detailed PR/test conventions into CONTRIBUTING.md.",
                    explanation="Setup instructions inside README enable engineers to run and validate changes locally.",
                    documentation_url=doc_url,
                )

            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="README exists, but lacks explicit setup/install section or CONTRIBUTING.md.",
                recommended_state="Add local setup commands to README.md and create CONTRIBUTING.md.",
                explanation="New contributors need copy-pasteable setup, test, and environment steps to onboard rapidly.",
                remediation_snippet_before="[README without setup or CONTRIBUTING.md]",
                remediation_snippet_after="## Local Setup\n1. Install dependencies\n2. Run tests locally\n\nSee CONTRIBUTING.md for PR conventions.",
                documentation_url=doc_url,
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.WARN,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="Missing README.md and setup/contributing documentation.",
            recommended_state="Add README.md with setup steps and CONTRIBUTING.md.",
            explanation="Without onboarding documentation, new engineers struggle to bootstrap local development and testing.",
            remediation_snippet_before="[No README.md or CONTRIBUTING.md]",
            remediation_snippet_after="# Service Overview\n\n## Getting Started\n...",
            documentation_url=doc_url,
        )
