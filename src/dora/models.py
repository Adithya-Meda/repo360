"""Data models, constants, and industry benchmarks for Repo360 DORA & Flow Metrics."""

from __future__ import annotations

import statistics
from collections import Counter
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
    "Deployment Rework Rate (%)",
    "Rework Tier",
    "Inactive Repos",
    "Open Incidents",
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
    "Deployment Rework Rate (%)",
    "Rework Tier",
    "Open Incidents",
    "Signal Mode",
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
    "Lead Time Method",
    "Rework / Hotfix",
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
    "Recovery Source",
    "Environment",
)

TIER_ORDER = {"Elite": 4, "High": 3, "Medium": 2, "Low": 1, "N/A": 0}


def get_df_tier(deploys_per_week: Optional[float]) -> str:
    """Classify Deployment Frequency tier based on weekly cadence."""
    if deploys_per_week is None:
        return "N/A"
    if deploys_per_week >= 7.0:
        return "Elite"
    if deploys_per_week >= 1.0:
        return "High"
    if deploys_per_week >= 0.25:
        return "Medium"
    return "Low"


def get_lttc_tier(lead_time_hours: Optional[float]) -> str:
    """Classify Lead Time for Changes tier using DORA Accelerate thresholds (<24h Elite)."""
    if lead_time_hours is None:
        return "N/A"
    if lead_time_hours < 24.0:  # < 1 day
        return "Elite"
    if lead_time_hours <= 168.0:  # <= 1 week
        return "High"
    if lead_time_hours <= 720.0:  # <= 1 month
        return "Medium"
    return "Low"


def get_cfr_tier(cfr_pct: Optional[float]) -> str:
    """Classify Change Failure Rate tier based on failure percentage."""
    if cfr_pct is None:
        return "N/A"
    if cfr_pct <= 5.0:
        return "Elite"
    if cfr_pct <= 15.0:
        return "High"
    if cfr_pct <= 30.0:
        return "Medium"
    return "Low"


def get_mttr_tier(mttr_hours: Optional[float]) -> str:
    """Classify Failed Deployment Recovery Time (MTTR) tier based on hours to restore service."""
    if mttr_hours is None:
        return "N/A"
    if mttr_hours < 1.0:
        return "Elite"
    if mttr_hours <= 24.0:
        return "High"
    if mttr_hours <= 168.0:
        return "Medium"
    return "Low"


def get_rework_tier(rework_pct: Optional[float]) -> str:
    """Classify Deployment Rework Rate tier based on percentage of remediation deployments."""
    if rework_pct is None:
        return "N/A"
    if rework_pct <= 2.0:
        return "Elite"
    if rework_pct <= 8.0:
        return "High"
    if rework_pct <= 15.0:
        return "Medium"
    return "Low"


def compute_composite_tier(
    tiers: List[str],
    cfr_tier: Optional[str] = None,
    rework_tier: Optional[str] = None,
) -> str:
    """Compute overall representative DORA tier with stability guardrails.

    Returns ``'N/A'`` when no valid metric tiers exist (e.g. inactive repositories).
    If stability is critically degraded (CFR or Rework tier is ``'Low'``), the
    composite rating is capped at ``'Medium'`` so unstable pipelines are never
    classified as High or Elite performers.
    """
    valid_tiers = [t for t in tiers if t in TIER_ORDER and t != "N/A"]
    if not valid_tiers:
        return "N/A"
    scores = [TIER_ORDER[t] for t in valid_tiers]
    avg_score = sum(scores) / len(scores)
    if avg_score >= 3.5:
        composite = "Elite"
    elif avg_score >= 2.5:
        composite = "High"
    elif avg_score >= 1.5:
        composite = "Medium"
    else:
        composite = "Low"

    if (cfr_tier == "Low" or rework_tier == "Low") and composite in ("Elite", "High"):
        return "Medium"
    return composite


