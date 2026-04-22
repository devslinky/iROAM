"""iROAM Dashboard data endpoints.

The front-end (``apps/api/static/iroam.html``) calls four JSON endpoints:

  GET /iroam/routes
      → catalog of routes + service dates + direction IDs with trajectories

  GET /iroam/stops?route_id=&direction_id=
      → ordered stops along the canonical shape with distance-along-route

  GET /iroam/buses?route_id=&service_date=&direction_id=&bunch_sec=&idle_min=&crowd_pct=
      → one entry per trip instance with ``points=[{t,d}]`` and
        ``anomalies=[{t,d,type}]`` in the exact shape the dashboard renders.
        ``t`` = minutes-of-day (local TZ), ``d`` = fractional stop index.

  GET /iroam/analytics?route_id=&service_date=&direction_id=&bunch_sec=&idle_min=&crowd_pct=
      → hour-by-hour aggregates for the Analytics page (anomaly counts per
        hour, per stop; operating speed per hour; running-time allocation).

All endpoints are read-only and safe to hit on every slider tweak — the slice
is already bounded by (route, date, direction).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.analytics.anomalies import (
    OCCUPANCY_PCT,
    BusTrajectory,
    TrajectoryPoint,
    _to_minute_of_day,
    detect_all,
)
from apps.analytics.stop_projection import (
    compute_route_stops,
    distance_to_stop_index,
)
from apps.api.deps import get_db
from db.queries.iroam import fetch_trajectories_for_slice, list_route_catalog

router = APIRouter(prefix="/iroam", tags=["iroam"])


@router.get("/routes")
def iroam_routes(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Catalog of routes with trajectory data and their available service dates."""
    return list_route_catalog(db)


@router.get("/stops")
def iroam_stops(
    route_id: str = Query(...),
    direction_id: int = Query(..., ge=0, le=1),
) -> dict[str, Any]:
    """Ordered stops along the canonical shape for (route, direction)."""
    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        raise HTTPException(
            status_code=404,
            detail=f"no static-GTFS shape for route_id={route_id} direction_id={direction_id}",
        )
    return {
        "route_id": route_stops.route_id,
        "direction_id": route_stops.direction_id,
        "shape_id": route_stops.shape_id,
        "shape_length_m": route_stops.shape_length_m,
        "stops": [
            {
                "stop_id": s.stop_id,
                "name": s.stop_name,
                "lat": s.stop_lat,
                "lon": s.stop_lon,
                "sequence": s.stop_sequence,
                "distance_m": s.distance_m,
            }
            for s in route_stops.stops
        ],
    }


