"""Shared CLI argument definitions and bounded validators for Repo360 scan commands."""

from __future__ import annotations

import argparse
from typing import Callable, Optional

from ..ai import add_ai_cli_arguments


def make_bounded_int_parser(min_value: int, max_value: Optional[int] = None) -> Callable[[str], int]:
    """Return an argparse type converter enforcing ``min_value <= value <= max_value``."""

    def _parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
        if max_value is not None and (parsed < min_value or parsed > max_value):
            raise argparse.ArgumentTypeError(f"must be between {min_value} and {max_value}")
        if parsed < min_value:
            raise argparse.ArgumentTypeError(f"must be >= {min_value}")
        return parsed

    return _parse


parse_non_negative_int = make_bounded_int_parser(0)
parse_positive_int = make_bounded_int_parser(1)
parse_workers_arg = make_bounded_int_parser(0, 32)
parse_lookback_arg = make_bounded_int_parser(1, 365)
parse_max_runs_arg = make_bounded_int_parser(1, 1000)


def add_common_scan_arguments(
    parser: argparse.ArgumentParser,
    *,
    ai_subject: str,
    include_lookback: bool = True,
    output_dir_default: str = "./repo360-output",
) -> None:
    """Register unified organization, output, scope, worker, and AI options."""
    parser.add_argument(
        "--org",
        default="",
        metavar="ORGS",
        help="Comma-separated orgs (e.g. 'org1,org2'; default: all).",
    )
    parser.add_argument(
        "--output-dir",
        default=output_dir_default,
        metavar="DIR",
        help="Output directory for Excel workbooks (default: ./repo360-output).",
    )
    if include_lookback:
        parser.add_argument(
            "--lookback-days",
            type=parse_lookback_arg,
            default=90,
            metavar="DAYS",
            help="Lookback window in days (1-365; default: 90).",
        )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived repos (default: skipped).",
    )
    parser.add_argument(
        "--workers",
        type=parse_workers_arg,
        default=0,
        metavar="N",
        help="Worker threads (0=auto, 1-32; default: 0).",
    )
    add_ai_cli_arguments(
        parser,
        help_text=f"AI {ai_subject}: yes or no (default: no; skips AI suggestions).",
    )
