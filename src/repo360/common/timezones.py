"""Unified timezone and timestamp normalization utilities for Repo360."""

from __future__ import annotations

import logging
import os
import time
import zoneinfo
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("repo360.timezones")

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


_EPOCH = datetime(1970, 1, 1)


class SystemLocalTimezone(tzinfo):
    """The operating system's local timezone, resolving the UTC offset per instant.

    Unlike ``datetime.now().astimezone().tzinfo`` (a fixed offset captured once), this applies
    the OS daylight-saving rules to every timestamp, so historical data across a DST change is
    converted correctly on Windows, macOS, and Linux without third-party dependencies.
    """

    @staticmethod
    def _local_struct(timestamp: float) -> time.struct_time:
        try:
            return time.localtime(timestamp)
        except (OSError, OverflowError, ValueError):
            return time.localtime()

    @classmethod
    def _wall_time_instants(cls, naive: datetime) -> List[float]:
        """Return every POSIX timestamp whose local wall time equals ``naive`` (two during DST fall-back)."""
        try:
            guess = time.mktime(naive.timetuple()[:8] + (-1,))
        except (OSError, OverflowError, ValueError):
            return []
        wall = naive.timetuple()[:6]
        return sorted(ts for ts in (guess - 3600, guess, guess + 3600) if cls._local_struct(ts)[:6] == wall) or [guess]

    @classmethod
    def _wall_time_timestamp(cls, dt: datetime) -> float:
        """Map a local wall time to its POSIX timestamp, honouring PEP 495 ``fold`` for repeated hours."""
        instants = cls._wall_time_instants(dt.replace(tzinfo=None))
        if not instants:
            return time.time()
        return instants[-1] if dt.fold else instants[0]

    def fromutc(self, dt: datetime) -> datetime:
        utc_naive = dt.replace(tzinfo=None)
        ts = (utc_naive - _EPOCH).total_seconds()
        local_naive = utc_naive + timedelta(seconds=self._local_struct(ts).tm_gmtoff)
        instants = self._wall_time_instants(local_naive.replace(microsecond=0))
        fold = 1 if len(instants) > 1 and abs(instants[-1] - int(ts)) < 1 else 0
        return local_naive.replace(tzinfo=self, fold=fold)

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        ts = time.time() if dt is None else self._wall_time_timestamp(dt)
        return timedelta(seconds=self._local_struct(ts).tm_gmtoff)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        ts = time.time() if dt is None else self._wall_time_timestamp(dt)
        return timedelta(hours=1) if self._local_struct(ts).tm_isdst > 0 else timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:
        ts = time.time() if dt is None else self._wall_time_timestamp(dt)
        return self._local_struct(ts).tm_zone or "UTC"

    def __repr__(self) -> str:
        return "SystemLocalTimezone()"


def get_local_timezone() -> tzinfo:
    """Resolve system local timezone with support for REPO360_TIMEZONE / TZ environment variables."""
    tz_env = os.environ.get("REPO360_TIMEZONE") or os.environ.get("TZ")
    if tz_env:
        cleaned = tz_env.strip()
        mapped = TZ_ALIASES.get(cleaned.upper(), cleaned)
        try:
            return zoneinfo.ZoneInfo(mapped)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning("Unknown timezone %r in REPO360_TIMEZONE/TZ; falling back to system local time.", tz_env)
    return SystemLocalTimezone()


def get_local_tz_name(dt: Optional[datetime] = None) -> str:
    """Return standard human-readable timezone abbreviation (e.g. 'IST', 'EST', 'PST', 'CET', 'UTC')."""
    tz = get_local_timezone()
    ref_dt = dt or datetime.now(tz)
    raw_name = ref_dt.tzname() or "UTC"
    if raw_name in WINDOWS_TZ_MAP:
        return WINDOWS_TZ_MAP[raw_name]
    if " " in raw_name or raw_name[:1] in "+-":
        # Unmapped Windows names ("Arabian Standard Time") and numeric IANA names ("+14") get a uniform label
        offset = ref_dt.utcoffset() or timedelta(0)
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"UTC{sign}{hours:02d}:{minutes:02d}"
    return raw_name


LOCAL_TZ = get_local_timezone()
LOCAL_TZ_NAME = get_local_tz_name()
# Deprecated alias kept for backward compatibility; it is the local timezone, not India Standard Time
IST = LOCAL_TZ


def file_timestamp(dt: Optional[datetime] = None) -> str:
    """Return a local-time ``YYYYMMDD_HHMMSS`` stamp for report file names."""
    return (dt or datetime.now(timezone.utc)).astimezone(LOCAL_TZ).strftime("%Y%m%d_%H%M%S")


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
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    target_tz = tz or get_local_timezone()
    return dt.astimezone(target_tz).strftime("%d-%m-%Y %H:%M")


def resolve_reporting_window(
    lookback_days: int,
    since: datetime,
    end: datetime,
    activity_dates: Optional[Sequence[Any]] = None,
) -> str:
    """Format an executive-grade reporting window string.

    When lookback_days == 0 (All-Time / Full History):
      Derives the true inception date from actual recorded activity timestamps,
      displaying: 'Full History (DD-MM-YYYY to DD-MM-YYYY, X days)'.
      If no activity is recorded, safely falls back to: 'Full History (All-Time) as of DD-MM-YYYY'.
      Never displays Unix epoch (1970) or '(0 days)'.

    When lookback_days > 0:
      Displays: 'DD-MM-YYYY to DD-MM-YYYY (X days)'.
    """
    end_local = end.astimezone(LOCAL_TZ)
    if lookback_days > 0:
        since_local = since.astimezone(LOCAL_TZ)
        return f"{since_local:{DATE_FMT}} to {end_local:{DATE_FMT}} ({lookback_days} days)"

    earliest_dt: Optional[datetime] = None
    if activity_dates:
        for item in activity_dates:
            if not item or item in ("-", "--", "---", "None", "No activity", "N/A"):
                continue
            dt_obj: Optional[datetime] = None
            if isinstance(item, datetime):
                dt_obj = item if item.tzinfo else item.replace(tzinfo=timezone.utc)
            elif isinstance(item, str):
                s = item.strip()
                for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt_obj = datetime.strptime(s, fmt).replace(tzinfo=LOCAL_TZ)
                        break
                    except ValueError:
                        continue
                if dt_obj is None:
                    try:
                        dt_obj = parse_ts(s)
                    except Exception:
                        continue

            if dt_obj is not None:
                dt_local = dt_obj.astimezone(LOCAL_TZ)
                # Ignore Unix epoch / 1970 artifacts or future dates beyond end
                if dt_local.year > 1970 and dt_local <= end_local:
                    if earliest_dt is None or dt_local < earliest_dt:
                        earliest_dt = dt_local

    if earliest_dt is not None:
        days_span = max(1, (end_local.date() - earliest_dt.date()).days)
        return f"Full History ({earliest_dt:{DATE_FMT}} to {end_local:{DATE_FMT}}, {days_span:,} days)"
    return f"Full History (All-Time) as of {end_local:{DATE_FMT}}"
