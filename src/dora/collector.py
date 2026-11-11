"""Collector and aggregator for cross-organization DORA and delivery flow metrics."""

from __future__ import annotations

import logging
import re
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests

from ..activity.models import fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import is_dormant_repo, map_concurrent, resolve_worker_count, run_concurrent_ai
from .models import (
    DORAConfig,
    DeploymentRecord,
    RecoveryIncident,
    RepoDORAMetrics,
    compute_composite_tier,
    get_cfr_tier,
    get_df_tier,
    get_lttc_tier,
    get_mttr_tier,
    get_rework_tier,
    resolve_stream_key,
)

logger = logging.getLogger("repo360.dora")

# Explicit word-boundary regexes prevent commit titles or CI check names from masquerading as deploys
DEPLOY_KEYWORDS = ("deploy", "release", "prod", "publish", "cd", "delivery")
_DEPLOY_WORKFLOW_RE = re.compile(
    r"(?:^|[\s_\-./\[])(deploy(?:ment|s)?|release|prod(?:uction)?|publish|cd|delivery|rollout)(?:$|[\s_\-./\]])",
    re.IGNORECASE,
)
_NON_PROD_ENV_RE = re.compile(
    r"(?:^|[\s_\-./])(staging|stage|dev(?:elopment)?|qa|uat|test(?:ing)?|eval(?:uation)?|sandbox|preview|ephemeral|demo|preprod|nonprod)(?:$|[\s_\-./])",
    re.IGNORECASE,
)
_CI_ONLY_WORKFLOW_RE = re.compile(
    r"^(?:ci|ci/cd|lint|test|tests|unit[-_ ]?tests?|codeql|security[-_ ]?scan|dependabot|stale|labeler|evaluation)$",
    re.IGNORECASE,
)
_RELEASE_BRANCH_RE = re.compile(r"^(?:release[s]?/|hotfix/|v?\d+\.\d+)", re.IGNORECASE)
_REWORK_TEXT_RE = re.compile(
    r"(?:^|[\s_\-./\[])(revert(?:ed|ing)?|rollback|roll[- ]back|hotfix|backout|fix[-_ ]prod|emergency[-_ ]fix)(?:$|[\s_\-./:\]])",
    re.IGNORECASE,
)
_NON_DEPLOY_EVENTS = frozenset({"pull_request", "pull_request_target", "pull_request_review", "schedule", "issue_comment"})
_MAX_COMPARE_CALLS_PER_REPO = 25


def _map_deployment_state(raw_state: Optional[str]) -> str:
    """Normalize a GitHub Deployment status string into SUCCESS, FAILURE, IN_PROGRESS, or INACTIVE."""
    state = (raw_state or "").strip().lower()
    if state in ("success", "active"):
        return "SUCCESS"
    if state in ("failure", "error", "timed_out"):
        return "FAILURE"
    if state in ("in_progress", "queued", "pending", "waiting"):
        return "IN_PROGRESS"
    if not state:
        return "IN_PROGRESS"
    return "INACTIVE"


def _parse_status_timestamp(status_obj: Optional[Dict[str, Any]]) -> Optional[datetime]:
    """Extract completion/status timestamp preferring creation time over later metadata edits."""
    if not isinstance(status_obj, dict):
        return None
    ts_str = (
        status_obj.get("createdAt")
        or status_obj.get("created_at")
        or status_obj.get("updatedAt")
        or status_obj.get("updated_at")
    )
    return parse_ts(ts_str) if ts_str else None


def _resolve_deployment_status(
    statuses: Optional[List[Dict[str, Any]]],
    latest_status: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[datetime]]:
    """Resolve effective deployment outcome and timestamp across GitHub status history.

    When a newer rollout supersedes a previously live deployment, GitHub marks the
    older deployment's ``latestStatus`` as ``INACTIVE``. Scanning status history preserves
    genuine prior ``SUCCESS``/``ACTIVE`` completions (with their original completion time)
    while keeping never-activated, cancelled, or status-less deployments out of ``SUCCESS``.
    """
    ordered: List[Dict[str, Any]] = []
    if statuses:
        with_ts: List[Tuple[datetime, Dict[str, Any]]] = []
        without_ts: List[Dict[str, Any]] = []
        for entry in statuses:
            if not isinstance(entry, dict):
                continue
            dt = _parse_status_timestamp(entry)
            if dt is not None:
                with_ts.append((dt, entry))
            else:
                without_ts.append(entry)
        with_ts.sort(key=lambda item: item[0], reverse=True)
        ordered = [item[1] for item in with_ts] + without_ts

    if not ordered and isinstance(latest_status, dict) and latest_status:
        ordered = [latest_status]

    if not ordered:
        return "IN_PROGRESS", None

    latest = ordered[0]
    latest_raw = str(latest.get("state") or "").strip().lower()
    latest_mapped = _map_deployment_state(latest_raw)
    latest_ts = _parse_status_timestamp(latest)

    if latest_raw == "inactive":
        for entry in ordered[1:]:
            if str(entry.get("state") or "").strip().lower() in ("success", "active"):
                return "SUCCESS", (_parse_status_timestamp(entry) or latest_ts)
        return "INACTIVE", latest_ts

    return latest_mapped, latest_ts


def _select_commit_timestamp(
    authored_str: Optional[str],
    committed_str: Optional[str],
) -> Optional[datetime]:
    """Select commit timestamp, correcting for >24h rebase/cherry-pick author-date drift."""
    authored_dt = parse_ts(authored_str) if authored_str else None
    committed_dt = parse_ts(committed_str) if committed_str else None
    if authored_dt and committed_dt and (committed_dt - authored_dt).total_seconds() > 86400:
        return committed_dt
    return authored_dt or committed_dt


def _is_production_environment(env_name: str, prod_environments: Tuple[str, ...]) -> bool:
    """Return True if an environment name targets production rather than staging/dev/preview."""
    cleaned = (env_name or "").strip().lower()
    if not cleaned:
        return False
    prod_set = {p.strip().lower() for p in prod_environments if p.strip()}
    if cleaned in prod_set:
        return True
    if _NON_PROD_ENV_RE.search(cleaned):
        return False
    tokens = [tok for tok in re.split(r"[\s_\-./:]+", cleaned) if tok]
    return any(tok in prod_set for tok in tokens)


def _is_rework_text(text: str, extra_keywords: Tuple[str, ...] = ()) -> bool:
    """Return True if text indicates a rollback, revert, or emergency hotfix."""
    if not text:
        return False
    if _REWORK_TEXT_RE.search(text):
        return True
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(kw.lower())}\b", lowered) for kw in extra_keywords if kw)


