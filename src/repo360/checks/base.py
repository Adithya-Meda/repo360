"""Base structures and abstractions for Repo360 advisory checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class CheckStatus(str, Enum):
    """Execution status of an advisory check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class CheckSeverity(str, Enum):
    """Risk severity associated with a finding."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class Persona(str, Enum):
    """Target audience for the recommendation."""

    ORG_ADMIN = "org_admin"
    REPO_ADMIN = "repo_admin"
    CONTRIBUTOR = "contributor"


@dataclass
class CheckResult:
    """Individual finding from an advisory check."""

    check_id: str
    check_name: str
    persona: Persona
    category: str
    status: CheckStatus
    severity: CheckSeverity
    org_name: str
    repo_name: str
    current_state: str
    recommended_state: str
    explanation: str
    remediation_snippet_before: str = ""
    remediation_snippet_after: str = ""
    remediation_command: str = ""
    documentation_url: str = ""
    extra_details: Dict[str, Any] = field(default_factory=dict)


class BaseCheck:
    """Abstract base class for all governance and advisory checks."""

    check_id: str = "base_check"
    check_name: str = "Base Check"
    persona: Persona = Persona.REPO_ADMIN
    category: str = "General"
    severity: CheckSeverity = CheckSeverity.MEDIUM

    def is_enabled(self, enabled_checks_config: Dict[str, Any]) -> bool:
        """Verify whether this check is enabled in advisory.json."""
        persona_config = enabled_checks_config.get(self.persona.value, {})
        return bool(persona_config.get(self.check_id, True))

