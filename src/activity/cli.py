"""CLI command orchestrator for Repo360 user activity analysis."""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..auth.github_app_auth import GitHubAppAuth
from ..common.cli import add_common_scan_arguments, parse_days_in_year_arg
from ..common.client import ResilientGitHubClient
from ..common.concurrency import format_ai_scorecard_status
from ..common.inventory import (
    discover_app_organizations,
    resolve_scan_targets,
)
from ..common.text import pluralize
from ..credentials import resolve_runtime_credentials
from .collector import (
    build_management_tables,
    build_org_rows,
    build_rows,
    collect_repository,
    fetch_members,
    fetch_team_access,
)
from .models import ActivityConfig, RepositoryReport
from .reporter import write_activity_workbook

logger = logging.getLogger("repo360.activity")


def print_activity_scorecard(
    user_rows: List[list],
    org_rows: List[list],
    repos_count: int,
    xlsx_path: Path,
    elapsed_seconds: float,
    failures: Optional[List[Tuple[str, str]]] = None,
    ai_engine: Optional[Any] = None,
) -> None:
    """Print an executive, color-coded terminal scorecard."""
    G = "\033[92m"
    Y = "\033[93m"
    C = "\033[96m"
    B = "\033[1m"
    X = "\033[0m"

    active_count = sum(1 for r in user_rows if r[2] == "Active")
    moderate_count = sum(1 for r in user_rows if r[2] == "Moderate")
    low_count = sum(1 for r in user_rows if r[2] == "Low")
    inactive_count = sum(1 for r in user_rows if r[2] == "Inactive")
    total_activities = sum(r[4] for r in user_rows)

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 CONTRIBUTOR & USER ACTIVITY SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    print(f"   {B}Organizations Scanned :{X} {len(org_rows)}")
    print(f"   {B}Repositories Scanned  :{X} {repos_count}")
    print(f"   {B}Total Users Tracked   :{X} {len(user_rows)}")
    print(f"   {B}Active Users          :{X} {G}{active_count:<5} Active (>= threshold){X}")
    print(f"   {B}Moderate Users        :{X} {Y}{moderate_count:<5} Moderate{X}")
    print(f"   {B}Low Users             :{X} {low_count:<5} Low")
    print(f"   {B}Inactive Users        :{X} {inactive_count:<5} Inactive (0 contributions)")
    print(f"   {B}Total Activities      :{X} {B}{total_activities:,}{X}")
    if failures:
        print(f"   {B}Warnings / Errors     :{X} {Y}{len(failures)} items (see workbook Summary){X}")
    print(f"   {B}AI Insights Engine    :{X} {format_ai_scorecard_status(ai_engine)}")
    print(f"   {B}Duration              :{X} {elapsed_str}")
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive Workbook (XLSX) :{X} {xlsx_path}")
    print(f"{C}{'=' * 76}{X}")
    print(f"{G}{B}   [SUCCESS] Activity analysis completed across {len(user_rows)} users in {elapsed_str}.{X}\n")


