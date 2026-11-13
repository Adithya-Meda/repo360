"""CLI command orchestrator for Repo360 DORA & Flow Metrics."""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

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
from .collector import build_dora_rows, collect_dora_data
from .models import DORAConfig, compute_fleet_dora_summary
from .reporter import write_dora_workbook

logger = logging.getLogger("repo360.dora")


def print_dora_scorecard(
    repo_rows: List[list],
    org_rows: List[list],
    deployment_rows: List[list],
    recovery_rows: List[list],
    xlsx_path: Path,
    elapsed_seconds: float,
    lookback_days: int,
    failures: Optional[List[Tuple[str, str]]] = None,
    ai_engine: Optional[Any] = None,
) -> None:
    """Print an executive color-coded DORA metrics scorecard to stdout."""
    G = "\033[92m"
    Y = "\033[93m"
    R = "\033[91m"
    C = "\033[96m"
    B = "\033[1m"
    X = "\033[0m"

    summary_data = compute_fleet_dora_summary(
        repo_rows,
        lookback_days,
        deployment_rows=deployment_rows,
        recovery_rows=recovery_rows,
    )
    total_repos = summary_data["total_repos"]
    active_repos = summary_data["active_repos"]
    inactive_repos = summary_data["inactive_repos"]
    total_deploys = summary_data["total_deploys"]
    total_successful = summary_data["total_successful"]
    code_change_deploys = summary_data["code_change_deploys"]
    no_op_deploys = summary_data["no_op_deploys"]
    stalled_deploys = summary_data["stalled_deploys"]
    collapsed_retries = summary_data["collapsed_retries"]
    lttc_coverage = summary_data.get("lttc_coverage_pct")
    effective_failed = summary_data["effective_failed"]
    median_df_weekly = summary_data["median_df_weekly"]
    total_df_weekly = summary_data["total_df_weekly"]
    median_lttc = summary_data["median_lttc_hours"]
    cfr_overall = summary_data["cfr_overall"]
    median_mttr = summary_data["median_mttr_hours"]
    rework_overall = summary_data["rework_overall"]
    fleet_tier = summary_data["fleet_tier"]
    open_incidents = summary_data["open_incidents"]
    permission_gap_repos = summary_data["permission_gap_repos"]
    resolved_count = sum(1 for r in recovery_rows if len(r) > 7 and r[7] == "Resolved")

    tier_colors = {"Elite": G, "High": G, "Medium": Y, "Low": R, "N/A": X}
    t_color = tier_colors.get(fleet_tier, X)

    df_str = (
        f"{median_df_weekly:.2f} / week per active repo (fleet total {total_df_weekly:.2f}/wk)"
        if median_df_weekly is not None
        else "N/A (no production deployments)"
    )
    cov_suffix = f" ({lttc_coverage:.1f}% coverage)" if lttc_coverage is not None else ""
    lttc_str = f"{median_lttc:.1f} hrs{cov_suffix}" if median_lttc is not None else "N/A"
    cfr_color = R if (cfr_overall is not None and cfr_overall > 20.0) else (
        Y if (cfr_overall is not None and cfr_overall > 10.0) else G
    )
    cfr_str = (
        f"{cfr_color}{cfr_overall:.1f}%{X} ({effective_failed} failed changes)"
        if cfr_overall is not None
        else "N/A"
    )
    mttr_str = f"{median_mttr:.1f} hrs" if median_mttr is not None else "N/A"
    rework_color = R if (rework_overall is not None and rework_overall > 15.0) else (
        Y if (rework_overall is not None and rework_overall > 8.0) else G
    )
    rework_str = f"{rework_color}{rework_overall:.1f}%{X}" if rework_overall is not None else "N/A"

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 DORA & ENGINEERING DELIVERY FLOW METRICS SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    print(f"   {B}Organizations Monitored        :{X} {len(org_rows)}")
    print(
        f"   {B}Repositories Assessed          :{X} {total_repos} "
        f"({active_repos} active, {inactive_repos} inactive/N/A)"
    )
    window_display = "Full Lifetime (All-Time)" if lookback_days == 0 else f"{lookback_days} Days"
    print(f"   {B}Analysis Lookback Window       :{X} {window_display}")
    print(f"   {B}Fleet Overall DORA Tier        :{X} {t_color}{B}{fleet_tier}{X}")
    print(
        f"   {B}Total Deployments & Releases   :{X} {B}{total_deploys:,}{X} "
        f"({total_successful:,} successful, {code_change_deploys:,} code-change)"
    )
    if no_op_deploys > 0 or collapsed_retries > 0:
        print(
            f"   {B}Normalized Signal Adjustments  :{X} "
            f"{no_op_deploys} no-op redeploy(s), {collapsed_retries} collapsed failure retry(s)"
        )
    print(f"   {B}Deployment Frequency (Median)  :{X} {df_str}")
    print(f"   {B}Median Lead Time (LTTC)        :{X} {lttc_str}")
    print(f"   {B}Change Failure Rate (CFR)      :{X} {cfr_str}")
    print(f"   {B}Median Recovery Time (MTTR)    :{X} {mttr_str}")
    print(f"   {B}Deployment Rework Rate         :{X} {rework_str}")
    print(f"   {B}Recovery Incidents Tracked     :{X} {resolved_count} resolved, {open_incidents} open")
    print(f"   {B}AI Advisory Engine             :{X} {format_ai_scorecard_status(ai_engine)}")
    print(f"   {B}Duration                       :{X} {elapsed_str}")
    if stalled_deploys > 0:
        print(
            f"   {Y}{B}[WARN]{X} {stalled_deploys} deployment(s) stalled IN_PROGRESS >24h."
        )
    if permission_gap_repos > 0:
        print(
            f"   {Y}{B}[WARN]{X} {permission_gap_repos} repo(s) lacked GitHub App "
            "'deployments: read' permission (403); run 'repo360 setup' to update manifest."
        )
    if failures:
        print(
            f"   {Y}{B}[WARN]{X} {len(failures)} collection/permission issue(s) recorded in Executive Summary."
        )
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive DORA Workbook (XLSX) :{X} {xlsx_path}")
    print(f"{C}{'=' * 76}{X}")
    print(f"{G}{B}   [SUCCESS] DORA flow analysis completed across {total_repos} repos in {elapsed_str}.{X}\n")


