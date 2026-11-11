"""Collector for secret scanning push protection overrides and branch ruleset bypasses."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ..activity.models import IST, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import is_dormant_repo, is_empty_repo, resolve_worker_count, run_concurrent_ai
from .models import (
    BypassConfig,
    PushProtectionBypassRecord,
    RepoBypassProfile,
    RulesetBypassRecord,
)

logger = logging.getLogger("repo360.bypass")


def collect_repo_push_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    ai_engine: Optional[Any] = None,
) -> List[PushProtectionBypassRecord]:
    """Retrieve secret scanning alerts with push protection bypass flags or resolved exemptions."""
    records: List[PushProtectionBypassRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/secret-scanning/alerts"
        # Query both open and resolved alerts
        for alert in client.paginate(org, endpoint, {"state": "all"}):
            is_bypassed = bool(alert.get("push_protection_bypassed"))
            resolution = alert.get("resolution")

            if not is_bypassed and not resolution:
                continue

            alert_num = alert.get("number", 0)
            secret_type = alert.get("secret_type_display_name") or alert.get("secret_type", "Secret")
            html_url = alert.get("html_url", "")
            comment = alert.get("resolution_comment") or "-"

            # Resolve bypass actor and timestamp
            bypassed_by = alert.get("push_protection_bypassed_by") or {}
            resolved_by = alert.get("resolved_by") or {}
            actor = bypassed_by.get("login") or resolved_by.get("login") or "Unknown"

            bypassed_at_str = alert.get("push_protection_bypassed_at") or alert.get("resolved_at") or alert.get("created_at")
            ts = parse_ts(bypassed_at_str) if bypassed_at_str else None

            if ts and ts < since:
                continue

            # Assess risk using AI engine or deterministic policy defaults
            risk = "Moderate Risk"
            advisory = f"Push protection was bypassed by {actor}. Review resolution comment: '{comment}'."
            if ai_engine is not None:
                risk, advisory = ai_engine.analyze_push_protection_bypass(
                    secret_type=secret_type,
                    comment=comment,
                    actor=actor,
                    repo=repo,
                )

            records.append(
                PushProtectionBypassRecord(
                    org=org,
                    repo=repo,
                    alert_number=alert_num,
                    secret_type=secret_type,
                    actor=actor,
                    timestamp=ts,
                    resolution=resolution or "Push Override",
                    resolution_comment=comment,
                    push_protection_bypassed=is_bypassed,
                    html_url=html_url,
                    ai_risk_assessment=risk,
                    ai_security_advisory=advisory,
                )
            )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Secret scanning alert query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error collecting push protection bypasses for %s/%s: %s", org, repo, err)
    return records


def collect_repo_ruleset_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
) -> List[RulesetBypassRecord]:
    """Retrieve branch and repository ruleset evaluation events where rules were bypassed."""
    records: List[RulesetBypassRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/rulesets/rule-suites"
        for suite in client.paginate(org, endpoint):
            created_str = suite.get("created_at")
            if not created_str:
                continue
            ts = parse_ts(created_str)
            if ts < since:
                break

            result = (suite.get("result") or "").lower()
            evaluations = suite.get("rule_evaluations") or []
            has_bypass = result == "bypass" or any((ev.get("result") or "").lower() == "bypass" for ev in evaluations)

            if not has_bypass:
                continue

            suite_id = suite.get("id", 0)
            actor = suite.get("actor_name") or "Unknown"
            ref_name = suite.get("ref_name") or "main"
            sha = suite.get("after_sha") or suite.get("before_sha") or ""

            bypassed_names = [ev.get("rule_type", "Rule") for ev in evaluations if (ev.get("result") or "").lower() == "bypass"]
            bypassed_rules_str = ", ".join(bypassed_names) if bypassed_names else "Policy Bypass Permission"

            records.append(
                RulesetBypassRecord(
                    org=org,
                    repo=repo,
                    rule_suite_id=suite_id,
                    ref=ref_name,
                    actor=actor,
                    evaluation_result="bypass",
                    bypassed_rules=bypassed_rules_str,
                    commit_sha=sha[:10] if sha else "-",
                    timestamp=ts,
                )
            )
    except requests.HTTPError as err:
        if err.response.status_code not in (404, 403):
            logger.debug("Ruleset rule-suites query failed for %s/%s: %s", org, repo, err)
    except Exception as err:
        logger.debug("Error collecting ruleset bypasses for %s/%s: %s", org, repo, err)
    return records


def profile_repo_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    meta: Dict[str, Any],
    ai_engine: Optional[Any] = None,
    check_push: bool = True,
) -> RepoBypassProfile:
    """Collect push protection overrides and ruleset bypasses for a repository."""
    profile = RepoBypassProfile(org=org, repo=repo)
    if is_empty_repo(meta):
        return profile

    push_disabled_in_meta = False
    sec_cfg = meta.get("security_and_analysis") if isinstance(meta, dict) else None
    if isinstance(sec_cfg, dict):
        ss_status = (sec_cfg.get("secret_scanning") or {}).get("status")
        pp_status = (sec_cfg.get("secret_scanning_push_protection") or {}).get("status")
        if ss_status == "disabled" and pp_status == "disabled":
            push_disabled_in_meta = True

    if check_push and not push_disabled_in_meta:
        profile.push_bypasses = collect_repo_push_bypasses(client, org, repo, since, ai_engine=ai_engine)
    if not is_dormant_repo(meta, since, parse_ts):
        profile.ruleset_bypasses = collect_repo_ruleset_bypasses(client, org, repo, since)
    return profile


def collect_bypass_data(
    client: ResilientGitHubClient,
    orgs: List[str],
    targets: List[Tuple[str, str]],
    since: datetime,
    cfg: BypassConfig,
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]],
    ai_engine: Optional[Any] = None,
) -> List[RepoBypassProfile]:
    """Collect bypass records across all targets using thread pooling and concurrent AI risk synthesis."""
    profiles: List[RepoBypassProfile] = []

    push_allowed_by_org: Dict[str, bool] = {}
    auth = getattr(client, "auth", None)
    get_perms = getattr(auth, "get_installation_permissions", None)
    if callable(get_perms):
        for org_name in orgs:
            try:
                perms = get_perms(org_name)
                if isinstance(perms, dict) and perms:
                    push_allowed_by_org[org_name.lower()] = perms.get("secret_scanning_alerts") in ("read", "write")
            except Exception:
                pass

    if targets:
        workers = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32)
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                can_check_push = push_allowed_by_org.get(org.lower(), True)
                fut = executor.submit(
                    profile_repo_bypasses,
                    client,
                    org,
                    repo,
                    since,
                    meta,
                    None,
                    check_push=can_check_push,
                )
                future_to_target[fut] = (org, repo)

            for fut in as_completed(future_to_target):
                org, repo = future_to_target[fut]
                try:
                    p = fut.result()
                    profiles.append(p)
                except Exception as err:
                    logger.warning("Error collecting bypasses for %s/%s: %s", org, repo, err)
                    profiles.append(RepoBypassProfile(org=org, repo=repo, errors=[str(err)]))
        except (KeyboardInterrupt, SystemExit):
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Batch AI risk analysis concurrently after HTTP scraping completes
    if ai_engine is not None:
        all_push_records = [rec for p in profiles for rec in p.push_bypasses]

        def _enrich_push(rec: PushProtectionBypassRecord) -> None:
            risk, advisory = ai_engine.analyze_push_protection_bypass(
                secret_type=rec.secret_type,
                comment=rec.resolution_comment,
                actor=rec.actor,
                repo=rec.repo,
            )
            rec.ai_risk_assessment = risk
            rec.ai_security_advisory = advisory

        run_concurrent_ai(all_push_records, _enrich_push, ai_engine=ai_engine, max_workers=8)

    profiles.sort(key=lambda p: (p.org.lower(), p.repo.lower()))
    return profiles


def build_bypass_rows(
    profiles: List[RepoBypassProfile],
    orgs: List[str],
) -> Tuple[List[list], List[list], List[list], List[list]]:
    """Build structured table rows for Push Protection, Ruleset, Org, and Actor sheets."""
    push_rows: List[list] = []
    ruleset_rows: List[list] = []
    org_rows: List[list] = []
    actor_rows: List[list] = []

    org_profiles: Dict[str, List[RepoBypassProfile]] = {org: [] for org in orgs}

    actor_stats: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"push": 0, "ruleset": 0, "secrets": Counter(), "last_ts": None}
    )

    for p in profiles:
        org_profiles.setdefault(p.org, []).append(p)

        # Push protection bypass rows
        for push in p.push_bypasses:
            ts_ist = fmt_timestamp(push.timestamp)
            override_flag = "Yes" if push.push_protection_bypassed else "Resolved Alert"
            push_rows.append([
                push.org,
                push.repo,
                push.alert_number,
                push.secret_type,
                push.actor,
                ts_ist,
                push.resolution,
                push.resolution_comment,
                push.ai_risk_assessment,
                push.ai_security_advisory,
                override_flag,
                push.html_url or "-",
            ])

            # Actor accumulation
            acc = actor_stats[(p.org, push.actor)]
            acc["push"] += 1
            acc["secrets"][push.secret_type] += 1
            if push.timestamp:
                acc["last_ts"] = max(acc["last_ts"], push.timestamp) if acc["last_ts"] else push.timestamp

        # Ruleset bypass rows
        for rule in p.ruleset_bypasses:
            ts_ist = fmt_timestamp(rule.timestamp)
            ruleset_rows.append([
                rule.org,
                rule.repo,
                rule.rule_suite_id,
                rule.ref,
                rule.actor,
                rule.evaluation_result.capitalize(),
                rule.bypassed_rules,
                rule.commit_sha,
                ts_ist,
            ])

            # Actor accumulation
            acc = actor_stats[(p.org, rule.actor)]
            acc["ruleset"] += 1
            if rule.timestamp:
                acc["last_ts"] = max(acc["last_ts"], rule.timestamp) if acc["last_ts"] else rule.timestamp

    # Actor rows
    for (org, actor), stat in sorted(actor_stats.items(), key=lambda item: -(item[1]["push"] + item[1]["ruleset"])):
        top_sec = stat["secrets"].most_common(1)[0][0] if stat["secrets"] else "-"
        last_ist = fmt_timestamp(stat["last_ts"])
        total = stat["push"] + stat["ruleset"]
        actor_rows.append([
            org,
            actor,
            stat["push"],
            stat["ruleset"],
            total,
            top_sec,
            last_ist,
        ])

    # Org rows
    for org in orgs:
        p_list = org_profiles.get(org, [])
        total_push = sum(len(p.push_bypasses) for p in p_list)
        total_ruleset = sum(len(p.ruleset_bypasses) for p in p_list)
        total_incidents = total_push + total_ruleset
        unique_org_actors = set()
        sec_counter = Counter()

        for p in p_list:
            unique_org_actors.update(p.unique_actors)
            for push in p.push_bypasses:
                sec_counter[push.secret_type] += 1

        top_secret = sec_counter.most_common(1)[0][0] if sec_counter else "None"

        risk_tier = "Low"
        if total_push > 10 or total_incidents > 25:
            risk_tier = "High"
        elif total_push > 0 or total_incidents > 5:
            risk_tier = "Medium"

        org_rows.append([
            org,
            len(p_list),
            total_push,
            total_ruleset,
            total_incidents,
            len(unique_org_actors),
            top_secret,
            risk_tier,
        ])

    return push_rows, ruleset_rows, org_rows, actor_rows
