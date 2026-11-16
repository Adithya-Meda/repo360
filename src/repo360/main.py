"""Repo360 Platform Governance & Intelligence CLI Entrypoint."""

from __future__ import annotations

import warnings

# Silence third-party SDK syntax/deprecation warnings (e.g. google-genai) on Python 3.12+
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*invalid escape sequence.*")
warnings.filterwarnings("ignore", message=".*automatic function calling.*")

import argparse
import concurrent.futures
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .common.interrupts import (
    finalize_interrupt_exit,
    install_signal_handlers,
    is_shutting_down,
    iter_completed,
)

# Arm Ctrl+C handlers and silent excepthook before importing heavy SDKs
install_signal_handlers()

from .ai import AIEngine, resolve_ai_cli_flag
from .auth.github_app_auth import GitHubAppAuth
from .checks import (
    ALL_CONTRIBUTOR_CHECKS,
    ALL_ORG_CHECKS,
    ALL_REPO_CHECKS,
    CheckResult,
    CheckStatus,
)
from .collector.client import Repo360Client
from .collector.repo_inspector import RepoInspector
from .common.cli import add_common_scan_arguments
from .common.text import pluralize
from .common.concurrency import (
    format_ai_scorecard_status,
    is_ai_disabled,
    is_ai_live,
    map_concurrent,
    resolve_worker_count,
)
from .reporters.excel_reporter import ExcelReporter
from .validator import ConfigValidator, RepoTarget

try:
    import colorama

    colorama.init(autoreset=True)
except ImportError:
    pass


LOG_LEVEL_PASS = 25
logging.addLevelName(LOG_LEVEL_PASS, "PASS")


class ColoredFormatter(logging.Formatter):
    """Color-coded logging formatter with standardized 4-character top-level badges."""

    LEVEL_LABELS = {
        logging.DEBUG: "DEBG",
        logging.INFO: "INFO",
        LOG_LEVEL_PASS: "PASS",
        logging.WARNING: "WARN",
        logging.ERROR: "FAIL",
        logging.CRITICAL: "CRIT",
    }

    LEVEL_COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[96m",
        LOG_LEVEL_PASS: "\033[92m\033[1m",
        logging.WARNING: "\033[93m\033[1m",
        logging.ERROR: "\033[91m\033[1m",
        logging.CRITICAL: "\033[95m\033[1m",
    }
    RESET = "\033[0m"
    DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        label = self.LEVEL_LABELS.get(record.levelno, record.levelname[:4])
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        time_str = self.formatTime(record, "%d-%m-%Y %H:%M:%S")
        levelname = f"{color}[{label:^4}]{self.RESET}"
        timestamp = f"{self.DIM}[{time_str}]{self.RESET}"
        return f"{timestamp} {levelname} {record.getMessage()}"


def setup_logger() -> logging.Logger:
    """Configure color-coded console logger and silence third-party SDK noise."""
    logging.getLogger("google").setLevel(logging.ERROR)
    logging.getLogger("google.genai").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    app_logger = logging.getLogger("repo360")
    app_logger.setLevel(logging.INFO)
    app_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColoredFormatter())
    app_logger.addHandler(handler)
    return app_logger


logger = setup_logger()


def _log_finding(res: CheckResult) -> None:
    """Log individual check outcome under [INFO] with professional [EXISTS], [MISSING], or [REVIEW] badges."""
    dim = "\033[2m"
    bold = "\033[1m"
    green = "\033[92m"
    yellow = "\033[93m"
    red = "\033[91m"
    reset = "\033[0m"

    raw_target = res.repo_name or res.org_name or "Organization-Wide"
    target_clean = raw_target.strip("[]")
    target = f"[{target_clean}]"

    if res.status == CheckStatus.PASS:
        badge = f"{green}{bold}[EXISTS] {reset}"
        logger.info("%s %s %s", badge, target, res.check_name)
    elif res.status == CheckStatus.FAIL:
        badge = f"{red}{bold}[MISSING]{reset}"
        logger.info("%s %s %s %s- %s%s", badge, target, res.check_name, dim, res.current_state, reset)
    elif res.status == CheckStatus.WARN:
        badge = f"{yellow}{bold}[REVIEW] {reset}"
        logger.info("%s %s %s %s- %s%s", badge, target, res.check_name, dim, res.current_state, reset)
    elif res.status == CheckStatus.SKIPPED:
        badge = f"{dim}[SKIPPED]{reset}"
        logger.info("%s %s %s %s(Skipped - %s)%s", badge, target, res.check_name, dim, res.current_state, reset)


