"""Shared AI CLI argument definitions and validation for Repo360 subcommands."""

from __future__ import annotations

import argparse
from typing import Optional


def parse_yes_no(value: str) -> bool:
    """Parse a strict case-insensitive 'yes' or 'no' CLI option value."""
    normalized = value.strip().lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    raise argparse.ArgumentTypeError(
        f"invalid choice: {value!r} (choose from 'yes', 'no')"
    )


def add_ai_cli_arguments(
    parser: argparse.ArgumentParser,
    help_text: Optional[str] = None,
) -> None:
    """Register unified '--require-ai {yes,no}' option (default: no) on a subcommand parser."""
    parser.add_argument(
        "--require-ai",
        nargs="?",
        const=True,
        default=False,
        type=parse_yes_no,
        metavar="{yes,no}",
        help=help_text or "AI insights: yes or no (default: no; skips AI remediation suggestions).",
    )


def resolve_ai_cli_flag(
    args: argparse.Namespace,
    parser: Optional[argparse.ArgumentParser] = None,
) -> bool:
    """Resolve effective AI enablement from parsed '--require-ai' CLI option."""
    del parser
    return bool(getattr(args, "require_ai", False))
