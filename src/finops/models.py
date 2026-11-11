"""Data models and constants for Repo360 FinOps storage & artifact profiling."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..activity.models import DATE_FMT, IST, parse_ts

FINOPS_SUMMARY_HEADERS = ("Scope", "Category", "Metric", "Value", "Optimization / Impact")

FINOPS_ORG_HEADERS = (
    "Organization",
    "Repositories Scanned",
    "Disk Storage (MB)",
    "Active Artifacts",
    "Artifact Storage (MB)",
    "90-Day Retention Artifacts",
    "Actions Caches",
    "Cache Storage (MB)",
    "Packages Tracked",
    "Total Storage Footprint (MB)",
    "Est. Inefficient Storage (MB)",
    "Storage Optimization Potential",
)

FINOPS_REPO_HEADERS = (
    "Organization",
    "Repository",
    "Visibility",
    "Disk Size (MB)",
    "Active Artifacts",
    "Artifact Storage (MB)",
    "90-Day Retention Count",
    "Actions Caches",
    "Cache Storage (MB)",
    "Packages Count",
    "Total Storage (MB)",
    "Optimization Status",
    "Actionable Recommendation",
)

FINOPS_ARTIFACT_HEADERS = (
    "Organization",
    "Repository",
    "Artifact ID",
    "Artifact Name",
    "Size (MB)",
    "Created (IST)",
    "Expires (IST)",
    "Retention Days",
    "Default 90-Day Policy",
    "Status",
    "Workflow Run ID",
)

FINOPS_CACHE_PACKAGE_HEADERS = (
    "Organization",
    "Repository",
    "Resource Type",
    "Identifier / Name",
    "Size (MB)",
    "Created (IST)",
    "Last Accessed / Updated (IST)",
    "Details / Versions",
)


def format_bytes_to_mb(size_in_bytes: int) -> float:
    """Convert bytes to Megabytes rounded to 2 decimal places."""
    return round(size_in_bytes / (1024 * 1024), 2)


def format_bytes_to_gb(size_in_bytes: int) -> float:
    """Convert bytes to Gigabytes rounded to 2 decimal places."""
    return round(size_in_bytes / (1024 * 1024 * 1024), 2)


@dataclass
class ArtifactRecord:
    """Metadata representing a GitHub Actions workflow artifact."""

    id: int
    name: str
    size_in_bytes: int
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    expired: bool = False
    workflow_run_id: Optional[int] = None

    @property
    def size_mb(self) -> float:
        return format_bytes_to_mb(self.size_in_bytes)

    @property
    def retention_days(self) -> int:
        if self.created_at and self.expires_at:
            delta = self.expires_at - self.created_at
            return max(0, delta.days)
        return 0

    @property
    def is_default_90_day(self) -> bool:
        """Flags artifacts configured with GitHub's default 90-day retention window."""
        return self.retention_days >= 85


@dataclass
class CacheRecord:
    """Metadata representing an Actions cache entry."""

    id: int
    key: str
    size_in_bytes: int
    created_at: Optional[datetime] = None
    last_accessed_at: Optional[datetime] = None

    @property
    def size_mb(self) -> float:
        return format_bytes_to_mb(self.size_in_bytes)

    @property
    def is_stale(self) -> bool:
        """Flags cache entries unaccessed for 30+ days."""
        if not self.last_accessed_at:
            return False
        now = datetime.now(timezone.utc)
        return (now - self.last_accessed_at).days >= 30


@dataclass
class PackageRecord:
    """Metadata representing a published GitHub package or container image."""

    id: int | str
    name: str
    package_type: str
    version_count: int = 1
    size_in_bytes: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    repo_name: Optional[str] = None

    @property
    def size_mb(self) -> float:
        return format_bytes_to_mb(self.size_in_bytes)


@dataclass
class RepoStorageProfile:
    """Aggregated FinOps storage profile for a single repository."""

    org: str
    repo: str
    visibility: str = "private"
    disk_size_kb: int = 0
    artifacts: List[ArtifactRecord] = field(default_factory=list)
    caches: List[CacheRecord] = field(default_factory=list)
    packages: List[PackageRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def disk_size_mb(self) -> float:
        return round(self.disk_size_kb / 1024, 2)

    @property
    def active_artifacts(self) -> List[ArtifactRecord]:
        return [a for a in self.artifacts if not a.expired]

    @property
    def active_artifact_bytes(self) -> int:
        return sum(a.size_in_bytes for a in self.active_artifacts)

    @property
    def active_artifact_mb(self) -> float:
        return format_bytes_to_mb(self.active_artifact_bytes)

    @property
    def default_90_day_artifacts(self) -> List[ArtifactRecord]:
        return [a for a in self.active_artifacts if a.is_default_90_day]

    @property
    def default_90_day_count(self) -> int:
        return len(self.default_90_day_artifacts)

    @property
    def cache_bytes(self) -> int:
        return sum(c.size_in_bytes for c in self.caches)

    @property
    def cache_mb(self) -> float:
        return format_bytes_to_mb(self.cache_bytes)

    @property
    def total_storage_mb(self) -> float:
        return round(self.disk_size_mb + self.active_artifact_mb + self.cache_mb, 2)

    @property
    def optimization_status(self) -> str:
        """Categorize storage posture into Optimized, Moderate Waste, or High Waste."""
        if self.default_90_day_count > 10 or self.active_artifact_mb > 500 or self.cache_mb > 1000:
            return "High Waste"
        if self.default_90_day_count > 0 or self.active_artifact_mb > 100 or self.cache_mb > 300:
            return "Moderate Waste"
        return "Optimized"

    @property
    def recommendation(self) -> str:
        """Formulate actionable cost and storage saving remediation."""
        recs = []
        if self.default_90_day_count > 0:
            inefficient_mb = format_bytes_to_mb(sum(a.size_in_bytes for a in self.default_90_day_artifacts))
            recs.append(f"Reduce retention-days from 90 to 7 on {self.default_90_day_count} artifacts (Saves ~{inefficient_mb} MB)")
        stale_caches = [c for c in self.caches if c.is_stale]
        if stale_caches:
            stale_mb = format_bytes_to_mb(sum(c.size_in_bytes for c in stale_caches))
            recs.append(f"Purge {len(stale_caches)} stale Actions caches (Frees ~{stale_mb} MB)")
        if not recs:
            if self.total_storage_mb > 2000:
                recs.append("Monitor repository git-lfs and history depth.")
            else:
                recs.append("Storage footprint optimized within standard thresholds.")
        return "; ".join(recs)


@dataclass(frozen=True)
class FinOpsConfig:
    """Runtime configuration for FinOps storage and artifact profiler."""

    max_workers: int = 0
    timeout_seconds: int = 30
    max_retries: int = 5
    include_archived: bool = False
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_finops"
