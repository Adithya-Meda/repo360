"""Repository Admin governance, security, and CI/CD checks."""

from __future__ import annotations

import re
from typing import Any, Dict, List
import requests

from ._helpers import (
    get_permission_gap,
    incomplete_workflow_coverage_result,
    is_repository_workflow,
    is_http_forbidden,
    make_permission_skipped_result,
)
from .base import BaseCheck, CheckResult, CheckSeverity, CheckStatus, Persona
from ..collector.repo_inspector import RepoInspector
from ..common.text import pluralize


def _format_codeowners_errors(api_errors: List[Any]) -> str:
    """Format raw GitHub CODEOWNERS API errors into a clean, concise, single-line summary."""
    if not api_errors:
        return ""

    line_unknown_owners: Dict[int, List[str]] = {}
    other_errors_by_line: Dict[int, List[str]] = {}
    general_errors: List[str] = []

    for err in api_errors:
        if isinstance(err, dict):
            line_num = err.get("line")
            raw_msg = str(err.get("message") or err.get("kind") or err.get("source") or "").strip()
            first_line = raw_msg.splitlines()[0].strip() if raw_msg else ""

            # Check if this error relates to unknown / unresolvable owners
            lower = first_line.lower()
            handles = re.findall(r"@[a-zA-Z0-9_/-]+", first_line)
            if handles and ("owner" in lower or "exist" in lower or "access" in lower or not first_line):
                if line_num:
                    line_unknown_owners.setdefault(int(line_num), []).extend(handles)
                else:
                    general_errors.append(f"unknown owner(s) {', '.join(dict.fromkeys(handles))} (must exist with write access)")
                continue

            # Non-owner or syntax error
            clean_msg = re.sub(r"\s+", " ", first_line)
            if line_num:
                # Strip redundant "line X:" or "on line X:" prefixes
                clean_msg = re.sub(rf"^(?:on\s+)?line\s+{line_num}[:\s]*", "", clean_msg, flags=re.IGNORECASE)
                clean_msg = re.sub(rf"\s+on\s+line\s+{line_num}", "", clean_msg, flags=re.IGNORECASE).strip()
                if clean_msg:
                    other_errors_by_line.setdefault(int(line_num), []).append(clean_msg)
            elif clean_msg:
                general_errors.append(clean_msg)
        elif isinstance(err, str) and err.strip():
            clean_str = re.sub(r"\s+", " ", err.splitlines()[0]).strip()
            if clean_str:
                general_errors.append(clean_str)

    summaries: List[str] = []

    for line_num in sorted(line_unknown_owners.keys()):
        unique_handles = list(dict.fromkeys(line_unknown_owners[line_num]))
        handles_str = ", ".join(unique_handles)
        summaries.append(f"line {line_num}: unknown owner(s) {handles_str} (must exist with write access)")

    for line_num in sorted(other_errors_by_line.keys()):
        for msg in list(dict.fromkeys(other_errors_by_line[line_num])):
            summaries.append(f"line {line_num}: {msg}")

    for msg in list(dict.fromkeys(general_errors)):
        summaries.append(msg)

    if not summaries:
        return ""

    if len(summaries) > 3:
        displayed = summaries[:3]
        remaining = len(summaries) - 3
        return f"{'; '.join(displayed)} (+{remaining} more error{'s' if remaining != 1 else ''})"

    return "; ".join(summaries)


