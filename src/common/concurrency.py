"""Shared concurrency utilities, adaptive worker scaling, and repository pruning helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, TypeVar

from .interrupts import is_shutting_down, iter_completed

T = TypeVar("T")
R = TypeVar("R")


def resolve_worker_count(
    requested_workers: Optional[int],
    item_count: int,
    max_cap: int = 32,
) -> int:
    """Resolve worker thread count for I/O-bound GitHub API workloads.

    When ``requested_workers`` is 0 or None, concurrency auto-tunes based on
    target volume (16 for small, 24 for medium, up to ``max_cap`` for large orgs)
    without being artificially bottlenecked by local CPU core count.
    """
    if item_count <= 1:
        return 1
    if requested_workers is not None and requested_workers > 0:
        return max(1, min(int(requested_workers), item_count))

    if item_count >= 250:
        target = min(max_cap, 32)
    elif item_count >= 50:
        target = min(max_cap, 24)
    else:
        target = min(max_cap, 16)
    return max(1, min(target, item_count))


def map_concurrent(
    items: Sequence[T],
    worker_fn: Callable[[T], R],
    max_workers: int = 0,
    max_cap: int = 32,
    thread_name_prefix: str = "repo360-worker",
) -> List[R]:
    """Execute ``worker_fn`` across ``items`` concurrently, preserving input order."""
    count = len(items)
    if count == 0:
        return []
    if count == 1:
        return [worker_fn(items[0])]

    workers = resolve_worker_count(max_workers, count, max_cap=max_cap)
    if workers <= 1:
        return [worker_fn(item) for item in items]

    results: List[Optional[R]] = [None] * count
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)
    try:
        future_to_idx = {
            executor.submit(worker_fn, item): idx for idx, item in enumerate(items)
        }
        for future in iter_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
    except (KeyboardInterrupt, SystemExit):
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return results  # type: ignore[return-value]


def run_concurrent_ai(
    items: Sequence[T],
    enrich_fn: Callable[[T], R],
    ai_engine: Optional[Any] = None,
    max_workers: int = 8,
) -> List[R]:
    """Execute AI advisory calls concurrently with provider-aware rate-limit throttling."""
    if is_shutting_down():
        raise KeyboardInterrupt
    if not items:
        return []
    is_avail_fn = getattr(ai_engine, "is_available", None)
    if ai_engine is None or (callable(is_avail_fn) and is_avail_fn() is False) or len(items) <= 1:
        return [enrich_fn(item) for item in items]

    rec_fn = getattr(ai_engine, "recommended_max_workers", None)
    capped_workers = max_workers
    if callable(rec_fn):
        candidate = rec_fn(max_workers)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            capped_workers = max(1, min(max_workers, candidate))

    if capped_workers <= 1:
        return [enrich_fn(item) for item in items]

    return map_concurrent(
        items,
        enrich_fn,
        max_workers=min(capped_workers, len(items)),
        max_cap=capped_workers,
        thread_name_prefix="repo360-ai",
    )


def is_empty_repo(meta: Optional[Dict[str, Any]]) -> bool:
    """Return True only when repository metadata explicitly reports size == 0."""
    return isinstance(meta, dict) and meta.get("size") == 0


def is_dormant_repo(
    meta: Optional[Dict[str, Any]],
    since: Optional[datetime],
    parse_ts_fn: Optional[Callable[[str], datetime]] = None,
) -> bool:
    """Return True when metadata guarantees zero repository activity since ``since``."""
    if not isinstance(meta, dict):
        return False
    if meta.get("size") == 0:
        return True
    if since is None or parse_ts_fn is None:
        return False

    pushed_raw = meta.get("pushed_at")
    updated_raw = meta.get("updated_at")
    if not pushed_raw or not updated_raw:
        return False
    try:
        pushed_at = parse_ts_fn(pushed_raw)
        updated_at = parse_ts_fn(updated_raw)
        return pushed_at < since and updated_at < since
    except Exception:
        return False
