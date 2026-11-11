"""CLI command orchestrator for Repo360 FinOps storage & artifact profiler."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..auth.github_app_auth import GitHubAppAuth
from ..common.client import ResilientGitHubClient
from ..common.inventory import (
    discover_app_organizations,
    discover_app_repositories,
)
from ..credentials import resolve_runtime_credentials
from .collector import build_finops_rows, collect_finops_data
from .models import FinOpsConfig
from .reporter import write_finops_workbook

logger = logging.getLogger("repo360.finops")


def print_finops_scorecard(
    repo_rows: List[list],
    org_rows: List[list],
    artifact_rows: List[list],
    cache_package_rows: List[list],
    xlsx_path: Path,
    elapsed_seconds: float,
) -> None:
    """Print an executive color-coded FinOps scorecard to stdout."""
    G = "\033[92m"
    Y = "\033[93m"
    R = "\033[91m"
    C = "\033[96m"
    B = "\033[1m"
    X = "\033[0m"

    total_repos = len(repo_rows)
    total_disk_mb = round(sum(r[3] for r in repo_rows if len(r) > 3 and isinstance(r[3], (int, float))), 2)
    total_active_artifacts = sum(r[4] for r in repo_rows if len(r) > 4 and isinstance(r[4], int))
    total_artifact_mb = round(sum(r[5] for r in repo_rows if len(r) > 5 and isinstance(r[5], (int, float))), 2)
    total_90d_count = sum(r[6] for r in repo_rows if len(r) > 6 and isinstance(r[6], int))
    total_caches = sum(r[7] for r in repo_rows if len(r) > 7 and isinstance(r[7], int))
    total_cache_mb = round(sum(r[8] for r in repo_rows if len(r) > 8 and isinstance(r[8], (int, float))), 2)
    total_packages = sum(r[9] for r in repo_rows if len(r) > 9 and isinstance(r[9], int))
    total_footprint_mb = round(sum(r[10] for r in repo_rows if len(r) > 10 and isinstance(r[10], (int, float))), 2)
    total_waste_mb = round(sum(r[10] for r in org_rows if len(r) > 10 and isinstance(r[10], (int, float))), 2)

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    waste_color = R if total_90d_count > 10 or total_waste_mb > 500 else (Y if total_90d_count > 0 or total_waste_mb > 100 else G)

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 FINOPS STORAGE QUOTA & ARTIFACT PROFILER SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    print(f"   {B}Organizations Monitored        :{X} {len(org_rows)}")
    print(f"   {B}Repositories Profiled          :{X} {total_repos}")
    print(f"   {B}Repository Git Storage         :{X} {total_disk_mb:,.2f} MB")
    print(f"   {B}Active Workflow Artifacts      :{X} {total_active_artifacts:,} ({total_artifact_mb:,.2f} MB)")
    print(f"   {B}Default 90-Day Retention Ineff.:{X} {waste_color}{total_90d_count} artifacts (Retention policy override recommended){X}")
    print(f"   {B}Actions Caches Tracked         :{X} {total_caches:,} ({total_cache_mb:,.2f} MB)")
    print(f"   {B}Packages & Containers          :{X} {total_packages:,}")
    print(f"   {B}Total Storage Footprint        :{X} {B}{total_footprint_mb:,.2f} MB{X}")
    print(f"   {B}Est. Inefficient Storage Waste :{X} {waste_color}{total_waste_mb:,.2f} MB{X}")
    print(f"   {B}Duration                       :{X} {elapsed_str}")
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive FinOps Workbook (XLSX):{X} {xlsx_path}")
    print(f"{C}{'=' * 76}{X}")
    print(f"{G}{B}   [SUCCESS] FinOps profiling completed across {total_repos} repos in {elapsed_str}.{X}\n")


def run_finops_cli(argv: Optional[List[str]] = None) -> int:
    """Entry point for 'repo360 finops' subcommand."""
    parser = argparse.ArgumentParser(
        prog="repo360 finops",
        description="Per-Repository Storage Quota & Artifact Profiler (identify cost drivers & storage waste).",
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
        "--include-archived",
        action="store_true",
        help="Include archived repositories in the analysis.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=0,
        help="Concurrent repository worker threads (default: 0 = auto-tune up to 32).",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable AI storage advisory and use deterministic policy rules only.",
    )
    parser.add_argument(
        "--require-ai",
        action="store_true",
        help="Halt execution if AI engine connection fails or is not configured.",
    )

    args = parser.parse_args(argv)
    start_time = time.time()

    # Initialize AI Engine (Multi-provider: Gemini, OpenAI, Claude)
    from ..ai import init_ai_engine
    try:
        ai_engine = init_ai_engine(require_ai=args.require_ai, no_ai=args.no_ai)
    except Exception as exc:
        print(f"\n\033[91m[FAIL] {exc}\033[0m\n")
        return 1

    # 1. Resolve Credentials
    creds = resolve_runtime_credentials()
    app_id = creds.get("app_id")
    private_key = creds.get("private_key")
    api_base_url = creds.get("api_base_url", "https://api.github.com")

    if not app_id or not private_key:
        print("\n\033[91m[FAIL] Missing GitHub App credentials.\033[0m")
        print("Run 'repo360 setup' or configure REPO360_APP_ID and REPO360_APP_PRIVATE_KEY.\n")
        return 1

    cfg = FinOpsConfig(
        max_workers=args.max_workers,
        include_archived=args.include_archived,
        output_dir=args.output_dir,
    )

    auth = GitHubAppAuth(
        app_id=app_id,
        private_key=private_key,
        api_base_url=api_base_url,
    )
    client = ResilientGitHubClient(auth=auth)

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")

    # 2. Resolve Repositories (Auto-discovery directly from GitHub App)
    org_filter = [o.strip() for o in args.org.split(",") if o.strip()] if args.org else None
    discovered_orgs = discover_app_organizations(auth=auth, org_filter=org_filter)
    if not discovered_orgs:
        print("\033[91m[FAIL] No active GitHub App installations found for the specified organization(s).\033[0m\n")
        return 1

    org_list_str = ", ".join(discovered_orgs)
    print(f"\033[96m[INFO]\033[0m Discovered {len(discovered_orgs)} organization(s) with GitHub App installed: {org_list_str}")
    print("\033[96m[INFO]\033[0m Auto-discovering repositories directly from GitHub App installations...")
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

    # Proactively verify packages permissions
    for org_name in orgs:
        perms = auth.get_installation_permissions(org_name)
        if perms and perms.get("packages") not in ("read", "write"):
            print(
                f"\033[93m[WARN]\033[0m GitHub App on '{org_name}' is missing permission: packages: read.\n"
                f"       Container (Docker / GHCR) tracking requires 'packages: read' permission.\n"
                f"       1. In GitHub App Settings -> Permissions & events -> Repository permissions -> set 'Packages' to Read-only.\n"
                f"       2. Accept updated permissions at: https://github.com/organizations/{org_name}/settings/installations\n"
            )

    print(f"\033[96m[INFO]\033[0m Profiling repository storage, workflow artifacts, and caches across {len(orgs)} organization(s)...")

    profiles, org_packages = collect_finops_data(client, orgs, targets, cfg, metadata_cache)
    repo_rows, org_rows, artifact_rows, cache_package_rows = build_finops_rows(profiles, org_packages, orgs, ai_engine=ai_engine)

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = out_dir / f"{cfg.file_prefix}_{stamp}.xlsx"

    elapsed = time.time() - start_time
    write_finops_workbook(
        path=xlsx_path,
        cfg=cfg,
        now=now,
        repo_rows=repo_rows,
        org_rows=org_rows,
        artifact_rows=artifact_rows,
        cache_package_rows=cache_package_rows,
        failures=[],
        execution_duration=elapsed,
    )

    print_finops_scorecard(
        repo_rows=repo_rows,
        org_rows=org_rows,
        artifact_rows=artifact_rows,
        cache_package_rows=cache_package_rows,
        xlsx_path=xlsx_path,
        elapsed_seconds=elapsed,
    )
    return 0
