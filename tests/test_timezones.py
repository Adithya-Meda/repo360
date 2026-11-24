"""Unit tests for Repo360 global timezone and timestamp normalization engine."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


from repo360.common.timezones import (
    IST,
    LOCAL_TZ,
    WINDOWS_TZ_MAP,
    fmt_timestamp,
    get_local_timezone,
    get_local_tz_name,
    parse_ts,
    resolve_reporting_window,
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


def test_backward_compatibility_alias():
    """The deprecated ``IST`` name must alias the configured local timezone, never a hardcoded India zone."""
    assert IST is LOCAL_TZ


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX TZ variable is not supported on Windows CRT")
@pytest.mark.parametrize(
    ("tz_spec", "zone"),
    [("EST5EDT", "America/New_York"), ("UTC0", "UTC")],
)
def test_system_local_timezone_applies_dst_per_instant(tz_spec, zone):
    """Without overrides, conversions must follow OS DST rules per timestamp (not one fixed startup offset)."""
    import subprocess
    import sys
    from pathlib import Path

    script = (
        "from datetime import datetime, timedelta, timezone\n"
        "from zoneinfo import ZoneInfo\n"
        "from repo360.common.timezones import SystemLocalTimezone\n"
        "tz, ref = SystemLocalTimezone(), ZoneInfo(%r)\n"
        "base = datetime(2026, 1, 1, tzinfo=timezone.utc)\n"
        "bad = 0\n"
        "for m in range(0, 366 * 24 * 2):\n"
        "    p = base + timedelta(minutes=30 * m)\n"
        "    a, b = p.astimezone(tz), p.astimezone(ref)\n"
        "    bad += (a.strftime('%%Y-%%m-%%d %%H:%%M'), a.utcoffset()) != (b.strftime('%%Y-%%m-%%d %%H:%%M'), b.utcoffset())\n"
        "    bad += a.astimezone(timezone.utc) != p\n"
        "print(bad)\n"
    ) % zone
    env = {k: v for k, v in os.environ.items() if k not in ("REPO360_TIMEZONE",)}
    env["TZ"] = tz_spec
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120, check=True)
    assert out.stdout.strip() == "0"


@pytest.mark.parametrize("bad_tz", ["Asia", "America", ":UTC", "Not/A_Zone"])
def test_invalid_timezone_env_falls_back_without_crashing(bad_tz):
    """Zone directory names and glibc-style TZ values must not crash module import or resolution."""
    with patch.dict(os.environ, {"REPO360_TIMEZONE": bad_tz}):
        tz = get_local_timezone()
        assert datetime.now(tz).tzinfo is not None


def test_numeric_zone_names_get_uniform_utc_offset_label():
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "Pacific/Kiritimati"}):
        assert get_local_tz_name() == "UTC+14:00"
    with patch.dict(os.environ, {"REPO360_TIMEZONE": "America/Sao_Paulo"}):
        assert get_local_tz_name() == "UTC-03:00"


def test_resolve_reporting_window_lookback_positive():
    """Verify positive lookback_days formats as 'since to end (X days)'."""
    since = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    res = resolve_reporting_window(90, since, end)
    assert "(90 days)" in res
    assert "2026" in res
    assert "1970" not in res
    assert "(0 days)" not in res


def test_resolve_reporting_window_all_time_with_activity():
    """Verify lookback_days=0 derives inception date from activity records and never shows 1970."""
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    activity_dates = [
        "15-04-2023 10:30",
        "20-08-2024 16:45",
        datetime(2022, 5, 10, 8, 0, tzinfo=timezone.utc),
        "-",
        "No activity",
    ]
    res = resolve_reporting_window(0, since, end, activity_dates)
    assert "Full History" in res
    assert "10-05-2022" in res
    assert "1970" not in res
    assert "(0 days)" not in res
    assert "days)" in res


def test_resolve_reporting_window_all_time_empty_activity():
    """Verify lookback_days=0 safely falls back to 'Full History (All-Time)' when no activity exists."""
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    res = resolve_reporting_window(0, since, end, [])
    assert "Full History (All-Time)" in res
    assert "1970" not in res
    assert "(0 days)" not in res


def test_resolve_reporting_window_ignores_epoch_artifacts():
    """Verify epoch 1970 artifacts in activity dates are ignored."""
    since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    activity_dates = [
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        "01-01-1970 05:30",
        "15-03-2024 14:00",
    ]
    res = resolve_reporting_window(0, since, end, activity_dates)
    assert "Full History" in res
    assert "15-03-2024" in res
    assert "1970" not in res

