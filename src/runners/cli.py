"""CLI command orchestrator for Actions self-hosted runners analytics."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..ai import add_ai_cli_arguments
from ..auth.github_app_auth import GitHubAppAuth
from ..common.client import ResilientGitHubClient
from ..common.inventory import (
    discover_app_organizations,
    discover_app_repositories,
)
from ..credentials import resolve_runtime_credentials
from .collector import build_runner_rows, collect_runners_and_metrics
from .models import ORG_RUNNER_HEADERS, RUNNER_HEADERS, RunnersConfig
from .reporter import write_runners_workbook

logger = logging.getLogger("repo360.runners")


def print_runners_scorecard(
    runner_rows: List[list],
    org_runner_rows: List[list],
    xlsx_path: Path,
    elapsed_seconds: float,
    org_runner_metrics: Optional[Dict[str, Any]] = None,
    failures: Optional[List[Tuple[str, str]]] = None,
) -> None:
    """Print an executive color-coded scorecard to stdout."""
    G = "\033[92m"
    Y = "\033[93m"
    R = "\033[91m"
    C = "\033[96m"
    B = "\033[1m"
    X = "\033[0m"

    total_runners = len(runner_rows)
    online_count = sum(1 for r in runner_rows if len(r) > 9 and str(r[9]).capitalize() == "Online")
    offline_count = sum(1 for r in runner_rows if len(r) > 9 and str(r[9]).capitalize() == "Offline")
    registered_count = online_count + offline_count
    busy_count = sum(1 for r in runner_rows if len(r) > 10 and str(r[10]).capitalize() == "Busy")
    ephemeral_count = sum(1 for r in runner_rows if len(r) > 8 and "Ephemeral" in str(r[8]))

    avg_wait_display = "N/A"
    p95_wait_display = "N/A"
    if org_runner_metrics:
        from .models import RunnerJobMetrics
        fleet_metrics = RunnerJobMetrics()
        for om in org_runner_metrics.values():
            fleet_metrics.merge(om)
        total_jobs = fleet_metrics.total_jobs
        total_compute_hours = fleet_metrics.total_compute_hours
        total_sla_breaches = fleet_metrics.queue_sla_breaches
        if isinstance(fleet_metrics.avg_queue_seconds, (int, float)):
            avg_wait_display = f"{fleet_metrics.avg_queue_seconds}s"
        if isinstance(fleet_metrics.p95_queue_seconds, (int, float)):
            p95_wait_display = f"{fleet_metrics.p95_queue_seconds}s"
    else:
        total_jobs = sum(o[10] for o in org_runner_rows if len(o) > 10 and isinstance(o[10], int)) if org_runner_rows else sum(
            r[13] for r in runner_rows if len(r) > 13 and isinstance(r[13], int)
        )
        total_compute_hours = sum(o[16] for o in org_runner_rows if len(o) > 16 and isinstance(o[16], (int, float))) if org_runner_rows else sum(
            r[19] for r in runner_rows if len(r) > 19 and isinstance(r[19], (int, float))
        )
        total_sla_breaches = sum(o[19] for o in org_runner_rows if len(o) > 19 and isinstance(o[19], int)) if org_runner_rows else sum(
            r[26] for r in runner_rows if len(r) > 26 and isinstance(r[26], int)
        )

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    if registered_count > 0:
        avail_pct = online_count / registered_count * 100
        avail_color = G if avail_pct >= 80 else (Y if avail_pct >= 50 else R)
        avail_str = f"{avail_color}{avail_pct:.1f}%{X}"
    elif total_runners > 0:
        avail_str = f"{C}N/A (Ephemeral Only){X}"
    else:
        avail_str = f"{Y}N/A{X}"

    fail_count = len(failures) if failures else 0

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 ACTIONS SELF-HOSTED RUNNER FLEET SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    print(f"   {B}Organizations Monitored :{X} {len(org_runner_rows)}")
    print(f"   {B}Total Runners in Fleet  :{X} {total_runners}")
    print(f"   {B}Online / Operational    :{X} {G}{online_count:<5} Online{X}")
    print(f"   {B}Offline / Unreachable   :{X} {R if offline_count else G}{offline_count:<5} Offline{X}")
    print(f"   {B}Currently Busy (Active) :{X} {Y}{busy_count:<5} Busy{X}")
    print(f"   {B}Autoscaling (Ephemeral) :{X} {ephemeral_count:<5} Ephemeral")
    print(f"   {B}Fleet Availability Rate :{X} {avail_str}")
    print(f"   {B}Workflow Jobs Executed  :{X} {B}{total_jobs:,}{X}")
    print(f"   {B}Total Compute Consumed  :{X} {total_compute_hours:.2f} Hours")
    print(f"   {B}Queue Wait (Avg / P95)  :{X} {avg_wait_display} / {p95_wait_display}")
    print(f"   {B}Queue SLA Delays (>60s) :{X} {R if total_sla_breaches else G}{total_sla_breaches} breaches{X}")
    if fail_count:
        print(f"   {B}Warnings / Errors       :{X} {Y}{fail_count} items (see workbook Summary){X}")
    print(f"   {B}Duration                :{X} {elapsed_str}")
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive Workbook (XLSX) :{X} {xlsx_path}")
    print(f"{C}{'=' * 76}{X}")
    print(f"{G}{B}   [SUCCESS] Runner analysis completed across {total_runners} runners in {elapsed_str}.{X}\n")


def run_runners_cli(argv: Optional[List[str]] = None) -> int:
    """Entry point for 'repo360 runners' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 runners",
        description="Inspect Actions self-hosted runner fleet usage, compute hours, and queue wait times.",
    )
    parser.add_argument(
        "--org",
        default="",
        help="Optional comma-separated list of organizations to analyze. If omitted, all installed organizations are analyzed.",
    )
    parser.add_argument(
        "--output-dir",
        default="./repo360-output",
        help="Target directory for generated Excel workbooks (default: ./repo360-output).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="Lookback analysis window in days (default: 90).",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived repositories in the analysis.",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=50,
        help="Maximum completed workflow runs to inspect per repository (default: 50).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=0,
        help="Concurrent repository worker threads (default: 0 = auto-tune up to 32).",
    )
    add_ai_cli_arguments(
        parser,
        help_text=(
            "Enable AI fleet advisory ('yes' or 'no', default: 'no'). "
            "Falls back to deterministic policy rules if AI is offline or unconfigured."
        ),
    )

    args = parser.parse_args(argv)
    from ..ai import init_ai_engine, resolve_ai_cli_flag
    ai_enabled = resolve_ai_cli_flag(args, parser)
    start_time = time.time()

    # 1. Resolve Credentials
    creds = resolve_runtime_credentials()
    app_id = creds.get("app_id")
    private_key = creds.get("private_key")
    api_base_url = creds.get("api_base_url", "https://api.github.com")

    if not app_id or not private_key:
        print("\n\033[91m[FAIL] Missing GitHub App credentials.\033[0m")
        print("Run 'repo360 setup' or configure REPO360_APP_ID and REPO360_APP_PRIVATE_KEY.\n")
        return 1

    # Initialize AI Engine (Multi-provider: Gemini, OpenAI, Claude)
    ai_engine = init_ai_engine(enable_ai=ai_enabled, creds=creds, log=logger)

    cfg = RunnersConfig(
        lookback_days=args.lookback_days,
        max_runner_runs_per_repo=args.max_runs,
        max_workers=args.max_workers,
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

    # 2. Resolve Repositories (Auto-discovery directly from GitHub App by default)
    targets: List[Tuple[str, str]] = []
    failures: List[Tuple[str, str]] = []
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    org_filter = [o.strip() for o in args.org.split(",") if o.strip()] if args.org else None
    discovered_orgs = discover_app_organizations(auth=auth, org_filter=org_filter)
    if not discovered_orgs:
        print("\033[91m[FAIL] No active GitHub App installations found for the specified organization(s).\033[0m\n")
        return 1

    org_list_str = ", ".join(discovered_orgs)
    print(f"\033[96m[INFO]\033[0m Discovered {len(discovered_orgs)} organization(s) with GitHub App installed: {org_list_str}")
    print(f"\033[96m[INFO]\033[0m Auto-discovering repositories directly from GitHub App installations...")
    targets, metadata_cache = discover_app_repositories(
        auth=auth,
        client=client,
        org_filter=org_filter,
        include_archived=args.include_archived,
        include_forks=True,
    )
    orgs = sorted(set(discovered_orgs) | {org for org, _ in targets}, key=str.lower)

    if not targets and not orgs:
        print("\033[91m[FAIL] No repositories or organizations discovered to analyze.\033[0m\n")
        return 1

    print(f"\033[96m[INFO]\033[0m Inspecting Actions runner fleet across {len(orgs)} organization(s)...")

    # Proactively check organization runner permissions
    for org_name in orgs:
        perms = auth.get_installation_permissions(org_name)
        if perms and perms.get("organization_self_hosted_runners") not in ("read", "write"):
            print(
                f"\033[93m[WARN]\033[0m GitHub App installation on '{org_name}' is missing: organization_self_hosted_runners: read.\n"
                f"       Accept updated permissions in GitHub:\n"
                f"       https://github.com/organizations/{org_name}/settings/installations"
            )

    runners, runner_metrics, org_runner_metrics = collect_runners_and_metrics(
        client, orgs, targets, since, end, cfg, failures, metadata_cache
    )
    runner_rows, org_runner_rows = build_runner_rows(runners, runner_metrics, org_runner_metrics, orgs)

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"{cfg.file_prefix}_{stamp}.xlsx"

    elapsed = time.time() - start_time
    write_runners_workbook(
        xlsx_path,
        cfg,
        since,
        end,
        runner_rows,
        org_runner_rows,
        failures,
        execution_duration=elapsed,
        ai_engine=ai_engine,
        org_runner_metrics=org_runner_metrics,
    )

    print_runners_scorecard(
        runner_rows=runner_rows,
        org_runner_rows=org_runner_rows,
        xlsx_path=xlsx_path,
        elapsed_seconds=elapsed,
        org_runner_metrics=org_runner_metrics,
        failures=failures,
    )
    return 0
