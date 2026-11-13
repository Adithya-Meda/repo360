"""User activity and contributor cadence collection engine."""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

from ..common.client import ResilientGitHubClient
from ..common.concurrency import map_concurrent, resolve_worker_count
from ..common.inventory import RepoEntry
from .models import (
    DATE_FMT,
    IST,
    LOCAL_TZ,
    LOCAL_TZ_NAME,
    METRIC_KEYS,
    METRIC_LABELS,
    METRICS,
    MGMT_KPI_HEADERS,
    REPO_INVENTORY_HEADERS,
    STATUS_IDX,
    STATUS_ORDER,
    STATUSES,
    TOP_REPOS_HEADERS,
    TOTAL_IDX,
    Activity,
    ActivityConfig,
    RepositoryReport,
    fmt_timestamp,
    is_bot,
    parse_ts,
)

logger = logging.getLogger("repo360.activity")


def resolve_activity_targets(
    client: ResilientGitHubClient,
    entries: List[RepoEntry],
    cfg: ActivityConfig,
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]]]:
    """Resolve and expand target repositories (including '*' wildcard) with metadata cache."""
    enabled_entries = [e for e in entries if e.enabled]
    org_wildcards = {e.org.lower(): e.org for e in enabled_entries if e.repo == "*"}
    specific_repos = [(e.org, e.repo) for e in enabled_entries if e.repo != "*"]

    targets: List[Tuple[str, str]] = []
    failures: List[Tuple[str, str]] = []
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _expand_org(original_org: str) -> Tuple[List[Tuple[str, str]], Dict[Tuple[str, str], Dict[str, Any]], Optional[Tuple[str, str]]]:
        org_targets: List[Tuple[str, str]] = []
        org_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            for repo_data in client.paginate(original_org, f"/orgs/{original_org}/repos", {"type": "all"}):
                repo_name = repo_data.get("name")
                if not repo_name:
                    continue
                if repo_data.get("archived") and not cfg.include_archived:
                    continue
                if repo_data.get("fork") and not cfg.include_forks:
                    continue
                org_targets.append((original_org, repo_name))
                org_meta[(original_org.casefold(), repo_name.casefold())] = repo_data
            return org_targets, org_meta, None
        except Exception as exc:
            logger.error("Failed expanding wildcard for org %s: %s", original_org, exc)
            return org_targets, org_meta, (f"{original_org}/*", str(exc))

    wildcard_batches = map_concurrent(
        list(org_wildcards.values()),
        _expand_org,
        max_workers=min(8, len(org_wildcards)) if org_wildcards else 1,
        max_cap=8,
        thread_name_prefix="act-wildcards",
    )
    for org_targets, org_meta, fail_item in wildcard_batches:
        targets.extend(org_targets)
        metadata_cache.update(org_meta)
        if fail_item:
            failures.append(fail_item)

    def _fetch_specific(pair: Tuple[str, str]) -> Tuple[Optional[Tuple[str, str]], Optional[Dict[str, Any]], Optional[Tuple[str, str]]]:
        org, repo = pair
        try:
            repo_data = client.get(org, f"/repos/{org}/{repo}")
            if repo_data.get("archived") and not cfg.include_archived:
                return None, None, None
            if repo_data.get("fork") and not cfg.include_forks:
                return None, None, None
            return (org, repo), repo_data, None
        except Exception as exc:
            logger.warning("Repository %s/%s inaccessible: %s", org, repo, exc)
            return None, None, (f"{org}/{repo}", str(exc))

    specific_results = map_concurrent(
        specific_repos,
        _fetch_specific,
        max_workers=resolve_worker_count(cfg.max_workers, len(specific_repos), max_cap=24) if specific_repos else 1,
        max_cap=24,
        thread_name_prefix="act-specific",
    )
    for target_pair, repo_data, fail_item in specific_results:
        if target_pair and repo_data is not None:
            org, repo = target_pair
            targets.append((org, repo))
            metadata_cache[(org.casefold(), repo.casefold())] = repo_data
        if fail_item:
            failures.append(fail_item)

    targets.sort(key=lambda t: (t[0].lower(), t[1].lower()))
    return targets, failures, metadata_cache


