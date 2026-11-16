"""Data models and constants for Repo360 user activity analysis."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from ..common.timezones import (
    DATE_FMT,
    IST,
    LOCAL_TZ,
    LOCAL_TZ_NAME,
    fmt_timestamp,
    parse_ts,
)

__all__ = [
    "BOT_SUFFIX",
    "ISO_Z_RE",
    "METRICS",
    "METRIC_KEYS",
    "METRIC_LABELS",
    "STATUSES",
    "STATUS_ORDER",
    "USER_HEADERS",
    "STATUS_IDX",
    "TOTAL_IDX",
    "METRIC_START_IDX",
    "REPO_HEADERS",
    "ORG_HEADERS",
    "REPO_INVENTORY_HEADERS",
    "TOP_REPOS_HEADERS",
    "Activity",
    "ActivityConfig",
    "RepositoryReport",
    "is_bot",
    "fmt_timestamp",
    "parse_ts",
    "IST",
    "DATE_FMT",
]

BOT_SUFFIX = "[bot]"
ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

METRICS = (
    ("commits", "Commits"),
    ("prs_opened", "PRs Opened"),
    ("prs_merged", "PRs Merged"),
    ("reviews", "PR Reviews"),
    ("review_comments", "Review Comments"),
    ("issues_opened", "Issues Opened"),
    ("issue_comments", "Issue/PR Comments"),
)
METRIC_KEYS = tuple(k for k, _ in METRICS)
METRIC_LABELS = tuple(lbl for _, lbl in METRICS)
STATUSES = ("Active", "Moderate", "Low", "Inactive")
STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}

USER_HEADERS = (
    "Organization", "User", "Activity Status", "Active Days", "Total Activities",
    *METRIC_LABELS, "Average Commits per Active Month",
    "Repositories Contributed", f"First Activity ({LOCAL_TZ_NAME})", f"Last Activity ({LOCAL_TZ_NAME})",
    "Days Since Last Activity", "Org Member", "Teams",
)
STATUS_IDX = USER_HEADERS.index("Activity Status")
TOTAL_IDX = USER_HEADERS.index("Total Activities")
METRIC_START_IDX = USER_HEADERS.index(METRIC_LABELS[0])

REPO_HEADERS = (
    "Organization", "Repository", "User", "Total Activities", *METRIC_LABELS,
    "Active Days", f"Last Activity ({LOCAL_TZ_NAME})",
)
ORG_HEADERS = ("Organization", "Repositories Scanned", "Contributors", *STATUSES, "Total Activities")

REPO_INVENTORY_HEADERS = (
    "Organization",
    "Repository",
    "Visibility",
    f"Created ({LOCAL_TZ_NAME})",
    "New in Period",
    "Archived",
    "Default Branch",
    "Protected Branches",
    "CODEOWNERS Present",
    "CODEOWNERS Path",
    "Activity Last 7 Days",
    "Total Activities",
    "Active Contributors",
    *METRIC_LABELS,
    "PR Cohort Merge Rate (%)",
    "Avg First Review Hours",
    "First Review Sample PRs",
    "Coverage",
)

TOP_REPOS_HEADERS = (
    "Organization",
    "Rank",
    "Repository",
    "Total Activities",
    "Active Contributors",
    *METRIC_LABELS,
    "Activity Last 7 Days",
    "Coverage",
)

MGMT_KPI_HEADERS = ("Scope", "Category", "Metric", "Value", "What this means")


def is_bot(user: Dict[str, Any]) -> bool:
    """Detect automated bot accounts."""
    if not isinstance(user, dict):
        return False
    if user.get("type") == "Bot":
        return True
    login = (user.get("login") or "").lower()
    return login.endswith(BOT_SUFFIX) or login.endswith("-bot") or login in ("dependabot", "github-actions")


@dataclass
class Activity:
    """Aggregated user activity metrics across a reporting window."""

    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0
    review_comments: int = 0
    issues_opened: int = 0
    issue_comments: int = 0
    active_days: Set[str] = field(default_factory=set)
    first_active: Optional[datetime] = None
    last_active: Optional[datetime] = None
    commit_months: Set[Tuple[int, int]] = field(default_factory=set)

    def total(self) -> int:
        return (
            self.commits
            + self.prs_opened
            + self.prs_merged
            + self.reviews
            + self.review_comments
            + self.issues_opened
            + self.issue_comments
        )

    def add(self, metric: str, when: datetime) -> None:
        current = getattr(self, metric, 0)
        setattr(self, metric, current + 1)
        when_local = when.astimezone(LOCAL_TZ)
        self.active_days.add(when_local.strftime(DATE_FMT))
        self.first_active = min(self.first_active, when) if self.first_active else when
        self.last_active = max(self.last_active, when) if self.last_active else when
        if metric == "commits":
            self.commit_months.add((when_local.year, when_local.month))

    def merge(self, other: Activity) -> None:
        for k in METRIC_KEYS:
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.active_days.update(other.active_days)
        self.commit_months.update(other.commit_months)
        if other.first_active:
            self.first_active = min(self.first_active, other.first_active) if self.first_active else other.first_active
        if other.last_active:
            self.last_active = max(self.last_active, other.last_active) if self.last_active else other.last_active


@dataclass
class RepositoryReport:
    """Collected activity report for a single repository."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    users: Dict[str, Activity] = field(default_factory=dict)
    protected: str = "Unknown"
    protected_branches: Tuple[str, ...] = ()
    codeowners: str = "Unknown"
    codeowners_path: str = "Not found"
    errors: List[str] = field(default_factory=list)
    activity_complete: bool = False
    review_hours: List[float] = field(default_factory=list)
    cohort_opened: int = 0
    cohort_merged: int = 0
    events: List[Tuple[Optional[str], str, datetime]] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityConfig:
    """Runtime configuration for activity scraping."""

    lookback_days: int = 90
    active_min_days: int = 8
    moderate_min_days: int = 3
    include_archived: bool = False
    include_forks: bool = True
    exclude_bots: bool = True
    include_inactive_members: bool = True
    max_workers: int = 0
    timeout_seconds: int = 30
    max_retries: int = 5
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_activity"
    history_file: Optional[str] = None
