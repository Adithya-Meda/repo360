"""Contributor and Fresher Developer Experience (DX) checks."""

from __future__ import annotations

from typing import Any, Dict

from ._helpers import (
    check_org_default_contributing,
    check_org_default_issue_templates,
    check_org_default_pr_template,
    get_org_default_contributing_path,
    get_org_default_issue_template_path,
    get_org_default_pr_template_path,
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
        devcontainer_file: str | None = None
        if inspector.check_file_exists(org, repo, ".devcontainer/devcontainer.json", default_branch):
            devcontainer_file = ".devcontainer/devcontainer.json"
        elif inspector.check_file_exists(org, repo, ".devcontainer.json", default_branch):
            devcontainer_file = ".devcontainer.json"
        else:
            tree_set = None
            cache_lock = getattr(inspector, "_cache_lock", None)
            tree_cache = getattr(inspector, "_repo_tree_cache", None)
            if cache_lock is not None and isinstance(tree_cache, dict):
                with cache_lock:
                    tree_set = tree_cache.get((org, repo, default_branch))
            if tree_set:
                devcontainer_file = next(
                    (p for p in sorted(tree_set) if p.startswith(".devcontainer/") and p.endswith("/devcontainer.json")),
                    None,
                )

        has_devcontainer_dir = inspector.check_file_exists(org, repo, ".devcontainer", default_branch)
        target_file = devcontainer_file or ".devcontainer/devcontainer.json"

        if devcontainer_file:
            content = inspector.get_file_content(org, repo, devcontainer_file, default_branch)
            if content is None:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{devcontainer_file} exists but could not be read. Please review.",
                    recommended_state="Provide a valid .devcontainer configuration with base image and settings.",
                    explanation="Dev container configuration could not be read or verified.",
                    documentation_url=doc_url,
                    remediation_snippet_before="{}",
                    remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"\n}",
                    extra_details={"target_file": target_file},
                )
            if not content.strip():
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{devcontainer_file} is empty. Please review.",
                    recommended_state="Provide a valid .devcontainer configuration with base image and settings.",
                    explanation="An empty devcontainer file cannot be used by Codespaces or VS Code.",
                    documentation_url=doc_url,
                    remediation_snippet_before="{}",
                    remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"\n}",
                    extra_details={"target_file": target_file},
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
                extra_details={"target_file": target_file},
            )

        if has_devcontainer_dir:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state=".devcontainer directory exists, but devcontainer.json is missing. Please review.",
                recommended_state="Provide a valid devcontainer.json inside .devcontainer/ with base image and settings.",
                explanation="GitHub Codespaces and VS Code Dev Containers require devcontainer.json to configure containerized environments.",
                documentation_url=doc_url,
                remediation_snippet_before="# .devcontainer exists without devcontainer.json",
                remediation_snippet_after="{\n  \"name\": \"Service Dev Container\",\n  \"image\": \"mcr.microsoft.com/devcontainers/python:3.12\"\n}",
                extra_details={"target_file": ".devcontainer/devcontainer.json"},
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
            extra_details={"target_file": ".devcontainer/devcontainer.json"},
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
            if content is None:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=".github/copilot-instructions.md exists but could not be read. Please review.",
                    recommended_state="Ensure file permissions allow reading .github/copilot-instructions.md.",
                    explanation="File exists in git tree but content could not be read to verify instructions.",
                    documentation_url=doc_url,
                )
            if not content.strip():
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
        found_file = next(
            (
                p
                for p in (
                    ".github/ISSUE_TEMPLATE.md",
                    ".github/issue_template.md",
                    "ISSUE_TEMPLATE.md",
                    "issue_template.md",
                    "docs/ISSUE_TEMPLATE.md",
                    "docs/issue_template.md",
                )
                if inspector.check_file_exists(org, repo, p, default_branch)
            ),
            None,
        )
        found_dir = next(
            (
                d
                for d in (
                    ".github/ISSUE_TEMPLATE",
                    "ISSUE_TEMPLATE",
                    "docs/ISSUE_TEMPLATE",
                )
                if inspector.check_file_exists(org, repo, d, default_branch)
            ),
            None,
        )

        if found_file:
            content = inspector.get_file_content(org, repo, found_file, default_branch)
            if content is not None and not content.strip():
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"Issue template '{found_file}' exists but is empty. Please review.",
                    recommended_state="Add structured issue form fields or markdown sections to the issue template.",
                    explanation="An empty issue template provides no guidance or triage structure for new issues.",
                    documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository",
                    extra_details={"target_file": found_file},
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
                current_state="Issue templates configured.",
                recommended_state="Keep issue forms updated with triage labels.",
                explanation="Structured issue templates prompt contributors for exact reproduction steps and environment details.",
                documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository",
                extra_details={"target_file": found_file},
            )

        if found_dir:
            dir_target = f"{found_dir}/"
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
                extra_details={"target_file": dir_target},
            )

        org_issue_template_path = get_org_default_issue_template_path(inspector, org)
        if repo != ".github" and (org_issue_template_path or check_org_default_issue_templates(inspector, org)):
            target_inherited = f"{org_issue_template_path} (Org-Inherited)" if org_issue_template_path else ".github/ISSUE_TEMPLATE/ (Org-Inherited)"
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
                extra_details={"target_file": target_inherited},
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
        found_template = next(
            (
                path
                for path in (
                    ".github/PULL_REQUEST_TEMPLATE.md",
                    ".github/pull_request_template.md",
                    ".github/PULL_REQUEST_TEMPLATE",
                    ".github/pull_request_template",
                    "PULL_REQUEST_TEMPLATE.md",
                    "pull_request_template.md",
                    "PULL_REQUEST_TEMPLATE",
                    "pull_request_template",
                    "docs/PULL_REQUEST_TEMPLATE.md",
                    "docs/pull_request_template.md",
                    "docs/PULL_REQUEST_TEMPLATE",
                    "docs/pull_request_template",
                )
                if inspector.check_file_exists(org, repo, path, default_branch)
            ),
            None,
        )

        if found_template:
            is_dir_template = found_template in (
                ".github/PULL_REQUEST_TEMPLATE",
                ".github/pull_request_template",
                "PULL_REQUEST_TEMPLATE",
                "pull_request_template",
                "docs/PULL_REQUEST_TEMPLATE",
                "docs/pull_request_template",
            )
            template_target = f"{found_template}/" if is_dir_template else found_template
            content = inspector.get_file_content(org, repo, found_template, default_branch)
            if content is not None and not content.strip():
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"Pull Request template '{template_target}' exists but is empty. Please review.",
                    recommended_state="Include testing checklists and summary sections in the PR template.",
                    explanation="An empty PR template does not prompt contributors with testing checklists or change descriptions.",
                    documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository",
                    extra_details={"target_file": template_target},
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
                current_state="Pull Request template configured.",
                recommended_state="Include testing checklists in PR template.",
                explanation="PR templates ensure that developers verify test coverage, security considerations, and changelogs before requesting review.",
                documentation_url="https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository",
                extra_details={"target_file": template_target},
            )

        org_pr_path = get_org_default_pr_template_path(inspector, org)
        if repo != ".github" and (org_pr_path or check_org_default_pr_template(inspector, org)):
            target_inherited = f"{org_pr_path} (Org-Inherited)" if org_pr_path else ".github/PULL_REQUEST_TEMPLATE.md (Org-Inherited)"
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
                extra_details={"target_file": target_inherited},
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
        precommit_file = next(
            (
                path
                for path in (".pre-commit-config.yaml", ".pre-commit-config.yml")
                if inspector.check_file_exists(org, repo, path, default_branch)
            ),
            None,
        )
        target_file = precommit_file or ".pre-commit-config.yaml"

        if precommit_file:
            content = inspector.get_file_content(org, repo, precommit_file, default_branch)
            if content is None:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{precommit_file} exists but could not be read. Please review.",
                    recommended_state="Ensure file permissions allow reading pre-commit configuration.",
                    explanation="Pre-commit config exists in repository tree but could not be read to verify hook definitions.",
                    documentation_url=doc_url,
                    extra_details={"target_file": target_file},
                )
            if isinstance(content, str) and (not content.strip() or "repos:" not in content):
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{precommit_file} exists but defines no active hooks. Please review.",
                    recommended_state="Configure linter/formatter hooks (e.g., ruff, black, eslint) in .pre-commit-config.yaml.",
                    explanation="A pre-commit configuration without hook repositories does not enforce any quality gates.",
                    remediation_snippet_before=content.strip() or "[Empty pre-commit config]",
                    remediation_snippet_after="repos:\n  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n    hooks:\n      - id: trailing-whitespace\n      - id: end-of-file-fixer\n      - id: check-added-large-files",
                    documentation_url=doc_url,
                    extra_details={"target_file": target_file},
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
                current_state=f"{precommit_file} found.",
                recommended_state="Keep pre-commit hook versions updated.",
                explanation="Pre-commit catches formatting, linting, and secret leakage errors immediately on the developer's machine.",
                documentation_url=doc_url,
                extra_details={"target_file": target_file},
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
            extra_details={"target_file": ".pre-commit-config.yaml"},
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
        existing_readme = next(
            (
                path
                for path in (
                    "README.md",
                    "README.rst",
                    "readme.md",
                    "readme.rst",
                    ".github/README.md",
                    ".github/README.rst",
                    ".github/readme.md",
                    ".github/readme.rst",
                    "docs/README.md",
                    "docs/README.rst",
                    "docs/readme.md",
                    "docs/readme.rst",
                )
                if inspector.check_file_exists(org, repo, path, default_branch)
            ),
            None,
        )
        has_readme = existing_readme is not None

        existing_contributing = next(
            (
                path
                for path in (
                    "CONTRIBUTING.md",
                    ".github/CONTRIBUTING.md",
                    "docs/CONTRIBUTING.md",
                    "CONTRIBUTING.rst",
                    ".github/CONTRIBUTING.rst",
                    "docs/CONTRIBUTING.rst",
                    "contributing.md",
                    ".github/contributing.md",
                    "docs/contributing.md",
                    "SETUP.md",
                    "setup.md",
                )
                if inspector.check_file_exists(org, repo, path, default_branch)
            ),
            None,
        )

        inherited_contributing = False
        org_contrib_path = None
        if not existing_contributing and repo != ".github":
            org_contrib_path = get_org_default_contributing_path(inspector, org)
            if org_contrib_path or check_org_default_contributing(inspector, org):
                inherited_contributing = True
                existing_contributing = f"{org_contrib_path or 'CONTRIBUTING.md'} (Org-Inherited)"

        has_contributing = existing_contributing is not None

        if has_readme and has_contributing:
            target_file = f"{existing_readme} / {existing_contributing}"
        elif has_readme:
            target_file = existing_readme
        elif has_contributing:
            target_file = existing_contributing
        else:
            target_file = "README.md / CONTRIBUTING.md"

        if has_contributing and has_readme:
            state_msg = (
                f"{existing_readme} and {existing_contributing} present (CONTRIBUTING inherited from organization '.github' repository)."
                if inherited_contributing
                else f"{existing_readme} and {existing_contributing} present."
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
                extra_details={"target_file": target_file},
            )

        if has_readme:
            raw_content = inspector.get_file_content(org, repo, existing_readme, default_branch) if existing_readme else ""
            readme_content = (raw_content or "").lower()
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
                    current_state=f"{existing_readme} contains setup/getting-started documentation.",
                    recommended_state="Consider extracting detailed PR/test conventions into CONTRIBUTING.md.",
                    explanation="Setup instructions inside README enable engineers to run and validate changes locally.",
                    documentation_url=doc_url,
                    extra_details={"target_file": target_file},
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
                current_state=f"{existing_readme} exists, but lacks explicit setup/install section or contributing documentation.",
                recommended_state=f"Add local setup commands to {existing_readme} and create CONTRIBUTING.md.",
                explanation="New contributors need copy-pasteable setup, test, and environment steps to onboard rapidly.",
                remediation_snippet_before=f"[{existing_readme} without setup or CONTRIBUTING.md]",
                remediation_snippet_after="## Local Setup\n1. Install dependencies\n2. Run tests locally\n\nSee CONTRIBUTING.md for PR conventions.",
                documentation_url=doc_url,
                extra_details={"target_file": target_file},
            )

        if has_contributing:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state=f"{existing_contributing} exists, but README is missing. Please review.",
                recommended_state="Add README.md with project overview and local setup commands.",
                explanation="A repository without a README lacks an entry point for engineers to understand the repository purpose.",
                documentation_url=doc_url,
                extra_details={"target_file": target_file},
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
            extra_details={"target_file": target_file},
        )