def _print_failure_banner(reason: str) -> None:
    """Print a standardized color-coded execution failure announcement."""
    R = "\033[91m"
    B = "\033[1m"
    X = "\033[0m"
    print(f"\n{R}{'=' * 76}{X}")
    print(f"{B}{R}   AUDIT FAILED / EXECUTION TERMINATED{X}")
    print(f"{R}{'=' * 76}{X}")
    for line in reason.strip().splitlines():
        print(f"   {line}")
    print(f"{R}{'=' * 76}{X}\n")


def print_terminal_scorecard(
    results: List[CheckResult],
    targets: List[RepoTarget],
    report_paths: Dict[str, str],
    ai_engine: Optional[AIEngine] = None,
    elapsed_seconds: float = 0.0,
    orgs: Optional[List[str]] = None,
) -> None:
    """Print an executive color-coded scorecard to stdout."""
    total = len(results)
    passed = sum(1 for r in results if r.status == CheckStatus.PASS)
    warned = sum(1 for r in results if r.status == CheckStatus.WARN)
    failed = sum(1 for r in results if r.status == CheckStatus.FAIL)
    skipped = sum(1 for r in results if r.status == CheckStatus.SKIPPED)
    effective = total - skipped

    G = "\033[92m"
    Y = "\033[93m"
    R = "\033[91m"
    C = "\033[96m"
    B = "\033[1m"
    D = "\033[2m"
    X = "\033[0m"

    if effective > 0:
        health_score = (passed / effective) * 100
        score_color = G if health_score >= 80 else (Y if health_score >= 50 else R)
        score_str = f"{score_color}{health_score:.1f}%{X}"
    elif total == 0:
        score_str = f"{G}100.0%{X}"
    else:
        score_str = f"{Y}N/A (All Skipped){X}"

    ai_enriched_count = sum(1 for r in results if r.extra_details.get("ai_enriched"))
    ai_status = format_ai_scorecard_status(ai_engine, enriched_count=ai_enriched_count)

    elapsed_str = (
        f"{int(elapsed_seconds // 60)}m {int(elapsed_seconds % 60)}s"
        if elapsed_seconds >= 60
        else f"{elapsed_seconds:.1f}s"
    )

    print(f"\n{C}{'=' * 76}{X}")
    print(f"{B}{C}   REPO360 PLATFORM GOVERNANCE & ENGINEERING SCORECARD{X}")
    print(f"{C}{'=' * 76}{X}")
    unique_orgs = sorted(set(orgs or []) | {target.org_name for target in targets})
    org_summary = (
        f"{len(unique_orgs)} ({', '.join(unique_orgs)})"
        if len(unique_orgs) <= 4
        else f"{len(unique_orgs)} organizations"
    )
    print(f"   {B}Organizations Audited:{X} {org_summary}")
    print(f"   {B}Repositories Audited :{X} {len(targets)}")
    print(f"   {B}Total Checks Run     :{X} {total}")
    print(f"   {B}Controls Present     :{X} {G}{passed:<4} EXISTS{X}")
    print(f"   {B}Controls Missing     :{X} {R}{failed:<4} MISSING{X}")
    print(f"   {B}Items Needing Review :{X} {Y}{warned:<4} REVIEW{X}")
    if skipped > 0:
        print(f"   {B}Skipped / Not App.   :{X} {D}{skipped:<4} SKIPPED{X}")
    print(f"   {B}Overall Posture Score:{X} {score_str}")
    print(f"   {B}AI Remediation Engine:{X} {ai_status}")
    if elapsed_seconds > 0:
        print(f"   {B}Execution Duration   :{X} {elapsed_str}")
    print(f"{C}{'-' * 76}{X}")
    print(f"   {B}Executive Workbook (XLSX) :{X} {report_paths.get('xlsx', '')}")
    print(f"{C}{'=' * 76}{X}")
    repo_noun = "repository" if len(targets) == 1 else "repositories"
    org_noun = "organization" if len(unique_orgs) == 1 else "organizations"
    time_suffix = f" in {elapsed_str}" if elapsed_seconds > 0 else ""
    print(f"{G}{B}   [SUCCESS] Audit completed successfully across {len(targets)} {repo_noun} in {len(unique_orgs)} {org_noun}{time_suffix}.{X}\n")