def collect_pull_requests_graphql(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    collected_since: datetime,
    since: datetime,
    end: datetime,
    exclude_bots: bool,
    report: Optional[RepositoryReport],
    record_fn: Any,
) -> bool:
    """Collect PRs, merges, and reviews via batched GraphQL query for fast retrieval."""
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(first: 50, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            number
            createdAt
            mergedAt
            updatedAt
            author {
              login
            }
            reviews(first: 50) {
              pageInfo {
                hasNextPage
                endCursor
              }
              nodes {
                author {
                  login
                }
                submittedAt
                state
              }
            }
          }
        }
      }
    }
    """
    reviews_page_query = """
    query($prId: ID!, $revCursor: String) {
      node(id: $prId) {
        ... on PullRequest {
          reviews(first: 100, after: $revCursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              author {
                login
              }
              submittedAt
              state
            }
          }
        }
      }
    }
    """
    cursor = None
    while True:
        data = client.graphql(org, query, {"owner": org, "name": repo, "cursor": cursor})
        repo_data = (data.get("data") or {}).get("repository")
        if not repo_data:
            return False

        prs = (repo_data.get("pullRequests") or {}).get("nodes") or []
        for pr in prs:
            updated_at = parse_ts(pr["updatedAt"])
            if updated_at < collected_since:
                return True

            author_dict = {"login": pr["author"]["login"]} if pr.get("author") and pr["author"].get("login") else None
            record_fn(author_dict, "prs_opened", pr.get("createdAt"))
            record_fn(author_dict, "prs_merged", pr.get("mergedAt"))
            created = parse_ts(pr["createdAt"])
            eligible = not (exclude_bots and author_dict and is_bot(author_dict))
            if report is not None and eligible and since <= created <= end:
                report.cohort_opened += 1
                if pr.get("mergedAt") and parse_ts(pr["mergedAt"]) <= end:
                    report.cohort_merged += 1

            first_review: Optional[datetime] = None
            reviews_conn = pr.get("reviews") or {}
            reviews_nodes = list(reviews_conn.get("nodes") or [])
            rev_page_info = reviews_conn.get("pageInfo") or {}
            pr_node_id = pr.get("id")
            while rev_page_info.get("hasNextPage") and rev_page_info.get("endCursor") and pr_node_id:
                extra = client.graphql(
                    org,
                    reviews_page_query,
                    {"prId": pr_node_id, "revCursor": rev_page_info["endCursor"]},
                )
                extra_conn = ((extra.get("data") or {}).get("node") or {}).get("reviews") or {}
                reviews_nodes.extend(extra_conn.get("nodes") or [])
                rev_page_info = extra_conn.get("pageInfo") or {}

            for review in reviews_nodes:
                if review.get("state") == "PENDING" or not review.get("submittedAt"):
                    continue
                rev_dict = {"login": review["author"]["login"]} if review.get("author") and review["author"].get("login") else None
                record_fn(rev_dict, "reviews", review["submittedAt"])
                submitted = parse_ts(review["submittedAt"])
                if (
                    rev_dict and rev_dict.get("login") and author_dict
                    and rev_dict["login"].lower() != (author_dict.get("login") or "").lower()
                    and not (exclude_bots and is_bot(rev_dict))
                    and created <= submitted <= end
                ):
                    first_review = min(first_review, submitted) if first_review else submitted

            if report is not None and first_review and since <= first_review <= end:
                report.review_hours.append((first_review - created).total_seconds() / 3600)

        page_info = (repo_data.get("pullRequests") or {}).get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return True


def _format_error(prefix: str, exc: Exception) -> str:
    """Format API exceptions into human-readable, actionable diagnostic strings."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 403:
        perm_map = {
            "Pulls": "pull_requests: read",
            "PR Comments": "pull_requests: read",
            "Issues": "issues: read",
            "Issue Comments": "issues: read",
            "Commits": "contents: read",
        }
        needed = perm_map.get(prefix)
        if needed:
            return f"{prefix}: 403 Forbidden (requires '{needed}' permission on GitHub App)"
        return f"{prefix}: 403 Forbidden (insufficient GitHub App permissions)"
    if status_code == 404:
        return f"{prefix}: 404 Not Found (resource disabled or unavailable)"
    return f"{prefix}: {exc}"


