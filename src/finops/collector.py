"""Collector and profiler for repository storage, workflow artifacts, and actions caches."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ..activity.models import IST, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from .models import (
    ArtifactRecord,
    CacheRecord,
    FinOpsConfig,
    PackageRecord,
    RepoStorageProfile,
    format_bytes_to_mb,
)

logger = logging.getLogger("repo360.finops")
PACKAGE_TYPES = ("container", "npm", "maven", "rubygems", "docker", "nuget")


def collect_repo_artifacts(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
) -> List[ArtifactRecord]:
    """Retrieve all workflow artifacts for a repository."""
    artifacts: List[ArtifactRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/actions/artifacts"
        for item in client.paginate_enveloped(org, endpoint, "artifacts"):
            art_id = item.get("id")
            name = item.get("name", "unnamed-artifact")
            size_bytes = item.get("size_in_bytes", 0)
            created_str = item.get("created_at")
            expires_str = item.get("expires_at")
            expired = bool(item.get("expired", False))
            run_info = item.get("workflow_run") or {}
            run_id = run_info.get("id") if isinstance(run_info, dict) else None

            created_at = parse_ts(created_str) if created_str else None
            expires_at = parse_ts(expires_str) if expires_str else None

            if art_id is not None:
                artifacts.append(
                    ArtifactRecord(
                        id=art_id,
                        name=name,
                        size_in_bytes=size_bytes,
                        created_at=created_at,
                        expires_at=expires_at,
                        expired=expired,
                        workflow_run_id=run_id,
                    )
                )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Failed to retrieve artifacts for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Unexpected error collecting artifacts for %s/%s: %s", org, repo, err)
    return artifacts


def collect_repo_caches(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
) -> List[CacheRecord]:
    """Retrieve Actions cache entries for a repository."""
    caches: List[CacheRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/actions/caches"
        for item in client.paginate_enveloped(org, endpoint, "actions_caches"):
            cache_id = item.get("id")
            key = item.get("key", "unnamed-cache")
            size_bytes = item.get("size_in_bytes", 0)
            created_str = item.get("created_at")
            accessed_str = item.get("last_accessed_at")

            created_at = parse_ts(created_str) if created_str else None
            accessed_at = parse_ts(accessed_str) if accessed_str else None

            if cache_id is not None:
                caches.append(
                    CacheRecord(
                        id=cache_id,
                        key=key,
                        size_in_bytes=size_bytes,
                        created_at=created_at,
                        last_accessed_at=accessed_at,
                    )
                )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Failed to retrieve caches for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Unexpected error collecting caches for %s/%s: %s", org, repo, err)
    return caches


def collect_org_packages(
    client: ResilientGitHubClient,
    org: str,
) -> List[PackageRecord]:
    """Retrieve packages published under an organization with GraphQL size and metadata enrichment."""
    packages: List[PackageRecord] = []
    seen_names: Set[str] = set()

    # 1. Fetch exact file sizes and repository mappings via GraphQL
    gql_query = """
    query($org: String!) {
      organization(login: $org) {
        packages(first: 100) {
          nodes {
            id
            name
            packageType
            repository {
              name
            }
            latestVersion {
              version
              files(first: 50) {
                nodes {
                  size
                }
              }
            }
          }
        }
      }
    }
    """
    gql_info: Dict[str, Tuple[int, Optional[str]]] = {}
    try:
        data = client.graphql(org, gql_query, {"org": org})
        nodes = (data.get("data") or {}).get("organization", {}).get("packages", {}).get("nodes") or []
        for n in nodes:
            pkg_name = n.get("name", "")
            files = (n.get("latestVersion") or {}).get("files", {}).get("nodes") or []
            total_bytes = sum(f.get("size", 0) for f in files if isinstance(f, dict))
            repo = (n.get("repository") or {}).get("name")
            if pkg_name:
                gql_info[pkg_name.lower()] = (total_bytes, repo)
    except Exception as err:
        logger.debug("GraphQL package query failed for %s: %s", org, err)

    # 2. Query REST packages across package types
    for pkg_type in PACKAGE_TYPES:
        try:
            endpoint = f"/orgs/{org}/packages"
            for item in client.paginate(org, endpoint, {"package_type": pkg_type}):
                pkg_id = item.get("id")
                name = item.get("name", "unnamed-package")
                type_name = item.get("package_type", pkg_type)
                version_count = item.get("version_count", 1)
                created_str = item.get("created_at")
                created_at = parse_ts(created_str) if created_str else None
                updated_str = item.get("updated_at")
                updated_at = parse_ts(updated_str) if updated_str else None
                repo_dict = item.get("repository") or {}
                repo_name = repo_dict.get("name") if isinstance(repo_dict, dict) else None

                # Enrich size and repository from GraphQL
                size_bytes = 0
                if name.lower() in gql_info:
                    gql_bytes, gql_repo = gql_info[name.lower()]
                    size_bytes = gql_bytes
                    if not repo_name and gql_repo:
                        repo_name = gql_repo

                # Fallback repository parsing from html_url
                if not repo_name and item.get("html_url"):
                    parts = item["html_url"].split("/")
                    if len(parts) >= 6 and parts[5] == "packages":
                        repo_name = parts[4]

                if pkg_id is not None:
                    seen_names.add(name.lower())
                    packages.append(
                        PackageRecord(
                            id=pkg_id,
                            name=name,
                            package_type=type_name,
                            version_count=version_count,
                            size_in_bytes=size_bytes,
                            created_at=created_at,
                            updated_at=updated_at,
                            repo_name=repo_name,
                        )
                    )
        except requests.HTTPError as err:
            if err.response.status_code not in (404, 403, 400):
                logger.debug("Package query failed for %s (type %s): %s", org, pkg_type, err)
        except Exception:
            pass
    return packages


def profile_single_repo(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    repo_meta: Dict[str, Any],
) -> RepoStorageProfile:
    """Analyze storage, artifacts, and caches for an individual repository."""
    disk_size_kb = int(repo_meta.get("size", 0)) if repo_meta else 0
    visibility = "private" if repo_meta.get("private") else ("internal" if repo_meta.get("visibility") == "internal" else "public")

    profile = RepoStorageProfile(
        org=org,
        repo=repo,
        visibility=visibility,
        disk_size_kb=disk_size_kb,
    )

    profile.artifacts = collect_repo_artifacts(client, org, repo)
    profile.caches = collect_repo_caches(client, org, repo)
    return profile


def collect_finops_data(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    cfg: FinOpsConfig,
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]],
) -> Tuple[List[RepoStorageProfile], Dict[str, List[PackageRecord]]]:
    """Collect storage profiles and packages across all targets using thread pooling."""
    profiles: List[RepoStorageProfile] = []
    org_packages: Dict[str, List[PackageRecord]] = {}

    # 1. Collect organization-level packages
    for org in orgs:
        org_packages[org] = collect_org_packages(client, org)

    # 2. Collect repository storage and artifact profiles in parallel
    if targets:
        workers = min(cfg.max_workers, len(targets))
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                fut = executor.submit(profile_single_repo, client, org, repo, meta)
                future_to_target[fut] = (org, repo)

            for fut in as_completed(future_to_target):
                org, repo = future_to_target[fut]
                try:
                    profile = fut.result()
                    # Associate repo packages if matched
                    matching_pkgs = [p for p in org_packages.get(org, []) if p.repo_name and p.repo_name.lower() == repo.lower()]
                    profile.packages.extend(matching_pkgs)
                    profiles.append(profile)
                except Exception as err:
                    logger.warning("Error profiling repository %s/%s: %s", org, repo, err)
                    err_profile = RepoStorageProfile(org=org, repo=repo, errors=[str(err)])
                    profiles.append(err_profile)
        except (KeyboardInterrupt, SystemExit):
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    profiles.sort(key=lambda p: (p.org.lower(), p.repo.lower()))
    return profiles, org_packages


def build_finops_rows(
    profiles: List[RepoStorageProfile],
    org_packages: Dict[str, List[PackageRecord]],
    orgs: List[str],
    ai_engine: Optional[Any] = None,
) -> Tuple[List[list], List[list], List[list], List[list]]:
    """Build structured data rows for each sheet in the FinOps Excel workbook."""
    repo_rows: List[list] = []
    artifact_rows: List[list] = []
    cache_package_rows: List[list] = []
    org_rows: List[list] = []

    # Map orgs to profiles
    org_profiles: Dict[str, List[RepoStorageProfile]] = {org: [] for org in orgs}
    for p in profiles:
        org_profiles.setdefault(p.org, []).append(p)

        stale_cache_count = sum(1 for c in p.caches if c.is_stale)
        is_baseline_optimized = (
            p.optimization_status == "Optimized"
            and p.default_90_day_count == 0
            and stale_cache_count == 0
            and p.total_storage_mb <= 2000
        )
        if is_baseline_optimized:
            rec = "-"
        else:
            rec = p.recommendation
            if ai_engine is not None:
                rec = ai_engine.generate_finops_advisory(
                    repo=f"{p.org}/{p.repo}",
                    disk_size_mb=p.disk_size_mb,
                    artifact_mb=p.active_artifact_mb,
                    default_90d_count=p.default_90_day_count,
                    stale_cache_count=stale_cache_count,
                    optimization_status=p.optimization_status,
                )

        # Repo row
        repo_rows.append([
            p.org,
            p.repo,
            p.visibility.capitalize(),
            p.disk_size_mb,
            len(p.active_artifacts),
            p.active_artifact_mb,
            p.default_90_day_count,
            len(p.caches),
            p.cache_mb,
            len(p.packages),
            p.total_storage_mb,
            p.optimization_status,
            rec,
        ])

        # Artifact detail rows
        for art in p.artifacts:
            created_ist = fmt_timestamp(art.created_at)
            expires_ist = fmt_timestamp(art.expires_at)
            policy_flag = "Default (90 Days)" if art.is_default_90_day else f"{art.retention_days} Days"
            status_flag = "Expired" if art.expired else "Active"
            run_id_val = str(art.workflow_run_id) if art.workflow_run_id else "-"
            artifact_rows.append([
                p.org,
                p.repo,
                art.id,
                art.name,
                art.size_mb,
                created_ist,
                expires_ist,
                art.retention_days,
                policy_flag,
                status_flag,
                run_id_val,
            ])

        # Cache rows
        for cache in p.caches:
            created_ist = fmt_timestamp(cache.created_at)
            accessed_ist = fmt_timestamp(cache.last_accessed_at)
            detail = "Stale (>30d unaccessed)" if cache.is_stale else "Active"
            cache_package_rows.append([
                p.org,
                p.repo,
                "Actions Cache",
                cache.key,
                cache.size_mb,
                created_ist,
                accessed_ist,
                detail,
            ])

    # Package rows
    for org, pkgs in org_packages.items():
        for pkg in pkgs:
            created_ist = fmt_timestamp(pkg.created_at)
            updated_ist = fmt_timestamp(pkg.updated_at)
            repo_target = pkg.repo_name or "[Org-Wide]"
            size_mb = pkg.size_mb if pkg.size_mb > 0 else (0.01 if pkg.size_in_bytes > 0 else 0.0)
            cache_package_rows.append([
                org,
                repo_target,
                f"Package ({pkg.package_type})",
                pkg.name,
                size_mb,
                created_ist,
                updated_ist,
                f"{pkg.version_count} versions",
            ])

    # Organization aggregation rows
    for org in orgs:
        p_list = org_profiles.get(org, [])
        pkgs = org_packages.get(org, [])
        total_disk_mb = round(sum(p.disk_size_mb for p in p_list), 2)
        total_art_count = sum(len(p.active_artifacts) for p in p_list)
        total_art_mb = round(sum(p.active_artifact_mb for p in p_list), 2)
        total_90d_count = sum(p.default_90_day_count for p in p_list)
        total_cache_count = sum(len(p.caches) for p in p_list)
        total_cache_mb = round(sum(p.cache_mb for p in p_list), 2)
        total_footprint_mb = round(sum(p.total_storage_mb for p in p_list), 2)

        # Estimate waste from 90-day retention inefficiencies (sum size of 90-day artifacts * ~0.8)
        inefficient_art_mb = round(
            sum(sum(a.size_mb for a in p.default_90_day_artifacts) for p in p_list),
            2,
        )
        stale_cache_mb = round(
            sum(sum(c.size_mb for c in p.caches if c.is_stale) for p in p_list),
            2,
        )
        est_waste_mb = round(inefficient_art_mb + stale_cache_mb, 2)

        savings_potential = "Low"
        if est_waste_mb > 500 or total_90d_count > 20:
            savings_potential = "High"
        elif est_waste_mb > 100 or total_90d_count > 5:
            savings_potential = "Moderate"

        org_rows.append([
            org,
            len(p_list),
            total_disk_mb,
            total_art_count,
            total_art_mb,
            total_90d_count,
            total_cache_count,
            total_cache_mb,
            len(pkgs),
            total_footprint_mb,
            est_waste_mb,
            savings_potential,
        ])

    return repo_rows, org_rows, artifact_rows, cache_package_rows
