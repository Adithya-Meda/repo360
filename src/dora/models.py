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
    "Code-Change Deploys",
    "No-Op Redeploys",
    "LTTC Coverage (%)",
    "Stalled Deploys (>24h)",
    "Unique Failed Changes",
    "Rework Deploys",
    "Collapsed Failure Retries",
    "Telemetry Scope",
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
    "Completed / Updated (IST)",
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
    """Classify Change Failure Rate tier using DORA 2023/2024 stability thresholds."""
    if cfr_pct is None:
        return "N/A"
    if cfr_pct <= 5.0:
        return "Elite"
    if cfr_pct <= 10.0:
        return "High"
    if cfr_pct <= 20.0:
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
    df_tier: Optional[str] = None,
    lttc_tier: Optional[str] = None,
    mttr_tier: Optional[str] = None,
    has_open_incidents: bool = False,
    successful_deploys: Optional[int] = None,
    code_change_deploys: Optional[int] = None,
    window_days: int = 90,
) -> str:
    """Compute overall representative DORA tier with stability, sample-size, and evidence guardrails.

    Returns ``'N/A'`` when no valid metric tiers exist (e.g. inactive repositories).
    Caps the composite rating at ``'Medium'`` when:
      - stability is critically degraded (CFR, Rework, or MTTR tier is ``'Low'``, or open unresolved incidents lack MTTR recovery),
      - throughput is ``'Low'``,
      - no value-delivering code changes were shipped (``code_change_deploys == 0``),
      - multi-week sample size is sparse (``successful_deploys < 3`` for ``window_days >= 14``), or
      - Lead Time for Changes is unmeasurable (``lttc_tier == 'N/A'``) without Elite cadence.
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

    stability_degraded = (
        cfr_tier == "Low"
        or rework_tier == "Low"
        or mttr_tier == "Low"
        or (has_open_incidents and mttr_tier == "N/A")
    )
    sparse_sample = (
        (code_change_deploys is not None and code_change_deploys == 0)
        or (successful_deploys is not None and window_days >= 14 and successful_deploys < 3)
    )
    if (stability_degraded or sparse_sample or df_tier == "Low") and composite in ("Elite", "High"):
        return "Medium"
    if lttc_tier == "N/A":
        if df_tier == "Elite" and composite == "Elite":
            return "High"
        if df_tier != "Elite" and composite in ("Elite", "High"):
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
    total_code_change = sum(
        r[21]
        if len(r) > 21 and isinstance(r[21], int)
        else (r[4] if len(r) > 4 and isinstance(r[4], int) else 0)
        for r in active_rows
    )
    total_no_op = sum(
        r[22] for r in active_rows if len(r) > 22 and isinstance(r[22], int)
    )
    stalled_deploys = sum(
        r[24] for r in repo_rows if len(r) > 24 and isinstance(r[24], int)
    )
    collapsed_retries = sum(
        r[27] for r in active_rows if len(r) > 27 and isinstance(r[27], int)
    )
    cfr_denominator = max(0, total_deploys - collapsed_retries)

    raw_effective_failed = 0.0
    raw_rework = 0.0
    for r in active_rows:
        row_collapsed = r[27] if len(r) > 27 and isinstance(r[27], int) else 0
        row_denom = max(0, r[3] - row_collapsed)
        if len(r) > 25 and isinstance(r[25], int):
            raw_effective_failed += float(r[25])
        elif len(r) > 10 and isinstance(r[10], (int, float)):
            raw_effective_failed += (float(r[10]) / 100.0) * row_denom
        elif len(r) > 5 and isinstance(r[5], int):
            raw_effective_failed += float(max(0, r[5] - row_collapsed))

        if len(r) > 26 and isinstance(r[26], int):
            raw_rework += float(r[26])
        elif len(r) > 17 and isinstance(r[17], (int, float)):
            raw_rework += (float(r[17]) / 100.0) * row_denom

    effective_failed = min(cfr_denominator, round(raw_effective_failed)) if cfr_denominator else 0
    rework_total = min(cfr_denominator, round(raw_rework)) if cfr_denominator else 0

    cfr_overall: Optional[float] = (
        round((effective_failed / cfr_denominator) * 100, 2) if cfr_denominator else None
    )
    rework_overall: Optional[float] = (
        round((rework_total / cfr_denominator) * 100, 2) if cfr_denominator else None
    )

    weeks = max(1.0, lookback_days / 7.0)
    total_df_weekly = round(total_code_change / weeks, 2) if total_code_change else 0.0
    df_values = [float(r[6]) for r in active_rows if len(r) > 6 and isinstance(r[6], (int, float))]
    median_df_weekly: Optional[float] = (
        round(float(statistics.median(df_values)), 2) if df_values else None
    )

    # Match org-level LTTC and MTTR statistical basis (event-level medians over completed outcomes)
    pr_lttc_count = 0
    commit_lttc_count = 0
    if deployment_rows is not None:
        lttc_values = [
            float(r[8])
            for r in deployment_rows
            if len(r) > 8 and r[5] == "SUCCESS" and isinstance(r[8], (int, float)) and float(r[8]) >= 0
        ]
        incomplete_deploys = sum(
            1 for r in deployment_rows if len(r) > 5 and r[5] not in ("SUCCESS", "FAILURE")
        )
        pr_lttc_count = sum(
            1
            for r in deployment_rows
            if len(r) > 10 and r[5] == "SUCCESS" and str(r[10]) == "pr_to_deploy"
        )
        commit_lttc_count = sum(
            1
            for r in deployment_rows
            if len(r) > 10
            and r[5] == "SUCCESS"
            and str(r[10]) in ("commit_to_deploy", "commit_timestamp")
        )
        lttc_coverage_pct: Optional[float] = (
            round(min(100.0, (len(lttc_values) / total_code_change) * 100.0), 1)
            if total_code_change > 0
            else None
        )
    else:
        lttc_values = [float(r[8]) for r in active_rows if len(r) > 8 and isinstance(r[8], (int, float))]
        incomplete_deploys = 0
        covered_weighted = 0.0
        for r in active_rows:
            cc = r[21] if len(r) > 21 and isinstance(r[21], int) else (r[4] if len(r) > 4 and isinstance(r[4], int) else 0)
            if cc > 0 and len(r) > 23 and isinstance(r[23], (int, float)):
                covered_weighted += (float(r[23]) / 100.0) * cc
        lttc_coverage_pct = (
            round(min(100.0, (covered_weighted / total_code_change) * 100.0), 1)
            if total_code_change > 0 and any(len(r) > 23 and isinstance(r[23], (int, float)) for r in active_rows)
            else None
        )
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

    open_incidents = sum(r[19] for r in repo_rows if len(r) > 19 and isinstance(r[19], int))
    permission_gap_repos = sum(
        1 for r in repo_rows if len(r) > 20 and "Permission Gap" in str(r[20])
    )
    tier_counts = Counter(r[2] for r in repo_rows if len(r) > 2)

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
            df_tier=df_tier,
            lttc_tier=lttc_tier,
            mttr_tier=mttr_tier,
            has_open_incidents=open_incidents > 0,
            successful_deploys=total_successful,
            code_change_deploys=total_code_change,
            window_days=lookback_days,
        )
        if active_rows
        else "N/A"
    )

    return {
        "total_repos": total_repos,
        "active_repos": active_repos,
        "inactive_repos": inactive_repos,
        "total_deploys": total_deploys,
        "total_successful": total_successful,
        "code_change_deploys": total_code_change,
        "no_op_deploys": total_no_op,
        "total_failed_deploys": total_failed_deploys,
        "collapsed_retries": collapsed_retries,
        "cfr_denominator": cfr_denominator,
        "incomplete_deploys": incomplete_deploys,
        "stalled_deploys": stalled_deploys,
        "effective_failed": effective_failed,
        "rework_total": rework_total,
        "median_df_weekly": median_df_weekly,
        "total_df_weekly": total_df_weekly,
        "median_lttc_hours": median_lttc_hours,
        "lttc_coverage_pct": lttc_coverage_pct,
        "pr_lttc_count": pr_lttc_count,
        "commit_lttc_count": commit_lttc_count,
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


def resolve_stream_key(ev: DeploymentRecord) -> str:
    """Return logical recovery stream identifier so unrelated workflows never cancel each other's failures."""
    if ev.stream_key:
        return ev.stream_key
    env_key = (ev.environment or "production").strip().lower()
    if ev.event_type == "Workflow Run":
        return f"workflow:{ev.name.strip().lower()}:{env_key}"
    return f"{ev.event_type.lower()}:{env_key}"


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
    stalled_deploys_count: int = 0

    @property
    def completed_deployments(self) -> List[DeploymentRecord]:
        """Return only completed production deployments (excluding in-progress/inactive)."""
        return [d for d in self.deployments if d.status in ("SUCCESS", "FAILURE")]

    def _deduplicate_failure_retries(self) -> Tuple[List[DeploymentRecord], int]:
        """Collapse consecutive unrecovered retries on the same commit/workflow stream."""
        by_stream: Dict[str, List[DeploymentRecord]] = {}
        for ev in self.completed_deployments:
            by_stream.setdefault(resolve_stream_key(ev), []).append(ev)

        unique_events: List[DeploymentRecord] = []
        redundant = 0
        for events in by_stream.values():
            events.sort(key=lambda item: item.created_at)
            active_fail_sha: Optional[str] = None
            last_missing_sha_fail: Optional[DeploymentRecord] = None
            in_failure_chain = False
            for ev in events:
                if ev.status == "SUCCESS":
                    in_failure_chain = False
                    active_fail_sha = None
                    last_missing_sha_fail = None
                    unique_events.append(ev)
                    continue

                sha = (ev.commit_sha or "").strip()
                if not in_failure_chain:
                    in_failure_chain = True
                    active_fail_sha = sha or None
                    last_missing_sha_fail = None if sha else ev
                    unique_events.append(ev)
                    continue

                if sha and active_fail_sha and sha == active_fail_sha:
                    redundant += 1
                    continue
                if (
                    not sha
                    and not active_fail_sha
                    and ev.event_type == "Workflow Run"
                    and last_missing_sha_fail is not None
                    and ev.ref == last_missing_sha_fail.ref
                    and (ev.created_at - last_missing_sha_fail.created_at).total_seconds() <= 1800.0
                ):
                    redundant += 1
                    last_missing_sha_fail = ev
                    continue

                active_fail_sha = sha or None
                last_missing_sha_fail = None if sha else ev
                unique_events.append(ev)
        return unique_events, redundant

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
    def code_change_deploys(self) -> int:
        """Count successful production deployments that introduced actual code/version changes."""
        return sum(
            1
            for d in self.deployments
            if d.status == "SUCCESS" and d.lead_time_source != "no_code_change"
        )

    @property
    def no_op_deploys(self) -> int:
        """Count successful same-SHA/zero-diff redeployments excluded from active delivery cadence."""
        return sum(
            1
            for d in self.deployments
            if d.status == "SUCCESS" and d.lead_time_source == "no_code_change"
        )

    @property
    def failed_deploys(self) -> int:
        return sum(1 for d in self.deployments if d.status == "FAILURE")

    @property
    def redundant_failure_retries(self) -> int:
        """Count consecutive same-change failure retries collapsed for CFR/Rework normalization."""
        _, redundant = self._deduplicate_failure_retries()
        return redundant

    @property
    def cfr_denominator(self) -> int:
        """Completed deployment denominator after collapsing redundant same-SHA failure retries."""
        return max(0, self.total_deploys - self.redundant_failure_retries)

    @property
    def effective_failed_changes(self) -> int:
        """Total unique failed deployment changes including unlinked production incident/rollback signals."""
        denom = self.cfr_denominator
        if not denom:
            return 0
        unique_events, _ = self._deduplicate_failure_retries()
        unique_failed = sum(1 for d in unique_events if d.status == "FAILURE")
        return min(denom, unique_failed + self.unlinked_prod_incidents)

    @property
    def rework_deploys(self) -> int:
        """Count of deduplicated completed deployments representing rollbacks, reverts, or emergency hotfixes."""
        denom = self.cfr_denominator
        if not denom:
            return 0
        unique_events, _ = self._deduplicate_failure_retries()
        flagged = sum(1 for d in unique_events if d.is_rework)
        return min(denom, flagged + self.unlinked_rework_prs)

    def deployment_frequency_per_week(self, window_days: int = 90) -> float:
        """Calculate value-delivering production deployments (excluding same-SHA no-ops) per 7-day week."""
        if self.code_change_deploys == 0:
            return 0.0
        weeks = max(1.0, window_days / 7.0)
        return round(self.code_change_deploys / weeks, 2)

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
    def lttc_coverage_pct(self) -> Optional[float]:
        """Share of successful code-change deployments with measured lead time."""
        denom = self.code_change_deploys
        if denom == 0:
            return None
        measured = sum(
            1
            for d in self.deployments
            if d.status == "SUCCESS"
            and d.lead_time_source != "no_code_change"
            and d.lead_time_hours is not None
            and d.lead_time_hours >= 0
        )
        return round(min(100.0, (measured / denom) * 100.0), 1)

    @property
    def telemetry_scope(self) -> str:
        """Describe signal completeness for executive auditability."""
        if self.total_deploys == 0:
            return "Permission Gap" if self.deployments_api_forbidden else "No Production Activity"
        parts: List[str] = [
            "Full PR+Issue"
            if (self.incident_issues_count > 0 or self.rollback_hotfix_prs > 0)
            else "Pipeline-Only MTTR/CFR"
        ]
        cov = self.lttc_coverage_pct
        if cov is None or cov == 0.0:
            parts.append("No LTTC")
        elif cov < 50.0:
            parts.append(f"Sparse LTTC ({cov:.0f}%)")
        if self.stalled_deploys_count > 0:
            parts.append(f"Stalled:{self.stalled_deploys_count}")
        if self.deployments_api_forbidden:
            parts.append("API 403 Gap")
        return " | ".join(parts)

    @property
    def change_failure_rate_pct(self) -> Optional[float]:
        """Calculate percentage of deployments resulting in failure or post-deploy incidents."""
        denom = self.cfr_denominator
        if not denom:
            return None
        return round((self.effective_failed_changes / denom) * 100, 2)

    @property
    def rework_rate_pct(self) -> Optional[float]:
        """Calculate percentage of deployments dedicated to rework, rollbacks, or hotfixes."""
        denom = self.cfr_denominator
        if not denom:
            return None
        return round((self.rework_deploys / denom) * 100, 2)

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
            df_tier=df_tier,
            lttc_tier=lttc_tier,
            mttr_tier=mttr_tier,
            has_open_incidents=self.open_incidents_count > 0,
            successful_deploys=self.successful_deploys,
            code_change_deploys=self.code_change_deploys,
            window_days=window_days,
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
