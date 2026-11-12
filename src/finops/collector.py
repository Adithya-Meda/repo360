"""Collector and profiler for repository storage, workflow artifacts, and actions caches."""

from __future__ import annotations

import base64
import fnmatch
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

from ..activity.models import IST, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import (
    AI_NOT_ENABLED_LABEL,
    is_ai_disabled,
    is_empty_repo,
    map_concurrent,
    resolve_worker_count,
    run_concurrent_ai,
)
from .models import (
    ArtifactRecord,
    BillingUsageRecord,
    CacheRecord,
    FinOpsConfig,
    OrgBillingSummary,
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
                id
                files(first: 50) {
                  pageInfo {
                    hasNextPage
                    endCursor
                  }
                  nodes {
                    size
                  }
                }
              }
            }
            latestVersion {
              id
              version
              files(first: 50) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
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
          versions(first: 100, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              id
              files(first: 100) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
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
    gql_files_query = """
    query($verId: ID!, $cursor: String) {
      node(id: $verId) {
        ... on PackageVersion {
          files(first: 100, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              size
            }
          }
        }
      }
    }
    """

    def _sum_files_connection(files_conn: Dict[str, Any], ver_id: Optional[str]) -> int:
        file_nodes = files_conn.get("nodes") or []
        total = sum(int(f.get("size") or 0) for f in file_nodes if isinstance(f, dict))
        f_page = files_conn.get("pageInfo") or {}
        f_cursor = f_page.get("endCursor")
        f_has_next = bool(f_page.get("hasNextPage"))
        f_hops = 0
        while ver_id and f_has_next and f_cursor and f_hops < 50:
            f_hops += 1
            f_data = client.graphql(org, gql_files_query, {"verId": ver_id, "cursor": f_cursor})
            next_files = ((f_data.get("data") or {}).get("node") or {}).get("files") or {}
            next_nodes = next_files.get("nodes") or []
            if not next_nodes:
                break
            total += sum(int(f.get("size") or 0) for f in next_nodes if isinstance(f, dict))
            next_page = next_files.get("pageInfo") or {}
            f_has_next = bool(next_page.get("hasNextPage"))
            next_cursor = next_page.get("endCursor")
            if not next_cursor or next_cursor == f_cursor:
                break
            f_cursor = next_cursor
        return total

    def _sum_version_files(version_nodes: List[Dict[str, Any]]) -> int:
        total = 0
        for ver in version_nodes:
            if not isinstance(ver, dict):
                continue
            files_conn = ver.get("files") or {}
            total += _sum_files_connection(files_conn, ver.get("id"))
        return total

    def _fetch_gql_info() -> Tuple[Dict[Tuple[str, str], Tuple[int, Optional[str]]], Dict[str, List[Tuple[int, Optional[str]]]]]:
        by_type_name: Dict[Tuple[str, str], Tuple[int, Optional[str]]] = {}
        by_name_fallback: Dict[str, List[Tuple[int, Optional[str]]]] = {}
        try:
            cursor: Optional[str] = None
            seen_cursors: Set[str] = set()
            for _ in range(50):
                data = client.graphql(org, gql_query, {"org": org, "cursor": cursor})
                pkgs_conn = (data.get("data") or {}).get("organization", {}).get("packages") or {}
                nodes = pkgs_conn.get("nodes") or []
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    pkg_name = n.get("name", "")
                    pkg_type_raw = n.get("packageType") or ""
                    versions_conn = n.get("versions") or {}
                    ver_nodes = versions_conn.get("nodes") or []
                    if ver_nodes:
                        total_bytes = _sum_version_files(ver_nodes)
                        v_page = versions_conn.get("pageInfo") or {}
                        v_cursor = v_page.get("endCursor")
                        v_has_next = bool(v_page.get("hasNextPage"))
                        pkg_id = n.get("id")
                        v_hops = 0
                        while pkg_id and v_has_next and v_cursor and v_hops < 200:
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
                        latest = n.get("latestVersion") or {}
                        total_bytes = _sum_files_connection(latest.get("files") or {}, latest.get("id"))

                    repo = (n.get("repository") or {}).get("name")
                    if pkg_name:
                        entry = (total_bytes, repo)
                        if pkg_type_raw:
                            by_type_name[(pkg_type_raw.lower(), pkg_name.lower())] = entry
                        by_name_fallback.setdefault(pkg_name.lower(), []).append(entry)

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
        return by_type_name, by_name_fallback

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
        except Exception as err:
            logger.debug("Unexpected error querying packages for %s (type %s): %s", org, pkg_type, err)
            rest_errors.append(f"Packages API error for '{pkg_type}': {err}")
        return []

    from ..common.interrupts import wait_future
    pool = ThreadPoolExecutor(max_workers=len(PACKAGE_TYPES) + 1, thread_name_prefix="finops-pkgs")
    try:
        gql_future = pool.submit(_fetch_gql_info)
        type_futures = [(pkg_type, pool.submit(_fetch_rest_pkg_type, pkg_type)) for pkg_type in PACKAGE_TYPES]
        gql_by_type_name, gql_by_name_fallback = wait_future(gql_future)
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
            type_key = (type_name.lower(), name.lower())
            if type_key in gql_by_type_name:
                gql_bytes, gql_repo = gql_by_type_name[type_key]
                size_bytes = gql_bytes
                if not repo_name and gql_repo:
                    repo_name = gql_repo
            else:
                candidates = gql_by_name_fallback.get(name.lower(), ())
                if len(candidates) == 1:
                    gql_bytes, gql_repo = candidates[0]
                    size_bytes = gql_bytes
                    if not repo_name and gql_repo:
                        repo_name = gql_repo

            if not repo_name and item.get("html_url"):
                parts = item["html_url"].split("/")
                if len(parts) >= 6 and parts[5] == "packages":
                    repo_name = parts[4]

            if pkg_id is not None:
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


def collect_org_cache_repos(client: ResilientGitHubClient, org: str) -> Optional[Set[str]]:
    """Return lowercased repository names that have active Actions caches, or None if unavailable."""
    try:
        active_repos: Set[str] = set()
        for item in client.paginate_enveloped(
            org, f"/orgs/{org}/actions/cache/usage-by-repository", "repository_cache_usages"
        ):
            if not isinstance(item, dict):
                continue
            has_cache = (item.get("active_caches_size_in_bytes") or 0) > 0 or (
                item.get("active_caches_count") or 0
            ) > 0
            if has_cache:
                full_name = str(item.get("full_name") or "")
                repo_short = full_name.split("/", 1)[-1].lower()
                if repo_short:
                    active_repos.add(repo_short)
        return active_repos
    except requests.HTTPError as err:
        logger.debug("Org cache usage summary unavailable for %s: %s", org, err)
        return None
    except Exception as err:
        logger.debug("Unexpected error checking org cache summary for %s: %s", org, err)
        return None


def _parse_lfs_patterns(gitattributes_text: str) -> List[str]:
    patterns: List[str] = []
    for raw_line in gitattributes_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "filter=lfs" not in line.lower():
            continue
        pattern = line.split()[0]
        if pattern:
            patterns.append(pattern)
    return patterns


def _parse_lfs_pointer_size(text: str) -> Optional[int]:
    if not text or not text.startswith("version https://git-lfs.github.com/spec/v1"):
        return None
    match = re.search(r"^size\s+(\d+)\s*$", text, re.MULTILINE)
    if match:
        return int(match.group(1))
    return None


def _matches_lfs_pattern(path: str, patterns: List[str]) -> bool:
    norm = path.lstrip("/")
    name = norm.rsplit("/", 1)[-1]
    for pat in patterns:
        pat_clean = pat.lstrip("/").replace("[[:space:]]", " ")
        if fnmatch.fnmatch(norm, pat_clean) or fnmatch.fnmatch(name, pat_clean):
            return True
    return False


def collect_org_lfs_usage(
    client: ResilientGitHubClient,
    org: str,
    target_repos: Set[str],
    errors_out: Optional[List[str]] = None,
) -> Dict[str, List[Tuple[str, int]]]:
    """Detect Git LFS tracked pointer objects and byte footprints per repository."""
    if not target_repos:
        return {}

    gql_gitattributes = """
    query($org: String!, $cursor: String) {
      organization(login: $org) {
        repositories(first: 100, after: $cursor) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            name
            object(expression: "HEAD:.gitattributes") {
              ... on Blob {
                text
              }
            }
          }
        }
      }
    }
    """
    lfs_candidates: Dict[str, List[str]] = {}
    try:
        cursor: Optional[str] = None
        seen: Set[str] = set()
        for _ in range(50):
            data = client.graphql(org, gql_gitattributes, {"org": org, "cursor": cursor})
            repos_conn = (data.get("data") or {}).get("organization", {}).get("repositories") or {}
            nodes = repos_conn.get("nodes") or []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                rname = str(node.get("name") or "")
                if not rname or rname.lower() not in target_repos:
                    continue
                blob_text = ((node.get("object") or {}).get("text")) or ""
                patterns = _parse_lfs_patterns(blob_text)
                if patterns:
                    lfs_candidates[rname] = patterns
            page_info = repos_conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor = page_info.get("endCursor")
            if not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
    except Exception as err:
        logger.debug("GraphQL .gitattributes scan unavailable for %s: %s", org, err)
        if errors_out is not None:
            errors_out.append(f"Git LFS .gitattributes scan unavailable: {err}")
        return {}

    lfs_by_repo: Dict[str, List[Tuple[str, int]]] = {}
    for repo_name, patterns in lfs_candidates.items():
        try:
            tree_resp = client.get(org, f"/repos/{org}/{repo_name}/git/trees/HEAD", {"recursive": "1"})
            tree_items = tree_resp.get("tree") if isinstance(tree_resp, dict) else None
            if not isinstance(tree_items, list):
                continue
            pointer_paths: List[str] = []
            for entry in tree_items:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                blob_size = int(entry.get("size") or 0)
                path = str(entry.get("path") or "")
                if 0 < blob_size <= 512 and _matches_lfs_pattern(path, patterns):
                    pointer_paths.append(path)
            if not pointer_paths:
                continue

            repo_objects: List[Tuple[str, int]] = []
            for path in pointer_paths[:100]:
                try:
                    content_resp = client.get(org, f"/repos/{org}/{repo_name}/contents/{quote(path, safe='/')}")
                    encoded = content_resp.get("content") if isinstance(content_resp, dict) else None
                    if not encoded:
                        continue
                    decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
                    lfs_size = _parse_lfs_pointer_size(decoded)
                    if lfs_size is not None and lfs_size > 0:
                        repo_objects.append((path, lfs_size))
                except Exception as err:
                    logger.debug("Failed reading LFS pointer %s/%s/%s: %s", org, repo_name, path, err)
            if repo_objects:
                lfs_by_repo[repo_name.lower()] = repo_objects
        except Exception as err:
            logger.debug("Failed inspecting Git LFS tree for %s/%s: %s", org, repo_name, err)
    return lfs_by_repo


def collect_org_retention_days(client: ResilientGitHubClient, org: str) -> Optional[int]:
    """Fetch organization-level Actions artifact and log retention setting in days."""
    try:
        data = client.get(org, f"/orgs/{org}/actions/permissions/artifact-and-log-retention")
        if isinstance(data, dict) and isinstance(data.get("days"), int):
            return int(data["days"])
    except Exception:
        return None
    return None


def collect_repo_retention_policy(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
) -> Tuple[Optional[int], bool]:
    """Fetch repository Actions artifact retention policy in days and whether permission was denied."""
    try:
        data = client.get(org, f"/repos/{org}/{repo}/actions/permissions/artifact-and-log-retention")
        if isinstance(data, dict) and isinstance(data.get("days"), int):
            return int(data["days"]), False
    except requests.HTTPError as err:
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 403:
            return None, True
    except Exception as err:
        logger.debug("Retention policy lookup failed for %s/%s: %s", org, repo, err)
    return None, False


def collect_org_billing(
    client: ResilientGitHubClient,
    org: str,
    errors_out: Optional[List[str]] = None,
) -> OrgBillingSummary:
    """Collect organization billing summary and per-repo usage from GitHub's Enhanced Billing Platform API."""
    summary = OrgBillingSummary(org=org)

    try:
        sum_data = client.get(org, f"/orgs/{org}/settings/billing/usage/summary")
        if isinstance(sum_data, dict):
            tp = sum_data.get("timePeriod") or {}
            if tp.get("year") and tp.get("month"):
                summary.period = f"{tp['year']}-{int(tp['month']):02d}"
            for it in sum_data.get("usageItems") or []:
                if not isinstance(it, dict):
                    continue
                prod = str(it.get("product") or "")
                sku = str(it.get("sku") or "")
                unit = str(it.get("unitType") or "")
                gross_qty = float(it.get("grossQuantity") or 0.0)
                gross_amt = float(it.get("grossAmount") or 0.0)
                disc_amt = float(it.get("discountAmount") or 0.0)
                net_amt = float(it.get("netAmount") or 0.0)
                summary.gross_amount_usd += gross_amt
                summary.discount_amount_usd += disc_amt
                summary.net_amount_usd += net_amt
                if "minute" in unit.lower():
                    summary.actions_compute_minutes += gross_qty
                elif "actions" in sku.lower() and "storage" in sku.lower():
                    summary.actions_storage_gb_hours += gross_qty
                elif "lfs" in sku.lower() and "storage" in sku.lower():
                    summary.git_lfs_gb_hours += gross_qty
                elif "package" in prod.lower() or "package" in sku.lower():
                    summary.packages_gb_hours += gross_qty
                summary.items.append(
                    BillingUsageRecord(
                        org=org,
                        scope="[Org Monthly Summary]",
                        period=summary.period or "Current Month",
                        product=prod,
                        sku=sku,
                        quantity=gross_qty,
                        unit_type=unit,
                        gross_amount=gross_amt,
                        discount_amount=disc_amt,
                        net_amount=net_amt,
                    )
                )
    except requests.HTTPError as err:
        status = getattr(getattr(err, "response", None), "status_code", None)
        if status == 403 and errors_out is not None:
            errors_out.append("Enhanced Billing usage blocked (403 Forbidden — grant organization 'Plan: read')")
        elif status not in (404, 410) and errors_out is not None:
            errors_out.append(f"Enhanced Billing summary HTTP {status}")
        return summary
    except Exception as err:
        logger.debug("Billing summary failed for %s: %s", org, err)
        if errors_out is not None:
            errors_out.append(f"Enhanced Billing summary error: {err}")
        return summary

    try:
        detail_data = client.get(org, f"/orgs/{org}/settings/billing/usage")
        usage_items = detail_data.get("usageItems") if isinstance(detail_data, dict) else None
        if isinstance(usage_items, list):
            grouped: Dict[Tuple[str, str, str, str], List[float]] = {}
            for it in usage_items:
                if not isinstance(it, dict):
                    continue
                repo_scope = str(it.get("repositoryName") or "[Org-Level]")
                prod = str(it.get("product") or "")
                sku = str(it.get("sku") or "")
                unit = str(it.get("unitType") or "")
                qty = float(it.get("quantity") or 0.0)
                gross_amt = float(it.get("grossAmount") or 0.0)
                disc_amt = float(it.get("discountAmount") or 0.0)
                net_amt = float(it.get("netAmount") or 0.0)
                agg = grouped.setdefault((repo_scope, prod, sku, unit), [0.0, 0.0, 0.0, 0.0])
                agg[0] += qty
                agg[1] += gross_amt
                agg[2] += disc_amt
                agg[3] += net_amt
                if "lfs" in sku.lower() and repo_scope != "[Org-Level]":
                    summary.repo_lfs_gb_hours[repo_scope.lower()] = (
                        summary.repo_lfs_gb_hours.get(repo_scope.lower(), 0.0) + qty
                    )
            for (repo_scope, prod, sku, unit), vals in sorted(grouped.items()):
                summary.items.append(
                    BillingUsageRecord(
                        org=org,
                        scope=repo_scope,
                        period="YTD Usage",
                        product=prod,
                        sku=sku,
                        quantity=vals[0],
                        unit_type=unit,
                        gross_amount=vals[1],
                        discount_amount=vals[2],
                        net_amount=vals[3],
                    )
                )
    except Exception as err:
        logger.debug("Billing usage detail failed for %s: %s", org, err)

    return summary


def profile_single_repo(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    repo_meta: Dict[str, Any],
    *,
    fetch_caches: bool = True,
    fetch_retention_policy: bool = False,
    org_default_retention_days: Optional[int] = None,
) -> RepoStorageProfile:
    """Analyze storage, artifacts, caches, and retention policy for an individual repository."""
    disk_size_kb = int(repo_meta.get("size", 0)) if repo_meta else 0
    visibility = "private" if repo_meta.get("private") else ("internal" if repo_meta.get("visibility") == "internal" else "public")

    profile = RepoStorageProfile(
        org=org,
        repo=repo,
        visibility=visibility,
        disk_size_kb=disk_size_kb,
        repo_retention_days=org_default_retention_days,
    )

    if is_empty_repo(repo_meta):
        return profile

    profile.artifacts = collect_repo_artifacts(client, org, repo, errors_out=profile.errors)
    if fetch_caches:
        profile.caches = collect_repo_caches(client, org, repo, errors_out=profile.errors)
    if fetch_retention_policy:
        repo_days, blocked = collect_repo_retention_policy(client, org, repo)
        if repo_days is not None:
            profile.repo_retention_days = repo_days
        elif blocked:
            profile.errors.append("__RETENTION_403__")
    return profile


def collect_finops_data(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    cfg: FinOpsConfig,
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]],
    failures_out: Optional[List[Tuple[str, str]]] = None,
    billing_out: Optional[Dict[str, OrgBillingSummary]] = None,
) -> Tuple[List[RepoStorageProfile], Dict[str, List[PackageRecord]]]:
    """Collect storage profiles, packages, Git LFS objects, and billing data across all targets."""
    profiles: List[RepoStorageProfile] = []
    org_packages: Dict[str, List[PackageRecord]] = {}
    org_cache_repos: Dict[str, Optional[Set[str]]] = {}
    org_retention_defaults: Dict[str, Optional[int]] = {}
    org_lfs_map: Dict[str, Dict[str, List[Tuple[str, int]]]] = {}
    org_billing_map: Dict[str, OrgBillingSummary] = {}

    targets_by_org: Dict[str, Set[str]] = {}
    for org_name, repo_name in targets:
        targets_by_org.setdefault(org_name.lower(), set()).add(repo_name.lower())

    if orgs:
        def _collect_org_signals(
            org_name: str,
        ) -> Tuple[
            str,
            List[PackageRecord],
            Optional[Set[str]],
            Optional[int],
            Dict[str, List[Tuple[str, int]]],
            OrgBillingSummary,
            List[str],
        ]:
            errs: List[str] = []
            pkgs = collect_org_packages(client, org_name, errors_out=errs)
            cache_repos = collect_org_cache_repos(client, org_name)
            org_ret = collect_org_retention_days(client, org_name)
            lfs_map = collect_org_lfs_usage(
                client, org_name, targets_by_org.get(org_name.lower(), set()), errors_out=errs
            )
            billing = collect_org_billing(client, org_name, errors_out=errs)
            return org_name, pkgs, cache_repos, org_ret, lfs_map, billing, errs

        org_batches = map_concurrent(
            orgs,
            _collect_org_signals,
            max_workers=min(8, len(orgs)),
            max_cap=8,
            thread_name_prefix="finops-org-signals",
        )
        for o, pkgs, cache_repos, org_ret, lfs_map, billing, errs in org_batches:
            org_packages[o] = pkgs
            org_cache_repos[o.lower()] = cache_repos
            org_retention_defaults[o.lower()] = org_ret
            org_lfs_map[o.lower()] = lfs_map
            org_billing_map[o] = billing
            if billing_out is not None:
                billing_out[o] = billing
            if failures_out is not None:
                for err_msg in errs:
                    failures_out.append((o, err_msg))

    pkg_index: Dict[Tuple[str, str], List[PackageRecord]] = {}
    for o, pkgs in org_packages.items():
        o_key = o.lower()
        for pkg in pkgs:
            if pkg.repo_name:
                pkg_index.setdefault((o_key, pkg.repo_name.lower()), []).append(pkg)

    retention_403_orgs: Set[str] = set()
    if targets:
        workers = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32)
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                active_cache_set = org_cache_repos.get(org.lower())
                should_fetch_caches = True if active_cache_set is None else (repo.lower() in active_cache_set)
                fut = executor.submit(
                    profile_single_repo,
                    client,
                    org,
                    repo,
                    meta,
                    fetch_caches=should_fetch_caches,
                    fetch_retention_policy=True,
                    org_default_retention_days=org_retention_defaults.get(org.lower()),
                )
                future_to_target[fut] = (org, repo)

            from ..common.interrupts import iter_completed
            for fut in iter_completed(future_to_target):
                org, repo = future_to_target[fut]
                try:
                    profile = fut.result()
                    if "__RETENTION_403__" in profile.errors:
                        profile.errors = [e for e in profile.errors if e != "__RETENTION_403__"]
                        retention_403_orgs.add(org)
                    profile.packages.extend(pkg_index.get((org.lower(), repo.lower()), ()))
                    lfs_items = org_lfs_map.get(org.lower(), {}).get(repo.lower(), [])
                    if lfs_items:
                        profile.lfs_files = lfs_items
                        profile.lfs_bytes = sum(sz for _, sz in lfs_items)
                    billing_summary = org_billing_map.get(org)
                    if billing_summary:
                        profile.lfs_gb_hours = billing_summary.repo_lfs_gb_hours.get(repo.lower(), 0.0)
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

    if failures_out is not None:
        for org_name in sorted(retention_403_orgs, key=str.lower):
            failures_out.append(
                (org_name, "Repository retention policy blocked (403 Forbidden — grant administration:read)")
            )

    profiles.sort(key=lambda p: (p.org.lower(), p.repo.lower()))
    return profiles, org_packages


