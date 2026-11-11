"""Flexible repository inventory parser for Repo360 activity and runner scrapers."""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .concurrency import map_concurrent

logger = logging.getLogger("repo360.inventory")

ORG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
TRUE_VALUES = frozenset({"true", "yes", "y", "1"})
FALSE_VALUES = frozenset({"false", "no", "n", "0"})


@dataclass(frozen=True)
class RepoEntry:
    """Repository entry targeting an organization and repo."""

    org: str
    repo: str
    enabled: bool = True


def load_flexible_repo_entries(path: str | os.PathLike) -> List[RepoEntry]:
    """Parse and validate repository inventory from CSV with flexible column headers.

    Supported column aliases:
      - Organization: 'org', 'org_name', 'organization'
      - Repository: 'repo', 'repo_name', 'repository'
      - Enabled flag: 'enabled', 'active' (optional, defaults to True)

    Supports wildcard '*' for repo to target all repositories in the organization.
    """
    file_path = os.path.abspath(path)
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Repository inventory CSV not found: {file_path}")

    entries: List[RepoEntry] = []
    seen: Dict[Tuple[str, str], bool] = {}
    errors: List[str] = []

    with open(file_path, "r", newline="", encoding="utf-8-sig") as handle:
        lines = [line for line in handle if line.strip() and not line.strip().startswith("#")]
        if not lines:
            raise ValueError(f"{os.path.basename(file_path)} contains no repository rows.")

        reader = csv.DictReader(lines)
        if not reader.fieldnames:
            raise ValueError(f"{os.path.basename(file_path)} is empty or missing headers.")

        field_map: Dict[str, str] = {}
        for fn in reader.fieldnames:
            if not fn:
                continue
            cleaned = fn.strip().lower()
            if cleaned in ("org", "org_name", "organization"):
                field_map["org"] = fn
            elif cleaned in ("repo", "repo_name", "repository"):
                field_map["repo"] = fn
            elif cleaned in ("enabled", "active"):
                field_map["enabled"] = fn

        if "org" not in field_map or "repo" not in field_map:
            raise ValueError(
                f"{os.path.basename(file_path)} must include organization and repository columns "
                "(e.g. 'org,repo' or 'org_name,repo_name')."
            )

        org_col = field_map["org"]
        repo_col = field_map["repo"]
        enabled_col = field_map.get("enabled")

        for line_no, raw in enumerate(reader, start=2):
            if not any(v and v.strip() for v in raw.values() if isinstance(v, str)):
                continue

            org = (raw.get(org_col) or "").strip()
            if not org or org.startswith("#"):
                continue

            repo = (raw.get(repo_col) or "").strip()
            if repo.startswith("#"):
                continue

            if enabled_col:
                flag = (raw.get(enabled_col) or "").strip().lower()
            else:
                flag = "true"

            problems: List[str] = []
            if not ORG_RE.fullmatch(org):
                problems.append(f"invalid organization '{org}' (alphanumeric/hyphens only)")
            if repo != "*" and (not REPO_RE.fullmatch(repo) or repo in {".", ".."}):
                problems.append(f"invalid repository '{repo}' (use '*' for all repositories)")
            if flag not in TRUE_VALUES and flag not in FALSE_VALUES:
                problems.append(f"invalid enabled value '{flag}' (must be true/false/yes/no/1/0)")

            if problems:
                errors.append(f"line {line_no}: {'; '.join(problems)}")
                continue

            is_enabled = flag in TRUE_VALUES
            key = (org.lower(), repo.lower())
            if key in seen:
                if seen[key] != is_enabled:
                    errors.append(f"line {line_no}: conflicting enabled values for {org}/{repo}")
                continue

            seen[key] = is_enabled
            entries.append(RepoEntry(org=org, repo=repo, enabled=is_enabled))

    if errors:
        raise ValueError(f"{os.path.basename(file_path)} validation failed:\n  " + "\n  ".join(errors))

    if not any(entry.enabled for entry in entries):
        raise ValueError(f"{os.path.basename(file_path)} contains no enabled repositories to scan.")

    return entries


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

    Eliminates the requirement for a repos.csv file by querying GitHub App installations and
    retrieving accessible repositories directly via the GitHub API.

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
