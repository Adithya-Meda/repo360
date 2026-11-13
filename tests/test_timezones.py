"""Unit tests for Repo360 global timezone and timestamp normalization engine."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.common.timezones import (
    DATE_FMT,
    IST,
    LOCAL_TZ,
    LOCAL_TZ_NAME,
    WINDOWS_TZ_MAP,
    fmt_timestamp,
    get_local_timezone,
    get_local_tz_name,
    parse_ts,
)


def test_get_local_timezone_default():
    """Verify default system timezone resolution returns a valid tzinfo instance."""
    tz = get_local_timezone()
    assert tz is not None
    now = datetime.now(tz)
    assert now.tzinfo is not None


def test_get_local_tz_name():
    """Verify get_local_tz_name produces a normalized string."""
    name = get_local_tz_name()
    assert isinstance(name, str)
    assert len(name) >= 2


def test_repo360_timezone_env_override():
    """Verify REPO360_TIMEZONE override takes precedence."""
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "America/New_York"}):
        tz = get_local_timezone()
        dt = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        name = get_local_tz_name(dt.astimezone(tz))
        assert name in ("EST", "EDT")
        formatted = fmt_timestamp(dt, tz=tz)
        # 12:00 UTC in January (EST: UTC-5) is 07:00
        assert formatted == "15-01-2026 07:00"


def test_repo360_timezone_utc():
    """Verify REPO360_TIMEZONE set to UTC behaves consistently."""
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "UTC"}):
        tz = get_local_timezone()
        dt = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
        name = get_local_tz_name(dt.astimezone(tz))
        assert name == "UTC"
        assert fmt_timestamp(dt, tz=tz) == "01-06-2026 14:30"


def test_repo360_timezone_ist_alias():
    """Verify friendly abbreviation 'IST' alias maps to Asia/Kolkata."""
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "IST"}):
        tz = get_local_timezone()
        dt = datetime(2026, 1, 15, 0, 0, tzinfo=timezone.utc)
        name = get_local_tz_name(dt.astimezone(tz))
        assert name == "IST"
        # 00:00 UTC is 05:30 IST (+5:30)
        assert fmt_timestamp(dt, tz=tz) == "15-01-2026 05:30"


def test_tz_env_variable_fallback():
    """Verify standard POSIX TZ variable is honored when REPO360_TIMEZONE is unset."""
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "", "TZ": "Europe/London"}):
        tz = get_local_timezone()
        # Winter date (GMT)
        dt_winter = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
        assert fmt_timestamp(dt_winter, tz=tz) == "10-01-2026 10:00"


def test_windows_tz_mapping():
    """Verify Windows standard timezone names normalize to standard IANA abbreviations."""
    assert WINDOWS_TZ_MAP["India Standard Time"] == "IST"
    assert WINDOWS_TZ_MAP["Pacific Standard Time"] == "PST"
    assert WINDOWS_TZ_MAP["Eastern Standard Time"] == "EST"
    assert WINDOWS_TZ_MAP["Central Europe Standard Time"] == "CET"
    assert WINDOWS_TZ_MAP["Tokyo Standard Time"] == "JST"
    assert WINDOWS_TZ_MAP["AUS Eastern Standard Time"] == "AEST"


def test_parse_ts_formats():
    """Verify parse_ts handles trailing Z, explicit offsets, and naive formats safely."""
    # UTC with Z
    dt_z = parse_ts("2026-05-10T08:30:00Z")
    assert dt_z.tzinfo == timezone.utc
    assert dt_z.hour == 8

    # UTC with +00:00
    dt_off = parse_ts("2026-05-10T08:30:00+00:00")
    assert dt_off.tzinfo == timezone.utc
    assert dt_off.hour == 8

    # Non-UTC offset normalized to UTC
    dt_ny = parse_ts("2026-05-10T08:30:00-04:00")
    assert dt_ny.tzinfo == timezone.utc
    assert dt_ny.hour == 12  # 08:30 EDT is 12:30 UTC
    assert dt_ny.minute == 30


def test_fmt_timestamp_none_and_custom_defaults():
    """Verify fmt_timestamp handles None inputs with customizable default values."""
    assert fmt_timestamp(None) == "No activity"
    assert fmt_timestamp(None, default="-") == "-"
    assert fmt_timestamp(None, default="N/A") == "N/A"


def test_backward_compatibility_aliases():
    """Verify legacy IST exports remain fully functional across all modules."""
    from src.activity.models import IST as ACT_IST
    from src.bypass.models import IST as BYPASS_IST
    from src.dora.models import IST as DORA_IST
    from src.finops.models import IST as FINOPS_IST
    from src.runners.models import IST as RUNNERS_IST

    assert ACT_IST == LOCAL_TZ
    assert BYPASS_IST == LOCAL_TZ
    assert DORA_IST == LOCAL_TZ
    assert FINOPS_IST == LOCAL_TZ
    assert RUNNERS_IST == LOCAL_TZ