def build_billing_rows(org_billing: Optional[Dict[str, OrgBillingSummary]]) -> List[list]:
    """Build structured rows for the Billing & Compute Usage sheet."""
    if not org_billing:
        return []
    rows: List[list] = []
    for org_name in sorted(org_billing, key=str.lower):
        summary = org_billing[org_name]
        for item in summary.items:
            rows.append([
                item.org,
                item.scope,
                item.period,
                item.product,
                item.sku,
                round(item.quantity, 4),
                item.unit_type,
                round(item.gross_amount, 4),
                round(item.discount_amount, 4),
                round(item.net_amount, 4),
            ])
    return rows


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
    ai_disabled = is_ai_disabled(ai_engine)

    for idx, p in enumerate(profiles):
        stale_cache_count = sum(1 for c in p.caches if c.is_stale)
        has_high_lfs = p.lfs_mb >= 50
        is_baseline_optimized = (
            p.optimization_status == "Optimized"
            and p.default_90_day_count == 0
            and stale_cache_count == 0
            and not has_high_lfs
            and p.total_storage_mb <= 2000
        )
        if is_baseline_optimized:
            rec_by_idx[idx] = "-"
        elif ai_disabled:
            rec_by_idx[idx] = AI_NOT_ENABLED_LABEL
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
        rec = rec_by_idx.get(idx, AI_NOT_ENABLED_LABEL if ai_disabled else p.recommendation)

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

        repo_policy_label = f"{p.repo_retention_days} Days" if p.repo_retention_days is not None else "-"
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
                repo_policy_label,
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

        for lfs_path, lfs_size in p.lfs_files:
            detail_parts = [f"{lfs_size:,} bytes"]
            if p.lfs_gb_hours > 0:
                detail_parts.append(f"{round(p.lfs_gb_hours, 2)} GB-hrs YTD")
            cache_package_rows.append([
                p.org,
                p.repo,
                "Git LFS Object",
                lfs_path,
                format_bytes_to_mb(lfs_size),
                "-",
                "-",
                " | ".join(detail_parts),
            ])

    for org, pkgs in org_packages.items():
        for pkg in pkgs:
            created_ist = fmt_timestamp(pkg.created_at)
            updated_ist = fmt_timestamp(pkg.updated_at)
            repo_target = pkg.repo_name or "[Org-Wide]"
            ver_label = "1 version" if pkg.version_count == 1 else f"{pkg.version_count} versions"
            cache_package_rows.append([
                org,
                repo_target,
                f"Package ({pkg.package_type})",
                pkg.name,
                pkg.size_mb,
                created_ist,
                updated_ist,
                ver_label,
            ])

    for org in orgs:
        p_list = org_profiles.get(org, [])
        pkgs = org_packages.get(org, [])
        total_disk_bytes = sum(p.disk_size_kb * 1024 for p in p_list)
        total_disk_mb = format_bytes_to_mb(total_disk_bytes)
        total_art_count = sum(len(p.active_artifacts) for p in p_list)
        total_art_bytes = sum(p.active_artifact_bytes for p in p_list)
        total_art_mb = format_bytes_to_mb(total_art_bytes)
        total_90d_count = sum(p.default_90_day_count for p in p_list)
        total_cache_count = sum(len(p.caches) for p in p_list)
        total_cache_bytes = sum(p.cache_bytes for p in p_list)
        total_cache_mb = format_bytes_to_mb(total_cache_bytes)
        total_pkg_bytes = sum(pkg.size_in_bytes for pkg in pkgs)
        total_lfs_bytes = sum(p.lfs_bytes for p in p_list)
        total_footprint_bytes = (
            total_disk_bytes + total_art_bytes + total_cache_bytes + total_pkg_bytes + total_lfs_bytes
        )
        total_footprint_mb = format_bytes_to_mb(total_footprint_bytes)

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
