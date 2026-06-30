"""Tests for scheduled-headway extraction from a synthetic GTFS bundle."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from apps.analytics.schedule_headways import (
    _parse_gtfs_time_s,
    active_service_ids,
    scheduled_headway_s,
)


@pytest.fixture()
def gtfs_dir(tmp_path: Path) -> Path:
    (tmp_path / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260601,20260630\n"
        "sat,0,0,0,0,0,1,0,20260601,20260630\n"
    )
    (tmp_path / "calendar_dates.txt").write_text(
        "service_id,date,exception_type\n"
        "wk,20260610,2\n"      # service removed on the 10th (a Wednesday)
        "sat,20260610,1\n"     # saturday schedule runs instead
    )
    (tmp_path / "trips.txt").write_text(
        "trip_id,route_id,service_id,direction_id,shape_id\n"
        "t1,29,wk,0,s1\n"
        "t2,29,wk,0,s1\n"
        "t3,29,wk,0,s1\n"
        "t4,29,wk,1,s2\n"
        "t5,29,sat,0,s1\n"
        "t6,29,sat,0,s1\n"
    )
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_name,stop_lat,stop_lon\na,A,43.7,-79.4\n"
    )
    (tmp_path / "shapes.txt").write_text(
        "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        "s1,43.7,-79.4,1\ns1,43.71,-79.41,2\n"
    )
    (tmp_path / "routes.txt").write_text("route_id,route_short_name\n29,29\n")
    (tmp_path / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,a,1\n"
        "t1,08:10:00,08:10:00,a,2\n"      # later stop must not win
        "t2,08:06:00,08:06:00,a,1\n"
        "t3,08:18:00,08:18:00,a,1\n"
        "t4,08:00:00,08:00:00,a,1\n"
        "t5,09:00:00,09:00:00,a,1\n"
        "t6,09:20:00,09:20:00,a,1\n"
    )
    return tmp_path


def test_parse_gtfs_time_handles_over_24h() -> None:
    assert _parse_gtfs_time_s("08:06:30") == 8 * 3600 + 6 * 60 + 30
    assert _parse_gtfs_time_s("25:15:00") == 25 * 3600 + 15 * 60
    assert _parse_gtfs_time_s("bogus") is None


def test_active_service_ids_weekday_window_and_exceptions(gtfs_dir: Path) -> None:
    # Tuesday the 9th: weekday service.
    assert active_service_ids(date(2026, 6, 9), gtfs_dir) == frozenset({"wk"})
    # Wednesday the 10th: wk removed, sat added by exception.
    assert active_service_ids(date(2026, 6, 10), gtfs_dir) == frozenset({"sat"})
    # Saturday the 13th: sat by weekday flag.
    assert active_service_ids(date(2026, 6, 13), gtfs_dir) == frozenset({"sat"})
    # Outside the calendar window.
    assert active_service_ids(date(2026, 7, 7), gtfs_dir) == frozenset()


def test_scheduled_headway_consecutive_trips(gtfs_dir: Path) -> None:
    d = date(2026, 6, 9)  # weekday
    # t1 is the first trip of the day → no predecessor.
    assert scheduled_headway_s("t1", "29", 0, d, gtfs_dir) is None
    # t2 follows t1 by 6 min; t3 follows t2 by 12 min.
    assert scheduled_headway_s("t2", "29", 0, d, gtfs_dir) == 360.0
    assert scheduled_headway_s("t3", "29", 0, d, gtfs_dir) == 720.0
    # Other direction has a single trip → no headway.
    assert scheduled_headway_s("t4", "29", 1, d, gtfs_dir) is None


def test_scheduled_headway_respects_service_calendar(gtfs_dir: Path) -> None:
    # On the exception date only sat trips run: t6 follows t5 by 20 min,
    # and the weekday trips resolve to None.
    d = date(2026, 6, 10)
    assert scheduled_headway_s("t6", "29", 0, d, gtfs_dir) == 1200.0
    assert scheduled_headway_s("t2", "29", 0, d, gtfs_dir) is None


def test_missing_calendar_returns_unknown(tmp_path: Path) -> None:
    assert active_service_ids(date(2026, 6, 9), tmp_path) is None