@router.get("/buses")
def iroam_buses(
    route_id: str = Query(...),
    service_date: date = Query(...),
    direction_id: int = Query(..., ge=0, le=1),
    bunch_sec: float = Query(default=120, ge=10, le=600),
    idle_min: float = Query(default=5, ge=0.5, le=60),
    crowd_pct: float = Query(default=100, ge=0, le=150),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Dashboard-shaped bus data for one (route, date, direction) slice."""
    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        raise HTTPException(
            status_code=404,
            detail=f"no static-GTFS shape for route_id={route_id} direction_id={direction_id}",
        )

    rows = fetch_trajectories_for_slice(
        db, service_date=service_date, route_id=route_id, direction_id=direction_id
    )
    buses = _group_into_buses(rows, route_stops)
    events = detect_all(
        buses,
        bunch_seconds_threshold=bunch_sec,
        idle_min_threshold=idle_min,
        crowd_pct_threshold=crowd_pct,
    )

    events_by_bus: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        events_by_bus[ev.bus_index].append(
            {"t": round(ev.minute_of_day, 2), "d": round(ev.stop_index, 3), "type": ev.type}
        )

    min_t = float("inf")
    max_t = float("-inf")
    payload_buses = []
    for bus in buses:
        pts = [
            {"t": round(_to_minute_of_day(p.datetime), 2), "d": round(p.stop_index, 3)}
            for p in bus.points
        ]
        if pts:
            min_t = min(min_t, pts[0]["t"])
            max_t = max(max_t, pts[-1]["t"])
        payload_buses.append(
            {
                "id": bus.bus_index,
                "trip_id": bus.trip_id,
                "start_date": bus.start_date,
                "vehicle_id": bus.vehicle_id,
                "points": pts,
                "anomalies": events_by_bus.get(bus.bus_index, []),
            }
        )

    # Anomaly totals for the top-bar legend.
    totals = Counter(ev.type for ev in events)

    return {
        "route_id": route_id,
        "direction_id": direction_id,
        "service_date": service_date.isoformat(),
        "num_stops": len(route_stops.stops),
        "time_window": {
            "start": min_t if min_t != float("inf") else None,
            "end": max_t if max_t != float("-inf") else None,
        },
        "totals": {"bunch": totals["bunch"], "idle": totals["idle"], "crowd": totals["crowd"]},
        "buses": payload_buses,
    }


@router.get("/analytics")
def iroam_analytics(
    route_id: str = Query(...),
    service_date: date = Query(...),
    direction_id: int = Query(..., ge=0, le=1),
    bunch_sec: float = Query(default=120, ge=10, le=600),
    idle_min: float = Query(default=5, ge=0.5, le=60),
    crowd_pct: float = Query(default=100, ge=0, le=150),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate metrics for the Analytics page."""
    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        raise HTTPException(status_code=404, detail="no static-GTFS shape")

    rows = fetch_trajectories_for_slice(
        db, service_date=service_date, route_id=route_id, direction_id=direction_id
    )
    buses = _group_into_buses(rows, route_stops)
    events = detect_all(
        buses,
        bunch_seconds_threshold=bunch_sec,
        idle_min_threshold=idle_min,
        crowd_pct_threshold=crowd_pct,
    )

    # Per-hour anomaly counts.
    hour_bunch = [0] * 24
    hour_idle = [0] * 24
    hour_crowd = [0] * 24
    for ev in events:
        h = int(ev.minute_of_day // 60) % 24
        if ev.type == "bunch":
            hour_bunch[h] += 1
        elif ev.type == "idle":
            hour_idle[h] += 1
        elif ev.type == "crowd":
            hour_crowd[h] += 1

    # Per-stop anomaly frequency (integer-rounded).
    stop_freq = [0] * len(route_stops.stops)
    for ev in events:
        si = int(round(ev.stop_index))
        if 0 <= si < len(stop_freq):
            stop_freq[si] += 1

    # Per-hour mean operating speed in km/h (only over "moving" samples).
    hour_speed_sum = [0.0] * 24
    hour_speed_n = [0] * 24
    for bus in buses:
        for p in bus.points:
            speed = p.moving_speed_m_s or 0.0
            if speed < 0.5:
                continue
            h = int(_to_minute_of_day(p.datetime) // 60) % 24
            hour_speed_sum[h] += speed * 3.6
            hour_speed_n[h] += 1
    speed_by_hour = [
        (hour_speed_sum[h] / hour_speed_n[h]) if hour_speed_n[h] > 0 else None
        for h in range(24)
    ]

    # Per-hour running-time allocation: fraction of samples in each state.
    # States: normal, idle, bunch_zone, crowd. bunch_zone = currently part of a
    # bunch event's vicinity (any sample within 30s of a bunch-event crossing).
    # Implementation: for each point, decide its dominant state by checking
    # (1) idle (speed<eps), (2) crowd (occupancy≥threshold), (3) bunch (time
    # proximity to any bunch event on the same bus), else normal.
    bunch_times_by_bus: dict[int, list[float]] = defaultdict(list)
    for ev in events:
        if ev.type == "bunch":
            bunch_times_by_bus[ev.bus_index].append(ev.minute_of_day)

    hour_totals = [0] * 24
    hour_idle_n = [0] * 24
    hour_bunch_n = [0] * 24
    hour_crowd_n = [0] * 24
    for bus in buses:
        b_times = sorted(bunch_times_by_bus.get(bus.bus_index, []))
        for p in bus.points:
            t_min = _to_minute_of_day(p.datetime)
            h = int(t_min // 60) % 24
            hour_totals[h] += 1
            speed = p.moving_speed_m_s or 0.0
            occ_pct = OCCUPANCY_PCT.get((p.occupancy_status or "").upper(), -1)
            if speed < 0.5:
                hour_idle_n[h] += 1
            elif occ_pct >= crowd_pct and occ_pct >= 0:
                hour_crowd_n[h] += 1
            elif _near_any(t_min, b_times, within=0.5):
                hour_bunch_n[h] += 1

    allocation = []
    for h in range(24):
        total = hour_totals[h]
        if total == 0:
            allocation.append([0, 0, 0, 0])
            continue
        idle_pct = 100 * hour_idle_n[h] / total
        bunch_pct = 100 * hour_bunch_n[h] / total
        crowd_pct_h = 100 * hour_crowd_n[h] / total
        normal_pct = max(0.0, 100 - idle_pct - bunch_pct - crowd_pct_h)
        allocation.append([
            round(normal_pct, 1),
            round(idle_pct, 1),
            round(bunch_pct, 1),
            round(crowd_pct_h, 1),
        ])

    return {
        "route_id": route_id,
        "service_date": service_date.isoformat(),
        "direction_id": direction_id,
        "num_vehicles": len({b.vehicle_id for b in buses if b.vehicle_id}),
        "hour_bunch": hour_bunch,
        "hour_idle": hour_idle,
        "hour_crowd": hour_crowd,
        "speed_by_hour": speed_by_hour,
        "stop_frequency": stop_freq,
        "allocation_by_hour": allocation,
    }


def _near_any(t: float, sorted_times: list[float], *, within: float) -> bool:
    """Binary-search proximity check: is any value in ``sorted_times`` within ``within`` of ``t``?"""
    if not sorted_times:
        return False
    import bisect

    i = bisect.bisect_left(sorted_times, t)
    for j in (i - 1, i):
        if 0 <= j < len(sorted_times) and abs(sorted_times[j] - t) <= within:
            return True
    return False


def _group_into_buses(rows: list, route_stops) -> list[BusTrajectory]:
    """Walk the DB rows (sorted by trip/start_date/datetime) → one BusTrajectory per instance."""
    buses: list[BusTrajectory] = []
    current_key: tuple[str, str] | None = None
    current_points: list[TrajectoryPoint] = []
    current_vehicle: str | None = None

    def flush(key: tuple[str, str] | None) -> None:
        if key is None or not current_points:
            return
        buses.append(
            BusTrajectory(
                bus_index=len(buses),
                trip_id=key[0],
                start_date=key[1],
                vehicle_id=current_vehicle,
                points=list(current_points),
            )
        )

    for r in rows:
        key = (r.trip_id, r.start_date)
        if key != current_key:
            flush(current_key)
            current_points = []
            current_vehicle = r.vehicle_id
            current_key = key
        stop_idx = distance_to_stop_index(r.travel_distance_m, route_stops)
        current_points.append(
            TrajectoryPoint(
                datetime=r.datetime,
                travel_distance_m=r.travel_distance_m,
                moving_speed_m_s=r.moving_speed_m_s,
                occupancy_status=r.occupancy_status,
                stop_index=stop_idx,
            )
        )
    flush(current_key)
    return buses
