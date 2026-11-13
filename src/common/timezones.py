"""Unified timezone and timestamp normalization utilities for Repo360."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Optional

DATE_FMT = "%d-%m-%Y"

WINDOWS_TZ_MAP: Dict[str, str] = {
    "India Standard Time": "IST",
    "Pacific Standard Time": "PST",
    "Pacific Daylight Time": "PDT",
    "Eastern Standard Time": "EST",
    "Eastern Daylight Time": "EDT",
    "Central Standard Time": "CST",
    "Central Daylight Time": "CDT",
    "Mountain Standard Time": "MST",
    "Mountain Daylight Time": "MDT",
    "Greenwich Mean Time": "GMT",
    "British Summer Time": "BST",
    "Coordinated Universal Time": "UTC",
    "UTC": "UTC",
    "W. Europe Standard Time": "CET",
    "Central Europe Standard Time": "CET",
    "Romance Standard Time": "CET",
    "GMT Standard Time": "GMT",
    "Singapore Standard Time": "SGT",
    "Tokyo Standard Time": "JST",
    "China Standard Time": "CST",
    "AUS Eastern Standard Time": "AEST",
    "E. Australia Standard Time": "AEST",
    "Cen. Australia Standard Time": "ACST",
    "W. Australia Standard Time": "AWST",
}

TZ_ALIASES: Dict[str, str] = {
    "IST": "Asia/Kolkata",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "GMT": "UTC",
    "UTC": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "CEST": "Europe/Paris",
    "JST": "Asia/Tokyo",
    "SGT": "Asia/Singapore",
    "AEST": "Australia/Sydney",
    "ACST": "Australia/Darwin",
    "AWST": "Australia/Perth",
}


def get_local_timezone() -> tzinfo:
    """Resolve system local timezone with support for REPO360_TIMEZONE / TZ environment variables."""
    tz_env = os.environ.get("REPO360_TIMEZONE") or os.environ.get("TZ")
    if tz_env:
        cleaned = tz_env.strip()
        mapped = TZ_ALIASES.get(cleaned.upper(), cleaned)
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(mapped)
        except Exception:
            pass
    local_tz = datetime.now().astimezone().tzinfo
    return local_tz if local_tz is not None else timezone.utc


def get_local_tz_name(dt: Optional[datetime] = None) -> str:
    """Return standard human-readable timezone abbreviation (e.g. 'IST', 'EST', 'PST', 'CET', 'UTC')."""
    tz = get_local_timezone()
    ref_dt = dt or datetime.now(tz)
    raw_name = ref_dt.tzname() or "UTC"
    return WINDOWS_TZ_MAP.get(raw_name, raw_name)


LOCAL_TZ = get_local_timezone()
LOCAL_TZ_NAME = get_local_tz_name()
# Backward-compatible alias for existing callers
IST = LOCAL_TZ


def parse_ts(ts_str: str) -> datetime:
    """Parse an ISO 8601 timestamp string into a timezone-aware UTC datetime."""
    clean = ts_str.strip()
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    dt = datetime.fromisoformat(clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_timestamp(
    dt: Optional[datetime],
    tz: Optional[tzinfo] = None,
    default: str = "No activity",
) -> str:
    """Format datetime into local timezone string representation (DD-MM-YYYY HH:MM)."""
    if not dt:
        return default
    target_tz = tz or get_local_timezone()
    return dt.astimezone(target_tz).strftime("%d-%m-%Y %H:%M")