def run_activity_cli(argv: Optional[List[str]] = None) -> int:
    """Entry point for 'repo360 activity' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 activity",
        description="Analyze contributor cadence, active days, seat allocation, and commit/PR metrics.",
    )
    add_common_scan_arguments(
        parser,
        ai_subject="contributor insights",
        include_lookback=True,
    )
    parser.add_argument(
        "--active-min-days",
        type=parse_days_in_year_arg,
        default=8,
        metavar="DAYS",
        help="Min active days for 'Active' tier (1-365; default: 8).",
    )
    parser.add_argument(
        "--moderate-min-days",
        type=parse_days_in_year_arg,
        default=3,
        metavar="DAYS",
        help="Min active days for 'Moderate' tier (1-365; default: 3).",
    )
    parser.add_argument(
        "--include-bots",
        action="store_true",
        help="Include bot accounts (default: excluded).",
    )

    args = parser.parse_args(argv)
    if args.moderate_min_days > args.active_min_days:
        parser.error("--moderate-min-days must be <= --active-min-days")
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

    cfg = ActivityConfig(
        lookback_days=args.lookback_days,
        active_min_days=args.active_min_days,
        moderate_min_days=args.moderate_min_days,
        include_archived=args.include_archived,
        exclude_bots=not args.include_bots,
        max_workers=args.workers,
        output_dir=args.output_dir,
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

    targets: List[Tuple[str, str]] = []
    failures: List[Tuple[str, str]] = []
    metadata_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

    org_filter = [o.strip() for o in args.org.split(",") if o.strip()] if args.org else None
    discovered_orgs = discover_app_organizations(auth=auth, org_filter=org_filter)
    if not discovered_orgs:
        target_noun = "organization" if (org_filter and len(org_filter) == 1) else "organizations"
        print(f"\033[91m[FAIL] No active GitHub App installations found for the specified {target_noun}.\033[0m\n")
        return 1

    org_list_str = ", ".join(discovered_orgs)
    print(f"\033[96m[INFO]\033[0m Discovered {pluralize(len(discovered_orgs), 'organization', include_count=True)} with GitHub App installed: {org_list_str}")
    print("\033[96m[INFO]\033[0m Auto-discovering repositories directly from GitHub App installations...")
    targets, metadata_cache = resolve_scan_targets(
        auth=auth,
        client=client,
        org_filter=org_filter,
        include_archived=cfg.include_archived,
        include_forks=cfg.include_forks,
    )
    orgs = sorted(set(discovered_orgs) | {org for org, _ in targets}, key=str.lower)

    if not targets and not orgs:
        print("\033[91m[FAIL] No repositories or organizations discovered to analyze.\033[0m\n")
        return 1

    print(f"\033[96m[INFO]\033[0m Scanning {pluralize(len(targets), 'repository', 'repositories', include_count=True)} across {pluralize(len(orgs), 'organization', include_count=True)}...")

    for org_name in orgs:
        perms = auth.get_installation_permissions(org_name)
        missing_perms = []
        if perms and perms.get("pull_requests") not in ("read", "write"):
            missing_perms.append("pull_requests: read")
        if perms and perms.get("issues") not in ("read", "write"):
            missing_perms.append("issues: read")
        if missing_perms:
            print(
                f"\033[93m[WARN]\033[0m GitHub App installation on '{org_name}' is missing: {', '.join(missing_perms)}.\n"
                f"       PR and Issue events will be skipped until updated permissions are accepted in GitHub:\n"
                f"       https://github.com/organizations/{org_name}/settings/installations"
            )

    reports: Dict[Tuple[str, str], RepositoryReport] = {}

    from ..common.concurrency import resolve_worker_count
    from ..common.interrupts import iter_completed
    worker_count = resolve_worker_count(cfg.max_workers, len(targets), max_cap=32) if targets else 1
    pool = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="activity-worker")
    try:
        futures = {
            pool.submit(
                collect_repository,
                client,
                org,
                repo,
                since,
                end,
                cfg.exclude_bots,
                metadata_cache.get((org.casefold(), repo.casefold())),
            ): (org, repo)
            for org, repo in targets
        }
        for done_idx, fut in enumerate(iter_completed(futures), start=1):
            org, repo = futures[fut]
            try:
                col = fut.result()
                reports[(org, repo)] = col
                if col.errors:
                    for err in col.errors:
                        failures.append((f"{org}/{repo}", err))
                logger.info("[%d/%d] Scanned %s/%s (%d contributors)", done_idx, len(targets), org, repo, len(col.users))
            except Exception as exc:
                failures.append((f"{org}/{repo}", str(exc)))
                logger.warning("[%d/%d] Error scanning %s/%s: %s", done_idx, len(targets), org, repo, exc)
    except (KeyboardInterrupt, SystemExit):
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    print(f"\033[96m[INFO]\033[0m Fetching organization members and teams...")
    members = fetch_members(client, orgs, cfg.exclude_bots, failures)
    teams_by_user, org_team_complete = fetch_team_access(
        client, orgs, cfg.exclude_bots
    )

    results = {k: v.users for k, v in reports.items() if v.activity_complete or v.users}
    user_rows, repo_rows = build_rows(results, members, cfg, end, teams_by_user, org_team_complete)
    org_rows = build_org_rows(user_rows, results, all_orgs=orgs)

    target_counts = Counter(org.casefold() for org, _ in targets)
    failed_orgs = {f[0].split("/")[0].split(" (")[0].casefold() for f in failures}
    members_norm = {k.casefold() for k in members}
    team_complete_norm = {k.casefold(): v for k, v in org_team_complete.items()}
    organization_status = {
        org: (
            len([key for key in reports if key[0].casefold() == org.casefold()]) == target_counts.get(org.casefold(), 0)
            and org.casefold() in members_norm
            and org.casefold() not in failed_orgs
            and team_complete_norm.get(org.casefold(), False)
            and all(not result.errors for (owner, _), result in reports.items() if owner.casefold() == org.casefold())
        )
        for org in orgs
    }
    management_tables = build_management_tables(
        reports=reports,
        members=members,
        since=since,
        end=end,
        organization_status=organization_status,
    )

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"{cfg.file_prefix}_{stamp}.xlsx"

    elapsed = time.time() - start_time
    write_activity_workbook(
        xlsx_path,
        cfg,
        since,
        end,
        user_rows,
        repo_rows,
        org_rows,
        failures,
        len(targets),
        management_tables=management_tables,
        execution_duration=elapsed,
        ai_engine=ai_engine,
    )

    print_activity_scorecard(
        user_rows=user_rows,
        org_rows=org_rows,
        repos_count=len(targets),
        xlsx_path=xlsx_path,
        elapsed_seconds=elapsed,
        failures=failures,
        ai_engine=ai_engine,
    )
    return 0