def run_dora_cli(argv: Optional[List[str]] = None) -> int:
    """Entry point for 'repo360 dora' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 dora",
        description="Analyze cross-organization DORA delivery metrics (Deployment Frequency, Lead Time, CFR, MTTR).",
    )
    add_common_scan_arguments(
        parser,
        ai_subject="flow diagnostics",
        include_lookback=True,
    )
    parser.add_argument(
        "--prod-envs",
        default="",
        metavar="LIST",
        help="Prod env names, comma-separated (default: prod,production,prd,live,release).",
    )
    parser.add_argument(
        "--include-non-prod",
        action="store_true",
        help="Include non-prod envs and branches (default: prod only).",
    )
    parser.add_argument(
        "--incident-labels",
        default="",
        metavar="LIST",
        help="Incident issue labels, comma-separated (default: built-in incident labels).",
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

    default_cfg = DORAConfig()
    prod_envs = tuple(
        token.strip().lower() for token in args.prod_envs.split(",") if token.strip()
    ) or default_cfg.prod_environments
    incident_labels = tuple(
        token.strip().lower() for token in args.incident_labels.split(",") if token.strip()
    ) or default_cfg.incident_labels

    cfg = DORAConfig(
        lookback_days=args.lookback_days,
        max_workers=args.workers,
        include_archived=args.include_archived,
        output_dir=args.output_dir,
        prod_environments=prod_envs,
        incident_labels=incident_labels,
        include_non_prod=args.include_non_prod,
    )

    auth = GitHubAppAuth(
        app_id=app_id,
        private_key=private_key,
        api_base_url=api_base_url,
    )
    client = ResilientGitHubClient(auth=auth)

    end = datetime.now(timezone.utc)
    since = datetime(1970, 1, 1, tzinfo=timezone.utc) if cfg.lookback_days == 0 else end - timedelta(days=cfg.lookback_days)
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

    print(f"\033[96m[INFO]\033[0m Measuring DORA flow metrics & recovery cadence across {len(orgs)} organization(s)...")

    metrics_list = collect_dora_data(client, orgs, targets, since, end, cfg, metadata_cache)
    repo_rows, org_rows, deployment_rows, recovery_rows = build_dora_rows(metrics_list, orgs, cfg.lookback_days, ai_engine=ai_engine)

    failures: List[Tuple[str, str]] = []
    for m in metrics_list:
        target_name = f"{m.org}/{m.repo}"
        if m.deployments_api_forbidden:
            failures.append(
                (target_name, "HTTP 403 on /deployments API (grant GitHub App 'deployments: read')")
            )
        for err_msg in m.errors:
            failures.append((target_name, err_msg))

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"{cfg.file_prefix}_{stamp}.xlsx"

    elapsed = time.time() - start_time
    write_dora_workbook(
        path=xlsx_path,
        cfg=cfg,
        since=since,
        end=end,
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=deployment_rows,
        recovery_rows=recovery_rows,
        failures=failures,
        execution_duration=elapsed,
        ai_engine=ai_engine,
    )

    print_dora_scorecard(
        repo_rows=repo_rows,
        org_rows=org_rows,
        deployment_rows=deployment_rows,
        recovery_rows=recovery_rows,
        xlsx_path=xlsx_path,
        elapsed_seconds=elapsed,
        lookback_days=cfg.lookback_days,
        failures=failures,
        ai_engine=ai_engine,
    )
    return 0