class CodeownersCheck(BaseCheck):
    """Verifies that a valid CODEOWNERS file exists and assigns team ownership."""

    check_id = "codeowners_validation"
    check_name = "CODEOWNERS Configuration"
    persona = Persona.REPO_ADMIN
    category = "Governance & Maintainability"
    severity = CheckSeverity.HIGH

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners"
        content: Optional[str] = None
        for candidate in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            res_content = inspector.get_file_content(org, repo, candidate, default_branch)
            if res_content is not None:
                content = res_content
                break

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
                current_state="CODEOWNERS file exists but is empty. Please review.",
                recommended_state="Add code ownership rules to CODEOWNERS defining responsible team handles.",
                explanation="An empty CODEOWNERS file provides no ownership or review requirements.",
                remediation_snippet_before="# Empty CODEOWNERS",
                remediation_snippet_after="# Catch-all default rule\n* @org/team-name",
                documentation_url=doc_url,
            )

        if content and content.strip():
            # Check 1: Query GitHub API's official CODEOWNERS error validation endpoint
            errors_fn = getattr(inspector, "get_codeowners_errors", None)
            api_errors = errors_fn(org, repo, default_branch) if callable(errors_fn) else []
            if isinstance(api_errors, list) and len(api_errors) > 0:
                err_detail = _format_codeowners_errors(api_errors)
                detail = f" ({err_detail})" if err_detail else ""
                first_line = content.strip().splitlines()[0] if content.strip() else ""
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"CODEOWNERS file contains errors on GitHub{detail}. Please review.",
                    recommended_state="Review and fix invalid user/team handles or syntax errors in CODEOWNERS.",
                    explanation="GitHub marks this CODEOWNERS file as containing errors. Rules with invalid or unpermitted owners are ignored, leaving paths unprotected.",
                    remediation_snippet_before=first_line or "[Invalid CODEOWNERS rule]",
                    remediation_snippet_after="# Correct team ownership syntax (ensure team has write access)\n* @org/team-name",
                    documentation_url=doc_url,
                )

            # Check 2: Check for trailing inline comments on rule lines (unsupported by GitHub CODEOWNERS parser)
            inline_comment_lines: List[int] = []
            has_wildcard = False
            for idx, line in enumerate(content.splitlines(), start=1):
                clean_line = line.strip()
                if not clean_line or clean_line.startswith("#"):
                    continue
                # If '#' is inside line, GitHub CODEOWNERS treats it as an owner name, creating an invalid owner error
                if "#" in clean_line:
                    inline_comment_lines.append(idx)
                stripped = line.split("#", 1)[0].strip()
                if stripped and re.match(r"^\*\s+\S+", stripped):
                    has_wildcard = True

            if inline_comment_lines:
                if len(inline_comment_lines) == 1:
                    lines_label = f"line {inline_comment_lines[0]}"
                elif len(inline_comment_lines) <= 5:
                    lines_label = f"lines {', '.join(str(l) for l in inline_comment_lines)}"
                else:
                    disp = ", ".join(str(l) for l in inline_comment_lines[:5])
                    rem = len(inline_comment_lines) - 5
                    lines_label = f"lines {disp} (+{rem} more)"
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"CODEOWNERS file contains inline comments on rule {lines_label}. Please review.",
                    recommended_state="Move comments to separate lines starting with '#' above the ownership rule.",
                    explanation="GitHub CODEOWNERS does not support inline comments; '#' on rule lines is parsed as an invalid owner handle.",
                    remediation_snippet_before="* @org/team # Catch-all ownership",
                    remediation_snippet_after="# Catch-all ownership\n* @org/team",
                    documentation_url=doc_url,
                )

            if has_wildcard:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.PASS,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state="Valid CODEOWNERS file with root wildcard rule present.",
                    recommended_state="Maintain CODEOWNERS with active team handles.",
                    explanation="CODEOWNERS guarantees that changes are reviewed and approved by designated domain maintainers.",
                    documentation_url=doc_url,
                )

            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=CheckSeverity.MEDIUM,
                org_name=org,
                repo_name=repo,
                current_state="CODEOWNERS file exists, but lacks a default '*' catch-all team rule. Please review.",
                recommended_state="Add a default catch-all rule '* @org/team' at the top of CODEOWNERS.",
                explanation="Without a default catch-all rule, files added to new directories may escape mandatory review.",
                remediation_snippet_before="# Partial CODEOWNERS without root rule\n/src/api/ @team-api",
                remediation_snippet_after="# Catch-all default rule\n* @org/core-maintainers\n/src/api/ @org/team-api",
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
            repo_name=repo,
            current_state="No CODEOWNERS file detected.",
            recommended_state="Create '.github/CODEOWNERS' defining responsible team handles.",
            explanation="Missing CODEOWNERS allows unauthorized or unreviewed modifications to critical code paths.",
            remediation_snippet_before="[Missing .github/CODEOWNERS]",
            remediation_snippet_after="# .github/CODEOWNERS\n* @org/team-leads\n/infra/ @org/devops",
            documentation_url="https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners",
        )


