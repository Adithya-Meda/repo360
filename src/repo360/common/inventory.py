"""Dynamic repository and organization discovery engine for Repo360."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .concurrency import map_concurrent

logger = logging.getLogger("repo360.inventory")


def discover_app_organizations(
    auth: Any,
    org_filter: Optional[List[str]] = None,
) -> List[str]:
    """Discover all organizations where the GitHub App is actively installed.

    Args:
        auth: GitHubAppAuth instance with credentials.
        org_filter: Optional list of organization names to restrict discovery to.

    Returns:
        Sorted list of discovered organization logins.
    """
    installations = auth.discover_installations()
    if not installations:
        return []

    org_set = {o.strip().lower() for o in org_filter if o.strip()} if org_filter else None
    discovered: List[str] = []

    for inst in installations:
        org_login = inst.get("account", {}).get("login", "")
        if not org_login:
            continue
        if org_set and org_login.lower() not in org_set:
            continue
        discovered.append(org_login)

    return sorted(discovered, key=str.lower)


def discover_app_repositories(
    auth: Any,
    client: Any,
    org_filter: Optional[List[str]] = None,
    include_archived: bool = False,
    include_forks: bool = True,
) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]]]:
    """Auto-discover all repositories directly accessible to the GitHub App across installed organizations.

    Queries GitHub App installations and retrieves accessible repositories directly via the GitHub API.

    Args:
        auth: GitHubAppAuth instance with credentials.
        client: ResilientGitHubClient instance.
        org_filter: Optional list of organization names to restrict discovery to.
        include_archived: Whether to include archived repositories.
        include_forks: Whether to include forked repositories.

    Returns:
        Tuple of (targets_list, metadata_cache_dict).
    """
    targets: List[Tuple[str, str]] = []
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    installations = auth.discover_installations()
    if not installations:
        return targets, metadata_cache

    org_set = {o.strip().lower() for o in org_filter if o.strip()} if org_filter else None
    matching_orgs: List[str] = []
    for inst in installations:
        org_login = inst.get("account", {}).get("login", "")
        if not org_login:
            continue
        if org_set and org_login.lower() not in org_set:
            continue
        matching_orgs.append(org_login)

    if not matching_orgs:
        return targets, metadata_cache

    def _fetch_org_repos(org_login: str) -> Tuple[str, List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]], int, int]:
        org_targets: List[Tuple[str, str]] = []
        org_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
        active_count = 0
        archived_count = 0

        try:
            repo_iter = list(client.paginate_enveloped(org_login, "/installation/repositories", "repositories"))
        except Exception:
            try:
                repo_iter = list(client.paginate(org_login, f"/orgs/{org_login}/repos", {"type": "all"}))
            except Exception:
                repo_iter = []

        for repo_data in repo_iter:
            repo_name = repo_data.get("name")
            if not repo_name:
                continue
            is_archived = bool(repo_data.get("archived"))
            is_fork = bool(repo_data.get("fork"))

            if is_archived:
                archived_count += 1
            if is_archived and not include_archived:
                continue
            if is_fork and not include_forks:
                continue

            active_count += 1
            org_targets.append((org_login, repo_name))
            org_meta[(org_login.casefold(), repo_name.casefold())] = repo_data

        return org_login, org_targets, org_meta, active_count, archived_count

    org_results = map_concurrent(
        matching_orgs,
        _fetch_org_repos,
        max_workers=min(8, len(matching_orgs)),
        max_cap=8,
        thread_name_prefix="inv-discovery",
    )

    for org_login, org_targets, org_meta, active_count, archived_count in org_results:
        targets.extend(org_targets)
        metadata_cache.update(org_meta)
        if active_count > 0:
            logger.info("Organization '%s': discovered %d active repositories.", org_login, active_count)
        elif archived_count > 0:
            logger.info(
                "Organization '%s': %d repositories found (all archived - pass --include-archived to analyze).",
                org_login,
                archived_count,
            )
        else:
            logger.info("Organization '%s': 0 repositories discovered.", org_login)

    return targets, metadata_cache


def resolve_scan_targets(
    auth: Any,
    client: Any,
    repos_file: Optional[str] = None,
    org_filter: Optional[List[str]] = None,
    include_archived: bool = False,
    include_forks: bool = True,
) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]]]:
    """Resolve scan targets dynamically via direct GitHub App auto-discovery."""
    return discover_app_repositories(
        auth=auth,
        client=client,
        org_filter=org_filter,
        include_archived=include_archived,
        include_forks=include_forks,
    )
