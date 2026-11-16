"""Self-hosted runners and workflow job execution collector."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..activity.models import fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import is_dormant_repo, map_concurrent, resolve_worker_count
from ..common.text import pluralize
from .models import RunnerInfo, RunnerJobMetrics, RunnerJobRecord, RunnersConfig

logger = logging.getLogger("repo360.runners")


def sanitize_api_error(exc: Exception) -> str:
    """Sanitize raw network exception tracebacks into clean executive diagnostic messages."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 403:
        return "403 Forbidden (requires 'administration: read' repository permission)"
    if status_code == 404:
        return "404 Not Found"
    if status_code == 429:
        return "429 Too Many Requests (secondary rate limit reached)"
    if status_code in (500, 502, 503, 504):
        return f"{status_code} GitHub Server Error"

    err_str = str(exc)
    err_lower = err_str.lower()
    if "ssleof" in err_lower or "unexpected_eof" in err_lower or "ssl: eof" in err_lower:
        return "Transient SSL connection reset by peer during read"
    if "connectionaborted" in err_lower or "10053" in err_lower or "connection aborted" in err_lower:
        return "Network connection aborted by host / software reset"
    if "remotedisconnected" in err_lower or "connection reset" in err_lower:
        return "Connection reset by peer"
    if "timeout" in err_lower:
        return "Request timed out"
    if "httpsconnectionpool" in err_lower:
        return "Transient network error (connection pool exhaustion)"
    return err_str[:120]


def resolve_hosted_runner_pool(labels: List[Any], job_os: Optional[str] = None) -> Tuple[str, str, str]:
    """Derive clean hosted pool name, OS, and architecture from GitHub-hosted runner job labels."""
    for l in labels:
        l_str = str(l).strip()
        l_lower = l_str.lower()
        if any(prefix in l_lower for prefix in ("ubuntu", "windows", "macos")):
            os_name = "Linux" if "ubuntu" in l_lower else ("Windows" if "windows" in l_lower else "macOS")
            arch = "arm64" if any(a in l_lower for a in ("arm", "m1", "m2", "m3", "m4")) else "x64"
            return f"GitHub-Hosted ({l_str})", os_name, arch

    os_name = (job_os or "Linux").capitalize()
    return f"GitHub-Hosted ({os_name})", os_name, "x64"