class BranchRulesetsCheck(BaseCheck):
    """Verifies that branch rulesets or protection rules guard the default branch."""

    check_id = "branch_rulesets"
    check_name = "Branch Governance & Approval Gates"
    persona = Persona.REPO_ADMIN
    category = "Governance & Delivery"
    severity = CheckSeverity.CRITICAL

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets"
        raw_req = thresholds.get("minimum_approvals", 2)
        required_approvals = raw_req if isinstance(raw_req, int) else 2

        try:
            rulesets = inspector.get_repo_rulesets(org, repo)
            branch_rules_fn = getattr(inspector, "get_repo_branch_rules", None)
            raw_branch_rules = branch_rules_fn(org, repo, default_branch) if callable(branch_rules_fn) else []
            branch_rules = raw_branch_rules if isinstance(raw_branch_rules, list) else []
            protection = inspector.get_branch_protection(org, repo, default_branch)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden inspecting branch rulesets or protection.", doc_url, repo=repo
                )
            raise

        perm_err = (
            get_permission_gap(inspector, org, "repo_branch_rules", repo=repo)
            or get_permission_gap(inspector, org, "repo_rulesets", repo=repo)
            or get_permission_gap(inspector, org, "branch_protection", repo=repo)
        )

        rulesets_list = rulesets if isinstance(rulesets, list) else []
        branch_rulesets = [
            r for r in rulesets_list
            if isinstance(r, dict) and r.get("target") in ("branch", None)
        ]
        active_rulesets = [r for r in branch_rulesets if r.get("enforcement") == "active"]
        evaluate_rulesets = [r for r in branch_rulesets if r.get("enforcement") == "evaluate"]

        inline_rules: List[Dict[str, Any]] = []
        if active_rulesets and not branch_rules:
            details_fn = getattr(inspector, "get_repo_ruleset_details", None)
            for rs in active_rulesets:
                if isinstance(rs.get("rules"), list):
                    inline_rules.extend(r for r in rs["rules"] if isinstance(r, dict))
                elif rs.get("id") and callable(details_fn):
                    det = details_fn(org, repo, rs["id"])
                    if isinstance(det, dict) and isinstance(det.get("rules"), list):
                        inline_rules.extend(r for r in det["rules"] if isinstance(r, dict))

        effective_rules = branch_rules or inline_rules
        protection_dict = protection if isinstance(protection, dict) else None

        has_active_control = bool(effective_rules or protection_dict)
        if not has_active_control:
            if perm_err:
                return make_permission_skipped_result(self, org, perm_err, doc_url, repo=repo)
            if evaluate_rulesets:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=(
                        f"Branch '{default_branch}' only has {pluralize(len(evaluate_rulesets), 'ruleset', include_count=True)} in 'evaluate' "
                        "(dry-run) mode and no active enforcement or branch protection."
                    ),
                    recommended_state=f"Enable 'Active' enforcement on rulesets targeting '{default_branch}' with {required_approvals} approvals.",
                    explanation="Evaluate-mode rulesets do not block non-compliant merges or direct pushes.",
                    remediation_snippet_before="Enforcement status: Evaluate (non-blocking)",
                    remediation_snippet_after="Enforcement status: Active",
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
                repo_name=repo,
                current_state=f"Default branch '{default_branch}' has no active rulesets or protection enabled.",
                recommended_state=f"Apply an active ruleset requiring PR approvals (min {required_approvals}).",
                explanation=f"Default branch must enforce peer reviews and status checks prior to merging into {default_branch}.",
                remediation_snippet_before="Direct push allowed to main without PR review or status checks.",
                remediation_snippet_after=f"Repository Ruleset targeting '{default_branch}':\n- Require Pull Request with {required_approvals} approvals\n- Require status checks to pass\n- Block force pushes and deletions",
                documentation_url=doc_url,
            )

        current_approvals = 0
        has_linear_history = False
        has_signed_commits = False
        blocks_force_pushes = False
        blocks_deletions = False
        inspected_rule_details = False

        if protection_dict is not None:
            inspected_rule_details = True
            pr_reviews = protection_dict.get("required_pull_request_reviews") or {}
            if isinstance(pr_reviews, dict):
                current_approvals = max(current_approvals, int(pr_reviews.get("required_approving_review_count") or 0))
            if (protection_dict.get("required_linear_history") or {}).get("enabled"):
                has_linear_history = True
            if (protection_dict.get("required_signatures") or {}).get("enabled"):
                has_signed_commits = True
            allow_fp = protection_dict.get("allow_force_pushes")
            if isinstance(allow_fp, dict):
                blocks_force_pushes = not bool(allow_fp.get("enabled", False))
            else:
                blocks_force_pushes = True
            allow_del = protection_dict.get("allow_deletions")
            if isinstance(allow_del, dict):
                blocks_deletions = not bool(allow_del.get("enabled", False))
            else:
                blocks_deletions = True

        for rule in effective_rules:
            if not isinstance(rule, dict):
                continue
            inspected_rule_details = True
            r_type = rule.get("type")
            params = rule.get("parameters") or {}
            if r_type == "pull_request" and isinstance(params, dict):
                current_approvals = max(current_approvals, int(params.get("required_approving_review_count") or 0))
            elif r_type == "required_linear_history":
                has_linear_history = True
            elif r_type == "required_signatures":
                has_signed_commits = True
            elif r_type == "non_fast_forward":
                blocks_force_pushes = True
            elif r_type == "deletion":
                blocks_deletions = True

        gaps: List[str] = []
        if current_approvals < required_approvals:
            gaps.append(f"approvals ({current_approvals} < policy min {required_approvals})")
        if inspected_rule_details:
            if thresholds.get("require_linear_history") and not has_linear_history:
                gaps.append("linear history not required")
            if thresholds.get("require_signed_commits") and not has_signed_commits:
                gaps.append("signed commits not required")
            if thresholds.get("disallow_force_pushes") and not blocks_force_pushes:
                gaps.append("force pushes permitted")
            if thresholds.get("disallow_deletions") and not blocks_deletions:
                gaps.append("branch deletions permitted")

        if gaps:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=CheckSeverity.HIGH,
                org_name=org,
                repo_name=repo,
                current_state=f"Branch '{default_branch}' protection gaps: {'; '.join(gaps)}.",
                recommended_state=(
                    f"Enforce >= {required_approvals} PR approvals and policy hardening "
                    "controls on the default branch."
                ),
                explanation=f"Default branch '{default_branch}' lacks full policy compliance: {'; '.join(gaps)}.",
                remediation_snippet_before=f"Approvals={current_approvals}; gaps: {', '.join(gaps)}",
                remediation_snippet_after=f"Required approvals: {required_approvals}, block force pushes & deletions, dismiss stale reviews",
                documentation_url=doc_url,
            )

        approval_noun = "approval" if required_approvals == 1 else "approvals"
        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.PASS,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state=f"Branch ruleset/protection active on '{default_branch}' compliant with {required_approvals} {approval_noun} standard.",
            recommended_state="Maintain branch protection policy.",
            explanation="Default branch has active protection rules enforcing reviews and safeguards.",
            documentation_url=doc_url,
        )


