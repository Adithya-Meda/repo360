"""CLI command orchestrator for Repo360 Push Protection & Ruleset Bypass Ledger."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..auth.github_app_auth import GitHubAppAuth
from ..common.cli import add_common_scan_arguments
from ..common.client import ResilientGitHubClient
from ..common.concurrency import format_ai_scorecard_status
from ..common.inventory import (
    discover_app_organizations,
    discover_app_repositories,
    resolve_scan_targets,
)
from ..credentials import resolve_runtime_credentials
from .collector import build_bypass_rows, collect_bypass_data, collapse_plan_limitations
from .models import BypassConfig, classify_bypass_risk, is_plan_limitation
from .reporter import write_bypass_workbook

logger = logging.getLogger("repo360.bypass")


def print_bypass_scorecard(
    push_rows: List[list],
    ruleset_rows: List[list],
    org_rows: List[list],
    actor_rows: List[list],
    xlsx_path: Path,
    elapsed_seconds: float,
    lookback_days: int,
    unaudited_count: int = 0,
    plan_limitation_count: int = 0,
    ai_engine: Optional[Any] = None,
) -> None:
    """Print an executive color-coded Bypass compliance scorecard to stdout."""
    G = "\033[92m"
    Y = "\033[93m"
    R = "\033[91m"
    C = "\033[96m"
    B = "\033[1m"
    X = "\033[0m"

    total_push = len(push_rows)
    total_ruleset = len(ruleset_rows)
    total_incidents = total_push + total_ruleset
    unique_actors = len(actor_rows)

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    risk_tier = classify_bypass_risk(total_push, total_incidents)
    incident_color = R if risk_tier == "High" else (Y if total_incidents > 0 or risk_tier == "Medium" else G)

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 PUSH PROTECTION & RULESET BYPASS LEDGER SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    retention_note = " (Push Protection: up to 365d; Branch Rulesets: max 30d API retention)" if lookback_days > 30 else ""
    print(f"   {B}Organizations Monitored        :{X} {len(org_rows)}")
    print(f"   {B}Analysis Lookback Window       :{X} {lookback_days} Days{retention_note}")
    print(f"   {B}Push Protection Bypasses       :{X} {R if total_push else G}{total_push} overrides{X}")
    print(f"   {B}Branch Ruleset Bypasses        :{X} {Y if total_ruleset else G}{total_ruleset} policy overrides{X}")
    print(f"   {B}Total Policy Bypass Incidents  :{X} {incident_color}{B}{total_incidents}{X}")
    print(f"   {B}Unique Bypassing Actors        :{X} {unique_actors}")
    if unaudited_count > 0:
        print(f"   {B}Unaudited / Permission Gaps    :{X} {Y}{unaudited_count} signal check(s) skipped (403/missing permission){X}")
    if plan_limitation_count > 0:
        print(f"   {B}GitHub Plan Limitations        :{X} {C}{plan_limitation_count} org-scoped plan restriction(s) (requires Team/GHEC){X}")
    print(f"   {B}AI Advisory Engine             :{X} {format_ai_scorecard_status(ai_engine)}")
    print(f"   {B}Duration                       :{X} {elapsed_str}")
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive Compliance Ledger    :{X} {xlsx_path}")
    print(f"{C}{'=' * 76}{X}")
    print(f"{G}{B}   [SUCCESS] Bypass ledger generated across {len(org_rows)} orgs in {elapsed_str}.{X}\n")


def run_bypass_cli(argv: Optional[List[str]] = None) -> int:
    """Entry point for 'repo360 bypass' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 bypass",
        description="Audit secret push-protection overrides and branch ruleset bypass records.",
    )
    add_common_scan_arguments(
        parser,
        ai_subject="risk assessment",
        include_lookback=False,
    )
    from ..common.cli import parse_lookback_arg
    parser.add_argument(
        "--lookback-days",
        type=parse_lookback_arg,
        default=30,
        metavar="DAYS",
        help=(
            "Lookback window in days (1-365; default: 30). "
            "Push protection alerts cover up to 365 days; GitHub branch ruleset logs are retained by GitHub for up to 30 days."
        ),
    )

    args = parser.parse_args(argv)
    from ..ai import init_ai_engine, resolve_ai_cli_flag
    ai_enabled = resolve_ai_cli_flag(args, parser)
    start_time = time.time()

    creds = resolve_runtime_credentials()
    app_id = creds.get("app_id")
    private_key = creds.get("private_key")
    api_base_url = creds.get("api_base_url", "https://api.github.com")

    if not app_id or not private_key:
        print("\n\033[91m[FAIL] Missing GitHub App credentials.\033[0m")
        print("Run 'repo360 setup' or configure REPO360_APP_ID and REPO360_APP_PRIVATE_KEY.\n")
        return 1

    ai_engine = init_ai_engine(enable_ai=ai_enabled, creds=creds, log=logger)

    cfg = BypassConfig(
        lookback_days=args.lookback_days,
        max_workers=args.workers,
        include_archived=args.include_archived,
        output_dir=args.output_dir,
    )

    auth = GitHubAppAuth(
        app_id=app_id,
        private_key=private_key,
        api_base_url=api_base_url,
    )
    client = ResilientGitHubClient(auth=auth)

    end = datetime.now(timezone.utc)
    since = end - timedelta(days=cfg.lookback_days)
    stamp = end.strftime("%Y%m%d_%H%M%S")

    org_filter = [o.strip() for o in args.org.split(",") if o.strip()] if args.org else None
    discovered_orgs = discover_app_organizations(auth=auth, org_filter=org_filter)
    if not discovered_orgs:
        print("\033[91m[FAIL] No active GitHub App installations found for the specified organization(s).\033[0m\n")
        return 1

    org_list_str = ", ".join(discovered_orgs)
    print(f"\033[96m[INFO]\033[0m Discovered {len(discovered_orgs)} organization(s) with GitHub App installed: {org_list_str}")
    repos_file_arg = getattr(args, "repos_file", "") or ("repos.csv" if os.path.isfile("repos.csv") else "")
    if repos_file_arg:
        print(f"\033[96m[INFO]\033[0m Resolving repositories from inventory {repos_file_arg}...")
    else:
        print("\033[96m[INFO]\033[0m Auto-discovering repositories directly from GitHub App installations...")
    targets, metadata_cache = resolve_scan_targets(
        auth=auth,
        client=client,
        repos_file=repos_file_arg or None,
        org_filter=org_filter,
        include_archived=args.include_archived,
        include_forks=True,
    )
    orgs = sorted(set(discovered_orgs) | {org for org, _ in targets}, key=str.lower)

    if not targets and not orgs:
        print("\033[91m[FAIL] No repositories or organizations discovered to analyze.\033[0m\n")
        return 1

    for org_name in orgs:
        perms = auth.get_installation_permissions(org_name)
        if perms and perms.get("secret_scanning_alerts") not in ("read", "write"):
            print(
                f"\033[93m[WARN]\033[0m GitHub App on '{org_name}' is missing: secret_scanning_alerts: read.\n"
                f"       Push protection bypass detection requires 'secret_scanning_alerts: read' permission.\n"
                f"       1. In GitHub App Settings -> Permissions & events -> Repository permissions -> set 'Secret scanning alerts' to Read-only.\n"
                f"       2. Accept updated permissions at: https://github.com/organizations/{org_name}/settings/installations\n"
            )

    print(f"\033[96m[INFO]\033[0m Auditing push protection exemptions and branch ruleset overrides across {len(orgs)} org(s)...")

    profiles = collect_bypass_data(client, orgs, targets, since, cfg, metadata_cache, ai_engine=ai_engine)
    profiles, failures = collapse_plan_limitations(profiles)
    push_rows, ruleset_rows, org_rows, actor_rows = build_bypass_rows(profiles, orgs)

    real_gaps = [f for f in failures if not is_plan_limitation(f[1])]
    plan_gaps = [f for f in failures if is_plan_limitation(f[1])]
    for scope, err in plan_gaps:
        print(f"\033[96m[INFO]\033[0m {scope}: {err}")

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"{cfg.file_prefix}_{stamp}.xlsx"

    elapsed = time.time() - start_time
    write_bypass_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=since,
        end=end,
        push_rows=push_rows,
        ruleset_rows=ruleset_rows,
        org_rows=org_rows,
        actor_rows=actor_rows,
        failures=failures,
        execution_duration=elapsed,
        ai_engine=ai_engine,
    )

    print_bypass_scorecard(
        push_rows=push_rows,
        ruleset_rows=ruleset_rows,
        org_rows=org_rows,
        actor_rows=actor_rows,
        xlsx_path=xlsx_path,
        elapsed_seconds=elapsed,
        lookback_days=cfg.lookback_days,
        unaudited_count=len(real_gaps),
        plan_limitation_count=len(plan_gaps),
        ai_engine=ai_engine,
    )
    return 0