def _log_repo_banner(
    idx: int,
    total: int,
    target: RepoTarget,
    tech_stack: Optional[Dict[str, Any]] = None,
) -> None:
    """Log an executive progress banner indicating the repository currently being evaluated."""
    bar_length = 20
    ratio = min(max(idx / total, 0.0), 1.0) if total > 0 else 1.0
    exact_pos = ratio * bar_length
    filled_chars = int(exact_pos)
    fraction = exact_pos - filled_chars

    if filled_chars >= bar_length:
        bar_fill = "=" * bar_length
        bar_empty = ""
    else:
        # Animate sub-character tip so long steps still show progress
        if fraction < 0.25:
            tip = "."
        elif fraction < 0.50:
            tip = "-"
        elif fraction < 0.75:
            tip = "="
        else:
            tip = ">"

        bar_fill = ("=" * filled_chars) + tip
        unfilled_len = max(0, bar_length - len(bar_fill))
        bar_empty = "-" * unfilled_len

    stack_str = ""
    if tech_stack:
        langs = tech_stack.get("languages", [])
        if langs:
            stack_str = f" | Stack: {', '.join(langs[:3])}"

    C = "\033[96m"
    B = "\033[1m"
    Y = "\033[93m"
    W = "\033[97m"
    D = "\033[2m"
    X = "\033[0m"

    pad = len(str(total))
    if total >= 100:
        pct_val = (idx / total * 100) if total > 0 else 100.0
        pct_badge = f"{B}{Y}[{pct_val:5.1f}% | {idx:>{pad}}/{total}]{X}"
    else:
        pct_int = int(round(idx / total * 100)) if total > 0 else 100
        pct_badge = f"{B}{Y}[{pct_int:3d}% | {idx:>{pad}}/{total}]{X}"

    bar_badge = f"{C}[{B}{bar_fill}{X}{D}{bar_empty}{C}]{X}"
    repo_title = f"{B}{W}{target.org_name}/{target.repo_name}{X}"
    branch_info = f"{D}(branch: {target.default_branch}{stack_str}){X}"

    separator = f"{D}{'-' * 74}{X}"
    logger.info(separator)
    logger.info("%s %s Auditing Repository: %s %s", pct_badge, bar_badge, repo_title, branch_info)
    logger.info(separator)


def _resolve_ai_pool_size(ai_engine: Optional[AIEngine], requested: int = 8) -> int:
    """Resolve provider-safe AI worker pool size consistent with concurrent collectors."""
    requested_safe = max(1, int(requested))
    if not is_ai_live(ai_engine):
        return 1
    rec_fn = getattr(ai_engine, "recommended_max_workers", None)
    if callable(rec_fn):
        candidate = rec_fn(requested_safe)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return max(1, min(requested_safe, candidate))
    return min(requested_safe, 4)


