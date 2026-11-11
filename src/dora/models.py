"""Data models, constants, and industry benchmarks for Repo360 DORA & Flow Metrics."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..activity.models import DATE_FMT, IST, parse_ts

DORA_SUMMARY_HEADERS = ("Scope", "Category", "Metric", "Value", "DORA Benchmark Tier", "Industry Standard Context")

DORA_ORG_HEADERS = (
    "Organization",
    "Repositories Scanned",
    "Active Deploy Repos",
    "Overall DORA Tier",
    "Total Deployments",
    "Deployment Frequency (/week)",
    "DF Tier",
    "Median Lead Time (Hours)",
    "LTTC Tier",
    "Change Failure Rate (%)",
    "CFR Tier",
    "Median MTTR (Hours)",
    "MTTR Tier",
)

DORA_REPO_HEADERS = (
    "Organization",
    "Repository",
    "DORA Tier",
    "Total Deployments",
    "Successful",
    "Failed",
    "Deployment Frequency (/week)",
    "DF Tier",
    "Median Lead Time (Hours)",
    "LTTC Tier",
    "Change Failure Rate (%)",
    "CFR Tier",
    "Median MTTR (Hours)",
    "MTTR Tier",
    "AI Flow Diagnostic & Advisory",
    "Total Releases",
    "Deploy Workflow Runs",
)

DORA_DEPLOYMENT_HEADERS = (
    "Organization",
    "Repository",
    "Event Type",
    "Identifier / Tag",
    "Environment / Name",
    "Status",
    "Trigger Ref / Branch",
    "Created (IST)",
    "Lead Time (Hours)",
    "Workflow Run ID",
)

DORA_RECOVERY_HEADERS = (
    "Organization",
    "Repository",
    "Failed Event",
    "Failure Time (IST)",
    "Restored Event",
    "Restored Time (IST)",
    "MTTR (Hours)",
    "Status",
)

TIER_ORDER = {"Elite": 4, "High": 3, "Medium": 2, "Low": 1, "N/A": 0}


def get_df_tier(deploys_per_week: float) -> str:
    """Classify Deployment Frequency tier based on weekly cadence."""
    if deploys_per_week >= 7.0:
        return "Elite"
    if deploys_per_week >= 1.0:
        return "High"
    if deploys_per_week >= 0.25:
        return "Medium"
    return "Low"


def get_lttc_tier(lead_time_hours: Optional[float]) -> str:
    """Classify Lead Time for Changes tier based on elapsed hours."""
    if lead_time_hours is None:
        return "N/A"
    if lead_time_hours < 1.0:
        return "Elite"
    if lead_time_hours <= 168.0:  # <= 1 week
        return "High"
    if lead_time_hours <= 720.0:  # <= 1 month
        return "Medium"
    return "Low"


def get_cfr_tier(cfr_pct: float) -> str:
    """Classify Change Failure Rate tier based on failure percentage."""
    if cfr_pct <= 5.0:
        return "Elite"
    if cfr_pct <= 15.0:
        return "High"
    if cfr_pct <= 30.0:
        return "Medium"
    return "Low"


def get_mttr_tier(mttr_hours: Optional[float]) -> str:
    """Classify Mean Time to Recovery tier based on hours to restore service."""
    if mttr_hours is None:
        return "N/A"
    if mttr_hours < 1.0:
        return "Elite"
    if mttr_hours <= 24.0:
        return "High"
    if mttr_hours <= 168.0:
        return "Medium"
    return "Low"


def compute_composite_tier(tiers: List[str]) -> str:
    """Compute overall representative DORA tier from metric tier list."""
    valid_tiers = [t for t in tiers if t in TIER_ORDER and t != "N/A"]
    if not valid_tiers:
        return "Low"
    scores = [TIER_ORDER[t] for t in valid_tiers]
    avg_score = sum(scores) / len(scores)
    if avg_score >= 3.5:
        return "Elite"
    if avg_score >= 2.5:
        return "High"
    if avg_score >= 1.5:
        return "Medium"
    return "Low"


@dataclass
class DeploymentRecord:
    """Single deployment, release, or production workflow event."""

    event_type: str  # "Deployment", "Release", or "Workflow Run"
    event_id: str
    name: str
    environment: str
    status: str  # "SUCCESS", "FAILURE", "IN_PROGRESS"
    ref: str
    created_at: datetime
    lead_time_hours: Optional[float] = None
    run_id: Optional[int] = None


@dataclass
class RecoveryIncident:
    """Failure incident followed by successful service restoration (MTTR calculation)."""

    failed_event: str
    failure_time: datetime
    restored_event: str
    restored_time: datetime
    mttr_hours: float
    status: str = "Resolved"


@dataclass
class RepoDORAMetrics:
    """Aggregated DORA and delivery flow metrics for a repository."""

    org: str
    repo: str
    deployments: List[DeploymentRecord] = field(default_factory=list)
    incidents: List[RecoveryIncident] = field(default_factory=list)
    total_releases: int = 0
    deploy_workflow_runs: int = 0
    flow_advisory: str = "-"
    errors: List[str] = field(default_factory=list)

    @property
    def total_deploys(self) -> int:
        return len(self.deployments)

    @property
    def successful_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status == "SUCCESS")

    @property
    def failed_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status == "FAILURE")

    def deployment_frequency_per_week(self, window_days: int = 90) -> float:
        """Calculate deployments per 7-day week over the analysis window."""
        weeks = max(1.0, window_days / 7.0)
        return round(self.total_deploys / weeks, 2)

    @property
    def median_lead_time_hours(self) -> Optional[float]:
        """Compute median lead time for changes in hours."""
        times = [d.lead_time_hours for d in self.deployments if d.lead_time_hours is not None and d.lead_time_hours >= 0]
        if not times:
            return None
        return round(float(statistics.median(times)), 2)

    @property
    def change_failure_rate_pct(self) -> float:
        """Calculate percentage of deployments that resulted in failure."""
        if not self.total_deploys:
            return 0.0
        return round((self.failed_deploys / self.total_deploys) * 100, 2)

    @property
    def median_mttr_hours(self) -> Optional[float]:
        """Compute median recovery duration in hours."""
        if not self.incidents:
            return None
        durations = [inc.mttr_hours for inc in self.incidents]
        return round(float(statistics.median(durations)), 2)

    def get_tiers(self, window_days: int = 90) -> Tuple[str, str, str, str, str]:
        """Return (overall_tier, df_tier, lttc_tier, cfr_tier, mttr_tier)."""
        df_tier = get_df_tier(self.deployment_frequency_per_week(window_days))
        lttc_tier = get_lttc_tier(self.median_lead_time_hours)
        cfr_tier = get_cfr_tier(self.change_failure_rate_pct)
        mttr_tier = get_mttr_tier(self.median_mttr_hours)
        overall = compute_composite_tier([df_tier, lttc_tier, cfr_tier, mttr_tier])
        return overall, df_tier, lttc_tier, cfr_tier, mttr_tier


@dataclass(frozen=True)
class DORAConfig:
    """Runtime configuration for DORA metrics collection."""

    lookback_days: int = 90
    max_workers: int = 0
    timeout_seconds: int = 30
    max_retries: int = 5
    include_archived: bool = False
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_dora"