def _has_matching_label(labels: Any, target_labels: Set[str]) -> bool:
    """Check whether any label dictionary or string matches the configured incident label set."""
    if not labels or not target_labels:
        return False
    for lbl in labels:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        normalized = name.strip().lower()
        if normalized in target_labels:
            return True
    return False


def _is_deploy_workflow_name(wf_name: str, wf_path: str = "", include_non_prod: bool = False) -> bool:
    """Evaluate strictly on workflow name/path (never commit title) whether a workflow deploys."""
    name_clean = (wf_name or "").strip()
    path_clean = (wf_path or "").strip()
    if not name_clean and not path_clean:
        return False

    if not include_non_prod:
        if _NON_PROD_ENV_RE.search(name_clean) or _NON_PROD_ENV_RE.search(path_clean):
            return False

    if _CI_ONLY_WORKFLOW_RE.match(name_clean):
        return False

    return bool(_DEPLOY_WORKFLOW_RE.search(name_clean) or _DEPLOY_WORKFLOW_RE.search(path_clean))


_resolve_stream_key = resolve_stream_key


def _extract_inline_deployment_hints(node: Dict[str, Any]) -> Dict[str, Any]:
    """Extract commit timestamps, PR open time, resolved status, and rework signals from a GraphQL Deployment node."""
    commit_node = node.get("commit") or {}
    commit_sha = str(node.get("commitOid") or commit_node.get("oid") or "").strip() or None
    commit_ts = _select_commit_timestamp(
        commit_node.get("authoredDate") if isinstance(commit_node, dict) else None,
        commit_node.get("committedDate") if isinstance(commit_node, dict) else None,
    )
    commit_msg = str(commit_node.get("messageHeadline") or "")

    pr_nodes = ((commit_node.get("associatedPullRequests") or {}).get("nodes") or []) if isinstance(commit_node, dict) else []
    pr_created_ts: Optional[datetime] = None
    is_rework = _is_rework_text(commit_msg)

    for pr in pr_nodes:
        if not isinstance(pr, dict):
            continue
        c_str = pr.get("createdAt")
        if c_str:
            c_dt = parse_ts(c_str)
            if pr_created_ts is None or c_dt < pr_created_ts:
                pr_created_ts = c_dt
        pr_title = str(pr.get("title") or "")
        head_ref = str(pr.get("headRefName") or "")
        lbl_nodes = (pr.get("labels") or {}).get("nodes") or []
        if _is_rework_text(pr_title) or _is_rework_text(head_ref) or _has_matching_label(lbl_nodes, {"hotfix", "rollback", "revert"}):
            is_rework = True

    statuses_conn = node.get("statuses") or {}
    status_nodes = statuses_conn.get("nodes") if isinstance(statuses_conn, dict) else None
    latest_status = node.get("latestStatus") or {}
    resolved_state, completed_at = _resolve_deployment_status(status_nodes, latest_status)

    return {
        "commit_sha": commit_sha,
        "commit_ts": commit_ts,
        "pr_created_ts": pr_created_ts,
        "completed_at": completed_at,
        "resolved_state": resolved_state,
        "is_rework": is_rework,
    }