def run_audit(
    inspector: RepoInspector,
    targets: List[RepoTarget],
    advisory_config: Dict[str, Any],
    ai_engine: Optional[AIEngine] = None,
    orgs: Optional[List[str]] = None,
    max_workers: Optional[int] = None,
) -> List[CheckResult]:
    """Execute all enabled checks across target organizations and repositories with high concurrency."""
    results: List[CheckResult] = []
    enabled_checks = advisory_config.get("enabled_checks", {})
    thresholds = advisory_config.get("policy_thresholds", {})

    unique_orgs = sorted(set(orgs or []) | {target.org_name for target in targets})

    def _audit_single_org(org: str) -> Tuple[str, List[CheckResult]]:
        org_res: List[CheckResult] = []
        for check_cls in ALL_ORG_CHECKS:
            check_instance = check_cls()
            if check_instance.is_enabled(enabled_checks):
                try:
                    res = check_instance.evaluate(inspector, org, thresholds)
                    org_res.append(res)
                except Exception as err:
                    logger.warning("Check %s failed on org %s: %s", check_instance.check_id, org, err)
        return org, org_res

    org_outcomes = map_concurrent(
        unique_orgs,
        _audit_single_org,
        max_workers=min(8, len(unique_orgs)) if unique_orgs else 1,
        max_cap=8,
        thread_name_prefix="audit-org",
    )
    D = "\033[2m"
    X = "\033[0m"
    for org, org_res in org_outcomes:
        logger.info(f"{D}{'-' * 74}{X}")
        logger.info("\033[1;96mAuditing Organization:\033[0m \033[1;97m%s\033[0m (Organization-Wide Governance)", org)
        logger.info(f"{D}{'-' * 74}{X}")
        for res in org_res:
            results.append(res)
            _log_finding(res)

    if not targets:
        return results

    total_repos = len(targets)
    pool_workers = resolve_worker_count(max_workers, total_repos, max_cap=32)

    repo_results_by_idx: Dict[int, List[CheckResult]] = {}
    completed_lock = threading.Lock()
    completed_count = 0
    stop_event = threading.Event()

    ai_futures: List[concurrent.futures.Future] = []
    ai_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    if is_ai_live(ai_engine):
        ai_pool_size = _resolve_ai_pool_size(ai_engine, requested=8)
        ai_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=ai_pool_size, thread_name_prefix="ai-enricher"
        )

    def _audit_single_target(idx: int, target: RepoTarget) -> List[CheckResult]:
        if stop_event.is_set():
            return []
        nonlocal completed_count
        repo_res: List[CheckResult] = []
        tech_stack: Dict[str, Any] = {}
        pending_ai_items: List[Tuple[CheckResult, Dict[str, Any]]] = []

        try:
            # Prefetch full repo tree in one call to avoid N+1 file lookups
            inspector.prefetch_repo_tree(target.org_name, target.repo_name, target.default_branch)
            if isinstance(inspector, RepoInspector) and not stop_event.is_set():
                inspector.prefetch_repo_audit_context(
                    target.org_name,
                    target.repo_name,
                    target.default_branch,
                    enabled_checks=enabled_checks,
                )
        except Exception as err:
            logger.debug("Tree prefetch skipped on %s/%s: %s", target.org_name, target.repo_name, err)

        if stop_event.is_set():
            return []

        try:
            tech_stack = inspector.detect_tech_stack(
                target.org_name, target.repo_name, target.default_branch
            )
        except Exception as err:
            logger.debug("Tech stack detection failed on %s/%s: %s", target.org_name, target.repo_name, err)
            tech_stack = {"languages": ["General Software"]}

        if stop_event.is_set():
            return []

        if "all" in target.target_personas or "repo_admin" in target.target_personas:
            for check_cls in ALL_REPO_CHECKS:
                if stop_event.is_set():
                    return []
                check_instance = check_cls()
                if check_instance.is_enabled(enabled_checks):
                    try:
                        res = check_instance.evaluate(
                            inspector,
                            target.org_name,
                            target.repo_name,
                            target.default_branch,
                            thresholds,
                        )
                        repo_res.append(res)
                        if (
                            ai_executor
                            and ai_engine
                            and not stop_event.is_set()
                            and ai_engine.is_eligible_for_remediation(res)
                        ):
                            pending_ai_items.append((res, dict(tech_stack)))
                    except Exception as err:
                        logger.warning(
                            "Check %s failed on %s/%s: %s",
                            check_instance.check_id,
                            target.org_name,
                            target.repo_name,
                            err,
                        )

        if "all" in target.target_personas or "contributor" in target.target_personas:
            for check_cls in ALL_CONTRIBUTOR_CHECKS:
                if stop_event.is_set():
                    return []
                check_instance = check_cls()
                if check_instance.is_enabled(enabled_checks):
                    try:
                        res = check_instance.evaluate(
                            inspector,
                            target.org_name,
                            target.repo_name,
                            target.default_branch,
                            thresholds,
                        )
                        repo_res.append(res)
                        if (
                            ai_executor
                            and ai_engine
                            and not stop_event.is_set()
                            and ai_engine.is_eligible_for_remediation(res)
                        ):
                            pending_ai_items.append((res, dict(tech_stack)))
                    except Exception as err:
                        logger.warning(
                            "Check %s failed on %s/%s: %s",
                            check_instance.check_id,
                            target.org_name,
                            target.repo_name,
                            err,
                        )

        if stop_event.is_set():
            return []

        with completed_lock:
            completed_count += 1
            current_progress = completed_count
            _log_repo_banner(current_progress, total_repos, target, tech_stack)
            for r in repo_res:
                _log_finding(r)

            if ai_executor and not stop_event.is_set() and is_ai_live(ai_engine):
                for res_item, stack_item in pending_ai_items:
                    fut = ai_executor.submit(ai_engine.enrich_remediation, res_item, stack_item)
                    ai_futures.append(fut)

        return repo_res

    try:
        if pool_workers > 1:
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=pool_workers, thread_name_prefix="audit-worker"
            )
            try:
                future_to_idx = {
                    executor.submit(_audit_single_target, idx, target): idx
                    for idx, target in enumerate(targets, start=1)
                }
                for future in iter_completed(future_to_idx):
                    if stop_event.is_set() or is_shutting_down():
                        break
                    target_idx = future_to_idx[future]
                    try:
                        repo_results_by_idx[target_idx] = future.result()
                    except Exception as exc:
                        logger.error("Audit worker failed for repository #%d: %s", target_idx, exc)
                        repo_results_by_idx[target_idx] = []
            except (KeyboardInterrupt, SystemExit):
                stop_event.set()
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            finally:
                # Keep stop_event clear on normal exit so pending AI futures still finish
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            for idx, target in enumerate(targets, start=1):
                if stop_event.is_set():
                    break
                repo_results_by_idx[idx] = _audit_single_target(idx, target)
    except (KeyboardInterrupt, SystemExit):
        stop_event.set()
        if ai_executor:
            ai_executor.shutdown(wait=False, cancel_futures=True)
        raise

    if ai_executor:
        try:
            if not stop_event.is_set():
                pending = [f for f in ai_futures if not f.done()]
                if pending:
                    D = "\033[2m"
                    X = "\033[0m"
                    logger.info(
                        "%sFinalizing background AI recommendations (%d in-flight tasks)...%s",
                        D,
                        len(pending),
                        X,
                    )
                    wait_budget = min(180.0, max(60.0, len(pending) * 5.0))
                    start_wait = time.time()
                    while not stop_event.is_set() and not is_shutting_down() and any(not f.done() for f in pending):
                        concurrent.futures.wait(pending, timeout=0.2)
                        if time.time() - start_wait > wait_budget:
                            break
                    if is_shutting_down():
                        raise KeyboardInterrupt
                    unfinished = [f for f in pending if not f.done()]
                    if unfinished:
                        for f in unfinished:
                            f.cancel()
                        still_running = [f for f in unfinished if not f.done() and not f.cancelled()]
                        if still_running:
                            concurrent.futures.wait(still_running, timeout=5.0)
                        task_noun = pluralize(len(unfinished), "background AI remediation task", include_count=True)
                        logger.warning(
                            "%s did not complete within %ds; deterministic templates preserved.",
                            task_noun,
                            int(wait_budget),
                        )
        finally:
            ai_executor.shutdown(wait=False, cancel_futures=True)

    for idx in sorted(repo_results_by_idx.keys()):
        results.extend(repo_results_by_idx[idx])

    return results


