"""Unit tests for ``apps.analytics.trajectory_extract.build_trip_trajectory``.

Constructs fake ``VehiclePosition`` rows in-memory (no DB) and a tiny synthetic
``trips_df``; asserts join correctness, sort order, and dedup.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from apps.analytics.trajectory_extract import (
    _parse_start_datetime,
    build_trip_trajectory,
)


def _fake_vp(
    *,
    id_: int,
    dt: datetime,
    lat: float | None = 43.65,
    lon: float | None = -79.38,
    trip_id: str = "T1",
    start_date: str = "20260420",
    start_time: str = "08:00:00",
    route_id: str = "29",
    direction_id: int | None = 0,
    vehicle_id: str = "V1",
    occupancy_status: str | None = "MANY_SEATS_AVAILABLE",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_,
        vehicle_timestamp=dt,
        fetched_at=dt,
        latitude=lat,
        longitude=lon,
        vehicle_id=vehicle_id,
        occupancy_status=occupancy_status,
        trip_id=trip_id,
        start_date=start_date,
        start_time=start_time,
        route_id=route_id,
        direction_id=direction_id,
    )


def _trips_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trip_id": "T1", "shape_id": "S1", "direction_id": 0},
            {"trip_id": "T2", "shape_id": "S2", "direction_id": 1},
        ]
    )


def test_parse_start_datetime_normal() -> None:
    dt = _parse_start_datetime("20260420", "08:15:00")
    assert dt is not None
    assert dt.tzinfo is not None
    # April is DST: 08:15 Toronto (EDT, UTC-4) == 12:15 UTC.
    assert dt.hour == 12 and dt.minute == 15


def test_parse_start_datetime_overnight_27_15() -> None:
    # 27:15:00 == 03:15 the following service-day morning.
    dt = _parse_start_datetime("20260420", "27:15:00")
    assert dt is not None
    # 27:15 Toronto == 03:15 next day Toronto (EDT) == 07:15 UTC on 2026-04-21.
    assert dt.day == 21
    assert dt.hour == 7 and dt.minute == 15


def test_parse_start_datetime_standard_time_winter() -> None:
    # January is standard time: 08:15 Toronto (EST, UTC-5) == 13:15 UTC.
    dt = _parse_start_datetime("20260120", "08:15:00")
    assert dt is not None
    assert dt.hour == 13 and dt.minute == 15


def test_parse_start_datetime_invalid_returns_none() -> None:
    assert _parse_start_datetime("", "08:00:00") is None
    assert _parse_start_datetime("not-a-date", "08:00:00") is None


def test_build_trip_trajectory_joins_shape_id_and_sorts() -> None:
    t0 = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 20, 13, 0, 10, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 20, 13, 0, 20, tzinfo=timezone.utc)
    # Deliberately pass in reverse order.
    rows = [
        _fake_vp(id_=3, dt=t2),
        _fake_vp(id_=1, dt=t0),
        _fake_vp(id_=2, dt=t1),
    ]
    df = build_trip_trajectory(rows, _trips_df())
    assert df["source_vehicle_position_id"].tolist() == [1, 2, 3]
    assert df["shape_id"].tolist() == ["S1", "S1", "S1"]
    assert "trip_start_datetime" in df.columns
    # Trip start 08:00 EDT == 12:00 UTC; observations begin 13:00 UTC.
    assert df["time_offset_seconds"].tolist() == [3600, 3610, 3620]


def test_build_trip_trajectory_drops_null_latlon() -> None:
    t0 = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 20, 13, 0, 10, tzinfo=timezone.utc)
    rows = [
        _fake_vp(id_=1, dt=t0),
        _fake_vp(id_=2, dt=t1, lat=None, lon=None),
    ]
    df = build_trip_trajectory(rows, _trips_df())
    assert df["source_vehicle_position_id"].tolist() == [1]


def test_build_trip_trajectory_dedupes_same_timestamp_keeps_latest_id() -> None:
    t0 = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    rows = [
        _fake_vp(id_=1, dt=t0, lat=43.65),
        _fake_vp(id_=2, dt=t0, lat=43.66),
    ]
    df = build_trip_trajectory(rows, _trips_df())
    assert len(df) == 1
    assert df["source_vehicle_position_id"].iloc[0] == 2


def test_build_trip_trajectory_empty_rows_returns_empty_with_schema() -> None:
    df = build_trip_trajectory([], _trips_df())
    assert df.empty
    for col in ("datetime", "shape_id", "trip_id", "time_offset_seconds"):
        assert col in df.columns


def test_build_trip_trajectory_fills_missing_direction_from_static() -> None:
    t0 = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc)
    rows = [_fake_vp(id_=1, dt=t0, trip_id="T2", direction_id=None)]
    df = build_trip_trajectory(rows, _trips_df())
    assert df["direction_id"].iloc[0] == 1