def _collect_repo_deployments_graphql(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    hints_out: Optional[Dict[str, Dict[str, Any]]] = None,
    baseline_out: Optional[Dict[str, str]] = None,
) -> Optional[List[DeploymentRecord]]:
    """Fetch deployments with inline status history, commit, and associated PR metadata via GraphQL."""
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        deployments(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            databaseId
            id
            createdAt
            environment
            commitOid
            ref {
              name
            }
            commit {
              oid
              authoredDate
              committedDate
              messageHeadline
              associatedPullRequests(first: 5) {
                nodes {
                  number
                  title
                  createdAt
                  mergedAt
                  headRefName
                  labels(first: 10) {
                    nodes {
                      name
                    }
                  }
                }
              }
            }
            statuses(last: 10) {
              nodes {
                state
                createdAt
                updatedAt
              }
            }
            latestStatus {
              state
              createdAt
              updatedAt
            }
          }
        }
      }
    }
    """
    deployments: List[DeploymentRecord] = []
    cursor: Optional[str] = None
    while True:
        data = client.graphql(org, query, {"owner": org, "name": repo, "cursor": cursor})
        if not isinstance(data, dict):
            return None
        repo_node = (data.get("data") or {}).get("repository")
        if not isinstance(repo_node, dict):
            return None

        conn = repo_node.get("deployments") or {}
        nodes = conn.get("nodes") or []
        pre_since_checked = 0
        hit_since = False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            created_str = node.get("createdAt")
            if not created_str:
                continue
            created_at = parse_ts(created_str)

            dep_id = str(node.get("databaseId") or node.get("id") or "")
            env_name = node.get("environment") or "production"
            ref_name = (node.get("ref") or {}).get("name") or "main"
            hints = _extract_inline_deployment_hints(node)
            status_val = hints["resolved_state"]

            if created_at < since:
                hit_since = True
                if baseline_out is not None and status_val == "SUCCESS" and hints["commit_sha"]:
                    baseline_out.setdefault(f"deployment:{env_name.strip().lower()}", hints["commit_sha"])
                pre_since_checked += 1
                if pre_since_checked >= 10:
                    return deployments
                continue
            if created_at > end:
                continue

            rec = DeploymentRecord(
                event_type="Deployment",
                event_id=dep_id,
                name=f"Deploy to {env_name}",
                environment=env_name,
                status=status_val,
                ref=ref_name,
                created_at=created_at,
                lead_time_hours=None,
                commit_sha=hints["commit_sha"],
                completed_at=hints["completed_at"],
                stream_key=f"deployment:{env_name.strip().lower()}",
                is_rework=bool(hints["is_rework"]),
            )
            if hints_out is not None:
                hints_out[f"dep:{dep_id}"] = hints
            deployments.append(rec)

        if hit_since:
            return deployments

        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
    return deployments


def collect_repo_deployments(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    hints_out: Optional[Dict[str, Dict[str, Any]]] = None,
    status_out: Optional[Dict[str, Any]] = None,
    baseline_out: Optional[Dict[str, str]] = None,
) -> List[DeploymentRecord]:
    """Retrieve GitHub Deployment API records for a repository within lookback window."""
    try:
        gql_deployments = _collect_repo_deployments_graphql(
            client, org, repo, since, end, hints_out=hints_out, baseline_out=baseline_out
        )
        if gql_deployments is not None:
            return gql_deployments
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403 and status_out is not None:
            status_out["deployments_forbidden"] = True
        logger.debug("GraphQL deployment query unavailable for %s/%s (%s); falling back to REST", org, repo, err)
    except Exception as err:
        if "forbidden" in str(err).lower() or "resource not accessible" in str(err).lower():
            if status_out is not None:
                status_out["deployments_forbidden"] = True
        logger.debug("GraphQL deployment query unavailable for %s/%s (%s); falling back to REST", org, repo, err)

    pending_deps: List[Tuple[str, str, str, datetime, Optional[str]]] = []
    try:
        endpoint = f"/repos/{org}/{repo}/deployments"
        for dep in client.paginate(org, endpoint):
            created_str = dep.get("created_at")
            if not created_str:
                continue
            created_at = parse_ts(created_str)
            if created_at < since:
                break
            if created_at > end:
                continue

            pending_deps.append((
                str(dep.get("id")),
                dep.get("environment") or "production",
                dep.get("ref") or "main",
                created_at,
                dep.get("sha"),
            ))
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403:
            if status_out is not None:
                status_out["deployments_forbidden"] = True
            logger.debug("Deployments API 403 Forbidden for %s/%s (missing deployments:read permission)", org, repo)
        elif err.response is not None and err.response.status_code != 404:
            logger.debug("Deployment API query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving deployments for %s/%s: %s", org, repo, err)

    if not pending_deps:
        return []

    def _build_record(item: Tuple[str, str, str, datetime, Optional[str]]) -> DeploymentRecord:
        dep_id, env_name, ref_name, created_at, sha = item
        status_state = "IN_PROGRESS"
        completed_at: Optional[datetime] = None
        try:
            statuses = client.get(org, f"/repos/{org}/{repo}/deployments/{dep_id}/statuses")
            if isinstance(statuses, list):
                status_state, completed_at = _resolve_deployment_status(statuses)
        except Exception:
            pass
        return DeploymentRecord(
            event_type="Deployment",
            event_id=dep_id,
            name=f"Deploy to {env_name}",
            environment=env_name,
            status=status_state,
            ref=ref_name,
            created_at=created_at,
            lead_time_hours=None,
            commit_sha=sha,
            completed_at=completed_at,
            stream_key=f"deployment:{env_name.strip().lower()}",
        )

    return map_concurrent(
        pending_deps,
        _build_record,
        max_workers=min(8, len(pending_deps)),
        max_cap=8,
        thread_name_prefix="dora-dep-status",
    )


def collect_repo_releases(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    include_non_prod: bool = False,
    baseline_out: Optional[Dict[str, str]] = None,
    errors_out: Optional[List[str]] = None,
) -> List[DeploymentRecord]:
    """Retrieve published production GitHub Release records within lookback window."""
    releases: List[DeploymentRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/releases"
        for rel in client.paginate(org, endpoint):
            if rel.get("draft"):
                continue
            is_prerelease = bool(rel.get("prerelease"))
            if is_prerelease and not include_non_prod:
                continue

            pub_str = rel.get("published_at") or rel.get("created_at")
            if not pub_str:
                continue
            published_at = parse_ts(pub_str)
            tag_name = rel.get("tag_name") or "untagged"
            env_name = "prerelease" if is_prerelease else "production"
            if published_at < since:
                if baseline_out is not None and tag_name != "untagged":
                    baseline_out.setdefault(f"release:{env_name}", tag_name)
                break
            if published_at > end:
                continue

            rel_name = rel.get("name") or tag_name
            target_branch = rel.get("target_commitish") or "main"
            body = str(rel.get("body") or "")
            is_rework = _is_rework_text(f"{tag_name} {rel_name} {body[:200]}")

            releases.append(
                DeploymentRecord(
                    event_type="Release",
                    event_id=tag_name,
                    name=rel_name,
                    environment=env_name,
                    status="SUCCESS",
                    ref=target_branch,
                    created_at=published_at,
                    lead_time_hours=None,
                    commit_sha=tag_name if tag_name != "untagged" else None,
                    completed_at=published_at,
                    stream_key=f"release:{env_name}",
                    is_rework=is_rework,
                )
            )
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403 and errors_out is not None:
            errors_out.append(f"Releases API forbidden (HTTP 403) on {org}/{repo}")
        elif err.response is not None and err.response.status_code not in (404, 403):
            logger.debug("Releases query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving releases for %s/%s: %s", org, repo, err)
    return releases


def collect_deploy_workflow_runs(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    default_branch: str = "main",
    include_non_prod: bool = False,
    hints_out: Optional[Dict[str, Dict[str, Any]]] = None,
    baseline_out: Optional[Dict[str, str]] = None,
    errors_out: Optional[List[str]] = None,
) -> List[DeploymentRecord]:
    """Retrieve completed production-branch deployment workflow runs (excluding PR/CI checks)."""
    runs: List[DeploymentRecord] = []
    allowed_branches = {default_branch.strip().lower(), "main", "master"} if default_branch else {"main", "master"}

    try:
        endpoint = f"/repos/{org}/{repo}/actions/runs"
        for item in client.paginate_enveloped(org, endpoint, "workflow_runs", {"status": "completed"}):
            created_str = item.get("created_at")
            if not created_str:
                continue
            created_at = parse_ts(created_str)

            event_trigger = (item.get("event") or "").lower()
            if event_trigger in _NON_DEPLOY_EVENTS:
                if created_at < since:
                    break
                continue

            wf_name = item.get("name") or ""
            wf_path = item.get("path") or ""
            if not _is_deploy_workflow_name(wf_name, wf_path, include_non_prod=include_non_prod):
                if created_at < since:
                    break
                continue

            head_branch = item.get("head_branch") or default_branch or "main"
            if not include_non_prod and event_trigger not in ("release", "deployment"):
                branch_l = head_branch.strip().lower()
                if branch_l not in allowed_branches and not _RELEASE_BRANCH_RE.match(branch_l):
                    if created_at < since:
                        break
                    continue

            conclusion = (item.get("conclusion") or "").lower()
            status_val = "SUCCESS" if conclusion == "success" else ("FAILURE" if conclusion in ("failure", "timed_out") else "OTHER")
            stream_key = f"workflow:{wf_name.strip().lower() or 'deploy'}:production"
            head_sha = item.get("head_sha")

            if created_at < since:
                if baseline_out is not None and status_val == "SUCCESS" and head_sha:
                    baseline_out.setdefault(stream_key, str(head_sha))
                break
            if created_at > end:
                continue
            if status_val == "OTHER":
                continue

            run_id = item.get("id")
            updated_str = item.get("updated_at")
            completed_at = parse_ts(updated_str) if updated_str else created_at
            head_commit = item.get("head_commit") or {}
            commit_ts_str = head_commit.get("timestamp")
            commit_ts = parse_ts(commit_ts_str) if commit_ts_str else None
            commit_msg = str(head_commit.get("message") or "")
            is_rework = _is_rework_text(f"{head_branch} {commit_msg.splitlines()[0] if commit_msg else ''}")

            rec = DeploymentRecord(
                event_type="Workflow Run",
                event_id=str(run_id),
                name=wf_name or "Deployment Pipeline",
                environment="production",
                status=status_val,
                ref=head_branch,
                created_at=created_at,
                lead_time_hours=None,
                run_id=run_id,
                commit_sha=head_sha,
                completed_at=completed_at,
                stream_key=stream_key,
                is_rework=is_rework,
            )
            if hints_out is not None and run_id is not None:
                hints_out[f"run:{run_id}"] = {
                    "commit_sha": head_sha,
                    "commit_ts": commit_ts,
                    "pr_created_ts": None,
                    "completed_at": completed_at,
                    "is_rework": is_rework,
                }
            runs.append(rec)
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403 and errors_out is not None:
            errors_out.append(f"Actions workflow runs API forbidden (HTTP 403) on {org}/{repo}")
        elif err.response is not None and err.response.status_code not in (404, 403):
            logger.debug("Actions runs query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving workflow runs for %s/%s: %s", org, repo, err)
    return runs


def _resolve_commit_pr_signal(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    ev: DeploymentRecord,
    hints: Dict[str, Any],
    rework_keywords: Tuple[str, ...],
    pr_cache: Dict[str, Optional[datetime]],
    pr_rework_cache: Dict[str, bool],
) -> Optional[datetime]:
    """Resolve earliest associated PR creation timestamp and rework flags for a deployment commit."""
    pr_ts = hints.get("pr_created_ts")
    sha = (ev.commit_sha or "").strip()
    if pr_ts is None and sha:
        if sha not in pr_cache:
            created_dt: Optional[datetime] = None
            rework_hit = False
            try:
                pulls = client.get(org, f"/repos/{org}/{repo}/commits/{quote(sha, safe='')}/pulls")
                if isinstance(pulls, list) and pulls:
                    created_dates = [parse_ts(p["created_at"]) for p in pulls if p.get("created_at")]
                    created_dt = min(created_dates) if created_dates else None
                    for p in pulls:
                        if _is_rework_text(
                            f"{p.get('title', '')} {(p.get('head') or {}).get('ref', '')}",
                            rework_keywords,
                        ):
                            rework_hit = True
            except Exception:
                created_dt = None
            pr_cache[sha] = created_dt
            pr_rework_cache[sha] = rework_hit
        pr_ts = pr_cache.get(sha)
        if pr_rework_cache.get(sha):
            ev.is_rework = True
    return pr_ts


def _enrich_lead_times_from_commits(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    events: List[DeploymentRecord],
    hints_map: Dict[str, Dict[str, Any]],
    rework_keywords: Tuple[str, ...],
    baseline_refs: Optional[Dict[str, str]] = None,
) -> None:
    """Compute commit/PR-to-deployment Lead Time strictly across successful production code changes."""
    if not events:
        return

    compare_cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    pr_cache: Dict[str, Optional[datetime]] = {}
    pr_rework_cache: Dict[str, bool] = {}
    compare_calls = 0
    prev_success_by_stream: Dict[str, str] = dict(baseline_refs or {})

    for ev in sorted(events, key=lambda d: d.created_at):
        # DORA LTTC measures completed, successful releases to production only
        if ev.status != "SUCCESS":
            ev.lead_time_hours = None
            ev.lead_time_source = {
                "IN_PROGRESS": "in_progress",
                "INACTIVE": "inactive_deploy",
            }.get(ev.status, "not_deployed")
            continue

        deploy_ts = ev.completed_at or ev.created_at
        hint_key = f"dep:{ev.event_id}" if ev.event_type == "Deployment" else (f"run:{ev.run_id}" if ev.run_id else "")
        hints = hints_map.get(hint_key, {})

        stream_key = _resolve_stream_key(ev)
        curr_ref = ev.commit_sha or (ev.event_id if ev.event_type == "Release" and ev.event_id != "untagged" else None)
        prev_success_ref = prev_success_by_stream.get(stream_key)

        # Identical SHA/tag redeploy or rebuild without code change has no new change lead time
        if prev_success_ref and curr_ref and prev_success_ref == curr_ref:
            ev.lead_time_hours = None
            ev.lead_time_source = "no_code_change"
            continue

        # 1. Primary: Compare commit range (prev_success_ref...curr_ref) for true batch commit lead time
        resolved_hours: Optional[float] = None
        method = ""
        single_commit_batch = False
        if prev_success_ref and curr_ref and prev_success_ref != curr_ref and compare_calls < _MAX_COMPARE_CALLS_PER_REPO:
            cache_key = (prev_success_ref, curr_ref)
            if cache_key not in compare_cache:
                compare_calls += 1
                try:
                    compare_cache[cache_key] = client.get(
                        org,
                        f"/repos/{org}/{repo}/compare/{quote(prev_success_ref, safe='')}...{quote(curr_ref, safe='')}",
                    )
                except Exception:
                    compare_cache[cache_key] = None

            cmp_data = compare_cache.get(cache_key)
            if isinstance(cmp_data, dict):
                commits = cmp_data.get("commits")
                if (
                    commits == []
                    and (cmp_data.get("status") == "identical" or cmp_data.get("ahead_by") == 0)
                ):
                    ev.lead_time_hours = None
                    ev.lead_time_source = "no_code_change"
                    prev_success_by_stream[stream_key] = curr_ref
                    continue

                single_commit_batch = len(commits or []) == 1
                deltas: List[float] = []
                for c in commits or []:
                    c_info = c.get("commit") or {}
                    c_author = c_info.get("author") or {}
                    c_committer = c_info.get("committer") or {}
                    msg = str(c_info.get("message") or "")
                    if _is_rework_text(msg.splitlines()[0] if msg else "", rework_keywords):
                        ev.is_rework = True
                    c_ts = _select_commit_timestamp(c_author.get("date"), c_committer.get("date"))
                    if c_ts is None:
                        continue
                    diff_hours = (deploy_ts - c_ts).total_seconds() / 3600.0
                    if -0.25 <= diff_hours <= 365.0 * 24.0:
                        deltas.append(max(0.01, round(diff_hours, 2)))
                if deltas:
                    resolved_hours = round(float(statistics.median(deltas)), 2)
                    method = "commit_to_deploy"

        # 2. Secondary: Associated Pull Request creation -> deployment completion
        # Also evaluate single-commit compare batches (Squash & Merge) so PR development/review
        # cycle time is captured instead of only post-merge CI runtime.
        if resolved_hours is None or (
            single_commit_batch and (hints.get("pr_created_ts") is not None or bool(ev.commit_sha))
        ):
            pr_ts = _resolve_commit_pr_signal(
                client, org, repo, ev, hints, rework_keywords, pr_cache, pr_rework_cache
            )
            if pr_ts is not None:
                diff_hours = (deploy_ts - pr_ts).total_seconds() / 3600.0
                if -0.25 <= diff_hours <= 365.0 * 24.0:
                    pr_hours = max(0.01, round(diff_hours, 2))
                    if resolved_hours is None or pr_hours > resolved_hours:
                        resolved_hours = pr_hours
                        method = "pr_to_deploy"

        # 3. Tertiary: Single commit timestamp -> deployment completion (initial baseline only)
        if resolved_hours is None:
            commit_ts = hints.get("commit_ts")
            if commit_ts is not None:
                diff_hours = (deploy_ts - commit_ts).total_seconds() / 3600.0
                if -0.25 <= diff_hours <= 365.0 * 24.0:
                    resolved_hours = max(0.01, round(diff_hours, 2))
                    method = "commit_timestamp"

        ev.lead_time_hours = resolved_hours
        ev.lead_time_source = method or "unavailable"

        if curr_ref:
            prev_success_by_stream[stream_key] = curr_ref


def _collect_rework_prs(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    default_branch: str,
    cfg: DORAConfig,
    errors_out: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Collect merged rollback, revert, and hotfix PRs targeting production branches."""
    rework_prs: List[Dict[str, Any]] = []
    incident_label_set = {lbl.strip().lower() for lbl in cfg.incident_labels if lbl.strip()}
    allowed_bases = {default_branch.strip().lower(), "main", "master"} if default_branch else {"main", "master"}

    try:
        endpoint = f"/repos/{org}/{repo}/pulls"
        for pr in client.paginate(org, endpoint, {"state": "closed", "sort": "updated", "direction": "desc"}):
            updated_str = pr.get("updated_at")
            if updated_str and parse_ts(updated_str) < since:
                break

            merged_str = pr.get("merged_at")
            if not merged_str:
                continue
            merged_at = parse_ts(merged_str)
            if merged_at < since or merged_at > end:
                continue

            base_ref = ((pr.get("base") or {}).get("ref") or "").strip().lower()
            if not cfg.include_non_prod and base_ref and base_ref not in allowed_bases and not _RELEASE_BRANCH_RE.match(base_ref):
                continue

            title = str(pr.get("title") or "")
            head_ref = str((pr.get("head") or {}).get("ref") or "")
            labels = pr.get("labels") or []
            if _is_rework_text(title, cfg.rework_keywords) or _is_rework_text(head_ref, cfg.rework_keywords) or _has_matching_label(labels, incident_label_set):
                rework_prs.append({
                    "number": pr.get("number"),
                    "title": title,
                    "merged_at": merged_at,
                    "created_at": parse_ts(pr["created_at"]) if pr.get("created_at") else merged_at,
                    "merge_commit_sha": pr.get("merge_commit_sha"),
                })
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403 and errors_out is not None:
            errors_out.append(f"Pull requests API forbidden (HTTP 403) on {org}/{repo}")
        elif err.response is not None and err.response.status_code not in (404, 403):
            logger.debug("Pulls query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving rework PRs for %s/%s: %s", org, repo, err)

    return rework_prs