def _print_root_help() -> int:
    """Print the top-level Repo360 CLI commands, grouped by category."""
    print("""Repo360 - 360-Degree Platform Governance, Compliance & Engineering Intelligence

Usage:
  repo360 <command> [options]

Analytics & Intelligence Commands:
  audit       Security, governance, and CI/CD compliance audit across repositories
  activity    Contributor activity metrics — commits, pull requests, reviews, and issues
  runners     Self-hosted runner fleet analytics — availability, compute hours, and queue latency
  finops      Storage and cost profiling — quotas, artifact retention, and package registries
  dora        DORA delivery metrics — Deployment Frequency, Lead Time, Change Failure Rate, MTTR
  bypass      Push-protection and ruleset bypass audit ledger — actor, type, and timestamp records

Configuration & Lifecycle Commands:
  setup       Register the GitHub App and configure credentials
  upgrade     Upgrade Repo360 to the latest release (or switch tags via --tag)
  health      Check GitHub App authentication, organization installations, and AI engine status
  version     Show installed Repo360 version
  uninstall   Remove the package, virtual environment, and config while preserving reports

Options:
  -h, --help  Show this help message or help for a specific command

Run 'repo360 <command> --help' for details on a specific command (e.g. 'repo360 audit -h').
""")
    return 0


def _run_health_cli() -> int:
    """CLI handler for 'repo360 health' subcommand."""
    import json
    from .credentials import resolve_runtime_credentials
    from .auth.github_app_auth import GitHubAppAuth
    from .ai import init_ai_engine
    from .upgrade import get_installation_details

    parser = argparse.ArgumentParser(
        prog="repo360 health",
        description="Check GitHub App authentication, organization installations, and AI engine status.",
    )
    parser.parse_args()

    install_details = get_installation_details()
    version_meta = {
        "version": install_details.get("version"),
        "installed_commit": install_details.get("commit_short"),
        "commit_source": install_details.get("commit_source"),
    }

    creds = resolve_runtime_credentials()
    app_id = creds.get("app_id")
    private_key = creds.get("private_key")
    if not app_id or not private_key:
        print(json.dumps({"status": "UNHEALTHY", "error": "Credentials unconfigured", **version_meta}, indent=2))
        return 1
    try:
        auth = GitHubAppAuth(
            app_id=app_id,
            private_key=private_key,
            api_base_url=creds.get("api_base_url", "https://api.github.com"),
        )
        installs = auth.discover_installations()
        ai_eng = init_ai_engine()
        health_data = {
            "status": "HEALTHY" if installs else "DEGRADED",
            "app_id": app_id,
            "installations_count": len(installs),
            "ai_engine": (
                "ONLINE"
                if is_ai_live(ai_eng)
                else ("RATE_LIMITED" if ai_eng.is_circuit_active() else "OFFLINE")
            ),
            **version_meta,
        }
        print(json.dumps(health_data, indent=2))
        return 0 if installs else 1
    except Exception as exc:
        print(json.dumps({"status": "UNHEALTHY", "error": str(exc), **version_meta}, indent=2))
        return 1


