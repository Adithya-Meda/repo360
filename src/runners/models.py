"""Data models and constants for Actions self-hosted runners analytics."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from ..common.timezones import DATE_FMT, IST, LOCAL_TZ, LOCAL_TZ_NAME

RUNNER_HEADERS = (
    "Organization", "Runner ID", "Runner Name", "Scope", "Runner Group", "OS", "Arch", "Agent Version",
    "Provisioning Type", "Status", "Current State", "Labels", "Repos Served",
    "Total Jobs", "Successful", "Failed", "Cancelled", "Timed Out", "Success Rate (%)",
    "Total Compute (Hours)", "Avg Duration (Mins)", "Median Duration (Mins)",
    "Min Duration (Mins)", "Max Duration (Mins)",
    "Avg Queue Wait (s)", "P95 Queue Wait (s)", "SLA Breaches (>60s)",
    f"First Active ({LOCAL_TZ_NAME})", f"Last Active ({LOCAL_TZ_NAME})",
    "Repositories Served",
)

ORG_RUNNER_HEADERS = (
    "Organization", "Total Runners", "Org-Level", "Repo-Level",
    "Online", "Offline", "Availability (%)", "Currently Busy", "Ephemeral Count",
    "Active Repos", "Total Jobs Run", "Successful", "Failed", "Cancelled", "Timed Out",
    "Success Rate (%)", "Total Compute (Hours)",
    "Avg Queue Wait (s)", "P95 Queue Wait (s)", "SLA Breaches (>60s)", "SLA Breach Rate (%)",
    "Active Repositories",
)

RUNNER_JOB_HEADERS = (
    "Organization",
    "Repository",
    "Workflow Name",
    "Run ID",
    "Job ID",
    "Job Name",
    "Runner Name",
    "Provisioning Type",
    "Conclusion",
    "Duration (Mins)",
    "Queue Wait (s)",
    "Queue SLA Breached (>60s)",
    f"Started At ({LOCAL_TZ_NAME})",
    f"Completed At ({LOCAL_TZ_NAME})",
    "Triggered By",
    "Job URL",
)


@dataclass
class RunnerJobRecord:
    """Individual workflow job execution event on a self-hosted runner."""

    org: str
    repo: str
    workflow_name: str
    run_id: int
    job_id: int
    job_name: str
    runner_id: int
    runner_name: str
    runner_type: str  # "Persistent (Static VM)" or "Ephemeral (Autoscaled)"
    conclusion: str   # "success", "failure", "cancelled", "timed_out", "other"
    duration_seconds: Optional[float] = None
    queue_seconds: Optional[float] = None
    sla_breached: bool = False
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    actor: str = "Unknown"
    html_url: str = ""


@dataclass
class RunnerInfo:
    """Metadata representing an Actions runner instance."""

    id: int
    name: str
    os: str
    status: str
    busy: bool
    labels: Tuple[str, ...]
    runner_group_id: Optional[int] = None
    runner_group_name: str = "Default"
    org: str = ""
    repo: Optional[str] = None
    arch: str = "x64"
    runner_version: str = "Unknown"
    ephemeral: bool = False


@dataclass
class RunnerJobMetrics:
    """Aggregated execution, compute, and queuing metrics for a runner or organization."""

    total_jobs: int = 0
    successful_jobs: int = 0
    failed_jobs: int = 0
    timed_out_jobs: int = 0
    cancelled_jobs: int = 0
    other_jobs: int = 0
    total_duration_seconds: float = 0.0
    total_queue_seconds: float = 0.0
    job_durations: List[float] = field(default_factory=list)
    queue_durations: List[float] = field(default_factory=list)
    repos_served: Set[str] = field(default_factory=set)
    first_job_time: Optional[datetime] = None
    last_job_time: Optional[datetime] = None

    @property
    def completed_jobs(self) -> int:
        return self.successful_jobs + self.failed_jobs + self.timed_out_jobs + self.cancelled_jobs + self.other_jobs

    @property
    def decisive_jobs(self) -> int:
        """Completed jobs excluding cancelled/neutral outcomes for true pass/fail rate denominators."""
        return self.successful_jobs + self.failed_jobs + self.timed_out_jobs

    @property
    def success_rate(self) -> Any:
        return round(self.successful_jobs / self.decisive_jobs * 100, 2) if self.decisive_jobs else "N/A"

    @property
    def failure_rate(self) -> Any:
        return round((self.failed_jobs + self.timed_out_jobs) / self.decisive_jobs * 100, 2) if self.decisive_jobs else "N/A"

    @property
    def avg_duration_minutes(self) -> Any:
        return round(self.total_duration_seconds / len(self.job_durations) / 60, 2) if self.job_durations else "N/A"

    @property
    def median_duration_minutes(self) -> Any:
        if not self.job_durations:
            return "N/A"
        sorted_d = sorted(self.job_durations)
        mid = len(sorted_d) // 2
        med = sorted_d[mid] if len(sorted_d) % 2 != 0 else (sorted_d[mid - 1] + sorted_d[mid]) / 2.0
        return round(med / 60.0, 2)

    @property
    def min_duration_minutes(self) -> Any:
        return round(min(self.job_durations) / 60.0, 2) if self.job_durations else "N/A"

    @property
    def max_duration_minutes(self) -> Any:
        return round(max(self.job_durations) / 60.0, 2) if self.job_durations else "N/A"

    @property
    def total_compute_hours(self) -> float:
        return round(self.total_duration_seconds / 3600, 2)

    @property
    def avg_queue_seconds(self) -> Any:
        return round(self.total_queue_seconds / len(self.queue_durations), 2) if self.queue_durations else "N/A"

    @property
    def p95_queue_seconds(self) -> Any:
        if not self.queue_durations:
            return "N/A"
        sorted_q = sorted(self.queue_durations)
        idx = max(0, min(len(sorted_q) - 1, math.ceil(0.95 * len(sorted_q)) - 1))
        return round(sorted_q[idx], 2)

    @property
    def queue_sla_breaches(self) -> int:
        return sum(1 for q in self.queue_durations if q > 60.0)

    @property
    def queue_sla_breach_rate(self) -> Any:
        return round(self.queue_sla_breaches / len(self.queue_durations) * 100, 2) if self.queue_durations else "N/A"

    def merge(self, other: RunnerJobMetrics) -> None:
        self.total_jobs += other.total_jobs
        self.successful_jobs += other.successful_jobs
        self.failed_jobs += other.failed_jobs
        self.timed_out_jobs += other.timed_out_jobs
        self.cancelled_jobs += other.cancelled_jobs
        self.other_jobs += other.other_jobs
        self.total_duration_seconds += other.total_duration_seconds
        self.total_queue_seconds += other.total_queue_seconds
        self.job_durations.extend(other.job_durations)
        self.queue_durations.extend(other.queue_durations)
        self.repos_served.update(other.repos_served)
        if other.first_job_time:
            self.first_job_time = min(self.first_job_time, other.first_job_time) if self.first_job_time else other.first_job_time
        if other.last_job_time:
            self.last_job_time = max(self.last_job_time, other.last_job_time) if self.last_job_time else other.last_job_time


@dataclass(frozen=True)
class RunnersConfig:
    """Runtime configuration for Actions self-hosted runners analytics."""

    lookback_days: int = 90
    max_runner_runs_per_repo: int = 50
    max_workers: int = 0
    timeout_seconds: int = 30
    max_retries: int = 5
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_runners"
