"""Validation engine for inventory CSV, config JSON, and advisory policy files."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Set

VALID_PERSONAS = {"org_admin", "repo_admin", "contributor", "all"}

DEFAULT_APP_CONFIG: Dict[str, Any] = {
    "github": {
        "api_base_url": "${REPO360_API_BASE_URL:-https://api.github.com}",
        "timeout_seconds": 30,
        "max_retries": 3,
        "retry_backoff_factor": 1.5,
    },
    "reporting": {
        "output_directory": "./repo360-output",
        "report_prefix": "repo360_audit",
        "generate_xlsx": True,
    },
    "execution": {
        "concurrency_limit": 5,
        "auto_discover_if_empty": True,
    },
    "llm": {
        "enabled": True,
        "provider": "${LLM_PROVIDER:-gemini}",
        "model": "${LLM_MODEL:-gemini-3.5-flash}",
    },
}

DEFAULT_ADVISORY_CONFIG: Dict[str, Any] = {
    "advisory_metadata": {
        "version": "1.0.0",
        "organization": "Enterprise-Platform-Engineering",
        "last_updated": "2026-11-04",
        "description": "Centralized Governance, Security & Best Practices Advisory Policies for Repo360",
    },
    "enabled_checks": {
        "org_admin": {
            "rulesets_adoption": True,
            "secret_scanning_push_protection": True,
            "saml_sso_enforcement": True,
            "runner_groups_governance": True,
            "default_workflow_permissions": True,
        },
        "repo_admin": {
            "codeowners_validation": True,
            "branch_rulesets": True,
            "environment_protection_gates": True,
            "workflow_sha_pinning": True,
            "workflow_concurrency_groups": True,
            "dependabot_configuration": True,
        },
        "contributor": {
            "devcontainers": True,
            "issue_templates": True,
            "pull_request_templates": True,
            "copilot_instructions": True,
            "pre_commit_hooks": True,
            "setup_documentation": True,
        },
    },
    "policy_thresholds": {
        "minimum_approvals": 2,
        "require_linear_history": True,
        "require_signed_commits": False,
        "disallow_force_pushes": True,
        "disallow_deletions": True,
    },
}



@dataclass
class RepoTarget:
    """Validated target repository representation."""

    org_name: str
    repo_name: str
    target_personas: Set[str]
    default_branch: str = "main"
    owner_team: str = ""


class ConfigValidator:
    """Validates configuration and inventory inputs against strict schemas."""

    ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

    @classmethod
    def substitute_env_vars(cls, text: str) -> str:
        """Replace ${ENV_VAR:-default} and ${ENV_VAR} patterns with environment values recursively."""
        for _ in range(5):
            new_text = cls.ENV_VAR_PATTERN.sub(
                lambda m: os.getenv(m.group(1), m.group(2) if m.group(2) is not None else ""),
                text,
            )
            if new_text == text:
                break
            text = new_text

        return text

    @staticmethod
    def update_env_file(key: str, value: str, env_path: str = ".env") -> bool:
        """Safely update or append a key=value pair in .env file without destroying other lines."""
        try:
            lines: List[str] = []
            found = False
            if os.path.isfile(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

            new_lines: List[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                    prefix = "export " if stripped.startswith("export ") else ""
                    new_lines.append(f"{prefix}{key}={value}\n")
                    found = True
                else:
                    new_lines.append(line)

            if not found:
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines[-1] += "\n"
                new_lines.append(f"{key}={value}\n")

            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            return True
        except Exception:
            return False

    @classmethod
    def get_default_app_config(cls) -> Dict[str, Any]:
        """Return a fresh copy of the default app configuration with env var interpolation."""
        raw_json = json.dumps(DEFAULT_APP_CONFIG)
        interpolated = cls.substitute_env_vars(raw_json)
        return json.loads(interpolated)

    @classmethod
    def get_default_advisory_config(cls) -> Dict[str, Any]:
        """Return a fresh copy of the default advisory configuration with all 14 checks enabled."""
        raw_json = json.dumps(DEFAULT_ADVISORY_CONFIG)
        interpolated = cls.substitute_env_vars(raw_json)
        return json.loads(interpolated)

    @classmethod
    def load_and_validate_json(cls, file_path: str, is_advisory: bool = False) -> Dict[str, Any]:
        """Load, substitute env vars, and validate JSON configuration file.

        Args:
            file_path: Path to JSON configuration.
            is_advisory: Whether this is the policy advisory.json file.

        Returns:
            Validated configuration dictionary.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Configuration file not found: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_text = f.read()
        except Exception as err:
            raise ValueError(f"Could not read config file {file_path}: {err}") from err

        interpolated_text = cls.substitute_env_vars(raw_text)

        try:
            data = json.loads(interpolated_text)
        except json.JSONDecodeError as err:
            raise ValueError(f"Malformed JSON in {file_path}: {err}") from err

        if not isinstance(data, dict):
            raise ValueError(f"Root of {file_path} must be a JSON object.")

        if is_advisory:
            cls._validate_advisory_structure(data, file_path)

        return data

    @staticmethod
    def _validate_advisory_structure(data: Dict[str, Any], file_path: str) -> None:
        """Ensure advisory.json conforms to expected schema."""
        if "enabled_checks" not in data:
            raise ValueError(f"Missing 'enabled_checks' object in {file_path}")

        enabled_checks = data["enabled_checks"]
        for persona in ["org_admin", "repo_admin", "contributor"]:
            if persona in enabled_checks and not isinstance(enabled_checks[persona], dict):
                raise ValueError(
                    f"'enabled_checks.{persona}' in {file_path} must be a dictionary of check toggles."
                )