def _collect_incident_issues(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    cfg: DORAConfig,
    errors_out: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Collect GitHub Issues labeled as production incidents within the lookback window."""
    incidents: List[Dict[str, Any]] = []
    incident_label_set = {lbl.strip().lower() for lbl in cfg.incident_labels if lbl.strip()}

    try:
        endpoint = f"/repos/{org}/{repo}/issues"
        params = {"state": "all", "since": since.isoformat(), "sort": "created", "direction": "desc"}
        for issue in client.paginate(org, endpoint, params):
            if "pull_request" in issue:
                continue
            created_str = issue.get("created_at")
            if not created_str:
                continue
            created_at = parse_ts(created_str)
            if created_at < since:
                break
            if created_at > end:
                continue

            labels = issue.get("labels") or []
            if not _has_matching_label(labels, incident_label_set):
                continue

            closed_str = issue.get("closed_at")
            closed_at = parse_ts(closed_str) if closed_str else None
            incidents.append({
                "number": issue.get("number"),
                "title": str(issue.get("title") or "Production Incident"),
                "created_at": created_at,
                "closed_at": closed_at,
                "state": str(issue.get("state") or "open").lower(),
            })
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 403 and errors_out is not None:
            errors_out.append(f"Issues API forbidden (HTTP 403) on {org}/{repo}")
        elif err.response is not None and err.response.status_code not in (404, 403):
            logger.debug("Issues query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving incident issues for %s/%s: %s", org, repo, err)

    return incidents


def _apply_native_incident_and_rework_signals(
    metrics: RepoDORAMetrics,
    rework_prs: List[Dict[str, Any]],
    incident_issues: List[Dict[str, Any]],
) -> None:
    """Link merged revert/hotfix PRs and incident issues to deployments without double-counting CFR."""
    deployments = metrics.deployments
    if not deployments:
        return

    metrics.rollback_hotfix_prs = len(rework_prs)
    metrics.incident_issues_count = len(incident_issues)

    # Match only against completed deployments; index failure timestamps already captured
    completed = [d for d in deployments if d.status in ("SUCCESS", "FAILURE")]
    failure_times: List[datetime] = [d.created_at for d in completed if d.status == "FAILURE"]
    sha_to_deploy: Dict[str, DeploymentRecord] = {
        d.commit_sha: d for d in completed if d.commit_sha
    }

    def _has_nearby_failure(ts: datetime, hours_back: float = 48.0, hours_forward: float = 2.0) -> bool:
        lo = ts - timedelta(hours=hours_back)
        hi = ts + timedelta(hours=hours_forward)
        return any(lo <= ft <= hi for ft in failure_times)

    for pr in rework_prs:
        m_sha = pr.get("merge_commit_sha")
        merged_at: datetime = pr["merged_at"]
        matched_deploy = sha_to_deploy.get(m_sha) if m_sha else None

        if matched_deploy is None:
            # Find nearest completed deployment shortly after PR merge (e.g. within 24h)
            candidates = [
                d for d in completed
                if timedelta(hours=-1) <= (d.created_at - merged_at) <= timedelta(hours=24)
            ]
            if candidates:
                matched_deploy = min(candidates, key=lambda d: abs((d.created_at - merged_at).total_seconds()))

        if matched_deploy is not None:
            matched_deploy.is_rework = True
        else:
            metrics.unlinked_rework_prs += 1

        # If a hotfix/revert PR followed a SUCCESS deploy with no prior FAILURE recorded, add to CFR
        prior_success = any(d.status == "SUCCESS" and d.created_at <= merged_at for d in deployments)
        if prior_success and not _has_nearby_failure(merged_at, hours_back=48.0, hours_forward=2.0):
            metrics.unlinked_prod_incidents += 1
            failure_times.append(merged_at)

    for iss in incident_issues:
        created_at: datetime = iss["created_at"]
        closed_at: Optional[datetime] = iss.get("closed_at")
        prior_success = any(d.status == "SUCCESS" and d.created_at <= created_at for d in deployments)
        if prior_success and not _has_nearby_failure(created_at, hours_back=48.0, hours_forward=2.0):
            metrics.unlinked_prod_incidents += 1
            failure_times.append(created_at)

        if closed_at is not None and closed_at >= created_at:
            mttr_h = max(0.05, round((closed_at - created_at).total_seconds() / 3600.0, 2))
            metrics.incidents.append(
                RecoveryIncident(
                    failed_event=f"Issue #{iss['number']}: {iss['title'][:45]}",
                    failure_time=created_at,
                    restored_event=f"Closed Issue #{iss['number']}",
                    restored_time=closed_at,
                    mttr_hours=mttr_h,
                    status="Resolved",
                    source="GitHub Incident Issue",
                    environment="production",
                )
            )
        else:
            metrics.incidents.append(
                RecoveryIncident(
                    failed_event=f"Issue #{iss['number']}: {iss['title'][:45]}",
                    failure_time=created_at,
                    restored_event="Pending Resolution",
                    restored_time=None,
                    mttr_hours=None,
                    status="Open",
                    source="GitHub Incident Issue",
                    environment="production",
                )
            )

    metrics.incidents.sort(key=lambda inc: inc.failure_time)


def calculate_recovery_incidents(deployments: List[DeploymentRecord]) -> List[RecoveryIncident]:
    """Analyze sequential failure/success events per stream to compute Recovery Time (MTTR/FDRT)."""
    incidents: List[RecoveryIncident] = []
    streams: Dict[str, List[DeploymentRecord]] = defaultdict(list)

    for ev in sorted(deployments, key=lambda d: d.created_at):
        streams[_resolve_stream_key(ev)].append(ev)

    for stream_events in streams.values():
        pending_failure: Optional[DeploymentRecord] = None
        for ev in stream_events:
            if ev.status == "FAILURE":
                if pending_failure is None:
                    pending_failure = ev
            elif ev.status == "SUCCESS":
                if pending_failure is not None:
                    restore_ts = ev.completed_at or ev.created_at
                    fail_ts = pending_failure.completed_at or pending_failure.created_at
                    duration_hours = max(0.05, round((restore_ts - fail_ts).total_seconds() / 3600.0, 2))
                    incidents.append(
                        RecoveryIncident(
                            failed_event=f"{pending_failure.event_type} {pending_failure.event_id}",
                            failure_time=fail_ts,
                            restored_event=f"{ev.event_type} {ev.event_id}",
                            restored_time=restore_ts,
                            mttr_hours=duration_hours,
                            status="Resolved",
                            source="Deployment Stream",
                            environment=pending_failure.environment or "production",
                        )
                    )
                    pending_failure = None

        if pending_failure is not None:
            fail_ts = pending_failure.completed_at or pending_failure.created_at
            incidents.append(
                RecoveryIncident(
                    failed_event=f"{pending_failure.event_type} {pending_failure.event_id}",
                    failure_time=fail_ts,
                    restored_event="Pending Restoration",
                    restored_time=None,
                    mttr_hours=None,
                    status="Open",
                    source="Deployment Stream",
                    environment=pending_failure.environment or "production",
                )
            )

    incidents.sort(key=lambda inc: inc.failure_time)
    return incidents


def _deduplicate_release_and_workflow_events(
    rels: List[DeploymentRecord],
    wf_runs: List[DeploymentRecord],
) -> List[DeploymentRecord]:
    """Combine Releases and Deploy Workflow Runs without double-counting the same release push."""
    if not rels:
        return list(wf_runs)
    if not wf_runs:
        return list(rels)

    release_times = [r.created_at for r in rels]
    deduped: List[DeploymentRecord] = list(rels)
    for wf in wf_runs:
        is_near_release = any(abs((wf.created_at - rt).total_seconds()) <= 1800 for rt in release_times)
        if not is_near_release:
            deduped.append(wf)
    return deduped


def profile_repo_dora(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    meta: Dict[str, Any],
    cfg: Optional[DORAConfig] = None,
) -> RepoDORAMetrics:
    """Collect production deployments, releases, workflows, lead times, incidents, and rework for a repo."""
    active_cfg = cfg or DORAConfig()
    metrics = RepoDORAMetrics(org=org, repo=repo)

    if is_dormant_repo(meta, since, parse_ts):
        return metrics

    default_branch = str(meta.get("default_branch") or "main")
    hints_map: Dict[str, Dict[str, Any]] = {}
    dep_status: Dict[str, Any] = {}
    baseline_refs: Dict[str, str] = {}

    all_deps = collect_repo_deployments(
        client,
        org,
        repo,
        since,
        end,
        hints_out=hints_map,
        status_out=dep_status,
        baseline_out=baseline_refs,
    )
    metrics.deployments_api_forbidden = bool(dep_status.get("deployments_forbidden"))

    if active_cfg.include_non_prod:
        deps = all_deps
    else:
        deps = [d for d in all_deps if _is_production_environment(d.environment, active_cfg.prod_environments)]

    rels = collect_repo_releases(
        client,
        org,
        repo,
        since,
        end,
        include_non_prod=active_cfg.include_non_prod,
        baseline_out=baseline_refs,
        errors_out=metrics.errors,
    )
    wf_runs = collect_deploy_workflow_runs(
        client,
        org,
        repo,
        since,
        end,
        default_branch=default_branch,
        include_non_prod=active_cfg.include_non_prod,
        hints_out=hints_map,
        baseline_out=baseline_refs,
        errors_out=metrics.errors,
    )

    metrics.total_releases = len(rels)
    metrics.deploy_workflow_runs = len(wf_runs)

    combined_events: List[DeploymentRecord] = []
    if deps:
        combined_events.extend(deps)
        metrics.signal_mode = "GitHub Deployments"
    elif rels or wf_runs:
        combined_events.extend(_deduplicate_release_and_workflow_events(rels, wf_runs))
        if rels and wf_runs:
            metrics.signal_mode = "Releases + Prod Workflows"
        elif rels:
            metrics.signal_mode = "GitHub Releases"
        else:
            metrics.signal_mode = "Production Workflows"
    elif metrics.deployments_api_forbidden:
        metrics.signal_mode = "Permission Gap (deployments:read)"
    else:
        metrics.signal_mode = "No Production Activity"

    metrics.deployments = sorted(combined_events, key=lambda d: d.created_at)
    metrics.stalled_deploys_count = sum(
        1
        for d in metrics.deployments
        if d.status == "IN_PROGRESS" and (end - d.created_at).total_seconds() > 86400.0
    )

    # Enrich commit/PR-to-deployment lead times & native GitHub incident/rework signals ONLY for active repos
    if metrics.deployments:
        _enrich_lead_times_from_commits(
            client,
            org,
            repo,
            metrics.deployments,
            hints_map,
            active_cfg.rework_keywords,
            baseline_refs=baseline_refs,
        )
        metrics.incidents = calculate_recovery_incidents(metrics.deployments)
        rework_prs = _collect_rework_prs(
            client, org, repo, since, end, default_branch, active_cfg, errors_out=metrics.errors
        )
        incident_issues = _collect_incident_issues(
            client, org, repo, since, end, active_cfg, errors_out=metrics.errors
        )
        _apply_native_incident_and_rework_signals(metrics, rework_prs, incident_issues)

    return metrics


def collect_dora_data(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    since: datetime,
    end: datetime,
    cfg: DORAConfig,
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[RepoDORAMetrics]:
    """Collect DORA delivery, rework, and recovery metrics across all repositories in parallel."""
    results: List[RepoDORAMetrics] = []

    if targets:
        workers = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32)
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                fut = executor.submit(profile_repo_dora, client, org, repo, since, end, meta, cfg)
                future_to_target[fut] = (org, repo)

            from ..common.interrupts import iter_completed
            for fut in iter_completed(future_to_target):
                org, repo = future_to_target[fut]
                try:
                    m = fut.result()
                    results.append(m)
                except Exception as err:
                    logger.warning("Error evaluating DORA metrics for %s/%s: %s", org, repo, err)
                    err_m = RepoDORAMetrics(org=org, repo=repo, errors=[str(err)])
                    results.append(err_m)
        except (KeyboardInterrupt, SystemExit):
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    results.sort(key=lambda m: (m.org.lower(), m.repo.lower()))
    return results


def build_dora_rows(
    metrics_list: List[RepoDORAMetrics],
    orgs: List[str],
    window_days: int = 90,
    ai_engine: Optional[Any] = None,
) -> Tuple[List[list], List[list], List[list], List[list]]:
    """Transform DORA metrics into structured table rows for Excel delivery."""
    repo_rows: List[list] = []
    deployment_rows: List[list] = []
    recovery_rows: List[list] = []
    org_rows: List[list] = []

    org_map: Dict[str, List[RepoDORAMetrics]] = {org: [] for org in orgs}

    ai_items: List[Tuple[int, RepoDORAMetrics, float, str, str, str, str, str]] = []
    tier_cache: Dict[int, Tuple[str, str, str, str, str, float]] = {}

    for idx, m in enumerate(metrics_list):
        overall, df_tier, lttc_tier, cfr_tier, mttr_tier = m.get_tiers(window_days)
        df_weekly = m.deployment_frequency_per_week(window_days)
        tier_cache[idx] = (overall, df_tier, lttc_tier, cfr_tier, mttr_tier, df_weekly)
        if m.total_deploys > 0 and ai_engine is not None:
            ai_items.append((idx, m, df_weekly, df_tier, lttc_tier, cfr_tier, mttr_tier, overall))

    ai_advisories: Dict[int, str] = {}
    if ai_items and ai_engine is not None:
        recs = run_concurrent_ai(
            ai_items,
            lambda item: ai_engine.diagnose_dora_flow(
                repo=f"{item[1].org}/{item[1].repo}",
                df_weekly=item[2],
                df_tier=item[3],
                lead_time_hours=item[1].median_lead_time_hours,
                lttc_tier=item[4],
                cfr_pct=item[1].change_failure_rate_pct or 0.0,
                cfr_tier=item[5],
                mttr_hours=item[1].median_mttr_hours,
                mttr_tier=item[6],
                composite_tier=item[7],
                rework_pct=item[1].rework_rate_pct,
                rework_tier=item[1].rework_tier,
                open_incidents=item[1].open_incidents_count,
                signal_mode=item[1].signal_mode,
                code_change_deploys=item[1].code_change_deploys,
                no_op_deploys=item[1].no_op_deploys,
                stalled_deploys=item[1].stalled_deploys_count,
                collapsed_retries=item[1].redundant_failure_retries,
                total_deploys=item[1].total_deploys,
            ),
            ai_engine=ai_engine,
            max_workers=8,
        )
        for item, adv in zip(ai_items, recs):
            ai_advisories[item[0]] = adv

    for idx, m in enumerate(metrics_list):
        org_map.setdefault(m.org, []).append(m)

        overall, df_tier, lttc_tier, cfr_tier, mttr_tier, df_weekly = tier_cache[idx]
        lead_time_val = m.median_lead_time_hours if m.median_lead_time_hours is not None else "N/A"
        cfr_val = m.change_failure_rate_pct if m.change_failure_rate_pct is not None else "N/A"
        mttr_val = m.median_mttr_hours if m.median_mttr_hours is not None else "N/A"
        rework_val = m.rework_rate_pct if m.rework_rate_pct is not None else "N/A"
        rework_tier = m.rework_tier

        if m.total_deploys == 0:
            flow_advisory = (
                f"Stalled: {m.stalled_deploys_count} deployment(s) IN_PROGRESS >24h without completion."
                if m.stalled_deploys_count > 0
                else "-"
            )
        else:
            if idx in ai_advisories:
                flow_advisory = ai_advisories[idx]
            else:
                flow_advisory = "Delivery throughput and stability are within standard benchmarks."
                if cfr_tier in ("Low", "Medium"):
                    flow_advisory = "Elevated change failure rate. Strengthen pre-deploy regression suites, canary checks, and rollback automation."
                elif rework_tier in ("Low", "Medium"):
                    flow_advisory = "High deployment rework rate from hotfixes/reverts. Enforce stricter PR validation and staged rollouts."
                elif m.open_incidents_count > 0:
                    flow_advisory = f"{m.open_incidents_count} open recovery incident(s) detected. Prioritize service restoration and automated rollback verification."
                elif m.code_change_deploys == 0 and m.no_op_deploys > 0:
                    flow_advisory = (
                        f"Only same-SHA/zero-diff rebuilds observed ({m.no_op_deploys} no-op redeploy(s)); "
                        "no new production code changes shipped in window."
                    )
                elif lttc_tier in ("Low", "Medium"):
                    flow_advisory = "Elevated commit-to-production lead time. Reduce PR batch sizes, streamline code review SLAs, and parallelize CI."
                elif df_tier in ("Low", "Medium") and cfr_tier in ("Elite", "High"):
                    flow_advisory = "Low deployment frequency despite strong stability. Automate continuous delivery triggers and ship smaller increments."
                elif mttr_tier in ("Low", "Medium"):
                    flow_advisory = "Extended recovery duration. Adopt automated zero-downtime rollbacks and healthcheck-driven alerts."
            if (
                m.stalled_deploys_count > 0
                and "stalled" not in flow_advisory.lower()
                and "in_progress" not in flow_advisory.lower()
            ):
                flow_advisory = f"{flow_advisory} ({m.stalled_deploys_count} stalled IN_PROGRESS deploy(s) >24h.)"
        m.flow_advisory = flow_advisory

        repo_rows.append([
            m.org,
            m.repo,
            overall,
            m.total_deploys,
            m.successful_deploys,
            m.failed_deploys,
            df_weekly if m.total_deploys > 0 else 0.0,
            df_tier,
            lead_time_val,
            lttc_tier,
            cfr_val,
            cfr_tier,
            mttr_val,
            mttr_tier,
            m.flow_advisory,
            m.total_releases,
            m.deploy_workflow_runs,
            rework_val,
            rework_tier,
            m.open_incidents_count,
            m.signal_mode,
            m.code_change_deploys,
            m.no_op_deploys,
            m.lttc_coverage_pct if m.lttc_coverage_pct is not None else "N/A",
            m.stalled_deploys_count,
            m.effective_failed_changes,
            m.rework_deploys,
            m.redundant_failure_retries,
            m.telemetry_scope,
        ])

        for d in m.deployments:
            c_ist = fmt_timestamp(d.created_at)
            lt_str = d.lead_time_hours if d.lead_time_hours is not None else "N/A"
            run_str = str(d.run_id) if d.run_id else "-"
            method_label = d.lead_time_source or ("unavailable" if d.lead_time_hours is None else "measured")
            rework_label = "Yes" if d.is_rework else "No"
            completed_str = (
                fmt_timestamp(d.completed_at)
                if d.completed_at is not None
                else ("Pending" if d.status == "IN_PROGRESS" else "-")
            )
            deployment_rows.append([
                m.org,
                m.repo,
                d.event_type,
                d.event_id,
                d.name,
                d.status,
                d.ref,
                c_ist,
                lt_str,
                run_str,
                method_label,
                rework_label,
                completed_str,
            ])

        for inc in m.incidents:
            f_ist = fmt_timestamp(inc.failure_time)
            r_ist = fmt_timestamp(inc.restored_time) if inc.restored_time is not None else "-"
            mttr_cell = inc.mttr_hours if inc.mttr_hours is not None else "Open"
            recovery_rows.append([
                m.org,
                m.repo,
                inc.failed_event,
                f_ist,
                inc.restored_event,
                r_ist,
                mttr_cell,
                inc.status,
                inc.source,
                inc.environment,
            ])

    # Organization aggregations — compute DF using median active-repo cadence so repo count never inflates DORA tier
    for org in orgs:
        p_list = org_map.get(org, [])
        active_repos = [m for m in p_list if m.total_deploys > 0]
        active_deploy_repos = len(active_repos)
        inactive_repos = len(p_list) - active_deploy_repos

        total_org_deploys = sum(m.total_deploys for m in active_repos)
        total_org_successful = sum(m.successful_deploys for m in active_repos)
        total_org_code_change = sum(m.code_change_deploys for m in active_repos)
        total_org_collapsed = sum(m.redundant_failure_retries for m in active_repos)
        org_cfr_denom = max(0, total_org_deploys - total_org_collapsed)
        total_org_failed_changes = sum(m.effective_failed_changes for m in active_repos)
        total_org_rework = sum(m.rework_deploys for m in active_repos)
        total_org_open_incidents = sum(m.open_incidents_count for m in active_repos)

        if active_deploy_repos > 0:
            active_dfs = [m.deployment_frequency_per_week(window_days) for m in active_repos]
            org_df = round(float(statistics.median(active_dfs)), 2)
            org_df_tier = get_df_tier(org_df)
        else:
            org_df = 0.0
            org_df_tier = "N/A"

        if org_cfr_denom > 0:
            org_cfr: Optional[float] = round((total_org_failed_changes / org_cfr_denom) * 100, 2)
            org_cfr_tier = get_cfr_tier(org_cfr)
            org_rework: Optional[float] = round((total_org_rework / org_cfr_denom) * 100, 2)
            org_rework_tier = get_rework_tier(org_rework)
        else:
            org_cfr = None
            org_cfr_tier = "N/A"
            org_rework = None
            org_rework_tier = "N/A"

        all_lead_times = [
            d.lead_time_hours
            for m in active_repos
            for d in m.deployments
            if d.status == "SUCCESS" and d.lead_time_hours is not None and d.lead_time_hours >= 0
        ]
        org_lead_time = round(float(statistics.median(all_lead_times)), 2) if all_lead_times else None

        all_mttrs = [
            inc.mttr_hours
            for m in active_repos
            for inc in m.resolved_incidents
            if inc.mttr_hours is not None
        ]
        org_mttr = round(float(statistics.median(all_mttrs)), 2) if all_mttrs else None

        org_lttc_tier = get_lttc_tier(org_lead_time)
        org_mttr_tier = get_mttr_tier(org_mttr)
        if active_deploy_repos == 0:
            org_overall = "N/A"
        else:
            org_overall = compute_composite_tier(
                [org_df_tier, org_lttc_tier, org_cfr_tier, org_mttr_tier],
                cfr_tier=org_cfr_tier,
                rework_tier=org_rework_tier,
                df_tier=org_df_tier,
                lttc_tier=org_lttc_tier,
                mttr_tier=org_mttr_tier,
                has_open_incidents=total_org_open_incidents > 0,
                successful_deploys=total_org_successful,
                code_change_deploys=total_org_code_change,
                window_days=window_days,
            )

        org_rows.append([
            org,
            len(p_list),
            active_deploy_repos,
            org_overall,
            total_org_deploys,
            org_df,
            org_df_tier,
            org_lead_time if org_lead_time is not None else "N/A",
            org_lttc_tier,
            org_cfr if org_cfr is not None else "N/A",
            org_cfr_tier,
            org_mttr if org_mttr is not None else "N/A",
            org_mttr_tier,
            org_rework if org_rework is not None else "N/A",
            org_rework_tier,
            inactive_repos,
            total_org_open_incidents,
        ])

    return repo_rows, org_rows, deployment_rows, recovery_rows