def compute_fleet_dora_summary(
    repo_rows: List[list],
    lookback_days: int,
    deployment_rows: Optional[List[list]] = None,
    recovery_rows: Optional[List[list]] = None,
) -> Dict[str, Any]:
    """Compute active-repo-scoped fleet DORA aggregates and tiers consistently with org metrics."""
    total_repos = len(repo_rows)
    active_rows = [r for r in repo_rows if len(r) > 3 and isinstance(r[3], int) and r[3] > 0]
    active_repos = len(active_rows)
    inactive_repos = total_repos - active_repos

    total_deploys = sum(r[3] for r in active_rows)
    total_successful = sum(r[4] for r in active_rows if len(r) > 4 and isinstance(r[4], int))
    total_failed_deploys = sum(r[5] for r in active_rows if len(r) > 5 and isinstance(r[5], int))

    effective_failed = 0
    rework_total = 0
    for r in active_rows:
        if len(r) > 10 and isinstance(r[10], (int, float)):
            effective_failed += round((r[10] / 100.0) * r[3])
        elif len(r) > 5 and isinstance(r[5], int):
            effective_failed += r[5]

        if len(r) > 17 and isinstance(r[17], (int, float)):
            rework_total += round((r[17] / 100.0) * r[3])

    cfr_overall: Optional[float] = (
        round((effective_failed / total_deploys) * 100, 2) if total_deploys else None
    )
    rework_overall: Optional[float] = (
        round((rework_total / total_deploys) * 100, 2) if total_deploys else None
    )

    weeks = max(1.0, lookback_days / 7.0)
    total_df_weekly = round(total_deploys / weeks, 2) if total_deploys else 0.0
    df_values = [float(r[6]) for r in active_rows if len(r) > 6 and isinstance(r[6], (int, float))]
    median_df_weekly: Optional[float] = (
        round(float(statistics.median(df_values)), 2) if df_values else None
    )

    # Match org-level LTTC and MTTR statistical basis (event-level medians over completed outcomes)
    if deployment_rows is not None:
        lttc_values = [
            float(r[8])
            for r in deployment_rows
            if len(r) > 8 and r[5] == "SUCCESS" and isinstance(r[8], (int, float)) and float(r[8]) >= 0
        ]
        incomplete_deploys = sum(
            1 for r in deployment_rows if len(r) > 5 and r[5] not in ("SUCCESS", "FAILURE")
        )
    else:
        lttc_values = [float(r[8]) for r in active_rows if len(r) > 8 and isinstance(r[8], (int, float))]
        incomplete_deploys = 0
    median_lttc_hours: Optional[float] = (
        round(float(statistics.median(lttc_values)), 2) if lttc_values else None
    )

    if recovery_rows is not None:
        mttr_values = [
            float(r[6])
            for r in recovery_rows
            if len(r) > 7 and r[7] == "Resolved" and isinstance(r[6], (int, float))
        ]
    else:
        mttr_values = [float(r[12]) for r in active_rows if len(r) > 12 and isinstance(r[12], (int, float))]
    median_mttr_hours: Optional[float] = (
        round(float(statistics.median(mttr_values)), 2) if mttr_values else None
    )

    df_tier = get_df_tier(median_df_weekly)
    lttc_tier = get_lttc_tier(median_lttc_hours)
    cfr_tier = get_cfr_tier(cfr_overall)
    mttr_tier = get_mttr_tier(median_mttr_hours)
    rework_tier = get_rework_tier(rework_overall)
    fleet_tier = (
        compute_composite_tier(
            [df_tier, lttc_tier, cfr_tier, mttr_tier],
            cfr_tier=cfr_tier,
            rework_tier=rework_tier,
        )
        if active_rows
        else "N/A"
    )

    open_incidents = sum(r[19] for r in repo_rows if len(r) > 19 and isinstance(r[19], int))
    permission_gap_repos = sum(
        1 for r in repo_rows if len(r) > 20 and "Permission Gap" in str(r[20])
    )
    tier_counts = Counter(r[2] for r in repo_rows if len(r) > 2)

    return {
        "total_repos": total_repos,
        "active_repos": active_repos,
        "inactive_repos": inactive_repos,
        "total_deploys": total_deploys,
        "total_successful": total_successful,
        "total_failed_deploys": total_failed_deploys,
        "incomplete_deploys": incomplete_deploys,
        "effective_failed": effective_failed,
        "rework_total": rework_total,
        "median_df_weekly": median_df_weekly,
        "total_df_weekly": total_df_weekly,
        "median_lttc_hours": median_lttc_hours,
        "cfr_overall": cfr_overall,
        "median_mttr_hours": median_mttr_hours,
        "rework_overall": rework_overall,
        "df_tier": df_tier,
        "lttc_tier": lttc_tier,
        "cfr_tier": cfr_tier,
        "mttr_tier": mttr_tier,
        "rework_tier": rework_tier,
        "fleet_tier": fleet_tier,
        "open_incidents": open_incidents,
        "permission_gap_repos": permission_gap_repos,
        "tier_counts": tier_counts,
    }


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
    commit_sha: Optional[str] = None
    completed_at: Optional[datetime] = None
    stream_key: str = ""
    is_rework: bool = False
    failure_source: str = ""
    lead_time_source: str = ""


