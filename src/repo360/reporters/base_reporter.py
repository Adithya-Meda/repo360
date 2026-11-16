"""Base interface for audit reporting."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Iterable, List

from ..checks.base import CheckResult


class BaseReporter(ABC):
    """Abstract base class for all report generators."""

    @abstractmethod
    def generate(
        self,
        results: List[CheckResult],
        output_path: str,
        executive_summary: str = "",
        model_name: str = "",
    ) -> str:
        """Generate report file and return the saved file path."""
        pass

    @staticmethod
    def get_local_timestamp() -> str:
        """Return formatted local timestamp with smart timezone detection in DD-MM-YYYY format (e.g. 04-11-2026 16:22:00 IST)."""
        from ..common.timezones import get_local_timezone, get_local_tz_name
        tz = get_local_timezone()
        now = datetime.now(tz)
        tz_abbr = get_local_tz_name(now)
        return f"{now.strftime('%d-%m-%Y %H:%M:%S')} {tz_abbr}"

    @staticmethod
    def sanitize_spreadsheet_value(value: object) -> object:
        """Sanitize values against CSV / Excel Formula Injection (CWE-1236).

        Prepends a single quote if the string begins with =, +, -, @, tab, carriage return,
        or any whitespace preceding these formula execution characters.
        """
        if isinstance(value, str):
            stripped = value.lstrip(" \t\r\n")
            # Standalone "-" is safe as a placeholder; "-expr" can trigger formulas
            if stripped == "-":
                return value
            if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
                return f"'{value}"
        return value

    @staticmethod
    def infer_ai_model(results: List[CheckResult], fallback: str = "Gemini") -> str:
        """Extract the active AI model name from audit findings."""
        for r in results:
            m = r.extra_details.get("ai_model")
            if m:
                return m
        return fallback

    @staticmethod
    def is_ai_result(result: CheckResult) -> bool:
        """Return True if finding was enriched with AI-synthesized remediation."""
        if result.extra_details.get("ai_enriched"):
            return True
        if result.remediation_snippet_after and "[AI Tailored" in result.remediation_snippet_after:
            return True
        return False

    @staticmethod
    def infer_target_file(check_id: str) -> str:
        """Map check ID to the relevant repository or governance configuration file."""
        file_map = {
            "copilot_instructions": ".github/copilot-instructions.md",
            "devcontainers": ".devcontainer/devcontainer.json",
            "dependabot_configuration": ".github/dependabot.yml",
            "workflow_sha_pinning": ".github/workflows/*.yml",
            "workflow_concurrency_groups": ".github/workflows/*.yml",
            "codeowners_validation": ".github/CODEOWNERS",
            "pull_request_templates": ".github/PULL_REQUEST_TEMPLATE.md",
            "issue_templates": ".github/ISSUE_TEMPLATE/bug_report.yml",
            "pre_commit_hooks": ".pre-commit-config.yaml",
            "branch_rulesets": "Branch Rulesets (Settings)",
            "rulesets_adoption": "Repository Rulesets (Settings)",
            "secret_scanning_push_protection": "Code Security (Settings)",
            "saml_sso_enforcement": "Authentication / SAML SSO (Settings)",
            "default_workflow_permissions": "Actions Permissions (Settings)",
            "runner_groups_governance": "Actions Runner Groups (Settings)",
            "runner_groups_isolation": "Actions Runner Groups (Settings)",
            "environment_protection_gates": "Repository Environments (Settings)",
            "setup_documentation": "README.md / CONTRIBUTING.md",
        }
        return file_map.get(check_id, "Repository / Organization Config")

    @staticmethod
    def format_detected_stacks(
        raw_stacks: str | Iterable[str],
        fallback: str = "General Platform",
        max_items: int = 6,
    ) -> str:
        """Normalize, deduplicate, and format comma-separated tech stack labels cleanly."""
        items = [raw_stacks] if isinstance(raw_stacks, str) else list(raw_stacks or [])
        tokens: List[str] = []
        seen: set[str] = set()
        generic_placeholders = {"general software", "general platform", "standard", "none"}

        for entry in items:
            if not entry:
                continue
            for part in str(entry).split(","):
                clean = part.strip()
                key = clean.casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                tokens.append(clean)

        concrete = [t for t in tokens if t.casefold() not in generic_placeholders]
        if concrete:
            specific_prefixes = {
                t.split(" / ", 1)[0].strip().casefold()
                for t in concrete
                if " / " in t
            }
            filtered = [
                t
                for t in concrete
                if " / " in t or t.casefold() not in specific_prefixes
            ]
            ordered = sorted(filtered, key=str.casefold)
        elif tokens:
            ordered = [tokens[0]]
        else:
            return fallback

        if len(ordered) > max_items:
            return f"{', '.join(ordered[:max_items])} (+{len(ordered) - max_items} more)"
        return ", ".join(ordered)

    @staticmethod
    def format_repo_list(repos: Iterable[str], max_display: int = 6) -> str:
        """Format repository names concisely without dumping unbounded fleet lists into cells."""
        seen: set[str] = set()
        unique_repos: List[str] = []
        for repo in repos:
            clean = str(repo).strip()
            if not clean:
                continue
            key = clean.casefold()
            if key not in seen:
                seen.add(key)
                unique_repos.append(clean)

        unique_repos.sort(key=str.casefold)
        if not unique_repos:
            return "-"
        if len(unique_repos) <= max_display:
            return ", ".join(unique_repos)
        remaining = len(unique_repos) - max_display
        return f"{', '.join(unique_repos[:max_display])} (+{remaining} more — see Findings tabs)"

    @classmethod
    def build_remediation_clusters(cls, results: List[CheckResult]) -> List[Dict[str, Any]]:
        """Cluster actionable WARN/FAIL findings strictly by check ID for consolidated, centralized fleet playbooks."""
        from ..checks.base import CheckStatus

        clusters: Dict[str, Dict[str, Any]] = {}

        for r in results:
            if r.status not in (CheckStatus.WARN, CheckStatus.FAIL):
                continue

            cluster_key = r.check_id
            stack = r.extra_details.get("detected_stack") or ""
            baseline_code = r.extra_details.get("baseline_remediation")
            is_ai_item = cls.is_ai_result(r)

            # Use generic baseline here so repo-specific AI snippets stay on the AI tab
            canonical_candidate = baseline_code or (
                r.remediation_snippet_after if not is_ai_item else ""
            )

            if cluster_key not in clusters:
                remediation_code = canonical_candidate or r.remediation_snippet_after or r.recommended_state
                clusters[cluster_key] = {
                    "cluster_key": cluster_key,
                    "check_id": r.check_id,
                    "check_name": r.check_name,
                    "category": r.category,
                    "severity": r.severity,
                    "persona": r.persona,
                    "detected_stacks": set(),
                    "target_file": cls.infer_target_file(r.check_id),
                    "current_state": r.current_state,
                    "recommended_state": r.recommended_state,
                    "remediation_code": remediation_code,
                    "remediation_snippet_before": r.remediation_snippet_before,
                    "explanation": r.explanation,
                    "documentation_url": r.documentation_url,
                    "is_ai": is_ai_item,
                    "ai_model": r.extra_details.get("ai_model", ""),
                    "_has_canonical_baseline": bool(canonical_candidate),
                    "affected_repos": [],
                }
            else:
                cluster = clusters[cluster_key]
                if is_ai_item and not cluster["is_ai"]:
                    cluster["is_ai"] = True
                    cluster["ai_model"] = r.extra_details.get("ai_model", "")
                if canonical_candidate and not cluster["_has_canonical_baseline"]:
                    cluster["remediation_code"] = canonical_candidate
                    cluster["_has_canonical_baseline"] = True
                if r.remediation_snippet_before and not cluster["remediation_snippet_before"]:
                    cluster["remediation_snippet_before"] = r.remediation_snippet_before

            if stack:
                clusters[cluster_key]["detected_stacks"].add(stack)

            repo_label = (
                f"{r.org_name} (Org-Wide)"
                if r.repo_name in ("Organization-Wide", "[Organization-Wide]")
                else r.repo_name
            )
            if repo_label not in clusters[cluster_key]["affected_repos"]:
                clusters[cluster_key]["affected_repos"].append(repo_label)

        severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        cluster_list = list(clusters.values())
        for c in cluster_list:
            c.pop("_has_canonical_baseline", None)
            c["affected_count"] = len(c["affected_repos"])
            c["affected_repos"].sort()
            c["detected_stack"] = cls.format_detected_stacks(
                c["detected_stacks"],
                fallback="General Platform",
            )

        cluster_list.sort(
            key=lambda x: (
                severity_order.get(x["severity"].value, 99),
                -x["affected_count"],
            )
        )
        return cluster_list

    @classmethod
    def build_organization_metrics(cls, results: List[CheckResult]) -> List[Dict[str, Any]]:
        """Compute organization-level compliance metrics, repo counts, and percentage breakdowns."""
        from ..checks.base import CheckSeverity, CheckStatus

        org_data: Dict[str, Dict[str, Any]] = {}
        for r in results:
            org = r.org_name or "Organization"
            if org not in org_data:
                org_data[org] = {
                    "org_name": org,
                    "repos": set(),
                    "total": 0,
                    "passed": 0,
                    "warned": 0,
                    "failed": 0,
                    "skipped": 0,
                    "critical_high": 0,
                }
            if r.repo_name and r.repo_name != "Organization-Wide":
                org_data[org]["repos"].add(r.repo_name)
            org_data[org]["total"] += 1

            if r.status == CheckStatus.PASS:
                org_data[org]["passed"] += 1
            elif r.status == CheckStatus.WARN:
                org_data[org]["warned"] += 1
                if r.severity in (CheckSeverity.CRITICAL, CheckSeverity.HIGH):
                    org_data[org]["critical_high"] += 1
            elif r.status == CheckStatus.FAIL:
                org_data[org]["failed"] += 1
                if r.severity in (CheckSeverity.CRITICAL, CheckSeverity.HIGH):
                    org_data[org]["critical_high"] += 1
            elif r.status == CheckStatus.SKIPPED:
                org_data[org]["skipped"] += 1

        org_list: List[Dict[str, Any]] = []
        for org, data in sorted(org_data.items()):
            effective = data["total"] - data["skipped"]
            repo_count = len(data["repos"])

            if effective > 0:
                score = round((data["passed"] / effective) * 100, 1)
                if score >= 80:
                    status_label = "COMPLIANT"
                elif score >= 50:
                    status_label = "REVIEW NEEDED"
                else:
                    status_label = "AT RISK"
            elif data["total"] == 0:
                score = 100.0
                status_label = "COMPLIANT"
            else:
                score = 0.0
                status_label = "NOT EVALUATED"

            org_list.append({
                "org_name": org,
                "repo_count": repo_count,
                "total": data["total"],
                "passed": data["passed"],
                "warned": data["warned"],
                "failed": data["failed"],
                "skipped": data["skipped"],
                "compliance_score": score,
                "critical_high": data["critical_high"],
                "status": status_label,
            })

        return org_list