class EnvironmentProtectionGatesCheck(BaseCheck):
    """Verifies deployment environments configure reviewers or branch protection gates."""

    check_id = "environment_protection_gates"
    check_name = "Deployment Environment Protection Gates"
    persona = Persona.REPO_ADMIN
    category = "Release & Deployment Governance"
    severity = CheckSeverity.HIGH

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/actions/managing-workflow-runs-and-deployments/managing-deployments/managing-environments-for-deployment"
        try:
            raw_envs = inspector.get_environments(org, repo)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden inspecting /environments.", doc_url, repo=repo
                )
            raise

        perm_err = get_permission_gap(inspector, org, "repo_environments", repo=repo)
        if perm_err and not raw_envs:
            return make_permission_skipped_result(self, org, perm_err, doc_url, repo=repo)

        envs = raw_envs if isinstance(raw_envs, list) else []
        if not envs:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.SKIPPED,
                severity=CheckSeverity.INFO,
                org_name=org,
                repo_name=repo,
                current_state="No GitHub deployment environments configured in repository.",
                recommended_state="Configure protected deployment environments when managing staged releases.",
                explanation="Repository has no defined deployment environments to audit.",
                documentation_url=doc_url,
            )

        unprotected_envs: List[str] = []
        for env in envs:
            if not isinstance(env, dict):
                continue
            env_name = str(env.get("name") or "unnamed")
            rules = env.get("protection_rules") or []
            branch_policy = env.get("deployment_branch_policy")
            has_gate = bool(rules) or isinstance(branch_policy, dict)
            if not has_gate:
                unprotected_envs.append(env_name)

        if unprotected_envs:
            if len(unprotected_envs) == 1:
                state_msg = f"Deployment environment '{unprotected_envs[0]}' lacks protection rules or deployment branch policies. Please review."
            elif len(unprotected_envs) <= 5:
                state_msg = f"Deployment environments lack protection rules or deployment branch policies: {', '.join(unprotected_envs)}. Please review."
            else:
                displayed = ", ".join(unprotected_envs[:5])
                remaining = len(unprotected_envs) - 5
                state_msg = f"Deployment environments lack protection rules or deployment branch policies: {displayed} (+{remaining} more). Please review."

            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state=state_msg,
                recommended_state="Configure required reviewers and deployment branch policies on deployment environments.",
                explanation="Deployment environments without protection rules or branch policies allow any workflow run or branch to access environment secrets and deploy.",
                remediation_snippet_before=f"Environment '{unprotected_envs[0]}': 0 protection rules or branch policies",
                remediation_snippet_after="Environment Settings -> Required reviewers + Selected branches (main/release tags only)",
                documentation_url=doc_url,
            )

        if len(envs) == 1:
            first_name = envs[0].get("name") if isinstance(envs[0], dict) else "default"
            state_msg = f"Deployment environment '{first_name}' enforces protection rules or branch policies."
        else:
            state_msg = f"All {len(envs)} deployment environments enforce protection rules or branch policies."

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
            recommended_state="Maintain reviewer approvals and branch restrictions on deployment environments.",
            explanation="Deployment environments restrict secrets and release approvals to authorized reviewers and branches.",
            documentation_url=doc_url,
        )


