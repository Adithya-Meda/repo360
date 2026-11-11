"""Collector and aggregator for cross-organization DORA and delivery flow metrics."""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ..activity.models import IST, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
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
)

logger = logging.getLogger("repo360.dora")
DEPLOY_KEYWORDS = ("deploy", "release", "prod", "publish", "cd", "delivery")


def collect_repo_deployments(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
) -> List[DeploymentRecord]:
    """Retrieve GitHub Deployment API records for a repository within lookback window."""
    deployments: List[DeploymentRecord] = []
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

            dep_id = str(dep.get("id"))
            env_name = dep.get("environment", "production")
            ref_name = dep.get("ref", "main")

            # Query deployment status
            status_state = "SUCCESS"
            try:
                statuses = client.get(org, f"/repos/{org}/{repo}/deployments/{dep_id}/statuses")
                if statuses and isinstance(statuses, list):
                    latest_state = (statuses[0].get("state") or "").lower()
                    if latest_state in ("failure", "error"):
                        status_state = "FAILURE"
                    elif latest_state in ("in_progress", "queued", "pending"):
                        status_state = "IN_PROGRESS"
                    else:
                        status_state = "SUCCESS"
            except Exception:
                pass

            deployments.append(
                DeploymentRecord(
                    event_type="Deployment",
                    event_id=dep_id,
                    name=f"Deploy to {env_name}",
                    environment=env_name,
                    status=status_state,
                    ref=ref_name,
                    created_at=created_at,
                    lead_time_hours=None,
                )
            )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Deployment API query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving deployments for %s/%s: %s", org, repo, err)
    return deployments


