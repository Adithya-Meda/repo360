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
    "Repos with 90-Day Policy",
    "Git LFS (MB)",
    "Release Assets (MB)",
)

FINOPS_REPO_HEADERS = (
    "Organization",
    "Repository",
    "Visibility",
    "Disk Size (MB)",
    "Active Artifacts",
    "Artifact Storage (MB)",
    "90-Day Retention Count",
    "Repo Policy (Days)",
    "Actions Caches",
    "Cache Storage (MB)",
    "Packages Count",
    "Git LFS (MB)",
    "Release Assets (MB)",
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
    "Repo Policy (Days)",
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

FINOPS_BILLING_HEADERS = (
    "Organization",
    "Scope / Repository",
    "Period",
    "Product",
    "SKU",
    "Quantity",
    "Unit",
    "Gross Amount (USD)",
    "Discount Amount (USD)",
    "Net Amount (USD)",
)


def _round_mb(mb_value: float) -> float:
    """Keep 2 decimals for normal sizes and 4 decimals for sub-0.01 MB values so non-zero items stay visible."""
    if mb_value <= 0:
        return 0.0
    if mb_value >= 0.01:
        return round(mb_value, 2)
    return max(0.0001, round(mb_value, 4))


def format_bytes_to_mb(size_in_bytes: int) -> float:
    """Convert bytes to MiB rounded with precision preservation for small resources."""
    return _round_mb(size_in_bytes / (1024 * 1024))


def format_kb_to_mb(size_in_kb: int) -> float:
    """Convert KiB to MiB rounded with precision preservation for small repositories."""
    return _round_mb(size_in_kb / 1024)


def format_bytes_to_gb(size_in_bytes: int) -> float:
    """Convert bytes to GiB rounded to 2 decimal places."""
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
            total_seconds = (self.expires_at - self.created_at).total_seconds()
            return max(0, round(total_seconds / 86400))
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

    def is_stale_for(self, stale_days: int = 30) -> bool:
        """Flags cache entries unaccessed for at least ``stale_days`` days."""
        if not self.last_accessed_at:
            return False
        now = datetime.now(timezone.utc)
        return (now - self.last_accessed_at).total_seconds() >= max(1, stale_days) * 86400

    @property
    def is_stale(self) -> bool:
        """Flags cache entries unaccessed for 30+ days."""
        return self.is_stale_for(30)


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
class BillingUsageRecord:
    """Single usage or summary line item from GitHub's Enhanced Billing Platform API."""

    org: str
    scope: str
    period: str
    product: str
    sku: str
    quantity: float
    unit_type: str
    gross_amount: float = 0.0
    discount_amount: float = 0.0
    net_amount: float = 0.0


@dataclass
class OrgBillingSummary:
    """Aggregated billing and compute spend for one organization."""

    org: str
    period: str = ""
    actions_compute_minutes: float = 0.0
    actions_storage_gb_hours: float = 0.0
    git_lfs_gb_hours: float = 0.0
    packages_gb_hours: float = 0.0
    gross_amount_usd: float = 0.0
    discount_amount_usd: float = 0.0
    net_amount_usd: float = 0.0
    repo_lfs_gb_hours: Dict[str, float] = field(default_factory=dict)
    items: List[BillingUsageRecord] = field(default_factory=list)

    @property
    def ytd_compute_minutes(self) -> float:
        return sum(
            it.quantity
            for it in self.items
            if it.period == "YTD Usage" and "minute" in it.unit_type.lower()
        )

    @property
    def ytd_gross_amount_usd(self) -> float:
        return sum(it.gross_amount for it in self.items if it.period == "YTD Usage")


@dataclass
class ReleaseAssetRecord:
    """Metadata representing a binary asset attached to a GitHub release."""

    tag_name: str
    asset_name: str
    size_in_bytes: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

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
    lfs_bytes: int = 0
    lfs_files: List[Tuple[str, int]] = field(default_factory=list)
    lfs_oids: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    lfs_gb_hours: float = 0.0
    release_assets: List[ReleaseAssetRecord] = field(default_factory=list)
    repo_retention_days: Optional[int] = None
    errors: List[str] = field(default_factory=list)

    @property
    def disk_size_mb(self) -> float:
        return format_kb_to_mb(self.disk_size_kb)

    @property
    def lfs_mb(self) -> float:
        return format_bytes_to_mb(self.lfs_bytes)

    @property
    def release_bytes(self) -> int:
        return sum(a.size_in_bytes for a in self.release_assets)

    @property
    def release_mb(self) -> float:
        return format_bytes_to_mb(self.release_bytes)

    @property
    def has_90_day_repo_policy(self) -> bool:
        return self.repo_retention_days is not None and self.repo_retention_days >= 85

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
    def package_bytes(self) -> int:
        return sum(pkg.size_in_bytes for pkg in self.packages)

    @property
    def package_mb(self) -> float:
        return format_bytes_to_mb(self.package_bytes)

    @property
    def total_storage_bytes(self) -> int:
        return (
            self.disk_size_kb * 1024
            + self.active_artifact_bytes
            + self.cache_bytes
            + self.package_bytes
            + self.lfs_bytes
            + self.release_bytes
        )

    @property
    def total_storage_mb(self) -> float:
        return format_bytes_to_mb(self.total_storage_bytes)

    @property
    def optimization_status(self) -> str:
        """Categorize storage posture into Optimized, Moderate Waste, or High Waste."""
        if (
            self.default_90_day_count > 10
            or self.active_artifact_mb > 500
            or self.cache_mb > 1000
            or self.lfs_mb > 500
            or self.release_mb > 500
        ):
            return "High Waste"
        if (
            self.default_90_day_count > 0
            or self.active_artifact_mb > 100
            or self.cache_mb > 300
            or self.lfs_mb >= 50
            or self.release_mb >= 50
        ):
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
        if self.lfs_mb >= 50:
            recs.append(f"Audit {len(self.lfs_files) or 'tracked'} Git LFS objects ({self.lfs_mb} MB) and purge obsolete binary blobs")
        if self.release_mb >= 50:
            recs.append(f"Prune superseded GitHub Release binaries ({len(self.release_assets)} assets, {self.release_mb} MB)")
        if self.repo_retention_days is not None and self.repo_retention_days >= 85 and self.active_artifacts:
            recs.append(f"Lower repository Actions artifact retention policy from {self.repo_retention_days} days to <=30 days")
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
    stale_cache_days: int = 30
    retention_threshold_days: int = 85
    large_repo_mb: float = 2000.0
    output_dir: str = "./repo360-output"
    file_prefix: str = "repo360_finops"