class WorkflowShaPinningCheck(BaseCheck):
    """Verifies that GitHub Actions use immutable commit SHAs instead of mutable tags."""

    check_id = "workflow_sha_pinning"
    check_name = "GitHub Actions Immutable SHA Pinning"
    persona = Persona.REPO_ADMIN
    category = "Supply Chain Security"
    severity = CheckSeverity.HIGH

    MUTABLE_TAG_PATTERN = re.compile(
        r"uses:\s*['\"]?([a-zA-Z0-9_.\-]+(?:/[a-zA-Z0-9_.\-]+)+)@([^\s'\"#]+)"
    )
    SHA_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")

    @staticmethod
    def _strip_yaml_comment(line: str) -> str:
        """Strip trailing # YAML comment while preserving # inside quoted strings."""
        in_single = False
        in_double = False
        for idx, ch in enumerate(line):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                return line[:idx]
        return line

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#using-third-party-actions"
        try:
            workflows = inspector.list_workflows(org, repo, default_branch)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden listing /actions/workflows.", doc_url, repo=repo
                )
            raise

        perm_err = get_permission_gap(inspector, org, "repo_workflows", repo=repo)
        if perm_err and not workflows:
            return make_permission_skipped_result(self, org, perm_err, doc_url, repo=repo)

        if not workflows:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.SKIPPED,
                severity=CheckSeverity.INFO,
                org_name=org,
                repo_name=repo,
                current_state="No GitHub Actions workflows configured in repository.",
                recommended_state="Use immutable SHAs when adopting Actions.",
                explanation="No workflow YAML files to inspect.",
                documentation_url=doc_url,
            )

        unpinned_actions: List[str] = []
        pinned_actions: List[str] = []
        unreadable: List[str] = []
        inspected = 0
        for wf in workflows:
            wf_path = wf.get("path")
            if not is_repository_workflow(wf):
                continue
            content = inspector.get_file_content(org, repo, wf_path, default_branch)
            if not content:
                unreadable.append(wf.get("name") or wf_path)
                continue
            inspected += 1

            for raw_line in content.splitlines():
                clean_line = self._strip_yaml_comment(raw_line).strip()
                if not clean_line or "uses:" not in clean_line:
                    continue
                after_uses = clean_line.split("uses:", 1)[1].strip().strip("'\"")
                if after_uses.startswith(("./", "docker://")):
                    continue
                match = self.MUTABLE_TAG_PATTERN.search(clean_line)
                if match:
                    action_name = match.group(1).strip()
                    ref = match.group(2).strip()
                    if not self.SHA_PATTERN.match(ref):
                        unpinned_actions.append(f"{wf.get('name')}: uses: {action_name}@{ref}")
                    else:
                        pinned_actions.append(f"{wf.get('name')}: uses: {action_name}@{ref}")

        if unpinned_actions:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state=f"{pluralize(len(unpinned_actions), 'unpinned third-party action', 'unpinned third-party actions', include_count=True)} detected across workflows. Please review.",
                recommended_state="Pin actions to immutable commit SHAs (40 or 64 hex characters) with comments.",
                explanation="Mutable tags (e.g., @v4) can be retargeted or hijacked by malicious actors to inject supply-chain attacks.",
                remediation_snippet_before="- uses: actions/checkout@v4",
                remediation_snippet_after="- uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1",
                documentation_url="https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#using-third-party-actions",
            )

        coverage_gap = incomplete_workflow_coverage_result(self, org, repo, inspected, unreadable, doc_url)
        if coverage_gap is not None:
            return coverage_gap

        if not pinned_actions:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.PASS,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="No third-party GitHub Actions referenced across workflows.",
                recommended_state="Pin third-party actions to immutable commit SHAs if introduced in future workflows.",
                explanation="Workflows execute native commands or local scripts without external third-party action dependencies, presenting no external action supply-chain risk.",
                documentation_url=doc_url,
            )

        if len(pinned_actions) == 1:
            action_desc = "The audited third-party action utilizes an immutable commit SHA."
        else:
            action_desc = f"All {len(pinned_actions)} audited third-party actions utilize immutable commit SHAs."

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.PASS,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state=action_desc,
            recommended_state="Continue pinning all action versions via SHA.",
            explanation="Immutable pinning guards against supply-chain tampering.",
            documentation_url=doc_url,
        )