def collect_repo_releases(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
) -> List[DeploymentRecord]:
    """Retrieve GitHub Release records for a repository within lookback window."""
    releases: List[DeploymentRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/releases"
        for rel in client.paginate(org, endpoint):
            pub_str = rel.get("published_at") or rel.get("created_at")
            if not pub_str:
                continue
            published_at = parse_ts(pub_str)
            if published_at < since:
                break
            if published_at > end:
                continue

            rel_id = str(rel.get("id"))
            tag_name = rel.get("tag_name", "untagged")
            rel_name = rel.get("name") or tag_name
            target_branch = rel.get("target_commitish", "main")

            # Lead time calculation: published_at minus created_at (or git release tag staging)
            lead_time = None
            created_str = rel.get("created_at")
            if created_str:
                c_at = parse_ts(created_str)
                delta_sec = (published_at - c_at).total_seconds()
                lead_time = max(0.1, round(delta_sec / 3600, 2))

            releases.append(
                DeploymentRecord(
                    event_type="Release",
                    event_id=tag_name,
                    name=rel_name,
                    environment="production",
                    status="SUCCESS",
                    ref=target_branch,
                    created_at=published_at,
                    lead_time_hours=lead_time,
                )
            )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
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
) -> List[DeploymentRecord]:
    """Retrieve completed deployment and release workflow runs for repositories without formal releases."""
    runs: List[DeploymentRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/actions/runs"
        for item in client.paginate_enveloped(org, endpoint, "workflow_runs", {"status": "completed"}):
            created_str = item.get("created_at")
            if not created_str:
                continue
            created_at = parse_ts(created_str)
            if created_at < since:
                break
            if created_at > end:
                continue

            wf_name = (item.get("name") or "").lower()
            run_name = (item.get("display_title") or "").lower()
            head_branch = item.get("head_branch") or "main"

            is_deploy_wf = any(kw in wf_name or kw in run_name for kw in DEPLOY_KEYWORDS)
            if not is_deploy_wf:
                continue

            conclusion = (item.get("conclusion") or "").lower()
            status_val = "SUCCESS" if conclusion == "success" else ("FAILURE" if conclusion in ("failure", "timed_out") else "OTHER")
            if status_val == "OTHER":
                continue

            run_id = item.get("id")
            updated_str = item.get("updated_at")
            lead_time = None
            if updated_str:
                u_at = parse_ts(updated_str)
                lead_time = max(0.05, round((u_at - created_at).total_seconds() / 3600, 2))

            runs.append(
                DeploymentRecord(
                    event_type="Workflow Run",
                    event_id=str(run_id),
                    name=item.get("name") or "Deployment Pipeline",
                    environment="production",
                    status=status_val,
                    ref=head_branch,
                    created_at=created_at,
                    lead_time_hours=lead_time,
                    run_id=run_id,
                )
            )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Actions runs query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error retrieving workflow runs for %s/%s: %s", org, repo, err)
    return runs


def calculate_recovery_incidents(deployments: List[DeploymentRecord]) -> List[RecoveryIncident]:
    """Analyze sequential failure events to compute Mean Time to Recovery (MTTR)."""
    incidents: List[RecoveryIncident] = []
    sorted_events = sorted(deployments, key=lambda d: d.created_at)

    pending_failure: Optional[DeploymentRecord] = None

    for ev in sorted_events:
        if ev.status == "FAILURE":
            if pending_failure is None:
                pending_failure = ev
        elif ev.status == "SUCCESS":
            if pending_failure is not None:
                duration_hours = max(0.05, round((ev.created_at - pending_failure.created_at).total_seconds() / 3600, 2))
                incidents.append(
                    RecoveryIncident(
                        failed_event=f"{pending_failure.event_type} {pending_failure.event_id}",
                        failure_time=pending_failure.created_at,
                        restored_event=f"{ev.event_type} {ev.event_id}",
                        restored_time=ev.created_at,
                        mttr_hours=duration_hours,
                        status="Resolved",
                    )
                )
                pending_failure = None

    return incidents


def profile_repo_dora(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    end: datetime,
    meta: Dict[str, Any],
) -> RepoDORAMetrics:
    """Collect deployments, releases, workflows, and recovery flow for a repository."""
    metrics = RepoDORAMetrics(org=org, repo=repo)

    deps = collect_repo_deployments(client, org, repo, since, end)
    rels = collect_repo_releases(client, org, repo, since, end)
    wf_runs = collect_deploy_workflow_runs(client, org, repo, since, end)

    metrics.total_releases = len(rels)
    metrics.deploy_workflow_runs = len(wf_runs)

    # Prefer Deployments API records if present, otherwise supplement with releases and deployment workflows
    combined_events: List[DeploymentRecord] = []
    if deps:
        combined_events.extend(deps)
    else:
        combined_events.extend(rels)
        combined_events.extend(wf_runs)

    metrics.deployments = sorted(combined_events, key=lambda d: d.created_at)
    metrics.incidents = calculate_recovery_incidents(metrics.deployments)
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
    """Collect DORA delivery and recovery metrics across all repositories in parallel."""
    results: List[RepoDORAMetrics] = []

    if targets:
        workers = min(cfg.max_workers, len(targets))
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                fut = executor.submit(profile_repo_dora, client, org, repo, since, end, meta)
                future_to_target[fut] = (org, repo)

            for fut in as_completed(future_to_target):
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

    for m in metrics_list:
        org_map.setdefault(m.org, []).append(m)

        overall, df_tier, lttc_tier, cfr_tier, mttr_tier = m.get_tiers(window_days)
        df_weekly = m.deployment_frequency_per_week(window_days)
        lead_time_val = m.median_lead_time_hours if m.median_lead_time_hours is not None else "N/A"
        mttr_val = m.median_mttr_hours if m.median_mttr_hours is not None else "N/A"

        if m.total_deploys == 0:
            flow_advisory = "-"
        elif ai_engine is not None:
            flow_advisory = ai_engine.diagnose_dora_flow(
                repo=f"{m.org}/{m.repo}",
                df_weekly=df_weekly,
                df_tier=df_tier,
                lead_time_hours=m.median_lead_time_hours,
                lttc_tier=lttc_tier,
                cfr_pct=m.change_failure_rate_pct,
                cfr_tier=cfr_tier,
                mttr_hours=m.median_mttr_hours,
                mttr_tier=mttr_tier,
                composite_tier=overall,
            )
        else:
            flow_advisory = "Deployment cadence is within standard benchmarks. Maintain branch protection and CI automation."
            if df_tier in ("Low", "Medium") and cfr_tier in ("Elite", "High"):
                flow_advisory = "Low deployment frequency with stable change failure rate. Decompose batch sizes and automate staging rollouts."
            elif cfr_tier in ("Low", "Medium"):
                flow_advisory = "High change failure rate detected. Implement automated regression testing, canary verification, and pre-deployment linting."
            elif lttc_tier in ("Low", "Medium"):
                flow_advisory = "Elevated lead time for changes. Streamline code review SLAs and optimize CI test parallelization."
            elif mttr_tier in ("Low", "Medium"):
                flow_advisory = "Extended recovery duration. Adopt automated zero-downtime rollbacks and healthcheck-driven alerts."
        m.flow_advisory = flow_advisory

        repo_rows.append([
            m.org,
            m.repo,
            overall,
            m.total_deploys,
            m.successful_deploys,
            m.failed_deploys,
            df_weekly,
            df_tier,
            lead_time_val,
            lttc_tier,
            m.change_failure_rate_pct,
            cfr_tier,
            mttr_val,
            mttr_tier,
            m.flow_advisory,
            m.total_releases,
            m.deploy_workflow_runs,
        ])

        for d in m.deployments:
            c_ist = fmt_timestamp(d.created_at)
            lt_str = d.lead_time_hours if d.lead_time_hours is not None else "-"
            run_str = str(d.run_id) if d.run_id else "-"
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
            ])

        for inc in m.incidents:
            f_ist = fmt_timestamp(inc.failure_time)
            r_ist = fmt_timestamp(inc.restored_time)
            recovery_rows.append([
                m.org,
                m.repo,
                inc.failed_event,
                f_ist,
                inc.restored_event,
                r_ist,
                inc.mttr_hours,
                inc.status,
            ])

    # Organization aggregations
    for org in orgs:
        p_list = org_map.get(org, [])
        active_deploy_repos = sum(1 for m in p_list if m.total_deploys > 0)
        total_org_deploys = sum(m.total_deploys for m in p_list)
        total_org_failed = sum(m.failed_deploys for m in p_list)
        weeks = max(1.0, window_days / 7.0)
        org_df = round(total_org_deploys / weeks, 2)
        org_cfr = round((total_org_failed / total_org_deploys * 100), 2) if total_org_deploys else 0.0

        all_lead_times = [
            d.lead_time_hours
            for m in p_list
            for d in m.deployments
            if d.lead_time_hours is not None and d.lead_time_hours >= 0
        ]
        org_lead_time = round(float(statistics.median(all_lead_times)), 2) if all_lead_times else None

        all_mttrs = [inc.mttr_hours for m in p_list for inc in m.incidents]
        org_mttr = round(float(statistics.median(all_mttrs)), 2) if all_mttrs else None

        org_df_tier = get_df_tier(org_df)
        org_lttc_tier = get_lttc_tier(org_lead_time)
        org_cfr_tier = get_cfr_tier(org_cfr)
        org_mttr_tier = get_mttr_tier(org_mttr)
        org_overall = compute_composite_tier([org_df_tier, org_lttc_tier, org_cfr_tier, org_mttr_tier])

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
            org_cfr,
            org_cfr_tier,
            org_mttr if org_mttr is not None else "N/A",
            org_mttr_tier,
        ])

    return repo_rows, org_rows, deployment_rows, recovery_rows
