"""Tests for ``apps.api.services.bus_grouping.group_into_buses``.

Covers the case where two physical vehicles report the **same** ``trip_id`` on
the **same** ``service_date`` (the TTC feed reuses scheduled-trip IDs across
the day when blocks rotate). Grouping by ``(trip_id, start_date)`` alone
collapses those into a single ``BusTrajectory`` whose SVG path connects two
disjoint trip runs with a straight diagonal — the visible bug in image4/5.

The fix is to include ``vehicle_id`` in the grouping key. These tests pin the
correct behaviour so the bug can't silently come back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from apps.analytics.stop_projection import RouteStops, StopOnRoute
from apps.api.services.bus_grouping import group_into_buses as _group_into_buses


UTC = timezone.utc


def _stops(n: int = 10, spacing_m: float = 250.0) -> RouteStops:
    stops = tuple(
        StopOnRoute(
            stop_id=f"s{i}",
            stop_name=f"Stop {i}",
            stop_lat=0.0,
            stop_lon=0.0,
            stop_sequence=i,
            distance_m=i * spacing_m,
        )
        for i in range(n)
    )
    return RouteStops(
        route_id="29",
        direction_id=0,
        shape_id="shp",
        shape_length_m=(n - 1) * spacing_m,
        stops=stops,
    )


def _row(
    trip_id: str,
    start_date: str,
    vehicle_id: str,
    t_sec: int,
    dist_m: float,
) -> SimpleNamespace:
    """Build a duck-typed DB row compatible with ``_group_into_buses``."""
    return SimpleNamespace(
        trip_id=trip_id,
        start_date=start_date,
        vehicle_id=vehicle_id,
        datetime=datetime(2026, 4, 23, 13, 0, 0, tzinfo=UTC) + timedelta(seconds=t_sec),
        travel_distance_m=dist_m,
        moving_speed_m_s=5.0,
        occupancy_status="MANY_SEATS_AVAILABLE",
    )


def test_groups_same_trip_id_different_vehicles_separately() -> None:
    """Same (trip_id, start_date) but two vehicles → two BusTrajectories.

    Scenario mirrors the production bug: one vehicle runs the trip in the
    morning, a second vehicle is reassigned to the same scheduled trip_id in
    the afternoon. Current (buggy) grouper keys on (trip_id, start_date)
    only, which merges them and connects their endpoints with a diagonal.
    """
    rows = [
        _row("T1", "20260423", "V1", 0,    500.0),
        _row("T1", "20260423", "V1", 60,   1500.0),
        _row("T1", "20260423", "V1", 120,  2500.0),
        # Second vehicle, same scheduled trip, much later in the day.
        _row("T1", "20260423", "V2", 36000, 500.0),
        _row("T1", "20260423", "V2", 36060, 1500.0),
        _row("T1", "20260423", "V2", 36120, 2500.0),
    ]
    buses = _group_into_buses(rows, _stops())

    assert len(buses) == 2, "two vehicles on same trip_id must not be merged"
    by_vehicle = {b.vehicle_id: b for b in buses}
    assert set(by_vehicle) == {"V1", "V2"}
    assert len(by_vehicle["V1"].points) == 3
    assert len(by_vehicle["V2"].points) == 3


def test_groups_handle_interleaved_timestamps() -> None:
    """Two vehicles run overlapping windows → must still be separated.

    If the DB query returns rows ordered by (trip_id, start_date, datetime)
    only, V1 and V2 rows interleave. The grouper (and the query's ORDER BY)
    must together keep each vehicle's points contiguous. We simulate the
    post-fix query order: (trip_id, start_date, vehicle_id, datetime).
    """
    rows_sorted = [
        # V1 block
        _row("T1", "20260423", "V1", 0,   500.0),
        _row("T1", "20260423", "V1", 60,  1500.0),
        _row("T1", "20260423", "V1", 900, 5000.0),
        # V2 block (overlaps V1 by clock but is sorted after it)
        _row("T1", "20260423", "V2", 120, 600.0),
        _row("T1", "20260423", "V2", 180, 1600.0),
        _row("T1", "20260423", "V2", 960, 5100.0),
    ]
    buses = _group_into_buses(rows_sorted, _stops())

    assert len(buses) == 2
    by_vehicle = {b.vehicle_id: b for b in buses}
    assert by_vehicle["V1"].points[0].travel_distance_m == 500.0
    assert by_vehicle["V1"].points[-1].travel_distance_m == 5000.0
    assert by_vehicle["V2"].points[0].travel_distance_m == 600.0
    assert by_vehicle["V2"].points[-1].travel_distance_m == 5100.0


def test_distinct_trip_ids_still_produce_distinct_buses() -> None:
    """Regression guard: non-buggy data still groups correctly."""
    rows = [
        _row("T1", "20260423", "V1", 0,  500.0),
        _row("T1", "20260423", "V1", 60, 1500.0),
        _row("T2", "20260423", "V2", 0,  500.0),
        _row("T2", "20260423", "V2", 60, 1500.0),
    ]
    buses = _group_into_buses(rows, _stops())
    assert len(buses) == 2
    assert {(b.trip_id, b.vehicle_id) for b in buses} == {("T1", "V1"), ("T2", "V2")}


def test_single_vehicle_single_trip_unchanged() -> None:
    rows = [
        _row("T1", "20260423", "V1", t, 500.0 + t * 10.0)
        for t in range(0, 300, 30)
    ]
    buses = _group_into_buses(rows, _stops())
    assert len(buses) == 1
    assert buses[0].vehicle_id == "V1"
    assert len(buses[0].points) == 10


# ───────────────────────────────────────────────────────────────────────────
# Ghost-trajectory / stale-GPS segmentation
# ───────────────────────────────────────────────────────────────────────────
#
# Separate from the multi-vehicle merging bug: in the TTC feed, individual
# vehicles sometimes keep broadcasting the same ``trip_id`` long after a
# physical trip has ended. The remaining points have ``|moving_speed_m_s|
# < 0.5`` and ``travel_distance_m`` drifts slowly (the rasterised pipeline
# still advances distance for each snapshot). Left alone, those points draw
# as a long shallow diagonal line spanning much of the x-axis — the residual
# diagonals visible after the vehicle-grouping fix. We segment the points at
# stale runs and drop segments that don't actually move.


def _row_spd(
    trip_id: str,
    start_date: str,
    vehicle_id: str,
    t_sec: int,
    dist_m: float,
    speed: float,
) -> SimpleNamespace:
    r = _row(trip_id, start_date, vehicle_id, t_sec, dist_m)
    r.moving_speed_m_s = speed
    return r


def test_stale_tail_after_real_trip_is_trimmed() -> None:
    """Real trip (hour 3) followed by 11 hours of near-zero-speed drift.

    After segmentation, only the real-trip segment survives — the stale tail
    is discarded. The surviving bus's last point is no later than the end of
    the real trip.
    """
    rows = []
    # Hour 3: real trip, speed ~5 m/s, distance grows 0 → 15000 m across 60 min.
    for t in range(0, 3600, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, t * (15000 / 3600), 5.0))
    # Hours 4-15: stale tail, speed ~-0.2, distance slowly decreases.
    stale_start = 3600
    stale_dist_start = 15000.0
    for t in range(stale_start, stale_start + 11 * 3600, 30):
        drift = (t - stale_start) * -0.2
        rows.append(_row_spd("T1", "20260423", "V1", t, stale_dist_start + drift, -0.2))

    buses = _group_into_buses(rows, _stops(n=60, spacing_m=250.0))

    assert len(buses) == 1, "stale tail must be dropped"
    bus = buses[0]
    # Last surviving point is within the real-trip window.
    last_t = (bus.points[-1].datetime - bus.points[0].datetime).total_seconds()
    assert last_t <= 3600, f"stale tail leaked through; bus spans {last_t}s"


def test_two_real_trips_with_long_layover_split_into_two_buses() -> None:
    """Real trip → 30-minute idle → real trip → all under the same trip_id/vehicle.

    The long idle should be treated as a trip boundary and the two real trips
    should emerge as separate BusTrajectory entries.
    """
    rows = []
    # Trip 1: 0–3600 s, speed 5 m/s, d 0→15000
    for t in range(0, 3600, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, t * (15000 / 3600), 5.0))
    # Layover: 3600–5400 s (30 min idle), speed 0, d holds at 15000
    for t in range(3600, 5400, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, 15000.0, 0.0))
    # Trip 2: 5400–9000 s, speed 5 m/s, d 0→15000 (distance reset for the
    # new trip start; the projection does this naturally when a new trip
    # begins at the depot end of the route).
    for t in range(5400, 9000, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, (t - 5400) * (15000 / 3600), 5.0))

    buses = _group_into_buses(rows, _stops(n=60, spacing_m=250.0))

    assert len(buses) == 2, "layover must split the trajectory"
    assert all(b.vehicle_id == "V1" for b in buses)
    assert all(b.trip_id == "T1" for b in buses)


def test_short_dwell_does_not_split() -> None:
    """A normal 2-minute passenger-stop dwell must NOT split the trip."""
    rows = []
    for t in range(0, 1800, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, t * 4.0, 4.0))
    # 2-minute dwell at a stop: speed 0, d flat.
    for t in range(1800, 1920, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, 7200.0, 0.0))
    for t in range(1920, 3600, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, 7200.0 + (t - 1920) * 4.0, 4.0))

    buses = _group_into_buses(rows, _stops(n=60, spacing_m=250.0))
    assert len(buses) == 1, "a short dwell must not split the trip"


def test_all_stale_vehicle_is_dropped() -> None:
    """A vehicle whose entire trajectory is near-zero-speed drift produces no bus."""
    rows = []
    for t in range(0, 11 * 3600, 30):
        rows.append(_row_spd("T1", "20260423", "V1", t, 15000.0 + t * -0.2, -0.2))

    buses = _group_into_buses(rows, _stops(n=60, spacing_m=250.0))
    assert buses == [], "ghost-only vehicle must be dropped"
