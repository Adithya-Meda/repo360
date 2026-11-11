"""Self-hosted runners and workflow job execution collector."""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from ..activity.models import fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import is_dormant_repo, map_concurrent, resolve_worker_count
from .models import RunnerInfo, RunnerJobMetrics, RunnersConfig

logger = logging.getLogger("repo360.runners")


def collect_runners_and_metrics(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    since: datetime,
    end: datetime,
    cfg: RunnersConfig,
    failures: List[Tuple[str, str]],
    metadata_cache: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Tuple[List[RunnerInfo], Dict[Tuple[str, str], RunnerJobMetrics], Dict[str, RunnerJobMetrics]]:
    """Discover registered self-hosted runners and scrape workflow execution and queue metrics."""
    runners: List[RunnerInfo] = []
    runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
    org_runner_metrics: Dict[str, RunnerJobMetrics] = defaultdict(RunnerJobMetrics)

    known_runner_names: Set[str] = set()
    known_runner_ids: Set[int] = set()

    # 1. Discover Organization-Level Runners & Groups concurrently across organizations
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

            logger.info("Discovered %d organization runner(s) for %s", len(org_runners), org)
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
            runners.append(r)
            known_runner_names.add(r.name.casefold())
            known_runner_ids.add(r.id)
        if org_fail:
            failures.append(org_fail)

    # Filter targets to active repositories
    active_targets: List[Tuple[str, str]] = []
    for org, repo in targets:
        meta = metadata_cache.get((org.casefold(), repo.casefold())) if metadata_cache else None
        if meta and is_dormant_repo(meta, since, parse_ts):
            continue
        active_targets.append((org, repo))

    # 2. Worker function to scan repository workflow runs and jobs
    def _scan_repo_runs(target: Tuple[str, str]) -> Tuple[
        List[RunnerInfo],
        Dict[Tuple[str, str], RunnerJobMetrics],
        Dict[str, RunnerJobMetrics],
        Optional[Tuple[str, str]],
    ]:
        org, repo = target
        loc_runners: List[RunnerInfo] = []
        loc_runner_metrics: Dict[Tuple[str, str], RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
        loc_org_metrics: Dict[str, RunnerJobMetrics] = defaultdict(RunnerJobMetrics)
        loc_known_names = set(known_runner_names)
        loc_known_ids = set(known_runner_ids)
        fail_item = None

        # Check repo-level runners
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
                    loc_known_ids.add(r_id)
        except Exception:
            pass

        # Check completed workflow runs
        try:
            runs_checked = 0
            consecutive_github_hosted = 0
            has_seen_self_hosted = False

            run_params = {
                "status": "completed",
                "created": f">={since.strftime('%Y-%m-%d')}",
            }
            for run_item in client.paginate_enveloped(
                org, f"/repos/{org}/{repo}/actions/runs", "workflow_runs", params=run_params
            ):
                created_str = run_item.get("created_at") or run_item.get("run_started_at")
                if not created_str:
                    continue
                run_time = parse_ts(created_str)
                if run_time < since:
                    break
                if run_time > end:
                    continue

                runs_checked += 1
                if runs_checked > cfg.max_runner_runs_per_repo:
                    break

                run_id = run_item.get("id")
                try:
                    jobs = list(client.paginate_enveloped(org, f"/repos/{org}/{repo}/actions/runs/{run_id}/jobs", "jobs"))
                except Exception:
                    continue

                run_had_self_hosted = False
                for job in jobs:
                    if not isinstance(job, dict):
                        continue
                    labels = [str(l).casefold() for l in job.get("labels", []) if isinstance(l, str)]
                    r_id = job.get("runner_id")
                    r_name = job.get("runner_name") or ""

                    is_self_hosted = (
                        "self-hosted" in labels
                        or (r_id and r_id in loc_known_ids)
                        or (r_name and r_name.casefold() in loc_known_names)
                    )
                    if not is_self_hosted:
                        continue

                    run_had_self_hosted = True
                    has_seen_self_hosted = True
                    matched_name = r_name or (f"runner-{r_id}" if r_id else "self-hosted-runner")

                    if matched_name.casefold() not in loc_known_names:
                        loc_known_names.add(matched_name.casefold())
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

                    created_at = parse_ts(job["created_at"]) if job.get("created_at") else None
                    started_at = parse_ts(job["started_at"]) if job.get("started_at") else None
                    completed_at = parse_ts(job["completed_at"]) if job.get("completed_at") else None
                    conclusion = str(job.get("conclusion", "other")).casefold()

                    queue_sec = max(0.0, (started_at - created_at).total_seconds()) if (started_at and created_at) else None
                    duration_sec = max(0.0, (completed_at - started_at).total_seconds()) if (completed_at and started_at) else None
                    job_end = completed_at or started_at

                    for target_bucket in (loc_runner_metrics[(org, matched_name)], loc_org_metrics[org]):
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

                if not run_had_self_hosted and not has_seen_self_hosted:
                    consecutive_github_hosted += 1
                    if consecutive_github_hosted >= 3:
                        break

        except Exception as exc:
            fail_item = (f"{org}/{repo} (Actions workflow runs)", str(exc))

        return loc_runners, loc_runner_metrics, loc_org_metrics, fail_item

    # Run workflow runs collection across worker pool
    if not active_targets:
        return runners, runner_metrics, org_runner_metrics

    workers = resolve_worker_count(cfg.max_workers, len(active_targets), max_cap=32)
    from ..common.interrupts import iter_completed
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="runner-pool")
    try:
        futures = {pool.submit(_scan_repo_runs, target): target for target in active_targets}
        for fut in iter_completed(futures):
            loc_r, loc_rm, loc_om, failure = fut.result()
            if failure:
                failures.append(failure)
            for r in loc_r:
                if r.name.casefold() not in known_runner_names:
                    known_runner_names.add(r.name.casefold())
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
    runner_rows: List[list] = []
    org_runner_rows: List[list] = []

    for r in sorted(runners, key=lambda x: (x.org.casefold(), x.name.casefold())):
        metrics = runner_metrics.get((r.org, r.name), RunnerJobMetrics())
        scope = f"Repo: {r.repo}" if r.repo else "Organization"
        runner_rows.append([
            r.org,
            r.id,
            r.name,
            scope,
            r.runner_group_name,
            r.os,
            r.arch,
            r.runner_version,
            "Ephemeral (Autoscaled)" if r.ephemeral else "Persistent (Static VM)",
            r.status,
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
        ])

    all_orgs = sorted(set(orgs or []) | {r.org for r in runners} | set(org_runner_metrics), key=str.casefold)
    for org in all_orgs:
        org_runners = [r for r in runners if r.org.casefold() == org.casefold()]
        total_count = len(org_runners)
        org_level_count = sum(1 for r in org_runners if not r.repo)
        repo_level_count = sum(1 for r in org_runners if r.repo)
        online_count = sum(1 for r in org_runners if r.status.casefold() == "online")
        offline_count = sum(1 for r in org_runners if r.status.casefold() in ("offline", "ephemeral"))
        busy_count = sum(1 for r in org_runners if r.busy)
        ephemeral_count = sum(1 for r in org_runners if r.ephemeral)
        availability = round(online_count / total_count * 100, 2) if total_count else "N/A"
        metrics = org_runner_metrics.get(org, RunnerJobMetrics())

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
        ])

    return runner_rows, org_runner_rows
