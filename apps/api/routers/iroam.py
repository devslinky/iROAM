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
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.analytics.anomalies import (
    OCCUPANCY_PCT,
    detect_all,
    to_minute_of_day,
)
from apps.analytics.stop_projection import compute_route_stops
from apps.api.deps import get_db
from apps.api.services.bunching_predictor import PredictorUnavailable
from apps.api.services.bus_grouping import group_into_buses
from apps.api.services.forecast import run_forecast
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
    bunch_dist: float = Query(default=150, ge=10, le=1000),
    bunch_method: Literal["time", "distance", "both"] = Query(default="time"),
    # Distance-detector hysteresis (exit threshold, m) and minimum run length
    # (s). Defaults preserve the pre-hysteresis behaviour exactly.
    bunch_dist_exit: float | None = Query(default=None, ge=10, le=2000),
    bunch_min_dur: float = Query(default=0, ge=0, le=3600),
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
    buses = group_into_buses(rows, route_stops)
    events = detect_all(
        buses,
        bunch_seconds_threshold=bunch_sec,
        idle_min_threshold=idle_min,
        crowd_pct_threshold=crowd_pct,
        bunch_distance_threshold_m=bunch_dist,
        bunch_method=bunch_method,
        bunch_distance_exit_m=bunch_dist_exit,
        bunch_min_duration_s=bunch_min_dur,
    )

    events_by_bus: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        entry: dict[str, Any] = {
            "t": round(ev.minute_of_day, 2),
            "d": round(ev.stop_index, 3),
            "type": ev.type,
        }
        if ev.method is not None:
            entry["method"] = ev.method
        events_by_bus[ev.bus_index].append(entry)

    min_t = float("inf")
    max_t = float("-inf")
    payload_buses = []
    for bus in buses:
        pts = [
            {"t": round(to_minute_of_day(p.datetime), 2), "d": round(p.stop_index, 3)}
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

    # Anomaly totals for the top-bar legend. (Distinct names from the
    # ``bunch_dist`` threshold parameter — shadowing it here once caused a
    # confusing read.)
    totals = Counter(ev.type for ev in events)
    bunch_time_count = sum(1 for ev in events if ev.type == "bunch" and ev.method == "time")
    bunch_dist_count = sum(1 for ev in events if ev.type == "bunch" and ev.method == "distance")

    return {
        "route_id": route_id,
        "direction_id": direction_id,
        "service_date": service_date.isoformat(),
        "num_stops": len(route_stops.stops),
        "time_window": {
            "start": min_t if min_t != float("inf") else None,
            "end": max_t if max_t != float("-inf") else None,
        },
        "totals": {
            "bunch": totals["bunch"],
            "idle": totals["idle"],
            "crowd": totals["crowd"],
            "bunch_time": bunch_time_count,
            "bunch_dist": bunch_dist_count,
        },
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
    bunch_dist: float = Query(default=150, ge=10, le=1000),
    bunch_method: Literal["time", "distance", "both"] = Query(default="time"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate metrics for the Analytics page."""
    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        raise HTTPException(status_code=404, detail="no static-GTFS shape")

    rows = fetch_trajectories_for_slice(
        db, service_date=service_date, route_id=route_id, direction_id=direction_id
    )
    buses = group_into_buses(rows, route_stops)
    events = detect_all(
        buses,
        bunch_seconds_threshold=bunch_sec,
        idle_min_threshold=idle_min,
        crowd_pct_threshold=crowd_pct,
        bunch_distance_threshold_m=bunch_dist,
        bunch_method=bunch_method,
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

    # Per-stop anomaly frequency (integer-rounded). Kept as a flat array
    # ``stop_frequency`` for the aggregate view and also split by type so the
    # Analytics page's "Anomaly Frequency by Stop" chart can filter.
    n_stops = len(route_stops.stops)
    stop_freq = [0] * n_stops
    stop_freq_bunch = [0] * n_stops
    stop_freq_idle = [0] * n_stops
    stop_freq_crowd = [0] * n_stops
    for ev in events:
        si = int(round(ev.stop_index))
        if 0 <= si < n_stops:
            stop_freq[si] += 1
            if ev.type == "bunch":
                stop_freq_bunch[si] += 1
            elif ev.type == "idle":
                stop_freq_idle[si] += 1
            elif ev.type == "crowd":
                stop_freq_crowd[si] += 1

    # Per-hour mean operating speed in km/h (only over "moving" samples).
    hour_speed_sum = [0.0] * 24
    hour_speed_n = [0] * 24
    for bus in buses:
        for p in bus.points:
            speed = p.moving_speed_m_s or 0.0
            if speed < 0.5:
                continue
            h = int(to_minute_of_day(p.datetime) // 60) % 24
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
            t_min = to_minute_of_day(p.datetime)
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
        "stop_frequency_by_type": {
            "bunch": stop_freq_bunch,
            "idle": stop_freq_idle,
            "crowd": stop_freq_crowd,
        },
        "allocation_by_hour": allocation,
    }


@router.get("/forecast")
def iroam_forecast(
    route_id: str = Query(...),
    service_date: date = Query(...),
    direction_id: int = Query(..., ge=0, le=1),
    t_ref_min: float = Query(
        ...,
        ge=0.0,
        le=1440.0,
        description="Reference time as minute-of-day (America/Toronto). "
        "Live mode: use latest /iroam/buses time_window.end. "
        "Historical mode: use current playhead.",
    ),
    freshness_s: float = Query(default=90.0, ge=10.0, le=600.0),
    edge_exclude: int = Query(default=2, ge=0, le=25),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-bus + aggregate bunching-risk forecast for one (route, date, direction).

    Runs the bundled LightGBM predictor (see ``deployment/bunching_lightgbm``)
    over every "running" bus in the slice: fresh sample, inside the active-stop
    band, enough history to fill a 60-tick × 9-channel window. Returns per-bus
    probability horizons + two aggregate series (``any_alert_rate`` and
    ``mean_prob``) sized to the model's 30-step horizon.

    Shape is stable across modes; the frontend passes the same payload shape
    into its Forecast panel whether this is live or historical.
    """
    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        raise HTTPException(
            status_code=404,
            detail=f"no static-GTFS shape for route_id={route_id} direction_id={direction_id}",
        )

    rows = fetch_trajectories_for_slice(
        db, service_date=service_date, route_id=route_id, direction_id=direction_id
    )
    buses = group_into_buses(rows, route_stops)

    try:
        result = run_forecast(
            buses,
            num_stops=len(route_stops.stops),
            t_ref_min=t_ref_min,
            freshness_s=freshness_s,
            edge_exclude=edge_exclude,
            # Pass shape length so the service can truncate each bus's
            # per-horizon prediction to its plausible remaining trip time —
            # see forecast.py docstring for why this matters.
            route_shape_length_m=float(route_stops.shape_length_m),
            # Mean stop latitude sharpens the unit conversion for bundles
            # trained on pre-fix EPSG:3857 distances (see forecast.py).
            route_mean_lat_deg=(
                sum(s.stop_lat for s in route_stops.stops) / len(route_stops.stops)
                if route_stops.stops
                else None
            ),
            # Identity passthrough so shadow mode (when enabled) can log
            # which slice each prediction came from. No effect otherwise.
            route_id=route_id,
            direction_id=int(direction_id),
            service_date_iso=service_date.isoformat(),
        )
    except PredictorUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"bunching predictor unavailable: {exc}",
        ) from exc

    return {
        "route_id": route_id,
        "service_date": service_date.isoformat(),
        "direction_id": direction_id,
        "t_ref_min": result.t_ref_min,
        "horizon_steps": result.horizon_steps,
        "step_seconds": result.step_seconds,
        "seq_len": result.seq_len,
        "feature_set": result.feature_set,
        "model_label": result.model_label,
        "shadow_mode": result.shadow_mode,
        "horizon_cap_min": result.horizon_cap_min,
        "input_distance_scale": result.input_distance_scale,
        "thresholds": result.thresholds,
        "num_buses_total": result.num_buses_total,
        "num_running": result.num_running,
        "num_eligible": result.num_eligible,
        "per_bus": result.per_bus,
        "horizon_summary": result.horizon_summary,
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