def collect_repo_events(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    exclude_bots: bool,
    end: Optional[datetime] = None,
    report: Optional[RepositoryReport] = None,
) -> Dict[str, Activity]:
    """Collect commit, PR, review, and issue activities for a single repository."""
    activity: Dict[str, Activity] = defaultdict(Activity)
    base = f"/repos/{org}/{repo}"
    end = end or datetime.now(timezone.utc)
    month_start = end.astimezone(LOCAL_TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    collected_since = min(since, end - timedelta(days=30), month_start) if report else since
    since_param = {"since": collected_since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def record(user: Optional[Dict[str, Any]], metric: str, timestamp: Optional[str]) -> None:
        if not timestamp or (user and exclude_bots and is_bot(user)):
            return
        when = parse_ts(timestamp)
        login = user.get("login") if user else None
        if collected_since <= when <= end and report is not None:
            report.events.append((login.lower() if login else None, metric, when))
        if since <= when <= end and login:
            activity[login].add(metric, when)

    pushed_at_str = report.metadata.get("pushed_at") if report and report.metadata else None
    pushed_at = parse_ts(pushed_at_str) if pushed_at_str else None
    if not (pushed_at and pushed_at < collected_since):
        try:
            for commit in client.paginate(
                org,
                f"{base}/commits",
                {**since_param, "until": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
            ):
                commit_meta = commit.get("commit", {})
                commit_ts = (commit_meta.get("author") or {}).get("date") or (commit_meta.get("committer") or {}).get("date")
                author_user = commit.get("author") or commit.get("committer")
                record(author_user, "commits", commit_ts)
        except Exception as exc:
            if report:
                report.errors.append(_format_error("Commits", exc))

    has_prs = report is None or report.metadata.get("has_pull_requests", True)
    prs_accessible = True
    if has_prs:
        pr_success = False
        try:
            pr_success = collect_pull_requests_graphql(
                client, org, repo, collected_since, since, end, exclude_bots, report, record
            )
        except Exception as exc:
            logger.debug("GraphQL PR collection failed for %s/%s (%s); falling back to REST", org, repo, exc)
            pr_success = False

        if not pr_success:
            try:
                pulls = client.paginate(org, f"{base}/pulls", {"state": "all", "sort": "updated", "direction": "desc"})
                for pr in pulls:
                    if parse_ts(pr["updated_at"]) < collected_since:
                        break
                    record(pr.get("user"), "prs_opened", pr.get("created_at"))
                    record(pr.get("user"), "prs_merged", pr.get("merged_at"))
                    created = parse_ts(pr["created_at"])
                    eligible = not (exclude_bots and is_bot(pr.get("user") or {}))
                    if report is not None and eligible and since <= created <= end:
                        report.cohort_opened += 1
                        if pr.get("merged_at") and parse_ts(pr["merged_at"]) <= end:
                            report.cohort_merged += 1

                    first_review: Optional[datetime] = None
                    for review in client.paginate(org, f"{base}/pulls/{pr['number']}/reviews"):
                        reviewer = review.get("user") or {}
                        if review.get("state") == "PENDING" or not review.get("submitted_at"):
                            continue
                        record(reviewer, "reviews", review["submitted_at"])
                        submitted = parse_ts(review["submitted_at"])
                        if (
                            reviewer.get("login")
                            and reviewer["login"].lower() != (pr.get("user") or {}).get("login", "").lower()
                            and not (exclude_bots and is_bot(reviewer))
                            and created <= submitted <= end
                        ):
                            first_review = min(first_review, submitted) if first_review else submitted

                    if report is not None and first_review and since <= first_review <= end:
                        report.review_hours.append((first_review - created).total_seconds() / 3600)
            except Exception as exc:
                prs_accessible = False
                if report:
                    report.errors.append(_format_error("Pulls", exc))

    has_issues = report.metadata.get("has_issues", True) if report and report.metadata else True
    issues_accessible = True
    if has_issues:
        try:
            for issue in client.paginate(org, f"{base}/issues", {"state": "all", **since_param}):
                if "pull_request" not in issue:
                    record(issue.get("user"), "issues_opened", issue.get("created_at"))
        except Exception as exc:
            issues_accessible = False
            if report:
                report.errors.append(_format_error("Issues", exc))

        if issues_accessible:
            try:
                for comment in client.paginate(org, f"{base}/issues/comments", since_param):
                    record(comment.get("user"), "issue_comments", comment.get("created_at"))
            except Exception as exc:
                if report:
                    report.errors.append(_format_error("Issue Comments", exc))

    if has_prs and prs_accessible:
        try:
            for comment in client.paginate(org, f"{base}/pulls/comments", since_param):
                record(comment.get("user"), "review_comments", comment.get("created_at"))
        except Exception as exc:
            if report:
                report.errors.append(_format_error("PR Comments", exc))

    return dict(activity)


def collect_repository(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    exclude_bots: bool,
    cached_metadata: Optional[Dict[str, Any]] = None,
) -> RepositoryReport:
    """Perform complete activity inspection and governance audit for a repository."""
    report = RepositoryReport()
    base = f"/repos/{org}/{repo}"

    if cached_metadata is not None:
        report.metadata = cached_metadata
    else:
        try:
            report.metadata = client.get(org, base)
        except Exception as exc:
            report.errors.append(f"Metadata: {exc}")
            return report

    size = report.metadata.get("size", 1)
    pushed_at = parse_ts(report.metadata["pushed_at"]) if report.metadata.get("pushed_at") else None
    updated_at = parse_ts(report.metadata["updated_at"]) if report.metadata.get("updated_at") else None
    month_start = end.astimezone(LOCAL_TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    collected_since = min(since, end - timedelta(days=30), month_start)

    is_dormant = bool(size == 0 or (pushed_at and pushed_at < collected_since and updated_at and updated_at < collected_since))
    if is_dormant:
        report.users = {}
        report.activity_complete = True
    else:
        pre_errors = len(report.errors)
        try:
            report.users = collect_repo_events(client, org, repo, since, exclude_bots, end, report)
            report.activity_complete = len(report.errors) == pre_errors
        except Exception as exc:
            report.errors.append(f"Activity: {exc}")

    branch = report.metadata.get("default_branch")
    if size == 0 or report.metadata.get("archived"):
        report.protected = "No"
        report.protected_branches = ()
    else:
        try:
            protected = [
                b["name"]
                for b in client.paginate(org, f"{base}/branches", {"protected": "true"})
                if isinstance(b, dict) and "name" in b
            ]
            if branch and branch not in protected:
                try:
                    branch_rules = client.get(org, f"{base}/rules/branches/{quote(branch, safe='')}")
                    if isinstance(branch_rules, list) and branch_rules:
                        protected.append(f"{branch} (Ruleset)")
                except Exception:
                    pass
            report.protected_branches = tuple(sorted(protected, key=str.casefold))
            report.protected = "Yes" if report.protected_branches else "No"
        except Exception as exc:
            report.errors.append(f"Branch protection: {exc}")

    if not branch or size == 0:
        report.codeowners = "No"
    else:
        try:
            tree_data = client.get(org, f"{base}/git/trees/{quote(branch, safe='')}", {"recursive": "1"})
            tree_paths = {item["path"] for item in tree_data.get("tree", []) if item.get("type") == "blob"}
            for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
                if path in tree_paths:
                    report.codeowners, report.codeowners_path = "Yes", path
                    break
            else:
                report.codeowners = "No"
        except Exception:
            for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
                try:
                    content = client.get(org, f"{base}/contents/{quote(path, safe='/')}", {"ref": branch})
                    if isinstance(content, dict) and content.get("type") == "file":
                        report.codeowners, report.codeowners_path = "Yes", path
                        break
                except Exception:
                    continue
            else:
                report.codeowners = "No"

    return report


def fetch_members(
    client: ResilientGitHubClient, orgs: List[str], exclude_bots: bool, failures: List[Tuple[str, str]]
) -> Dict[str, Set[str]]:
    """Fetch current organization members concurrently across organizations."""
    members: Dict[str, Set[str]] = {}

    def _fetch_org_members(org: str) -> Tuple[str, Optional[Set[str]], Optional[Tuple[str, str]]]:
        try:
            org_members = {
                m["login"] for m in client.paginate(org, f"/orgs/{org}/members")
                if isinstance(m, dict) and "login" in m and not (exclude_bots and is_bot(m))
            }
            return org, org_members, None
        except Exception as exc:
            logger.warning("Failed retrieving members for %s: %s", org, exc)
            return org, None, (f"{org} (members)", str(exc))

    results = map_concurrent(
        orgs,
        _fetch_org_members,
        max_workers=min(8, len(orgs)) if orgs else 1,
        max_cap=8,
        thread_name_prefix="act-members",
    )
    for org, org_members, fail_item in results:
        if org_members is not None:
            members[org] = org_members
        if fail_item:
            failures.append(fail_item)
    return members


def fetch_team_access(
    client: ResilientGitHubClient,
    orgs: List[str],
    exclude_bots: bool,
) -> Tuple[Dict[Tuple[str, str], Set[str]], Dict[str, bool]]:
    """Map user team memberships across target organizations concurrently."""
    teams_by_user: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    org_team_complete: Dict[str, bool] = {}

    def _fetch_org_team_slugs(org: str) -> Tuple[str, List[str], bool]:
        try:
            slugs = [
                str(team["slug"])
                for team in client.paginate(org, f"/orgs/{org}/teams")
                if isinstance(team, dict) and team.get("slug")
            ]
            return org, slugs, True
        except Exception as exc:
            logger.debug("Teams lookup unavailable for %s: %s", org, exc)
            return org, [], False

    org_team_batches = map_concurrent(
        orgs,
        _fetch_org_team_slugs,
        max_workers=min(8, len(orgs)) if orgs else 1,
        max_cap=8,
        thread_name_prefix="act-teams",
    )

    team_tasks: List[Tuple[str, str]] = []
    for org, slugs, ok in org_team_batches:
        org_team_complete[org] = ok
        for slug in slugs:
            team_tasks.append((org, slug))

    def _fetch_single_team_members(pair: Tuple[str, str]) -> Tuple[str, str, Set[str], bool]:
        org, slug = pair
        try:
            t_members = {
                m["login"] for m in client.paginate(org, f"/orgs/{org}/teams/{slug}/members")
                if isinstance(m, dict) and "login" in m and not (exclude_bots and is_bot(m))
            }
            return org, slug, t_members, True
        except Exception as exc:
            logger.debug("Team %s/%s member lookup failed: %s", org, slug, exc)
            return org, slug, set(), False

    if team_tasks:
        member_results = map_concurrent(
            team_tasks,
            _fetch_single_team_members,
            max_workers=0,
            max_cap=24,
            thread_name_prefix="act-team-members",
        )
        for org, slug, t_members, ok in member_results:
            if not ok:
                org_team_complete[org] = False
            for login in t_members:
                teams_by_user[(org, login)].add(slug)

    return teams_by_user, org_team_complete


def classify(active_days: int, cfg: ActivityConfig) -> str:
    """Categorize user into Active, Moderate, Low, or Inactive based on distinct days."""
    if active_days >= cfg.active_min_days:
        return "Active"
    if active_days >= cfg.moderate_min_days:
        return "Moderate"
    return "Low" if active_days else "Inactive"


def build_rows(
    results: Dict[Tuple[str, str], Dict[str, Activity]],
    members: Dict[str, Set[str]],
    cfg: ActivityConfig,
    end: datetime,
    teams_by_user: Dict[Tuple[str, str], Set[str]],
    org_team_complete: Dict[str, bool],
) -> Tuple[List[list], List[list]]:
    """Compile formatted tabular user rows and repo activity rows."""
    per_user: Dict[Tuple[str, str], Activity] = defaultdict(Activity)
    repos_by_user: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    teams_norm: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    canonical_org: Dict[str, str] = {}
    canonical_login: Dict[Tuple[str, str], str] = {}
    norm_members: Dict[str, Dict[str, str]] = {}
    team_complete_norm = {k.casefold(): v for k, v in org_team_complete.items()}
    repo_rows: List[list] = []

    for m_org, m_logins in members.items():
        org_k = m_org.casefold()
        canonical_org.setdefault(org_k, m_org)
        m_map = norm_members.setdefault(org_k, {})
        for login in m_logins:
            lk = login.casefold()
            m_map.setdefault(lk, login)
            canonical_login[(org_k, lk)] = login

    for (org, login), teams in teams_by_user.items():
        teams_norm[(org.casefold(), login.casefold())].update(teams)

    for (org, repo), users in sorted(results.items(), key=lambda x: (x[0][0].lower(), x[0][1].lower())):
        org_k = org.casefold()
        disp_org = canonical_org.setdefault(org_k, org)
        repo_user_agg: Dict[str, Tuple[str, Activity]] = {}
        for login, act in users.items():
            lk = login.casefold()
            disp_login = canonical_login.setdefault((org_k, lk), login)
            if lk not in repo_user_agg:
                merged_act = Activity()
                merged_act.merge(act)
                repo_user_agg[lk] = (disp_login, merged_act)
            else:
                repo_user_agg[lk][1].merge(act)

        for lk, (disp_login, act) in sorted(repo_user_agg.items(), key=lambda x: (-x[1][1].total(), x[0])):
            per_user[(org_k, lk)].merge(act)
            repos_by_user[(org_k, lk)].add(repo.casefold())
            repo_rows.append([
                disp_org, repo, disp_login, act.total(),
                act.commits, act.prs_opened, act.prs_merged, act.reviews,
                act.review_comments, act.issues_opened, act.issue_comments,
                len(act.active_days), fmt_timestamp(act.last_active),
            ])

    if cfg.include_inactive_members:
        for org_k, m_map in norm_members.items():
            for lk in m_map:
                per_user.setdefault((org_k, lk), Activity())

    user_rows: List[list] = []
    for (org_k, lk), act in per_user.items():
        disp_org = canonical_org.get(org_k, org_k)
        disp_login = canonical_login.get((org_k, lk), lk)
        member_flag = ("Yes" if lk in norm_members[org_k] else "No") if org_k in norm_members else "Unknown"
        teams = sorted(teams_norm.get((org_k, lk), ()), key=str.casefold)
        team_str = "; ".join(teams)
        if not team_complete_norm.get(org_k, False):
            team_str = f"{team_str}; Unknown" if team_str else "Unknown"
        elif not team_str:
            team_str = "None"

        active_days_count = len(act.active_days)
        active_months = len(act.commit_months)
        avg_commits = round(act.commits / active_months, 2) if active_months else None
        days_since_last = max(0, int((end - act.last_active).total_seconds() // 86400)) if act.last_active else None

        user_rows.append([
            disp_org,
            disp_login,
            classify(active_days_count, cfg),
            active_days_count,
            act.total(),
            act.commits,
            act.prs_opened,
            act.prs_merged,
            act.reviews,
            act.review_comments,
            act.issues_opened,
            act.issue_comments,
            avg_commits,
            len(repos_by_user.get((org_k, lk), ())),
            fmt_timestamp(act.first_active),
            fmt_timestamp(act.last_active),
            days_since_last,
            member_flag,
            team_str,
        ])

    user_rows.sort(key=lambda r: (r[0].lower(), STATUS_ORDER.get(r[STATUS_IDX], 99), -r[TOTAL_IDX], r[1].lower()))
    return user_rows, repo_rows


def build_org_rows(
    user_rows: List[list],
    results: Dict[Tuple[str, str], Dict[str, Activity]],
    all_orgs: Optional[List[str]] = None,
) -> List[list]:
    """Aggregate per-organization contributor metrics and status distribution."""
    canonical_org: Dict[str, str] = {}
    for o in (all_orgs or []):
        canonical_org.setdefault(o.casefold(), o)
    for org, _ in results:
        canonical_org.setdefault(org.casefold(), org)

    repo_sets: Dict[str, Set[str]] = defaultdict(set)
    for org, repo in results:
        repo_sets[org.casefold()].add(repo.casefold())

    stats: Dict[str, Counter] = defaultdict(Counter)
    for row in user_rows:
        org = row[0]
        ok = org.casefold()
        canonical_org.setdefault(ok, org)
        stats[ok][row[STATUS_IDX]] += 1
        stats[ok]["total"] += row[TOTAL_IDX]
        if row[TOTAL_IDX] > 0:
            stats[ok]["contributors"] += 1

    org_keys = sorted(canonical_org, key=lambda k: canonical_org[k].lower())
    return [
        [
            canonical_org[ok],
            len(repo_sets[ok]),
            stats[ok]["contributors"],
            stats[ok]["Active"],
            stats[ok]["Moderate"],
            stats[ok]["Low"],
            stats[ok]["Inactive"],
            stats[ok]["total"],
        ]
        for ok in org_keys
    ]


def build_management_tables(
    reports: Dict[Tuple[str, str], RepositoryReport],
    members: Dict[str, Set[str]],
    since: datetime,
    end: datetime,
    organization_status: Optional[Dict[str, bool]] = None,
) -> Dict[str, Tuple[Tuple[str, ...], List[list]]]:
    """Construct Repository Inventory, Top Active Repositories, and Management KPIs tables."""
    tables: Dict[str, Tuple[Tuple[str, ...], List[list]]] = {}
    inventory: List[list] = []
    created_dates: Dict[Tuple[str, str], Optional[datetime]] = {}

    for key, result in reports.items():
        raw_created = result.metadata.get("created_at") if result.metadata else None
        created_dates[key] = parse_ts(raw_created) if raw_created else None

    for (org, repo), result in reports.items():
        meta = result.metadata or {}
        visibility = meta.get("visibility")
        if not visibility:
            visibility = "private" if meta.get("private") else ("public" if meta.get("private") is False else "Unknown")
        visibility = str(visibility).lower()

        events = [event for event in result.events if since <= event[2] <= end]
        counts = Counter(metric for _, metric, _ in events)
        contributors = {login for login, _, _ in events if login}

        created_dt = created_dates.get((org, repo))
        created_str = fmt_timestamp(created_dt) if created_dt else "Unknown"
        new_in_period = "Yes" if created_dt and since <= created_dt <= end else "No"
        archived = "Yes" if meta.get("archived") else "No"
        default_branch = meta.get("default_branch", "")

        if result.protected == "Yes":
            protected_branches_str = "; ".join(result.protected_branches) if result.protected_branches else "Yes"
        elif result.protected == "No":
            protected_branches_str = "None"
        else:
            protected_branches_str = "Unknown"

        activity_available = result.activity_complete
        active_last_7 = (
            ("Yes" if any(end - timedelta(days=7) <= when <= end for _, _, when in result.events) else "No")
            if activity_available
            else "Unknown"
        )
        total_activities = len(events) if activity_available else "N/A"
        active_contributors = len(contributors) if activity_available else "N/A"
        metric_values = [counts[metric] if activity_available else "N/A" for metric in METRIC_KEYS]
        merge_rate = round(result.cohort_merged / result.cohort_opened * 100, 2) if result.cohort_opened else "N/A"
        avg_review_hours = round(sum(result.review_hours) / len(result.review_hours), 2) if result.review_hours else "N/A"
        review_samples = len(result.review_hours)
        coverage = "Complete" if activity_available and not result.errors else "Partial"

        inventory.append([
            org,
            repo,
            visibility,
            created_str,
            new_in_period,
            archived,
            default_branch,
            protected_branches_str,
            result.codeowners,
            result.codeowners_path,
            active_last_7,
            total_activities,
            active_contributors,
            *metric_values,
            merge_rate,
            avg_review_hours,
            review_samples,
            coverage,
        ])

    tot_idx = REPO_INVENTORY_HEADERS.index("Total Activities")
    inventory.sort(
        key=lambda row: (
            -(row[tot_idx] if isinstance(row[tot_idx], int) else -1),
            str(row[0]).casefold(),
            str(row[1]).casefold(),
        )
    )
    tables["Repository Inventory"] = (REPO_INVENTORY_HEADERS, inventory)

    top_rows: List[list] = []
    inv_repo_idx = REPO_INVENTORY_HEADERS.index("Repository")
    inv_tot_idx = tot_idx
    inv_contrib_idx = REPO_INVENTORY_HEADERS.index("Active Contributors")
    inv_last7_idx = REPO_INVENTORY_HEADERS.index("Activity Last 7 Days")
    inv_cov_idx = REPO_INVENTORY_HEADERS.index("Coverage")

    orgs_in_inventory = sorted({row[0] for row in inventory}, key=str.casefold)
    for org in orgs_in_inventory:
        org_repos = [row for row in inventory if row[0] == org]
        org_repos.sort(
            key=lambda row: (
                -(row[inv_tot_idx] if isinstance(row[inv_tot_idx], int) else -1),
                str(row[inv_repo_idx]).casefold(),
            )
        )
        for rank, row in enumerate(org_repos[:10], start=1):
            top_rows.append([
                org,
                rank,
                row[inv_repo_idx],
                row[inv_tot_idx],
                row[inv_contrib_idx],
                *(row[REPO_INVENTORY_HEADERS.index(label)] for label in METRIC_LABELS),
                row[inv_last7_idx],
                row[inv_cov_idx],
            ])
    tables["Top Active Repositories"] = (TOP_REPOS_HEADERS, top_rows)

    kpis: List[list] = []
    org_status_norm = {k.casefold(): v for k, v in (organization_status or {}).items()}
    members_norm = {k.casefold(): {login.lower() for login in v} for k, v in members.items()}
    org_lookup: Dict[str, str] = {}
    for org, _ in reports:
        org_lookup.setdefault(org.casefold(), org)
    for org in members:
        org_lookup.setdefault(org.casefold(), org)
    for org in (organization_status or {}):
        org_lookup.setdefault(org.casefold(), org)
    organizations = sorted(org_lookup.values(), key=str.casefold)
    month_start = end.astimezone(LOCAL_TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for org in organizations:
        org_k = org.casefold()
        selected = [result for (owner, _), result in reports.items() if owner.casefold() == org_k]
        events = [event for result in selected for event in result.events if since <= event[2] <= end]
        counts = Counter(metric for _, metric, _ in events)
        contributors = {login for login, _, _ in events if login}
        org_members = members_norm.get(org_k, set())
        member_coverage = org_k in members_norm
        rolling = {
            login
            for result in selected
            for login, _, when in result.events
            if login and end - timedelta(days=30) <= when <= end
        }
        monthly = {
            login
            for result in selected
            for login, _, when in result.events
            if login and month_start <= when <= end
        }
        hours = [h for result in selected for h in result.review_hours]
        opened = sum(result.cohort_opened for result in selected)
        merged = sum(result.cohort_merged for result in selected)

        def add(category: str, metric: str, value: Any, definition: str) -> None:
            kpis.append([org, category, metric, value, definition])

        add(
            "User Adoption",
            "Current onboarded org members",
            len(org_members) if member_coverage else "N/A",
            "How many people are currently listed as members of this organization. This is not a paid-seat or sign-in count.",
        )
        add(
            "User Adoption",
            "Active onboarded members",
            len(org_members & contributors) if member_coverage else "N/A",
            "Current organization members who made at least one tracked contribution during this report period.",
        )
        add(
            "User Adoption",
            "Monthly active users (month to date)",
            len(monthly),
            f"Distinct people with tracked activity so far this calendar month ({LOCAL_TZ_NAME}), including people who are not members.",
        )
        add(
            "User Adoption",
            "Rolling 30-day active users",
            len(rolling),
            "Distinct people with tracked activity during the last 30 days, including non-members.",
        )

        add(
            "Repository Landscape",
            "Repositories hosted (selected scope)",
            len(selected),
            "Repositories selected in inventory that this App can access. This may not include every repository in the enterprise.",
        )
        add(
            "Repository Landscape",
            "New repositories created",
            sum(
                bool(created and since <= created <= end)
                for (owner, _), created in created_dates.items()
                if owner.casefold() == org.casefold()
            ),
            "Selected repositories created during this period that still exist here. Deleted or transferred repositories are not counted.",
        )
        for visibility in ("private", "internal", "public"):
            add(
                "Repository Landscape",
                f"{visibility.title()} repositories",
                sum(
                    (
                        (item.metadata.get("visibility") or ("private" if item.metadata.get("private") else "public")).lower()
                        if item.metadata
                        else ""
                    ) == visibility
                    for item in selected
                ),
                f"Selected repositories currently set to {visibility} visibility.",
            )

        add(
            "Developer Activity",
            "Repositories active in last 7 days",
            sum(any(end - timedelta(days=7) <= when <= end for _, _, when in item.events) for item in selected),
            "Repositories with at least one tracked contribution in the last seven days. This does not measure logins or every kind of push.",
        )
        add(
            "Developer Activity",
            "Total commit volume",
            counts["commits"],
            "Commit records on default branches during the report period, counted by author date. Some commits may not be linked to a GitHub user; the bot filter applies.",
        )
        add(
            "Developer Activity",
            "Active contributors",
            len(contributors),
            "Distinct GitHub users with at least one tracked contribution, including people outside the organization.",
        )
        add(
            "Developer Activity",
            "Average commits per active user",
            round(counts["commits"] / len(contributors), 2) if contributors else "N/A",
            "Default-branch commits divided by distinct active contributors. Unlinked commits can be included in the commit count but not the user count.",
        )
        add(
            "Developer Activity",
            "Top Active Repositories by Activity",
            "See Top Active Repositories sheet",
            "The sheet lists this organization's ten repositories with the most tracked events in the report period. It is an activity count, not a productivity score.",
        )

        add(
            "Collaboration",
            "Pull requests created",
            counts["prs_opened"],
            "Pull requests opened during the report period.",
        )
        add(
            "Collaboration",
            "Pull requests merged",
            counts["prs_merged"],
            "Pull requests merged during the report period, even if they were opened earlier.",
        )
        add(
            "Collaboration",
            "PR merge rate (%)",
            round(merged / opened * 100, 2) if opened else "N/A",
            "Of the PRs opened during this period, the percentage merged by the report cutoff. Shows N/A if no PRs were opened.",
        )
        add(
            "Collaboration",
            "Code reviews completed",
            counts["reviews"],
            "Submitted pull-request reviews during the period. Draft reviews are excluded; submitted reviews count even if later dismissed.",
        )
        add(
            "Collaboration",
            "Average PR review turnaround (hours)",
            round(sum(hours) / len(hours), 2) if hours else "N/A",
            "Average time from opening a PR to its first submitted review by someone else. Unreviewed PRs are excluded; weekends and draft time count.",
        )
        add(
            "Collaboration",
            "First review sample PRs",
            len(hours),
            "Number of PRs used to calculate the average first-review time.",
        )

        for attribute, title in (("protected", "Repositories with branch protection"), ("codeowners", "Repositories with CODEOWNERS")):
            add(
                "Security & Governance",
                title,
                sum(getattr(item, attribute) == "Yes" for item in selected),
                "Repositories with at least one protected branch." if attribute == "protected" else
                "Repositories with a CODEOWNERS file in a recognized location on the default branch. The file's rules are not validated.",
            )
            add(
                "Security & Governance",
                f"{title} - unknown",
                sum(getattr(item, attribute) == "Unknown" for item in selected),
                "Repositories where GitHub did not provide enough information to confirm this check. They are not counted as having no protection/file.",
            )

        add(
            "Coverage",
            "Activity scans completed",
            sum(item.activity_complete for item in selected),
            "Number of this organization's selected repositories whose activity scan completed successfully.",
        )
        add(
            "Coverage",
            "Run status",
            "Complete" if org_status_norm.get(org.casefold(), False) else "Partial",
            "Complete means this organization's selected repositories and member list were collected without errors. A failure in another organization does not change this status.",
        )

    tables["Management KPIs"] = (MGMT_KPI_HEADERS, kpis)
    return tables


def load_history(path: Path) -> Dict[str, Any]:
    """Load historical adoption trend snapshots."""
    if not path.is_file():
        return {"version": 1, "snapshots": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("snapshots"), list):
            return data
    except Exception as exc:
        logger.debug("Could not read adoption history from %s: %s", path, exc)
    return {"version": 1, "snapshots": []}


def save_history(path: Path, data: Dict[str, Any]) -> None:
    """Safely persist adoption history snapshots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp.replace(path)
    except Exception as exc:
        logger.debug("Failed saving adoption history: %s", exc)
        if temp.exists():
            temp.unlink(missing_ok=True)