class WorkflowConcurrencyGroupsCheck(BaseCheck):
    """Verifies workflows define concurrency groups to auto-cancel redundant builds."""

    check_id = "workflow_concurrency_groups"
    check_name = "CI Concurrency & Cost Optimization"
    persona = Persona.REPO_ADMIN
    category = "CI/CD & Cost"
    severity = CheckSeverity.MEDIUM

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/actions/using-jobs/using-concurrency"
        try:
            workflows = inspector.list_workflows(org, repo, default_branch)
        except requests.exceptions.HTTPError as err:
            if is_http_forbidden(err):
                return make_permission_skipped_result(
                    self, org, "HTTP 403 Forbidden listing /actions/workflows.", doc_url, repo=repo
                )
            raise

        perm_err = get_permission_gap(inspector, org, "repo_workflows", repo=repo)
        if perm_err and not workflows:
            return make_permission_skipped_result(self, org, perm_err, doc_url, repo=repo)

        if not workflows:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.SKIPPED,
                severity=CheckSeverity.INFO,
                org_name=org,
                repo_name=repo,
                current_state="No workflows present.",
                recommended_state="Add concurrency controls when workflows are created.",
                explanation="No workflow files found.",
                documentation_url=doc_url,
            )

        missing_concurrency: List[str] = []
        unreadable: List[str] = []
        inspected = 0
        for wf in workflows:
            wf_path = wf.get("path")
            if not is_repository_workflow(wf):
                continue
            content = inspector.get_file_content(org, repo, wf_path, default_branch)
            if not content:
                unreadable.append(wf.get("name") or wf_path)
                continue
            inspected += 1
            has_concurrency_key = any(
                re.match(r"^\s*concurrency\s*:", line.split("#", 1)[0])
                for line in content.splitlines()
            )
            if not has_concurrency_key:
                missing_concurrency.append(wf.get("name", wf_path))

        if missing_concurrency:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.WARN,
                severity=self.severity,
                org_name=org,
                repo_name=repo,
                current_state="Workflows do not have concurrency controls configured. Please review.",
                recommended_state="Configure concurrency with cancel-in-progress: true.",
                explanation="Without concurrency groups, pushing multiple commits in a PR queues redundant builds, wasting cloud runner minutes.",
                remediation_snippet_before="# Workflow without concurrency\non: [pull_request]\njobs: ...",
                remediation_snippet_after="concurrency:\n  group: ${{ github.workflow }}-${{ github.ref }}\n  cancel-in-progress: true",
                documentation_url="https://docs.github.com/en/actions/using-jobs/using-concurrency",
            )

        coverage_gap = incomplete_workflow_coverage_result(self, org, repo, inspected, unreadable, doc_url)
        if coverage_gap is not None:
            return coverage_gap

        if inspected == 0:
            return CheckResult(
                check_id=self.check_id,
                check_name=self.check_name,
                persona=self.persona,
                category=self.category,
                status=CheckStatus.SKIPPED,
                severity=CheckSeverity.INFO,
                org_name=org,
                repo_name=repo,
                current_state="No active repository GitHub Actions workflows found to inspect.",
                recommended_state="Add concurrency controls when workflows are created.",
                explanation="No active repository workflows under .github/workflows/ were available to inspect.",
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
            current_state="Workflows implement concurrency cancellation groups.",
            recommended_state="Maintain concurrency groups on branch builds.",
            explanation="Redundant in-flight CI runs are automatically canceled.",
            documentation_url="https://docs.github.com/en/actions/using-jobs/using-concurrency",
        )


