"""Data models and constants for Repo360 Push Protection & Ruleset Bypass Ledger."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from ..common.timezones import DATE_FMT, IST, LOCAL_TZ, LOCAL_TZ_NAME, parse_ts

BYPASS_SUMMARY_HEADERS = ("Scope", "Category", "Metric", "Value", "Risk Context & Compliance Impact")

BYPASS_ORG_HEADERS = (
    "Organization",
    "Repositories Scanned",
    "Push Protection Bypasses",
    "Ruleset Bypasses",
    "Total Bypass Incidents",
    "Unique Bypassing Actors",
    "Top Secret Exposed",
    "Compliance Risk Tier",
)

BYPASS_PUSH_PROTECTION_HEADERS = (
    "Organization",
    "Repository",
    "Alert #",
    "Secret Type",
    "Actor (Bypasser)",
    f"Timestamp ({LOCAL_TZ_NAME})",
    "Resolution Reason",
    "Resolution Justification / Comment",
    "AI Risk Assessment",
    "AI Security Advisory",
    "Push Protection Overridden",
    "Alert URL",
)

BYPASS_RULESET_HEADERS = (
    "Organization",
    "Repository",
    "Rule Suite ID",
    "Branch / Ref",
    "Actor",
    "Result",
    "Bypassed Rules",
    "Commit SHA",
    f"Timestamp ({LOCAL_TZ_NAME})",
    "Audit / Commit URL",
)

BYPASS_ACTOR_HEADERS = (
    "Organization",
    "Actor",
    "Push Protection Bypasses",
    "Ruleset Bypasses",
    "Total Bypasses",
    "Most Frequent Secret",
    f"Last Bypass ({LOCAL_TZ_NAME})",
)


@dataclass
class PushProtectionBypassRecord:
    """Secret scanning push protection bypass event."""

    org: str
    repo: str
    alert_number: int
    secret_type: str
    actor: str
    timestamp: Optional[datetime] = None
    resolution: str = "Unspecified"
    resolution_comment: str = "-"
    push_protection_bypassed: bool = True
    html_url: str = ""
    ai_risk_assessment: str = "Unassessed"
    ai_security_advisory: str = "-"


@dataclass
class RulesetBypassRecord:
    """Branch protection or repository ruleset policy bypass event."""

    org: str
    repo: str
    rule_suite_id: int
    ref: str
    actor: str
    evaluation_result: str = "bypass"
    bypassed_rules: str = "All Rules"
    commit_sha: str = ""
    timestamp: Optional[datetime] = None
    html_url: str = ""


@dataclass
class RepoBypassProfile:
    """Aggregated bypass records for an individual repository."""

    org: str
    repo: str
    push_bypasses: List[PushProtectionBypassRecord] = field(default_factory=list)
    ruleset_bypasses: List[RulesetBypassRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def total_bypasses(self) -> int:
        return len(self.push_bypasses) + len(self.ruleset_bypasses)

    @property
    def unique_actors(self) -> Set[str]:
        actors = {p.actor for p in self.push_bypasses if p.actor}
        actors.update(r.actor for r in self.ruleset_bypasses if r.actor)
        return actors


@dataclass(frozen=True)
class BypassConfig:
    """Runtime configuration for push protection and ruleset bypass ledger."""

    lookback_days: int = 30
    max_workers: int = 0
    timeout_seconds: int = 30
    max_retries: int = 5
    include_archived: bool = False
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_bypass"


PLAN_LIMITATION_RULESET_MSG = (
    "Branch rulesets unavailable on private repositories (GitHub Plan limitation: requires GitHub Team or Enterprise Cloud)"
)
PLAN_LIMITATION_PUSH_MSG = (
    "Secret scanning unavailable on private repositories (GitHub Plan limitation: requires GitHub Advanced Security)"
)


def is_plan_limitation(msg: str) -> bool:
    """Return True if message denotes a GitHub subscription plan limitation rather than an API permission gap."""
    msg_l = (msg or "").lower()
    return (
        "plan limitation" in msg_l
        or "requires github team" in msg_l
        or "requires github advanced security" in msg_l
        or "upgrade to github pro" in msg_l
        or "make this repository public" in msg_l
    )


def classify_bypass_risk(total_push: int, total_incidents: int) -> str:
    """Classify enterprise/org bypass risk tier with unified compliance thresholds."""
    if total_push > 10 or total_incidents > 20:
        return "High"
    if total_push > 0 or total_incidents > 5:
        return "Medium"
    return "Low"