def collect_runners_and_metrics(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    since: datetime,
    end: datetime,
    cfg: RunnersConfig,
    failures: List[Tuple[str, str]],
    metadata_cache: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    job_records_out: Optional[List[RunnerJobRecord]] = None,
    truncation_notes_out: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[List[RunnerInfo], Dict[Tuple[str, str], RunnerJobMetrics], Dict[str, RunnerJobMetrics]]:
    """Discover registered self-hosted runners and scrape workflow execution and queue metrics."""
    runners: List[RunnerInfo] = []
    runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
    org_runner_metrics: Dict[str, RunnerJobMetrics] = defaultdict(RunnerJobMetrics)

    org_known_names: Dict[str, Set[str]] = defaultdict(set)
    org_known_ids: Dict[str, Set[int]] = defaultdict(set)
    org_id_to_name: Dict[str, Dict[int, str]] = defaultdict(dict)
    org_name_map: Dict[str, Dict[str, str]] = defaultdict(dict)
    seen_runners: Set[Tuple[str, str, int, str]] = set()
    seen_ephemeral_keys: Set[Tuple[str, str]] = set()

    def _discover_org_runners(org: str) -> Tuple[List[RunnerInfo], Optional[Tuple[str, str]]]:
        org_runners: List[RunnerInfo] = []
        groups: Dict[int, str] = {}
        try:
            for item in client.paginate_enveloped(org, f"/orgs/{org}/actions/runner-groups", "runner_groups"):
                if isinstance(item, dict) and "id" in item:
                    groups[item["id"]] = item.get("name", "Default")
        except Exception as exc:
            logger.debug("Could not retrieve runner groups for %s: %s", org, exc)

        try:
            for item in client.paginate_enveloped(org, f"/orgs/{org}/actions/runners", "runners"):
                if not isinstance(item, dict) or "id" not in item:
                    continue
                r_id = item["id"]
                name = item.get("name", f"runner-{r_id}")
                os_name = item.get("os", "Unknown")
                status = str(item.get("status", "unknown")).capitalize()
                busy = bool(item.get("busy", False))
                raw_labels = item.get("labels", [])
                labels = tuple(sorted(l.get("name") for l in raw_labels if isinstance(l, dict) and l.get("name")))
                g_id = item.get("runner_group_id")
                g_name = groups.get(g_id, "Default") if g_id else "Default"
                arch = item.get("arch", "x64")
                version = item.get("runner_version") or item.get("version") or "Unknown"
                ephemeral = bool(item.get("ephemeral", False))

                org_runners.append(
                    RunnerInfo(
                        id=r_id,
                        name=name,
                        os=os_name,
                        status=status,
                        busy=busy,
                        labels=labels,
                        runner_group_id=g_id,
                        runner_group_name=g_name,
                        org=org,
                        repo=None,
                        arch=arch,
                        runner_version=version,
                        ephemeral=ephemeral,
                    )
                )

            logger.info(
                "Discovered %s for %s",
                pluralize(len(org_runners), "organization runner", include_count=True),
                org,
            )
            return org_runners, None
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code == 403:
                err_msg = "403 Forbidden (requires 'organization_self_hosted_runners: read' permission on GitHub App)"
            else:
                err_msg = str(exc)
            logger.warning("Actions self-hosted runners query failed for %s: %s", org, err_msg)
            return org_runners, (f"{org} (Actions runners)", err_msg)

    org_results = map_concurrent(
        orgs,
        _discover_org_runners,
        max_workers=min(8, len(orgs)) if orgs else 1,
        max_cap=8,
        thread_name_prefix="runner-orgs",
    )
    for org_runners, org_fail in org_results:
        for r in org_runners:
            ok = r.org.casefold()
            runners.append(r)
            org_known_names[ok].add(r.name.casefold())
            org_name_map[ok][r.name.casefold()] = r.name
            if r.id:
                org_known_ids[ok].add(r.id)
                org_id_to_name[ok][r.id] = r.name
            seen_runners.add((ok, "", r.id or 0, r.name.casefold()))
        if org_fail:
            failures.append(org_fail)

    active_targets: List[Tuple[str, str]] = []
    for org, repo in targets:
        meta = metadata_cache.get((org.casefold(), repo.casefold())) if metadata_cache else None
        if meta and is_dormant_repo(meta, since, parse_ts):
            continue
        active_targets.append((org, repo))

    def _scan_repo_runs(target: Tuple[str, str]) -> Tuple[
        List[RunnerInfo],
        Dict[Tuple[str, str], RunnerJobMetrics],
        Dict[str, RunnerJobMetrics],
        List[Tuple[str, str]],
    ]:
        org, repo = target
        org_k = org.casefold()
        loc_runners: List[RunnerInfo] = []
        loc_runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
        loc_org_metrics: Dict[str, RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
        loc_jobs: List[RunnerJobRecord] = []
        loc_truncations: List[Tuple[str, str]] = []
        loc_known_names: Set[str] = set(org_known_names.get(org_k, ()))
        loc_known_ids: Set[int] = set(org_known_ids.get(org_k, ()))
        loc_id_to_name: Dict[int, str] = dict(org_id_to_name.get(org_k, {}))
        loc_name_map: Dict[str, str] = dict(org_name_map.get(org_k, {}))
        repo_failures: List[Tuple[str, str]] = []

        if cfg.scan_repo_runners:
            try:
                for item in client.paginate_enveloped(org, f"/repos/{org}/{repo}/actions/runners", "runners"):
                    if isinstance(item, dict) and "id" in item:
                        r_id = item["id"]
                        name = item.get("name", f"runner-{r_id}")
                        loc_runners.append(
                            RunnerInfo(
                                id=r_id,
                                name=name,
                                os=item.get("os", "Unknown"),
                                status=str(item.get("status", "unknown")).capitalize(),
                                busy=bool(item.get("busy", False)),
                                labels=tuple(sorted(l.get("name") for l in item.get("labels", []) if isinstance(l, dict) and l.get("name"))),
                                runner_group_id=None,
                                runner_group_name="Repository-level",
                                org=org,
                                repo=repo,
                                arch=item.get("arch", "x64"),
                                runner_version=item.get("runner_version") or item.get("version") or "Unknown",
                                ephemeral=bool(item.get("ephemeral", False)),
                            )
                        )
                        loc_known_names.add(name.casefold())
                        loc_name_map[name.casefold()] = name
                        if r_id:
                            loc_known_ids.add(r_id)
                            loc_id_to_name[r_id] = name
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code == 403:
                    repo_failures.append(
                        (f"{org}/{repo} (Repo-level runners)", "403 Forbidden (requires 'administration: read' repository permission)")
                    )
                elif status_code != 404:
                    repo_failures.append((f"{org}/{repo} (Repo-level runners)", sanitize_api_error(exc)))

        wf_runner_cache: Dict[Any, Dict[str, Any]] = {}
        try:
            runs_checked = 0
            truncated_runs = False
            jobs_error_recorded = False

            run_params = {
                "status": "completed",
                "created": f">={since.strftime('%Y-%m-%d')}",
            }
            for run_item in client.paginate_enveloped(
                org, f"/repos/{org}/{repo}/actions/runs", "workflow_runs", params=run_params
            ):
                created_str = run_item.get("run_started_at") or run_item.get("created_at")
                if not created_str:
                    continue
                run_time = parse_ts(created_str)
                if run_time < since:
                    continue
                if run_time > end:
                    continue

                runs_checked += 1
                if cfg.max_runner_runs_per_repo > 0 and runs_checked > cfg.max_runner_runs_per_repo:
                    truncated_runs = True
                    break

                run_id = run_item.get("id")
                wf_id = run_item.get("workflow_id")
                wf_key = wf_id or run_item.get("path") or run_item.get("name")
                cached_profile = wf_runner_cache.get(wf_key) if wf_key is not None else None

                # Fast-path for profiled GitHub-Hosted workflows: bypass costly /jobs API calls
                if cached_profile is not None and not cached_profile.get("has_self_hosted"):
                    if not cfg.include_github_hosted:
                        continue

                    run_conclusion = str(run_item.get("conclusion") or "other").casefold()
                    if run_conclusion == "skipped":
                        continue

                    r_created_str = run_item.get("created_at")
                    r_started_str = run_item.get("run_started_at") or r_created_str
                    r_updated_str = run_item.get("updated_at")

                    r_created_at = parse_ts(r_created_str) if r_created_str else None
                    r_started_at = parse_ts(r_started_str) if r_started_str else None
                    r_completed_at = parse_ts(r_updated_str) if r_updated_str else None

                    r_queue_sec = max(0.0, (r_started_at - r_created_at).total_seconds()) if (r_started_at and r_created_at) else None
                    r_duration_sec = max(0.0, (r_completed_at - r_started_at).total_seconds()) if (r_completed_at and r_started_at) else None
                    r_job_end = r_completed_at or r_started_at

                    cached_jobs = cached_profile.get("jobs") or [
                        {
                            "name": run_item.get("name") or "job",
                            "pool_name": cached_profile.get("pool_name", "GitHub-Hosted (Linux)"),
                            "os": cached_profile.get("host_os", "Linux"),
                            "arch": cached_profile.get("host_arch", "x64"),
                        }
                    ]
                    sub_duration = (
                        (r_duration_sec / len(cached_jobs))
                        if (r_duration_sec is not None and len(cached_jobs) > 1)
                        else r_duration_sec
                    )

                    workflow_name = run_item.get("name") or run_item.get("path") or "Workflow"
                    actor_name = (
                        (run_item.get("actor") or {}).get("login")
                        or (run_item.get("triggering_actor") or {}).get("login")
                        or "Unknown"
                    )
                    run_html_url = run_item.get("html_url") or f"https://github.com/{org}/{repo}/actions/runs/{run_id}"

                    for c_job in cached_jobs:
                        p_name = c_job.get("pool_name", "GitHub-Hosted (Linux)")
                        h_os = c_job.get("os", "Linux")
                        h_arch = c_job.get("arch", "x64")
                        j_name = c_job.get("name", "job")
                        runner_type = "GitHub-Hosted (Standard)"

                        if p_name.casefold() not in loc_known_names:
                            loc_known_names.add(p_name.casefold())
                            loc_name_map[p_name.casefold()] = p_name
                            loc_runners.append(
                                RunnerInfo(
                                    id=0,
                                    name=p_name,
                                    os=h_os,
                                    status="Hosted",
                                    busy=False,
                                    labels=tuple([p_name]),
                                    runner_group_name="GitHub Actions",
                                    org=org,
                                    repo=None,
                                    arch=h_arch,
                                    runner_version="GitHub Managed",
                                    ephemeral=True,
                                )
                            )

                        target_buckets = (loc_runner_metrics[(org, p_name)], loc_org_metrics[org])
                        for target_bucket in target_buckets:
                            target_bucket.total_jobs += 1
                            target_bucket.repos_served.add(repo)
                            if run_conclusion == "success":
                                target_bucket.successful_jobs += 1
                            elif run_conclusion == "failure":
                                target_bucket.failed_jobs += 1
                            elif run_conclusion == "timed_out":
                                target_bucket.timed_out_jobs += 1
                            elif run_conclusion == "cancelled":
                                target_bucket.cancelled_jobs += 1
                            else:
                                target_bucket.other_jobs += 1

                            if sub_duration is not None:
                                target_bucket.total_duration_seconds += sub_duration
                                target_bucket.job_durations.append(sub_duration)
                            if r_queue_sec is not None:
                                target_bucket.total_queue_seconds += r_queue_sec
                                target_bucket.queue_durations.append(r_queue_sec)
                            if r_started_at:
                                target_bucket.first_job_time = (
                                    min(target_bucket.first_job_time, r_started_at)
                                    if target_bucket.first_job_time else r_started_at
                                )
                            if r_job_end:
                                target_bucket.last_job_time = (
                                    max(target_bucket.last_job_time, r_job_end)
                                    if target_bucket.last_job_time else r_job_end
                                )

                        loc_jobs.append(
                            RunnerJobRecord(
                                org=org,
                                repo=repo,
                                workflow_name=workflow_name,
                                run_id=run_id or 0,
                                job_id=run_id or 0,
                                job_name=j_name,
                                runner_id=0,
                                runner_name=p_name,
                                runner_type=runner_type,
                                conclusion=run_conclusion.capitalize(),
                                duration_seconds=sub_duration,
                                queue_seconds=r_queue_sec,
                                sla_breached=bool(r_queue_sec is not None and r_queue_sec > 60.0),
                                started_at=r_started_at,
                                completed_at=r_completed_at,
                                actor=actor_name,
                                html_url=run_html_url,
                            )
                        )
                    continue

                try:
                    jobs = list(client.paginate_enveloped(org, f"/repos/{org}/{repo}/actions/runs/{run_id}/jobs", "jobs"))
                except Exception as exc:
                    if not jobs_error_recorded:
                        status_code = getattr(getattr(exc, "response", None), "status_code", None)
                        if status_code != 404:
                            repo_failures.append((f"{org}/{repo} (Workflow run jobs)", sanitize_api_error(exc)))
                            jobs_error_recorded = True
                    continue

                has_self_hosted = False
                non_skipped_jobs = 0
                discovered_jobs_template: List[Dict[str, Any]] = []
                primary_hosted_pool = "GitHub-Hosted (Linux)"
                primary_hosted_os = "Linux"
                primary_hosted_arch = "x64"

                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    conclusion = str(job.get("conclusion") or "other").casefold()
                    if conclusion == "skipped":
                        continue
                    non_skipped_jobs += 1

                    labels = [str(l).casefold() for l in job.get("labels", []) if isinstance(l, str)]
                    r_id = job.get("runner_id")
                    r_name = (job.get("runner_name") or "").strip()

                    is_self_hosted = (
                        "self-hosted" in labels
                        or bool(r_id and r_id in loc_known_ids)
                        or bool(r_name and r_name.casefold() in loc_known_names)
                        or (bool(job.get("runner_group_name")) and job.get("runner_group_name") != "GitHub Actions")
                    )
                    if not is_self_hosted and not cfg.include_github_hosted:
                        continue
                    if is_self_hosted:
                        has_self_hosted = True
                    else:
                        pool_name, host_os, host_arch = resolve_hosted_runner_pool(job.get("labels", []), job.get("os"))
                        primary_hosted_pool, primary_hosted_os, primary_hosted_arch = pool_name, host_os, host_arch
                        discovered_jobs_template.append({
                            "name": job.get("name") or "job",
                            "pool_name": pool_name,
                            "os": host_os,
                            "arch": host_arch,
                        })

                    created_at = parse_ts(job["created_at"]) if job.get("created_at") else None
                    started_at = parse_ts(job["started_at"]) if job.get("started_at") else None
                    completed_at = parse_ts(job["completed_at"]) if job.get("completed_at") else None

                    queue_sec = max(0.0, (started_at - created_at).total_seconds()) if (started_at and created_at) else None
                    duration_sec = max(0.0, (completed_at - started_at).total_seconds()) if (completed_at and started_at) else None
                    job_end = completed_at or started_at

                    if is_self_hosted:
                        matched_name = r_name or f"runner-{r_id}" if r_id else "runner-unknown"
                        if not r_id and not r_name:
                            target_buckets: Tuple[RunnerJobMetrics, ...] = (loc_org_metrics[org],)
                            runner_type = "Ephemeral (Autoscaled)"
                        else:
                            if r_id and r_id in loc_id_to_name:
                                matched_name = loc_id_to_name[r_id]
                            elif r_name and r_name.casefold() in loc_name_map:
                                matched_name = loc_name_map[r_name.casefold()]
                            else:
                                matched_name = r_name or f"runner-{r_id}"

                            if matched_name.casefold() not in loc_known_names:
                                loc_known_names.add(matched_name.casefold())
                                loc_name_map[matched_name.casefold()] = matched_name
                                if r_id:
                                    loc_known_ids.add(r_id)
                                    loc_id_to_name[r_id] = matched_name
                                loc_runners.append(
                                    RunnerInfo(
                                        id=r_id or 0,
                                        name=matched_name,
                                        os=job.get("os", "Linux"),
                                        status="Ephemeral",
                                        busy=False,
                                        labels=tuple(job.get("labels", ["self-hosted"])),
                                        runner_group_name=job.get("runner_group_name") or "Default",
                                        org=org,
                                        repo=None,
                                        arch=job.get("arch", "x64"),
                                        runner_version="Unknown",
                                        ephemeral=True,
                                    )
                                )
                            target_buckets = (loc_runner_metrics[(org, matched_name)], loc_org_metrics[org])
                            runner_type = "Ephemeral (Autoscaled)" if (job.get("status") == "Ephemeral" or any(r.name == matched_name and r.ephemeral for r in loc_runners)) else "Persistent (Static VM)"
                    else:
                        pool_name, host_os, host_arch = resolve_hosted_runner_pool(job.get("labels", []), job.get("os"))
                        matched_name = pool_name
                        runner_type = "GitHub-Hosted (Standard)"
                        if matched_name.casefold() not in loc_known_names:
                            loc_known_names.add(matched_name.casefold())
                            loc_name_map[matched_name.casefold()] = matched_name
                            loc_runners.append(
                                RunnerInfo(
                                    id=0,
                                    name=matched_name,
                                    os=host_os,
                                    status="Hosted",
                                    busy=False,
                                    labels=tuple(job.get("labels", [matched_name])),
                                    runner_group_name="GitHub Actions",
                                    org=org,
                                    repo=None,
                                    arch=host_arch,
                                    runner_version="GitHub Managed",
                                    ephemeral=True,
                                )
                            )
                        target_buckets = (loc_runner_metrics[(org, matched_name)], loc_org_metrics[org])

                    for target_bucket in target_buckets:
                        target_bucket.total_jobs += 1
                        target_bucket.repos_served.add(repo)
                        if conclusion == "success":
                            target_bucket.successful_jobs += 1
                        elif conclusion == "failure":
                            target_bucket.failed_jobs += 1
                        elif conclusion == "timed_out":
                            target_bucket.timed_out_jobs += 1
                        elif conclusion == "cancelled":
                            target_bucket.cancelled_jobs += 1
                        else:
                            target_bucket.other_jobs += 1

                        if duration_sec is not None:
                            target_bucket.total_duration_seconds += duration_sec
                            target_bucket.job_durations.append(duration_sec)
                        if queue_sec is not None:
                            target_bucket.total_queue_seconds += queue_sec
                            target_bucket.queue_durations.append(queue_sec)
                        if started_at:
                            target_bucket.first_job_time = (
                                min(target_bucket.first_job_time, started_at)
                                if target_bucket.first_job_time else started_at
                            )
                        if job_end:
                            target_bucket.last_job_time = (
                                max(target_bucket.last_job_time, job_end)
                                if target_bucket.last_job_time else job_end
                            )

                    # Record granular workflow job execution event
                    workflow_name = run_item.get("name") or run_item.get("path") or "Workflow"
                    actor_name = (
                        (run_item.get("actor") or {}).get("login")
                        or (run_item.get("triggering_actor") or {}).get("login")
                        or "Unknown"
                    )
                    job_html_url = job.get("html_url") or run_item.get("html_url") or f"https://github.com/{org}/{repo}/actions/runs/{run_id}"
                    loc_jobs.append(
                        RunnerJobRecord(
                            org=org,
                            repo=repo,
                            workflow_name=workflow_name,
                            run_id=run_id or 0,
                            job_id=job.get("id") or 0,
                            job_name=job.get("name") or "job",
                            runner_id=r_id or 0,
                            runner_name=matched_name,
                            runner_type=runner_type,
                            conclusion=conclusion.capitalize(),
                            duration_seconds=duration_sec,
                            queue_seconds=queue_sec,
                            sla_breached=bool(queue_sec is not None and queue_sec > 60.0),
                            started_at=started_at,
                            completed_at=completed_at,
                            actor=actor_name,
                            html_url=job_html_url,
                        )
                    )

                if wf_key is not None and cached_profile is None and non_skipped_jobs > 0:
                    if has_self_hosted:
                        wf_runner_cache[wf_key] = {"has_self_hosted": True}
                    else:
                        wf_runner_cache[wf_key] = {
                            "has_self_hosted": False,
                            "pool_name": primary_hosted_pool,
                            "host_os": primary_hosted_os,
                            "host_arch": primary_hosted_arch,
                            "jobs": discovered_jobs_template or [
                                {
                                    "name": "job",
                                    "pool_name": primary_hosted_pool,
                                    "os": primary_hosted_os,
                                    "arch": primary_hosted_arch,
                                }
                            ],
                        }

            if truncated_runs:
                loc_truncations.append(
                    (
                        f"{org}/{repo} (Run history truncated)",
                        f"Inspected latest {cfg.max_runner_runs_per_repo} runs; earlier runs exist in window (increase --max-runs for full capture)",
                    )
                )

        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code != 404:
                repo_failures.append((f"{org}/{repo} (Actions workflow runs)", sanitize_api_error(exc)))

        return loc_runners, loc_runner_metrics, loc_org_metrics, repo_failures, loc_jobs, loc_truncations

    if not active_targets:
        return runners, runner_metrics, org_runner_metrics

    workers = resolve_worker_count(cfg.max_workers, len(active_targets), max_cap=32)
    logger.info(
        "Scanning %s for Actions workflow runs across %s...",
        pluralize(len(active_targets), "active repository", "active repositories", include_count=True),
        pluralize(workers, "concurrent worker", include_count=True),
    )
    total_targets = len(active_targets)
    completed_targets = 0
    last_log_time = time.time()
    from ..common.interrupts import iter_completed
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="runner-pool")
    try:
        futures = {pool.submit(_scan_repo_runs, target): target for target in active_targets}
        for fut in iter_completed(futures):
            loc_r, loc_rm, loc_om, repo_fails, loc_jobs, loc_truncs = fut.result()
            completed_targets += 1
            now = time.time()
            if completed_targets == 1 or completed_targets % 25 == 0 or completed_targets == total_targets or (now - last_log_time >= 10.0):
                last_log_time = now
                pct = (completed_targets / total_targets) * 100
                logger.info(
                    "Actions runner audit progress: %s of %s processed (%.1f%%)...",
                    pluralize(completed_targets, "repository", "repositories", include_count=True),
                    pluralize(total_targets, "repository", "repositories", include_count=True),
                    pct,
                )
            if repo_fails:
                failures.extend(repo_fails)
            if job_records_out is not None and loc_jobs:
                job_records_out.extend(loc_jobs)
            if truncation_notes_out is not None and loc_truncs:
                truncation_notes_out.extend(loc_truncs)
            for r in loc_r:
                ok = r.org.casefold()
                if r.repo:
                    dedup_key = (ok, r.repo.casefold(), r.id or 0, r.name.casefold())
                    if dedup_key not in seen_runners:
                        seen_runners.add(dedup_key)
                        runners.append(r)
                else:
                    eph_key = (ok, r.name.casefold())
                    if r.name.casefold() not in org_known_names[ok] and eph_key not in seen_ephemeral_keys:
                        seen_ephemeral_keys.add(eph_key)
                        runners.append(r)
            for k, m in loc_rm.items():
                runner_metrics[k].merge(m)
            for ok, om in loc_om.items():
                org_runner_metrics[ok].merge(om)
    except (KeyboardInterrupt, SystemExit):
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return runners, runner_metrics, org_runner_metrics


def build_runner_rows(
    runners: List[RunnerInfo],
    runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics],
    org_runner_metrics: Dict[str, RunnerJobMetrics],
    orgs: Optional[List[str]] = None,
) -> Tuple[List[list], List[list]]:
    """Compile formatted tabular records for individual runners and organization aggregates."""
    norm_runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
    for (m_org, m_name), m_val in runner_metrics.items():
        norm_runner_metrics[(m_org.casefold(), m_name.casefold())].merge(m_val)

    norm_org_metrics: Dict[str, RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
    for m_org, m_val in org_runner_metrics.items():
        norm_org_metrics[m_org.casefold()].merge(m_val)

    runner_rows: List[list] = []
    org_runner_rows: List[list] = []

    for r in sorted(runners, key=lambda x: (x.org.casefold(), x.name.casefold())):
        metrics = norm_runner_metrics.get((r.org.casefold(), r.name.casefold()), RunnerJobMetrics())
        if r.status.casefold() == "hosted":
            scope = "GitHub-Hosted"
            prov_type = "GitHub-Hosted (Standard)"
        else:
            scope = f"Repo: {r.repo}" if r.repo else "Organization"
            prov_type = "Ephemeral (Autoscaled)" if r.ephemeral else "Persistent (Static VM)"
        runner_rows.append([
            r.org,
            r.id,
            r.name,
            scope,
            r.runner_group_name,
            r.os,
            r.arch,
            r.runner_version,
            prov_type,
            r.status.capitalize(),
            "Busy" if r.busy else "Idle",
            "; ".join(r.labels) if r.labels else "None",
            len(metrics.repos_served),
            metrics.total_jobs,
            metrics.successful_jobs,
            metrics.failed_jobs,
            metrics.cancelled_jobs,
            metrics.timed_out_jobs,
            metrics.success_rate,
            metrics.total_compute_hours,
            metrics.avg_duration_minutes,
            metrics.median_duration_minutes,
            metrics.min_duration_minutes,
            metrics.max_duration_minutes,
            metrics.avg_queue_seconds,
            metrics.p95_queue_seconds,
            metrics.queue_sla_breaches,
            fmt_timestamp(metrics.first_job_time),
            fmt_timestamp(metrics.last_job_time),
            ", ".join(sorted(metrics.repos_served)) if metrics.repos_served else "-",
        ])

    org_lookup: Dict[str, str] = {}
    for o in (orgs or []):
        org_lookup.setdefault(o.casefold(), o)
    for r in runners:
        org_lookup.setdefault(r.org.casefold(), r.org)
    for m_org in org_runner_metrics:
        org_lookup.setdefault(m_org.casefold(), m_org)

    all_orgs = sorted(org_lookup.values(), key=str.casefold)
    for org in all_orgs:
        org_k = org.casefold()
        org_runners = [r for r in runners if r.org.casefold() == org_k]
        total_count = len(org_runners)
        org_level_count = sum(1 for r in org_runners if not r.repo and r.status.casefold() != "hosted")
        repo_level_count = sum(1 for r in org_runners if r.repo and r.status.casefold() != "hosted")
        online_count = sum(1 for r in org_runners if r.status.casefold() == "online")
        offline_count = sum(1 for r in org_runners if r.status.casefold() == "offline")
        registered_count = online_count + offline_count
        busy_count = sum(1 for r in org_runners if r.busy)
        ephemeral_count = sum(1 for r in org_runners if r.ephemeral and r.status.casefold() != "hosted")
        hosted_count = sum(1 for r in org_runners if r.status.casefold() == "hosted")
        if registered_count > 0:
            availability: Any = round(online_count / registered_count * 100, 2)
        elif total_count > 0:
            if hosted_count > 0 and ephemeral_count > 0:
                availability = "N/A (Ephemeral / Hosted Only)"
            elif hosted_count > 0:
                availability = "N/A (Hosted Only)"
            else:
                availability = "N/A (Ephemeral Only)"
        else:
            availability = "N/A"
        metrics = norm_org_metrics.get(org_k, RunnerJobMetrics())

        org_runner_rows.append([
            org,
            total_count,
            org_level_count,
            repo_level_count,
            online_count,
            offline_count,
            availability,
            busy_count,
            ephemeral_count,
            len(metrics.repos_served),
            metrics.total_jobs,
            metrics.successful_jobs,
            metrics.failed_jobs,
            metrics.cancelled_jobs,
            metrics.timed_out_jobs,
            metrics.success_rate,
            metrics.total_compute_hours,
            metrics.avg_queue_seconds,
            metrics.p95_queue_seconds,
            metrics.queue_sla_breaches,
            metrics.queue_sla_breach_rate,
            ", ".join(sorted(metrics.repos_served)) if metrics.repos_served else "-",
        ])

    return runner_rows, org_runner_rows


def build_runner_job_rows(jobs: List[RunnerJobRecord]) -> List[list]:
    """Compile formatted tabular records for individual workflow job executions."""
    rows: List[list] = []
    min_aware_dt = datetime.min.replace(tzinfo=timezone.utc)
    for j in sorted(jobs, key=lambda x: (x.started_at or min_aware_dt, x.org.casefold(), x.repo.casefold()), reverse=True):
        dur_mins = round(j.duration_seconds / 60.0, 2) if j.duration_seconds is not None else "N/A"
        q_sec = round(j.queue_seconds, 2) if j.queue_seconds is not None else "N/A"
        rows.append([
            j.org,
            j.repo,
            j.workflow_name,
            j.run_id,
            j.job_id,
            j.job_name,
            j.runner_name,
            j.runner_type,
            j.conclusion,
            dur_mins,
            q_sec,
            "Yes" if j.sla_breached else "No",
            fmt_timestamp(j.started_at),
            fmt_timestamp(j.completed_at),
            j.actor,
            j.html_url,
        ])
    return rows