class DependabotConfigCheck(BaseCheck):
    """Verifies that Dependabot version and security updates are configured."""

    check_id = "dependabot_configuration"
    check_name = "Dependabot Dependency Hygiene"
    persona = Persona.REPO_ADMIN
    category = "Security"
    severity = CheckSeverity.HIGH
    ECOSYSTEM_PATTERN = re.compile(
        r"^[ \t]*-?[ \t]*package-ecosystem[ \t]*:[ \t]*[\"']?([A-Za-z0-9_-]+)[\"']?[ \t]*(?:#.*)?$",
        re.MULTILINE,
    )

    def evaluate(
        self,
        inspector: RepoInspector,
        org: str,
        repo: str,
        default_branch: str,
        thresholds: Dict[str, Any],
    ) -> CheckResult:
        doc_url = "https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuring-dependabot-version-updates"
        config_path = next(
            (
                path
                for path in (".github/dependabot.yml", ".github/dependabot.yaml")
                if inspector.check_file_exists(org, repo, path, default_branch)
            ),
            None,
        )

        if config_path:
            content = inspector.get_file_content(org, repo, config_path, default_branch)
            if not isinstance(content, str) or not content:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.WARN,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{config_path} exists but its contents could not be read for validation.",
                    recommended_state="Grant 'contents: read' so the Dependabot configuration can be verified.",
                    explanation="An unreadable configuration cannot be confirmed to enable any update ecosystem.",
                    documentation_url=doc_url,
                )
            normalized = content.replace("\r\n", "\n").replace("\r", "\n")
            ecosystems = sorted(set(self.ECOSYSTEM_PATTERN.findall(normalized)))
            if not ecosystems:
                return CheckResult(
                    check_id=self.check_id,
                    check_name=self.check_name,
                    persona=self.persona,
                    category=self.category,
                    status=CheckStatus.FAIL,
                    severity=self.severity,
                    org_name=org,
                    repo_name=repo,
                    current_state=f"{config_path} defines no valid 'package-ecosystem' update entries.",
                    recommended_state="Declare at least one update entry with a non-empty package-ecosystem.",
                    explanation="A Dependabot file without valid update entries is rejected by GitHub and monitors nothing.",
                    remediation_snippet_before="updates:\n  - package-ecosystem: \"\"",
                    remediation_snippet_after="version: 2\nupdates:\n  - package-ecosystem: 'pip'\n    directory: '/'\n    schedule:\n      interval: 'weekly'",
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
                current_state=f"Dependabot configured for: {', '.join(ecosystems)}.",
                recommended_state="Keep package ecosystems updated in dependabot.yml.",
                explanation="Dependabot automates patch updates for vulnerable dependencies.",
                documentation_url="https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuring-dependabot-version-updates",
            )

        return CheckResult(
            check_id=self.check_id,
            check_name=self.check_name,
            persona=self.persona,
            category=self.category,
            status=CheckStatus.FAIL,
            severity=self.severity,
            org_name=org,
            repo_name=repo,
            current_state="Missing .github/dependabot.yml file.",
            recommended_state="Add .github/dependabot.yml for your ecosystem (e.g., pip, npm, docker).",
            explanation="Unmonitored third-party dependencies accumulate unpatched CVEs over time.",
            remediation_snippet_before="[No dependabot.yml file]",
            remediation_snippet_after="version: 2\nupdates:\n  - package-ecosystem: 'pip'\n    directory: '/'\n    schedule:\n      interval: 'weekly'",
            documentation_url="https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/configuring-dependabot-version-updates",
        )

