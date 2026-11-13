"""Collector for secret scanning push protection overrides and branch ruleset bypasses."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from ..common.timezones import LOCAL_TZ, LOCAL_TZ_NAME, fmt_timestamp, parse_ts
from ..common.client import ResilientGitHubClient
from ..common.concurrency import (
    AI_NOT_ENABLED_LABEL,
    AI_NOT_ENABLED_MESSAGE,
    is_ai_disabled,
    is_dormant_repo,
    is_empty_repo,
    resolve_worker_count,
    run_concurrent_ai,
)
from .models import (
    PLAN_LIMITATION_PUSH_MSG,
    PLAN_LIMITATION_RULESET_MSG,
    BypassConfig,
    PushProtectionBypassRecord,
    RepoBypassProfile,
    RulesetBypassRecord,
    classify_bypass_risk,
    is_plan_limitation,
)

logger = logging.getLogger("repo360.bypass")


def parse_github_api_error(err: Exception) -> Tuple[Optional[int], str, bool]:
    """Extract HTTP status, response message, and determine whether error is a GitHub Plan tier limitation."""
    resp = getattr(err, "response", None)
    if resp is None:
        return None, str(err), False
    status = getattr(resp, "status_code", None)
    msg = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("message", "")
    except Exception:
        msg = getattr(resp, "text", "") or ""

    msg_lower = msg.lower()
    is_plan = any(
        k in msg_lower
        for k in [
            "upgrade to github pro",
            "make this repository public",
            "advanced security",
            "secret scanning is not enabled",
            "plan",
        ]
    )
    return status, msg, is_plan


def parse_push_protection_alert(
    alert: Dict[str, Any],
    org: str,
    repo: str,
    since: datetime,
    ai_engine: Optional[Any] = None,
) -> Optional[PushProtectionBypassRecord]:
    """Parse a single secret scanning alert dict into a PushProtectionBypassRecord if bypassed within lookback."""
    is_bypassed = bool(alert.get("push_protection_bypassed"))
    if not is_bypassed:
        return None

    resolution = alert.get("resolution")
    alert_num = alert.get("number", 0)
    secret_type = alert.get("secret_type_display_name") or alert.get("secret_type", "Secret")
    html_url = alert.get("html_url") or f"https://github.com/{org}/{repo}/security/secret-scanning/{alert_num}"
    comment = alert.get("resolution_comment") or alert.get("push_protection_bypass_request_comment") or "-"

    bypassed_by = alert.get("push_protection_bypassed_by")
    resolved_by = alert.get("resolved_by")
    reviewer = alert.get("push_protection_bypass_request_reviewer")
    actor = (
        (bypassed_by.get("login") if isinstance(bypassed_by, dict) else str(bypassed_by))
        if bypassed_by else None
    ) or (
        (resolved_by.get("login") if isinstance(resolved_by, dict) else str(resolved_by))
        if resolved_by else None
    ) or (
        (reviewer.get("login") if isinstance(reviewer, dict) else str(reviewer))
        if reviewer else None
    ) or (
        (alert.get("actor") or {}).get("login") if isinstance(alert.get("actor"), dict) else None
    ) or (
        (alert.get("created_by") or {}).get("login") if isinstance(alert.get("created_by"), dict) else None
    ) or (
        (alert.get("user") or {}).get("login") if isinstance(alert.get("user"), dict) else None
    ) or "Unknown"

    bypassed_at_str = alert.get("push_protection_bypassed_at") or alert.get("created_at")
    ts = parse_ts(bypassed_at_str) if bypassed_at_str else None

    if ts and ts < since:
        return None

    if is_ai_disabled(ai_engine):
        risk = AI_NOT_ENABLED_LABEL
        advisory = AI_NOT_ENABLED_MESSAGE
    else:
        if not resolution or resolution in ("wont_fix", "revoked"):
            risk = "High Risk"
        elif resolution in ("false_positive", "used_in_tests") and comment != "-":
            risk = "Moderate Risk"
        else:
            risk = "Moderate Risk"
        advisory = f"Push protection was bypassed by {actor}. Review resolution comment: '{comment}'."
        if ai_engine is not None:
            risk, advisory = ai_engine.analyze_push_protection_bypass(
                secret_type=secret_type,
                comment=comment,
                actor=actor,
                repo=repo,
            )

    return PushProtectionBypassRecord(
        org=org,
        repo=repo,
        alert_number=alert_num,
        secret_type=secret_type,
        actor=actor,
        timestamp=ts,
        resolution=resolution or "Open / Unresolved",
        resolution_comment=comment,
        push_protection_bypassed=True,
        html_url=html_url,
        ai_risk_assessment=risk,
        ai_security_advisory=advisory,
    )


def collect_repo_push_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    ai_engine: Optional[Any] = None,
    errors_out: Optional[List[str]] = None,
) -> List[PushProtectionBypassRecord]:
    """Retrieve secret scanning alerts where push protection was explicitly bypassed."""
    records: List[PushProtectionBypassRecord] = []
    try:
        endpoint = f"/repos/{org}/{repo}/secret-scanning/alerts"
        for alert in client.paginate(org, endpoint):
            rec = parse_push_protection_alert(alert, org, repo, since, ai_engine=ai_engine)
            if rec is not None:
                records.append(rec)
    except requests.HTTPError as err:
        status, _, is_plan = parse_github_api_error(err)
        if status == 403:
            if is_plan:
                if errors_out is not None:
                    errors_out.append(PLAN_LIMITATION_PUSH_MSG)
            else:
                if errors_out is not None:
                    errors_out.append("Push protection alerts blocked (403 Forbidden — grant secret_scanning_alerts:read)")
        elif status != 404:
            logger.debug("Secret scanning alert query failed for %s/%s: %s", org, repo, err)
            if errors_out is not None:
                errors_out.append(f"Secret scanning alerts HTTP {status}")
    except Exception as err:
        logger.debug("Error collecting push protection bypasses for %s/%s: %s", org, repo, err)
        if errors_out is not None:
            errors_out.append(f"Secret scanning alert collection error: {err}")
    return records


def resolve_ruleset_metadata(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    ruleset_cache: Optional[Dict[str, Dict[int, Tuple[str, str]]]] = None,
) -> Dict[int, Tuple[str, str]]:
    """Fetch and cache ruleset definitions (Organization vs Repository scope) for a repository.

    Returns mapping of ruleset_id -> (source_type, ruleset_name), where source_type is
    'Organization' or 'Repository'.
    """
    repo_key = f"repo:{org.casefold()}/{repo.casefold()}"
    if ruleset_cache is not None and repo_key in ruleset_cache:
        return ruleset_cache[repo_key]

    meta_map: Dict[int, Tuple[str, str]] = {}

    # 1. Check or populate cached organization-level rulesets
    org_key = f"org:{org.casefold()}"
    if ruleset_cache is not None and org_key in ruleset_cache:
        meta_map.update(ruleset_cache[org_key])
    else:
        org_map: Dict[int, Tuple[str, str]] = {}
        try:
            org_rulesets = client.get(org, f"/orgs/{org}/rulesets")
            if isinstance(org_rulesets, list):
                for rs in org_rulesets:
                    if isinstance(rs, dict) and "id" in rs:
                        rs_id = rs["id"]
                        rs_name = rs.get("name") or f"Org Ruleset #{rs_id}"
                        org_map[rs_id] = ("Organization", rs_name)
        except Exception:
            pass
        if ruleset_cache is not None:
            ruleset_cache[org_key] = org_map
        meta_map.update(org_map)

    # 2. Query repository rulesets with includes_parents=true to capture both repo and inherited org rulesets
    try:
        repo_rulesets = client.get(org, f"/repos/{org}/{repo}/rulesets?includes_parents=true")
        if isinstance(repo_rulesets, list):
            for rs in repo_rulesets:
                if isinstance(rs, dict) and "id" in rs:
                    rs_id = rs["id"]
                    rs_name = rs.get("name") or f"Ruleset #{rs_id}"
                    src_type = (rs.get("source_type") or "").strip().lower()
                    if src_type in ("organization", "enterprise"):
                        scope = "Organization"
                    elif src_type == "repository":
                        scope = "Repository"
                    else:
                        scope = "Organization" if rs_id in meta_map else "Repository"
                    meta_map[rs_id] = (scope, rs_name)
    except Exception:
        pass

    if ruleset_cache is not None:
        ruleset_cache[repo_key] = meta_map

    return meta_map


def collect_repo_ruleset_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    errors_out: Optional[List[str]] = None,
    ruleset_cache: Optional[Dict[str, Dict[int, Tuple[str, str]]]] = None,
) -> List[RulesetBypassRecord]:
    """Retrieve branch and repository ruleset evaluation events where rules were bypassed."""
    records: List[RulesetBypassRecord] = []
    try:
        now = datetime.now(timezone.utc)
        delta_days = (now - since).total_seconds() / 86400.0
        if delta_days <= 1.05:
            time_period = "day"
        elif delta_days <= 7.05:
            time_period = "week"
        else:
            time_period = "month"

        ruleset_meta = resolve_ruleset_metadata(client, org, repo, ruleset_cache)

        endpoint = f"/repos/{org}/{repo}/rulesets/rule-suites"
        params = {"time_period": time_period, "per_page": 100}
        for suite in client.paginate(org, endpoint, params):
            ts_str = suite.get("pushed_at") or suite.get("created_at")
            if not ts_str:
                continue
            ts = parse_ts(ts_str)
            if ts < since:
                continue

            result = (suite.get("result") or "").lower()
            evaluations = suite.get("rule_evaluations") or []
            has_bypass = result == "bypass" or any((ev.get("result") or "").lower() == "bypass" for ev in evaluations)

            if not has_bypass:
                continue

            suite_id = suite.get("id", 0)
            actor = suite.get("actor_name") or "Unknown"
            raw_ref = suite.get("ref") or suite.get("ref_name") or "main"
            if raw_ref.startswith("refs/heads/"):
                ref_name = raw_ref[11:]
            elif raw_ref.startswith("refs/tags/"):
                ref_name = raw_ref[10:]
            elif raw_ref.startswith("refs/pull/"):
                ref_name = raw_ref[5:]
            elif raw_ref.startswith("refs/"):
                ref_name = raw_ref[5:]
            else:
                ref_name = raw_ref

            raw_after = suite.get("after_sha")
            raw_before = suite.get("before_sha")

            def _is_valid_sha(s: Optional[str]) -> bool:
                return bool(s and s != "PENDING" and not all(c == "0" for c in s))

            if _is_valid_sha(raw_after):
                sha = raw_after
            elif _is_valid_sha(raw_before):
                sha = raw_before
            else:
                sha = (raw_after if raw_after and raw_after != "PENDING" else raw_before) or ""

            commit_url = (
                f"https://github.com/{org}/{repo}/commit/{sha}"
                if _is_valid_sha(sha)
                else f"https://github.com/{org}/{repo}/rules/history"
            )

            # GitHub rule-suites list endpoint omits rule_evaluations; fetch detailed suite for bypasses
            if suite_id and (not evaluations or actor == "Unknown"):
                try:
                    detailed = client.get(org, f"/repos/{org}/{repo}/rulesets/rule-suites/{suite_id}")
                    if isinstance(detailed, dict):
                        if not evaluations:
                            evaluations = detailed.get("rule_evaluations") or []
                        if actor == "Unknown" and detailed.get("actor_name"):
                            actor = detailed["actor_name"]
                except Exception:
                    pass

            bypassed_names: List[str] = []
            suite_scopes: List[str] = []
            ruleset_names: List[str] = []

            for ev in evaluations:
                ev_res = (ev.get("result") or "").lower()
                if ev_res in ("bypass", "fail"):
                    rule_source = ev.get("rule_source") or {}
                    rs_type = (rule_source.get("type") or "").lower()
                    rs_id = rule_source.get("id")
                    rs_name = rule_source.get("name")

                    rule_type = ev.get("rule_type")
                    if rule_type and rule_type not in bypassed_names:
                        bypassed_names.append(rule_type)
                    elif rs_name and rs_name not in bypassed_names:
                        bypassed_names.append(rs_name)

                    # Classify ruleset scope and display name
                    if rs_type == "protected_branch":
                        suite_scopes.append("Repository")
                        if "Protected Branch" not in ruleset_names:
                            ruleset_names.append("Protected Branch")
                    elif rs_id and rs_id in ruleset_meta:
                        scope, name = ruleset_meta[rs_id]
                        suite_scopes.append(scope)
                        if name and name not in ruleset_names:
                            ruleset_names.append(name)
                    elif rs_type == "ruleset":
                        src_type = (rule_source.get("source_type") or ev.get("source_type") or "").lower()
                        if src_type in ("organization", "enterprise"):
                            suite_scopes.append("Organization")
                        else:
                            suite_scopes.append("Repository")
                        if rs_name and rs_name not in ruleset_names:
                            ruleset_names.append(rs_name)

            resolved_scope = "Organization" if "Organization" in suite_scopes else "Repository"
            resolved_ruleset_name = ", ".join(ruleset_names) if ruleset_names else "-"
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
                    html_url=commit_url,
                    ruleset_scope=resolved_scope,
                    ruleset_name=resolved_ruleset_name,
                )
            )
    except requests.HTTPError as err:
        status, _, is_plan = parse_github_api_error(err)
        if status == 403:
            if is_plan:
                if errors_out is not None:
                    errors_out.append(PLAN_LIMITATION_RULESET_MSG)
            else:
                if errors_out is not None:
                    errors_out.append("Ruleset rule-suites blocked (403 Forbidden — grant administration:read)")
        elif status != 404:
            logger.debug("Ruleset rule-suites query failed for %s/%s: %s", org, repo, err)
            if errors_out is not None:
                errors_out.append(f"Ruleset rule-suites HTTP {status}")
    except Exception as err:
        logger.debug("Error collecting ruleset bypasses for %s/%s: %s", org, repo, err)
        if errors_out is not None:
            errors_out.append(f"Ruleset rule-suites error: {err}")
    return records


def profile_repo_bypasses(
    client: ResilientGitHubClient,
    org: str,
    repo: str,
    since: datetime,
    meta: Dict[str, Any],
    ai_engine: Optional[Any] = None,
    check_push: bool = True,
    skip_rulesets: bool = False,
    org_push_bypasses: Optional[Dict[str, Dict[str, List[PushProtectionBypassRecord]]]] = None,
    ruleset_cache: Optional[Dict[str, Dict[int, Tuple[str, str]]]] = None,
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

    if not check_push:
        profile.errors.append("Secret scanning alerts disabled/unpermitted on org App installation (secret_scanning_alerts:read missing)")
    elif org_push_bypasses is not None and org.lower() in org_push_bypasses:
        profile.push_bypasses = org_push_bypasses[org.lower()].get(repo.casefold(), [])
    elif not push_disabled_in_meta:
        profile.push_bypasses = collect_repo_push_bypasses(
            client, org, repo, since, ai_engine=ai_engine, errors_out=profile.errors
        )
    if not is_dormant_repo(meta, since, parse_ts):
        if skip_rulesets:
            profile.errors.append(PLAN_LIMITATION_RULESET_MSG)
        else:
            profile.ruleset_bypasses = collect_repo_ruleset_bypasses(
                client, org, repo, since, errors_out=profile.errors, ruleset_cache=ruleset_cache
            )
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
    free_plan_orgs: Set[str] = set()

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

    for org_name in orgs:
        try:
            org_meta = client.get(org_name, f"/orgs/{org_name}")
            if isinstance(org_meta, dict):
                plan_name = ((org_meta.get("plan") or {}).get("name") or "").lower()
                if plan_name == "free":
                    free_plan_orgs.add(org_name.lower())
                    logger.info(
                        "Organization '%s' is on GitHub Free plan: branch rulesets on private repositories require GitHub Team or Enterprise Cloud.",
                        org_name,
                    )
        except Exception:
            pass

    # Shared cache for ruleset definitions across all target evaluations
    ruleset_cache: Dict[str, Dict[int, Tuple[str, str]]] = {}
    for org_name in orgs:
        org_key = f"org:{org_name.casefold()}"
        org_map: Dict[int, Tuple[str, str]] = {}
        try:
            org_rulesets = client.get(org_name, f"/orgs/{org_name}/rulesets")
            if isinstance(org_rulesets, list):
                for rs in org_rulesets:
                    if isinstance(rs, dict) and "id" in rs:
                        rs_id = rs["id"]
                        rs_name = rs.get("name") or f"Org Ruleset #{rs_id}"
                        org_map[rs_id] = ("Organization", rs_name)
        except Exception:
            pass
        ruleset_cache[org_key] = org_map

    org_push_bypasses: Dict[str, Dict[str, List[PushProtectionBypassRecord]]] = {}
    for org_name in orgs:
        org_targets = [t for t in targets if t[0].lower() == org_name.lower()]
        if len(org_targets) > 1 and push_allowed_by_org.get(org_name.lower(), True):
            try:
                org_endpoint = f"/orgs/{org_name}/secret-scanning/alerts"
                org_map: Dict[str, List[PushProtectionBypassRecord]] = defaultdict(list)
                for alert in client.paginate(org_name, org_endpoint):
                    repo_dict = alert.get("repository") or {}
                    repo_name = repo_dict.get("name") or alert.get("repo")
                    if not repo_name and len(org_targets) == 1:
                        repo_name = org_targets[0][1]
                    if not repo_name:
                        continue
                    rec = parse_push_protection_alert(alert, org_name, repo_name, since, ai_engine=None)
                    if rec is not None:
                        org_map[repo_name.casefold()].append(rec)
                org_push_bypasses[org_name.lower()] = org_map
                logger.info(
                    "Organization '%s': retrieved organization-level secret scanning alerts across all repositories.",
                    org_name,
                )
            except Exception as e:
                logger.debug(
                    "Org-level secret scanning alerts query failed for %s (falling back to per-repo): %s",
                    org_name,
                    e,
                )

    if targets:
        workers = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32)
        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            future_to_target = {}
            for org, repo in targets:
                meta = metadata_cache.get((org.casefold(), repo.casefold()), {})
                can_check_push = push_allowed_by_org.get(org.lower(), True)
                is_private = bool(meta.get("private", True)) if isinstance(meta, dict) else True
                skip_rulesets = is_private and (org.lower() in free_plan_orgs)
                fut = executor.submit(
                    profile_repo_bypasses,
                    client,
                    org,
                    repo,
                    since,
                    meta,
                    None,
                    check_push=can_check_push,
                    skip_rulesets=skip_rulesets,
                    org_push_bypasses=org_push_bypasses,
                    ruleset_cache=ruleset_cache,
                )
                future_to_target[fut] = (org, repo)

            from ..common.interrupts import iter_completed
            for fut in iter_completed(future_to_target):
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

    if is_ai_disabled(ai_engine):
        for rec in (rec for p in profiles for rec in p.push_bypasses):
            rec.ai_risk_assessment = AI_NOT_ENABLED_LABEL
            rec.ai_security_advisory = AI_NOT_ENABLED_MESSAGE
    elif ai_engine is not None:
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


def collapse_plan_limitations(
    profiles: List[RepoBypassProfile],
) -> Tuple[List[RepoBypassProfile], List[Tuple[str, str]]]:
    """Separate org-scoped plan limitations from repo profiles, preventing false failure attribution.

    Returns:
        Tuple of (cleaned_profiles, failures_list) where failures_list includes individual repo
        errors and single aggregated entries for organization-scoped plan tier restrictions.
    """
    failures: List[Tuple[str, str]] = []
    seen_failures: Set[Tuple[str, str]] = set()
    org_plan_limitations: Set[Tuple[str, str]] = set()

    for p in profiles:
        clean_errors: List[str] = []
        for err in p.errors:
            if is_plan_limitation(err):
                org_plan_limitations.add((f"{p.org} (Private Repositories)", err))
            else:
                clean_errors.append(err)
                fail_key = (f"{p.org}/{p.repo}", err)
                if fail_key not in seen_failures:
                    seen_failures.add(fail_key)
                    failures.append(fail_key)
        p.errors = clean_errors

    for scope, err in sorted(org_plan_limitations):
        failures.append((scope, err))

    return profiles, failures


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
        lambda: {
            "push": 0,
            "org_ruleset": 0,
            "repo_ruleset": 0,
            "ruleset": 0,
            "secrets": Counter(),
            "last_ts": None,
        }
    )

    for p in profiles:
        org_profiles.setdefault(p.org, []).append(p)

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

            acc = actor_stats[(p.org, push.actor.casefold())]
            acc.setdefault("display", push.actor)
            acc["push"] += 1
            acc["secrets"][push.secret_type] += 1
            if push.timestamp:
                acc["last_ts"] = max(acc["last_ts"], push.timestamp) if acc["last_ts"] else push.timestamp

        for rule in p.ruleset_bypasses:
            ts_ist = fmt_timestamp(rule.timestamp)
            rule_url = rule.html_url or (
                f"https://github.com/{rule.org}/{rule.repo}/commit/{rule.commit_sha}"
                if rule.commit_sha and rule.commit_sha != "-"
                else f"https://github.com/{rule.org}/{rule.repo}/rules/history"
            )
            ruleset_rows.append([
                rule.org,
                rule.repo,
                rule.rule_suite_id,
                rule.ref,
                rule.actor,
                rule.ruleset_scope,
                rule.ruleset_name,
                rule.evaluation_result.capitalize(),
                rule.bypassed_rules,
                rule.commit_sha,
                ts_ist,
                rule_url,
            ])

            acc = actor_stats[(p.org, rule.actor.casefold())]
            acc.setdefault("display", rule.actor)
            acc["ruleset"] += 1
            if (rule.ruleset_scope or "").lower() == "organization":
                acc["org_ruleset"] += 1
            else:
                acc["repo_ruleset"] += 1
            if rule.timestamp:
                acc["last_ts"] = max(acc["last_ts"], rule.timestamp) if acc["last_ts"] else rule.timestamp

    for (org, _actor_key), stat in sorted(actor_stats.items(), key=lambda item: -(item[1]["push"] + item[1]["ruleset"])):
        top_sec = stat["secrets"].most_common(1)[0][0] if stat["secrets"] else "-"
        last_ist = fmt_timestamp(stat["last_ts"])
        total = stat["push"] + stat["ruleset"]
        actor_rows.append([
            org,
            stat.get("display", _actor_key),
            stat["push"],
            stat["org_ruleset"],
            stat["repo_ruleset"],
            stat["ruleset"],
            total,
            top_sec,
            last_ist,
        ])

    for org in orgs:
        p_list = org_profiles.get(org, [])
        total_push = sum(len(p.push_bypasses) for p in p_list)
        total_org_ruleset = sum(p.org_ruleset_bypasses for p in p_list)
        total_repo_ruleset = sum(p.repo_ruleset_bypasses for p in p_list)
        total_ruleset = total_org_ruleset + total_repo_ruleset
        total_incidents = total_push + total_ruleset
        unique_org_actors = set()
        sec_counter = Counter()
        org_had_errors = False

        for p in p_list:
            unique_org_actors.update(a.casefold() for a in p.unique_actors)
            if any(e for e in p.errors if not is_plan_limitation(e)):
                org_had_errors = True
            for push in p.push_bypasses:
                sec_counter[push.secret_type] += 1

        top_secret = sec_counter.most_common(1)[0][0] if sec_counter else "None"

        if total_incidents == 0 and org_had_errors:
            risk_tier = "Unknown (Partial)"
        else:
            risk_tier = classify_bypass_risk(total_push, total_incidents)

        org_rows.append([
            org,
            len(p_list),
            total_push,
            total_org_ruleset,
            total_repo_ruleset,
            total_ruleset,
            total_incidents,
            len(unique_org_actors),
            top_secret,
            risk_tier,
        ])

    return push_rows, ruleset_rows, org_rows, actor_rows