def _run_interruptible_command(fn: Callable[[], int]) -> int:
    """Install fast signal/watchdog handlers, run ``fn()``, and finalize Ctrl+C exit silently."""
    install_signal_handlers()
    try:
        return fn()
    except KeyboardInterrupt:
        return finalize_interrupt_exit(130)


def main() -> int:
    """CLI entrypoint with strict one-command-per-function routing.

    Running bare 'repo360' prints available commands and does NOT invoke any operation.
    """
    install_signal_handlers()

    try:
        if len(sys.argv) <= 1:
            return _print_root_help()

        subcmd = sys.argv[1].lower()

        if subcmd in ("-h", "--help"):
            return _print_root_help()

        if subcmd == "version":
            sys.argv.pop(1)
            from .upgrade import run_version_cli
            return _run_interruptible_command(run_version_cli)

        if subcmd == "audit":
            sys.argv.pop(1)
            return _run_interruptible_command(_run_cli)

        if subcmd == "setup":
            sys.argv.pop(1)
            from .setup_app import main as setup_main
            return _run_interruptible_command(setup_main)

        if subcmd == "activity":
            sys.argv.pop(1)
            from .activity.cli import run_activity_cli
            return _run_interruptible_command(run_activity_cli)

        if subcmd == "runners":
            sys.argv.pop(1)
            from .runners.cli import run_runners_cli
            return _run_interruptible_command(run_runners_cli)

        if subcmd == "finops":
            sys.argv.pop(1)
            from .finops.cli import run_finops_cli
            return _run_interruptible_command(run_finops_cli)

        if subcmd == "dora":
            sys.argv.pop(1)
            from .dora.cli import run_dora_cli
            return _run_interruptible_command(run_dora_cli)

        if subcmd == "bypass":
            sys.argv.pop(1)
            from .bypass.cli import run_bypass_cli
            return _run_interruptible_command(run_bypass_cli)

        if subcmd == "upgrade":
            sys.argv.pop(1)
            from .upgrade import run_upgrade_cli
            return _run_interruptible_command(run_upgrade_cli)

        if subcmd == "uninstall":
            sys.argv.pop(1)
            from .uninstall import run_uninstall_cli
            return _run_interruptible_command(run_uninstall_cli)

        if subcmd == "health":
            sys.argv.pop(1)
            return _run_interruptible_command(_run_health_cli)

        print(f"Error: Unknown command '{sys.argv[1]}'. Run 'repo360 --help' for available commands.\n")
        return 1
    except KeyboardInterrupt:
        return finalize_interrupt_exit(130)


def run_audit_cli() -> int:
    """Dedicated entrypoint for programmatic governance audit execution."""
    return _run_interruptible_command(_run_cli)


