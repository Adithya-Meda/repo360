"""Collector and profiler for repository storage, workflow artifacts, and actions caches."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ..activity.models import IST, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import is_empty_repo, map_concurrent, resolve_worker_count, run_concurrent_ai
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
    errors_out: Optional[List[str]] = None,
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
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 403:
            if errors_out is not None:
                errors_out.append("Actions artifacts blocked (403 Forbidden — grant actions:read)")
        elif status != 404:
            logger.debug("Failed to retrieve artifacts for %s/%s: %s", org, repo, err)
            if errors_out is not None:
                errors_out.append(f"Actions artifacts HTTP {status}")
    except Exception as err:
        logger.debug("Unexpected error collecting artifacts for %s/%s: %s", org, repo, err)
        if errors_out is not None:
            errors_out.append(f"Actions artifacts error: {err}")
    return artifacts


def collect_repo_caches(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    errors_out: Optional[List[str]] = None,
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
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 403:
            if errors_out is not None:
                errors_out.append("Actions caches blocked (403 Forbidden — grant actions:read)")
        elif status != 404:
            logger.debug("Failed to retrieve caches for %s/%s: %s", org, repo, err)
            if errors_out is not None:
                errors_out.append(f"Actions caches HTTP {status}")
    except Exception as err:
        logger.debug("Unexpected error collecting caches for %s/%s: %s", org, repo, err)
        if errors_out is not None:
            errors_out.append(f"Actions caches error: {err}")
    return caches


def collect_org_packages(
    client: ResilientGitHubClient,
    org: str,
    errors_out: Optional[List[str]] = None,
) -> List[PackageRecord]:
    """Retrieve packages published under an organization with paginated GraphQL size enrichment."""
    packages: List[PackageRecord] = []
    seen_names: Set[str] = set()

    # Paginate packages and version files via GraphQL for total storage size
    gql_query = """
    query($org: String!, $cursor: String) {
      organization(login: $org) {
        packages(first: 100, after: $cursor) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            name
            packageType
            repository {
              name
            }
            versions(first: 50) {
              pageInfo {
                hasNextPage
                endCursor
              }
              nodes {
                files(first: 50) {
                  nodes {
                    size
                  }
                }
              }
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
    gql_versions_query = """
    query($pkgId: ID!, $cursor: String) {
      node(id: $pkgId) {
        ... on Package {
          versions(first: 50, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
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

    def _sum_version_files(version_nodes: List[Dict[str, Any]]) -> int:
        total = 0
        for ver in version_nodes:
            if not isinstance(ver, dict):
                continue
            files = (ver.get("files") or {}).get("nodes") or []
            total += sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))
        return total

    def _fetch_gql_info() -> Dict[str, Tuple[int, Optional[str]]]:
        info: Dict[str, Tuple[int, Optional[str]]] = {}
        try:
            cursor: Optional[str] = None
            seen_cursors: Set[str] = set()
            for _ in range(50):  # bounded up to 5,000 packages
                data = client.graphql(org, gql_query, {"org": org, "cursor": cursor})
                pkgs_conn = (data.get("data") or {}).get("organization", {}).get("packages") or {}
                nodes = pkgs_conn.get("nodes") or []
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    pkg_name = n.get("name", "")
                    versions_conn = n.get("versions") or {}
                    ver_nodes = versions_conn.get("nodes") or []
                    if ver_nodes:
                        total_bytes = _sum_version_files(ver_nodes)
                        v_page = versions_conn.get("pageInfo") or {}
                        v_cursor = v_page.get("endCursor")
                        v_has_next = bool(v_page.get("hasNextPage"))
                        pkg_id = n.get("id")
                        v_hops = 0
                        while pkg_id and v_has_next and v_cursor and v_hops < 20:
                            v_hops += 1
                            v_data = client.graphql(org, gql_versions_query, {"pkgId": pkg_id, "cursor": v_cursor})
                            next_conn = ((v_data.get("data") or {}).get("node") or {}).get("versions") or {}
                            next_nodes = next_conn.get("nodes") or []
                            if not next_nodes:
                                break
                            total_bytes += _sum_version_files(next_nodes)
                            next_page = next_conn.get("pageInfo") or {}
                            v_has_next = bool(next_page.get("hasNextPage"))
                            next_cursor = next_page.get("endCursor")
                            if not next_cursor or next_cursor == v_cursor:
                                break
                            v_cursor = next_cursor
                    else:
                        files = ((n.get("latestVersion") or {}).get("files") or {}).get("nodes") or []
                        total_bytes = sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))

                    repo = (n.get("repository") or {}).get("name")
                    if pkg_name:
                        info[pkg_name.lower()] = (total_bytes, repo)

                page_info = pkgs_conn.get("pageInfo") or {}
                if not page_info.get("hasNextPage"):
                    break
                next_cursor = page_info.get("endCursor")
                if not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except Exception as err:
            logger.debug("GraphQL package query failed for %s: %s", org, err)
            if errors_out is not None:
                errors_out.append(f"GraphQL package size enrichment unavailable: {err}")
        return info

    rest_errors: List[str] = []

    def _fetch_rest_pkg_type(pkg_type: str) -> List[Dict[str, Any]]:
        try:
            return list(client.paginate(org, f"/orgs/{org}/packages", {"package_type": pkg_type}))
        except requests.HTTPError as err:
            status = getattr(getattr(err, "response", None), "status_code", None)
            if status == 403:
                rest_errors.append(f"Packages REST API blocked for '{pkg_type}' (403 Forbidden — grant packages:read)")
            elif status not in (404, 400):
                logger.debug("Package query failed for %s (type %s): %s", org, pkg_type, err)
                rest_errors.append(f"Packages API HTTP {status} for '{pkg_type}'")
        except Exception:
            pass
        return []

    from ..common.interrupts import wait_future
    pool = ThreadPoolExecutor(max_workers=len(PACKAGE_TYPES) + 1, thread_name_prefix="finops-pkgs")
    try:
        gql_future = pool.submit(_fetch_gql_info)
        type_futures = [(pkg_type, pool.submit(_fetch_rest_pkg_type, pkg_type)) for pkg_type in PACKAGE_TYPES]
        gql_info = wait_future(gql_future)
        raw_by_type = [(pkg_type, wait_future(fut)) for pkg_type, fut in type_futures]
    except (KeyboardInterrupt, SystemExit):
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if errors_out is not None and rest_errors:
        errors_out.extend(sorted(set(rest_errors)))

    for pkg_type, items in raw_by_type:
        for item in items:
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

            size_bytes = 0
            if name.lower() in gql_info:
                gql_bytes, gql_repo = gql_info[name.lower()]
                size_bytes = gql_bytes
                if not repo_name and gql_repo:
                    repo_name = gql_repo

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

    if is_empty_repo(repo_meta):
        return profile

    profile.artifacts = collect_repo_artifacts(client, org, repo, errors_out=profile.errors)
    profile.caches = collect_repo_caches(client, org, repo, errors_out=profile.errors)
    return profile


def collect_finops_data(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    cfg: FinOpsConfig,
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]],
    failures_out: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[List[RepoStorageProfile], Dict[str, List[PackageRecord]]]:
    """Collect storage profiles and packages across all targets using thread pooling."""
    profiles: List[RepoStorageProfile] = []
    org_packages: Dict[str, List[PackageRecord]] = {}

    if orgs:
        def _collect_org_with_errors(org_name: str) -> Tuple[str, List[PackageRecord], List[str]]:
            errs: List[str] = []
            pkgs = collect_org_packages(client, org_name, errors_out=errs)
            return org_name, pkgs, errs

        org_pkg_batches = map_concurrent(
            orgs,
            _collect_org_with_errors,
            max_workers=min(8, len(orgs)),
            max_cap=8,
            thread_name_prefix="finops-org-pkgs",
        )
        for o, pkgs, errs in org_pkg_batches:
            org_packages[o] = pkgs
            if failures_out is not None:
                for err_msg in errs:
                    failures_out.append((o, err_msg))

    pkg_index: Dict[Tuple[str, str], List[PackageRecord]] = {}
    for o, pkgs in org_packages.items():
        o_key = o.lower()
        for pkg in pkgs:
            if pkg.repo_name:
                pkg_index.setdefault((o_key, pkg.repo_name.lower()), []).append(pkg)

    if targets:
        workers = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32)
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                fut = executor.submit(profile_single_repo, client, org, repo, meta)
                future_to_target[fut] = (org, repo)

            from ..common.interrupts import iter_completed
            for fut in iter_completed(future_to_target):
                org, repo = future_to_target[fut]
                try:
                    profile = fut.result()
                    profile.packages.extend(pkg_index.get((org.lower(), repo.lower()), ()))
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

    ai_items: List[Tuple[int, RepoStorageProfile, int]] = []
    rec_by_idx: Dict[int, str] = {}

    for idx, p in enumerate(profiles):
        stale_cache_count = sum(1 for c in p.caches if c.is_stale)
        is_baseline_optimized = (
            p.optimization_status == "Optimized"
            and p.default_90_day_count == 0
            and stale_cache_count == 0
            and p.total_storage_mb <= 2000
        )
        if is_baseline_optimized:
            rec_by_idx[idx] = "-"
        elif ai_engine is not None:
            ai_items.append((idx, p, stale_cache_count))
        else:
            rec_by_idx[idx] = p.recommendation

    if ai_items and ai_engine is not None:
        ai_recs = run_concurrent_ai(
            ai_items,
            lambda item: ai_engine.generate_finops_advisory(
                repo=f"{item[1].org}/{item[1].repo}",
                disk_size_mb=item[1].disk_size_mb,
                artifact_mb=item[1].active_artifact_mb,
                default_90d_count=item[1].default_90_day_count,
                stale_cache_count=item[2],
                optimization_status=item[1].optimization_status,
            ),
            ai_engine=ai_engine,
            max_workers=8,
        )
        for (idx, _, _), rec_val in zip(ai_items, ai_recs):
            rec_by_idx[idx] = rec_val

    org_profiles: Dict[str, List[RepoStorageProfile]] = {org: [] for org in orgs}
    for idx, p in enumerate(profiles):
        org_profiles.setdefault(p.org, []).append(p)
        rec = rec_by_idx.get(idx, p.recommendation)

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

    # Sum raw bytes before MB conversion to avoid rounding drift
    for org in orgs:
        p_list = org_profiles.get(org, [])
        pkgs = org_packages.get(org, [])
        total_disk_mb = round(sum(p.disk_size_kb for p in p_list) / 1024, 2)
        total_art_count = sum(len(p.active_artifacts) for p in p_list)
        total_art_bytes = sum(p.active_artifact_bytes for p in p_list)
        total_art_mb = format_bytes_to_mb(total_art_bytes)
        total_90d_count = sum(p.default_90_day_count for p in p_list)
        total_cache_count = sum(len(p.caches) for p in p_list)
        total_cache_bytes = sum(p.cache_bytes for p in p_list)
        total_cache_mb = format_bytes_to_mb(total_cache_bytes)
        total_pkg_bytes = sum(pkg.size_in_bytes for pkg in pkgs)
        total_pkg_mb = format_bytes_to_mb(total_pkg_bytes)
        total_footprint_mb = round(total_disk_mb + total_art_mb + total_cache_mb + total_pkg_mb, 2)

        inefficient_art_bytes = sum(a.size_in_bytes for p in p_list for a in p.default_90_day_artifacts)
        stale_cache_bytes = sum(c.size_in_bytes for p in p_list for c in p.caches if c.is_stale)
        est_waste_mb = format_bytes_to_mb(inefficient_art_bytes + stale_cache_bytes)

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