@dataclass
class RecoveryIncident:
    """Failure incident and service restoration (Failed Deployment Recovery Time / MTTR)."""

    failed_event: str
    failure_time: datetime
    restored_event: str
    restored_time: Optional[datetime]
    mttr_hours: Optional[float]
    status: str = "Resolved"
    source: str = "Deployment Stream"
    environment: str = "production"


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
    unlinked_prod_incidents: int = 0
    unlinked_rework_prs: int = 0
    incident_issues_count: int = 0
    rollback_hotfix_prs: int = 0
    signal_mode: str = "No Production Activity"
    deployments_api_forbidden: bool = False

    @property
    def completed_deployments(self) -> List[DeploymentRecord]:
        """Return only completed production deployments (excluding in-progress/inactive)."""
        return [d for d in self.deployments if d.status in ("SUCCESS", "FAILURE")]

    @property
    def total_deploys(self) -> int:
        return len(self.completed_deployments)

    @property
    def in_progress_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status not in ("SUCCESS", "FAILURE"))

    @property
    def successful_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status == "SUCCESS")

    @property
    def failed_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status == "FAILURE")

    @property
    def effective_failed_changes(self) -> int:
        """Total failed deployment changes including unlinked production incident/rollback signals."""
        if not self.total_deploys:
            return 0
        return min(self.total_deploys, self.failed_deploys + self.unlinked_prod_incidents)

    @property
    def rework_deploys(self) -> int:
        """Count of completed deployments representing rollbacks, reverts, or emergency hotfixes."""
        if not self.total_deploys:
            return 0
        flagged = sum(1 for d in self.completed_deployments if d.is_rework)
        return min(self.total_deploys, flagged + self.unlinked_rework_prs)

    def deployment_frequency_per_week(self, window_days: int = 90) -> float:
        """Calculate completed deployments per 7-day week (0.0 when no deployment succeeded)."""
        if self.successful_deploys == 0:
            return 0.0
        weeks = max(1.0, window_days / 7.0)
        return round(self.total_deploys / weeks, 2)

    @property
    def median_lead_time_hours(self) -> Optional[float]:
        """Compute median lead time for changes in hours strictly over successful deployments."""
        times = [
            d.lead_time_hours
            for d in self.deployments
            if d.status == "SUCCESS" and d.lead_time_hours is not None and d.lead_time_hours >= 0
        ]
        if not times:
            return None
        return round(float(statistics.median(times)), 2)

    @property
    def change_failure_rate_pct(self) -> Optional[float]:
        """Calculate percentage of deployments resulting in failure or post-deploy incidents."""
        if not self.total_deploys:
            return None
        return round((self.effective_failed_changes / self.total_deploys) * 100, 2)

    @property
    def rework_rate_pct(self) -> Optional[float]:
        """Calculate percentage of deployments dedicated to rework, rollbacks, or hotfixes."""
        if not self.total_deploys:
            return None
        return round((self.rework_deploys / self.total_deploys) * 100, 2)

    @property
    def resolved_incidents(self) -> List[RecoveryIncident]:
        """Return only incidents with verified restoration timestamps."""
        return [inc for inc in self.incidents if inc.status == "Resolved" and inc.mttr_hours is not None]

    @property
    def open_incidents_count(self) -> int:
        """Return count of unresolved production failure incidents."""
        return sum(1 for inc in self.incidents if inc.status == "Open")

    @property
    def median_mttr_hours(self) -> Optional[float]:
        """Compute median recovery duration in hours across resolved incidents."""
        durations = [inc.mttr_hours for inc in self.resolved_incidents if inc.mttr_hours is not None]
        if not durations:
            return None
        return round(float(statistics.median(durations)), 2)

    @property
    def rework_tier(self) -> str:
        """Return Deployment Rework Rate benchmark tier."""
        return get_rework_tier(self.rework_rate_pct)

    def get_tiers(self, window_days: int = 90) -> Tuple[str, str, str, str, str]:
        """Return (overall_tier, df_tier, lttc_tier, cfr_tier, mttr_tier)."""
        if self.total_deploys == 0:
            return "N/A", "N/A", "N/A", "N/A", "N/A"
        df_tier = get_df_tier(self.deployment_frequency_per_week(window_days))
        lttc_tier = get_lttc_tier(self.median_lead_time_hours)
        cfr_tier = get_cfr_tier(self.change_failure_rate_pct)
        mttr_tier = get_mttr_tier(self.median_mttr_hours)
        overall = compute_composite_tier(
            [df_tier, lttc_tier, cfr_tier, mttr_tier],
            cfr_tier=cfr_tier,
            rework_tier=self.rework_tier,
        )
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
    prod_environments: Tuple[str, ...] = ("prod", "production", "prd", "live", "release")
    incident_labels: Tuple[str, ...] = (
        "incident",
        "production-incident",
        "prod-incident",
        "sev1",
        "sev-1",
        "sev2",
        "sev-2",
        "p0",
        "p1",
        "outage",
        "hotfix",
        "rollback",
    )
    rework_keywords: Tuple[str, ...] = (
        "revert",
        "rollback",
        "hotfix",
        "fix-prod",
        "emergency-fix",
        "backout",
    )
    include_non_prod: bool = False