def _run_cli() -> int:
    """CLI orchestrator."""
    start_time = time.time()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(
        prog="repo360 audit",
        description="Audit security, governance, and CI/CD compliance across GitHub organizations and repositories.",
    )
    add_common_scan_arguments(
        parser,
        ai_subject="remediation and executive briefing",
        include_lookback=False,
        output_dir_default="",
    )
    parser.add_argument(
        "--mock-mode",
        action="store_true",
        help="Run offline with simulated data (no API calls).",
    )

    args = parser.parse_args()
    ai_enabled = resolve_ai_cli_flag(args, parser)

    app_config = ConfigValidator.get_default_app_config()
    advisory_config = ConfigValidator.get_default_advisory_config()
    targets: List[RepoTarget] = []

    output_dir = args.output_dir or app_config.get("reporting", {}).get("output_directory", "./repo360-output")
    os.makedirs(output_dir, exist_ok=True)

    from .credentials import resolve_runtime_credentials
    creds = resolve_runtime_credentials()

    from .ai import init_ai_engine
    ai_engine = init_ai_engine(
        enable_ai=ai_enabled,
        verify=not args.mock_mode,
        creds=creds,
        log=logger,
    )

    results: List[CheckResult] = []

    if args.mock_mode:
        logger.info("Running in offline mock mode (simulating Repo360 audit results)...")
        from .mock_generator import create_mock_results
        if not targets:
            mock_orgs = (
                [o.strip() for o in args.org.split(",") if o.strip()]
                if args.org
                else ["fintech-core"]
            )
            targets = []
            for org in mock_orgs:
                targets.extend([
                    RepoTarget(org_name=org, repo_name="payment-gateway", target_personas={"all"}),
                    RepoTarget(org_name=org, repo_name="auth-service", target_personas={"all"}),
                ])
        results = create_mock_results(targets)

        D = "\033[2m"
        X = "\033[0m"
        unique_mock_orgs = sorted({target.org_name for target in targets})
        for org in unique_mock_orgs:
            logger.info(f"{D}{'-' * 74}{X}")
            logger.info("\033[1;96mAuditing Organization:\033[0m \033[1;97m%s\033[0m (Organization-Wide Governance)", org)
            logger.info(f"{D}{'-' * 74}{X}")
            for res in results:
                if res.org_name == org and res.repo_name == "[Organization-Wide]":
                    _log_finding(res)

        mock_stacks = [
            {"languages": ["Python", "Docker"]},
            {"languages": ["TypeScript", "Node.js"]},
        ]
        total_targets = len(targets)
        for idx, target in enumerate(targets, start=1):
            stack = mock_stacks[(idx - 1) % len(mock_stacks)]
            _log_repo_banner(idx, total_targets, target, stack)
            for res in results:
                if res.org_name == target.org_name and res.repo_name == target.repo_name:
                    _log_finding(res)
    else:
        app_id = creds["app_id"]
        installation_id = creds["installation_id"]
        private_key = creds["private_key"]
        api_base_url = creds["api_base_url"] or app_config.get("github", {}).get("api_base_url", "https://api.github.com")

        if not app_id or not private_key:
            logger.error(
                "Missing required GitHub App credentials. Run 'repo360 setup' to automatically register "
                "your GitHub App and download credentials, or set REPO360_APP_ID and REPO360_APP_PRIVATE_KEY."
            )
            _print_failure_banner(
                "Missing required GitHub App credentials.\n"
                "  Action: Run 'repo360 setup' to automatically register your GitHub App and download credentials,\n"
                "  or configure REPO360_APP_ID and REPO360_APP_PRIVATE_KEY."
            )
            return 1

        try:
            auth = GitHubAppAuth(
                app_id=app_id,
                installation_id=installation_id,
                private_key=private_key,
                api_base_url=api_base_url,
            )

            all_installations = auth.discover_installations()
            if not all_installations:
                logger.error(
                    "No active installations found for GitHub App ID %s. "
                    "Ensure an Organization Owner has installed the App on your GitHub organization.",
                    app_id,
                )
                _print_failure_banner(
                    f"No active installations found for GitHub App ID {app_id}.\n"
                    "  Action: Ensure an Organization Owner has installed the App on your GitHub organization."
                )
                return 1

            requested_orgs_raw = getattr(args, "org", "").strip()
            if requested_orgs_raw:
                filter_orgs = {o.strip().lower() for o in requested_orgs_raw.split(",") if o.strip()}
                active_installations = [
                    inst for inst in all_installations
                    if inst.get("account", {}).get("login", "").lower() in filter_orgs
                ]
                found_orgs = {
                    inst.get("account", {}).get("login", "").lower() for inst in active_installations
                }
                missing_orgs = filter_orgs - found_orgs
                if missing_orgs:
                    logger.warning(
                        "The following requested organizations were not found among GitHub App installations: %s",
                        ", ".join(sorted(missing_orgs)),
                    )
                if not active_installations:
                    target_org_noun = "organization" if len(filter_orgs) == 1 else "organizations"
                    logger.error("None of the specified organizations (%s) have the GitHub App installed.", requested_orgs_raw)
                    _print_failure_banner(
                        f"None of the specified organizations ({requested_orgs_raw}) have the GitHub App installed.\n"
                        f"  Action: Install the GitHub App in the specified {target_org_noun} or adjust the --org parameter."
                    )
                    return 1
            else:
                active_installations = all_installations

            if not auth.installation_id and active_installations:
                auth.set_installation_id(active_installations[0]["id"])

            client = Repo360Client(
                auth=auth,
                timeout_seconds=app_config.get("github", {}).get("timeout_seconds", 30),
                max_retries=app_config.get("github", {}).get("max_retries", 3),
            )
            inspector = RepoInspector(client=client)

            if not targets:
                logger.info(
                    "Discovering accessible repositories across %s...",
                    pluralize(len(active_installations), "GitHub organization", include_count=True),
                )
                valid_installs = [inst for inst in active_installations if inst.get("id")]

                def _discover_single_install(inst: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
                    org_login = inst.get("account", {}).get("login", "")
                    inst_id = inst.get("id")
                    repo_sel = inst.get("repository_selection", "all")
                    discovered = inspector.list_installation_repositories(installation_id=inst_id)
                    return org_login, repo_sel, discovered

                install_batches = map_concurrent(
                    valid_installs,
                    _discover_single_install,
                    max_workers=min(8, len(valid_installs)) if valid_installs else 1,
                    max_cap=8,
                    thread_name_prefix="audit-discovery",
                )
                include_archived = bool(getattr(args, "include_archived", False))
                skipped_archived = 0
                skipped_empty = 0
                for org_login, repo_sel, discovered in install_batches:
                    if repo_sel != "all":
                        logger.warning(
                            "GitHub App access on '%s' is set to 'selected' repositories. "
                            "To automatically cover all current and future repositories, change to 'All repositories' in Org Settings.",
                            org_login,
                        )
                    for r in discovered:
                        if r.get("archived") and not include_archived:
                            skipped_archived += 1
                            continue
                        if "size" in r and r.get("size", 0) == 0 and not r.get("pushed_at"):
                            skipped_empty += 1
                            continue
                        owner = r.get("owner", {}).get("login", "") or org_login
                        name = r.get("name", "")
                        default_branch = r.get("default_branch", "main") or "main"
                        if owner and name:
                            inspector.seed_repo_metadata(owner, name, r)
                            targets.append(
                                RepoTarget(
                                    org_name=owner,
                                    repo_name=name,
                                    target_personas={"all"},
                                    default_branch=default_branch,
                                )
                            )
                if skipped_archived or skipped_empty:
                    total_skipped = skipped_archived + skipped_empty
                    logger.info(
                        "Filtered out %d archived and %d empty %s during discovery.",
                        skipped_archived,
                        skipped_empty,
                        pluralize(total_skipped, "repository", "repositories"),
                    )

                installed_org_logins = [
                    inst.get("account", {}).get("login", "")
                    for inst in active_installations
                    if inst.get("account", {}).get("login")
                ]
                unique_discovered_orgs = sorted(set(installed_org_logins) | {t.org_name for t in targets})
                repo_noun = pluralize(len(targets), "repository", "repositories")
                org_count_str = pluralize(len(unique_discovered_orgs), "organization", include_count=True)
                logger.info(
                    "Auto-discovered %d accessible %s across %s: %s.",
                    len(targets),
                    repo_noun,
                    org_count_str,
                    ", ".join(unique_discovered_orgs),
                )
            else:
                installed_org_logins = sorted({t.org_name for t in targets})

            if not targets and not installed_org_logins:
                logger.warning("No repositories found to audit. Ensure the GitHub App is installed on repositories.")
                return 0

            workers_arg = args.workers if getattr(args, "workers", 0) > 0 else None
            results = run_audit(
                inspector,
                targets,
                advisory_config,
                ai_engine=ai_engine,
                orgs=installed_org_logins,
                max_workers=workers_arg,
            )
        except Exception as err:
            logger.error("Execution failed: %s", err)
            _print_failure_banner(f"Audit execution error: {err}")
            return 1

    ai_not_enabled = is_ai_disabled(ai_engine)
    executive_summary = ""
    active_model = ""
    if args.mock_mode:
        if not ai_not_enabled:
            executive_summary = (
                "Simulated Executive Risk & Governance Briefing for offline audit verification."
            )
    elif not ai_not_enabled:
        unique_orgs = sorted(
            set(installed_org_logins if "installed_org_logins" in locals() else [])
            | {t.org_name for t in targets}
        )
        org_scope_str = ", ".join(unique_orgs)
        if is_ai_live(ai_engine) and targets:
            logger.info("Synthesizing Executive Risk & Governance Narrative via AI...")
            executive_summary = ai_engine.generate_executive_summary(results, org_scope_str)
            if executive_summary:
                active_model = ai_engine.model_name
        if not executive_summary and results:
            if ai_engine and ai_engine.is_circuit_active():
                logger.warning(
                    "AI rate-limit circuit active; using deterministic Executive Risk & Governance summary."
                )
            executive_summary = AIEngine.build_deterministic_executive_summary(results, org_scope_str)
            active_model = "Deterministic Policy Engine"

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = app_config.get("reporting", {}).get("report_prefix", "repo360_audit")

    xlsx_path = os.path.join(output_dir, f"{prefix}_{timestamp}.xlsx")

    logger.info("Generating Excel Report: %s", xlsx_path)
    ExcelReporter().generate(
        results,
        xlsx_path,
        executive_summary=executive_summary,
        model_name=active_model,
        ai_enabled=not ai_not_enabled,
    )

    report_paths = {
        "xlsx": os.path.abspath(xlsx_path),
    }
    elapsed = time.time() - start_time
    print_terminal_scorecard(
        results,
        targets,
        report_paths,
        ai_engine=ai_engine,
        elapsed_seconds=elapsed,
        orgs=installed_org_logins if 'installed_org_logins' in locals() else None,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
