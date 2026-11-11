"""Shared CLI argument definitions and integer validators for Repo360 scan commands."""

from __future__ import annotations

import argparse

from ..ai import add_ai_cli_arguments


def parse_non_negative_int(value: str) -> int:
    """Parse an integer >= 0 (raises ArgumentTypeError for invalid or negative inputs)."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def parse_positive_int(value: str) -> int:
    """Parse an integer >= 1 (raises ArgumentTypeError for invalid or non-positive inputs)."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def add_common_scan_arguments(
    parser: argparse.ArgumentParser,
    *,
    ai_subject: str,
    include_lookback: bool = True,
    output_dir_default: str = "./repo360-output",
) -> None:
    """Register consistent organization, output, scope, worker, and AI options."""
    parser.add_argument(
        "--org",
        default="",
        help=(
            "Comma-separated GitHub organizations (e.g. 'org1,org2'). "
            "If omitted, all installed organizations are included."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=output_dir_default,
        help="Directory for generated Excel workbooks (default: ./repo360-output).",
    )
    if include_lookback:
        parser.add_argument(
            "--lookback-days",
            type=parse_positive_int,
            default=90,
            help="Lookback analysis window in days (default: 90).",
        )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived repositories (skipped by default).",
    )
    parser.add_argument(
        "--workers",
        type=parse_non_negative_int,
        default=0,
        help="Concurrent repository worker threads (default: 0 = auto-tune up to 32).",
    )
    add_ai_cli_arguments(
        parser,
        help_text=(
            f"Enable AI {ai_subject} ('yes' or 'no', default: 'no'). "
            "Falls back to deterministic policy rules if AI is offline or unconfigured."
        ),
    )
