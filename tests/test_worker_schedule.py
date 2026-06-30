"""Worker tick scheduling — midnight straddle.

Trips that span local midnight keep their *previous* day's effective
start_date, so the first ticks of a new day must also refresh yesterday or
the final pre-midnight observations are never folded into the trajectory.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from apps.analytics.worker import _service_dates_for_tick

_TZ = ZoneInfo("America/Toronto")


def test_normal_daytime_tick_processes_today_only() -> None:
    now = datetime(2026, 6, 11, 14, 30, tzinfo=_TZ)
    assert _service_dates_for_tick(now) == [date(2026, 6, 11)]


def test_just_after_midnight_processes_yesterday_first() -> None:
    now = datetime(2026, 6, 11, 0, 2, tzinfo=_TZ)
    assert _service_dates_for_tick(now) == [date(2026, 6, 10), date(2026, 6, 11)]


def test_grace_window_boundary() -> None:
    inside = datetime(2026, 6, 11, 0, 59, 59, tzinfo=_TZ)
    outside = datetime(2026, 6, 11, 1, 0, 0, tzinfo=_TZ)
    assert date(2026, 6, 10) in _service_dates_for_tick(inside)
    assert _service_dates_for_tick(outside) == [date(2026, 6, 11)]


def test_month_rollover() -> None:
    now = datetime(2026, 7, 1, 0, 30, tzinfo=_TZ)
    assert _service_dates_for_tick(now) == [date(2026, 6, 30), date(2026, 7, 1)]
