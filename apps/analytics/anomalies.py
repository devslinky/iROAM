"""Anomaly detection over upsampled trip trajectories.

Produces the three event types the iROAM dashboard renders:

  * ``idle``  — bus stationary for ≥ ``idle_min_threshold`` minutes
  * ``bunch`` — two consecutive trip instances pass the same point within
                ≤ ``bunch_seconds_threshold`` of each other
  * ``crowd`` — a point's GTFS-RT OccupancyStatus maps to ≥ ``crowd_pct_threshold``

Input is a list of ``BusTrajectory`` rows (one per trip instance) with their
time/distance samples. Output is a flat list of ``AnomalyEvent`` rows keyed by
``bus_index`` so the caller can merge them back per-bus.

All functions here are pure — no DB calls, no I/O — so they're trivial to
unit-test and cheap to re-run on every threshold change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

IDLE_SPEED_THRESHOLD_M_S = 0.5

# Map GTFS-RT OccupancyStatus enum to a coarse "percent full" value.
# Values are advisory only — the feed doesn't give true load factors.
OCCUPANCY_PCT = {
    "EMPTY": 0,
    "MANY_SEATS_AVAILABLE": 25,
    "FEW_SEATS_AVAILABLE": 55,
    "STANDING_ROOM_ONLY": 75,
    "CRUSHED_STANDING_ROOM_ONLY": 95,
    "FULL": 100,
    "NOT_ACCEPTING_PASSENGERS": 100,
}

AnomalyType = Literal["bunch", "idle", "crowd"]


@dataclass(frozen=True)
class TrajectoryPoint:
    datetime: datetime
    travel_distance_m: float
    moving_speed_m_s: float | None
    occupancy_status: str | None
    stop_index: float  # fractional — precomputed by stop_projection


@dataclass(frozen=True)
class BusTrajectory:
    bus_index: int         # per-request zero-based index used by the dashboard
    trip_id: str
    start_date: str
    vehicle_id: str | None
    points: list[TrajectoryPoint]


@dataclass(frozen=True)
class AnomalyEvent:
    bus_index: int
    minute_of_day: float    # time in minutes since midnight (UTC), float for sub-minute precision
    stop_index: float
    type: AnomalyType


def _to_minute_of_day(dt: datetime) -> float:
    """Convert a UTC datetime to a float minute-of-day in America/Toronto.

    The design plots minute-of-day from 360 (6:00) to 1320 (22:00) on a local
    wall-clock axis; we convert from UTC once here so callers can just hand
    back the value.
    """
    # Lazy import — zoneinfo is stdlib in 3.9+ but importing once per call is fine.
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo("America/Toronto"))
    except Exception:  # pragma: no cover
        local = dt
    return local.hour * 60 + local.minute + local.second / 60.0


def detect_idle_events(
    bus: BusTrajectory, *, idle_min_threshold: float
) -> list[AnomalyEvent]:
    """Emit one event per contiguous stationary run ≥ ``idle_min_threshold`` minutes."""
    events: list[AnomalyEvent] = []
    threshold_s = idle_min_threshold * 60.0
    run_start: TrajectoryPoint | None = None
    last_in_run: TrajectoryPoint | None = None

    def flush(start: TrajectoryPoint, end: TrajectoryPoint) -> None:
        dur = (end.datetime - start.datetime).total_seconds()
        if dur < threshold_s:
            return
        mid_epoch = (start.datetime.timestamp() + end.datetime.timestamp()) / 2.0
        mid_dt = datetime.fromtimestamp(mid_epoch, tz=start.datetime.tzinfo)
        mid_dist_m = (start.travel_distance_m + end.travel_distance_m) / 2.0
        # Weighted stop_index at midpoint (start/end have same stop_index when
        # idle, but use the average to be safe).
        mid_stop_idx = (start.stop_index + end.stop_index) / 2.0
        events.append(
            AnomalyEvent(
                bus_index=bus.bus_index,
                minute_of_day=_to_minute_of_day(mid_dt),
                stop_index=mid_stop_idx,
                type="idle",
            )
        )

    for p in bus.points:
        speed = p.moving_speed_m_s or 0.0
        is_idle = speed < IDLE_SPEED_THRESHOLD_M_S
        if is_idle:
            if run_start is None:
                run_start = p
            last_in_run = p
        else:
            if run_start is not None and last_in_run is not None:
                flush(run_start, last_in_run)
            run_start = None
            last_in_run = None

    if run_start is not None and last_in_run is not None:
        flush(run_start, last_in_run)

    return events


def detect_crowd_events(
    bus: BusTrajectory, *, crowd_pct_threshold: float
) -> list[AnomalyEvent]:
    """Emit one event per contiguous run where occupancy%≥threshold."""
    events: list[AnomalyEvent] = []
    run_start: TrajectoryPoint | None = None
    last_in_run: TrajectoryPoint | None = None

    def flush(start: TrajectoryPoint, end: TrajectoryPoint) -> None:
        mid_epoch = (start.datetime.timestamp() + end.datetime.timestamp()) / 2.0
        mid_dt = datetime.fromtimestamp(mid_epoch, tz=start.datetime.tzinfo)
        mid_stop = (start.stop_index + end.stop_index) / 2.0
        events.append(
            AnomalyEvent(
                bus_index=bus.bus_index,
                minute_of_day=_to_minute_of_day(mid_dt),
                stop_index=mid_stop,
                type="crowd",
            )
        )

    for p in bus.points:
        pct = OCCUPANCY_PCT.get((p.occupancy_status or "").upper(), -1)
        if pct >= crowd_pct_threshold and pct >= 0:
            if run_start is None:
                run_start = p
            last_in_run = p
        else:
            if run_start is not None and last_in_run is not None:
                flush(run_start, last_in_run)
            run_start = None
            last_in_run = None

    if run_start is not None and last_in_run is not None:
        flush(run_start, last_in_run)

    return events


def detect_bunch_events(
    buses: list[BusTrajectory], *, bunch_seconds_threshold: float
) -> list[AnomalyEvent]:
    """Flag trip pairs whose passage through the same ~stop is closer than threshold.

    Strategy: for every integer stop index, compute the time each bus passes it
    (by linear interpolation of ``(datetime, stop_index)`` samples). Sort by
    time; any pair with Δt < threshold → emit an event on the trailing bus
    at the crossing time/stop.
    """
    events: list[AnomalyEvent] = []
    if not buses:
        return events

    max_stop = max((p.stop_index for b in buses for p in b.points), default=0.0)
    max_stop_int = int(max_stop)

    for si in range(max_stop_int + 1):
        passes: list[tuple[datetime, int, float]] = []  # (time, bus_index, stop_idx)
        for bus in buses:
            crossing = _interpolate_crossing_time(bus.points, si)
            if crossing is not None:
                passes.append((crossing, bus.bus_index, float(si)))
        passes.sort(key=lambda r: r[0])
        for a, b in zip(passes, passes[1:]):
            gap_s = (b[0] - a[0]).total_seconds()
            if 0 < gap_s < bunch_seconds_threshold:
                events.append(
                    AnomalyEvent(
                        bus_index=b[1],
                        minute_of_day=_to_minute_of_day(b[0]),
                        stop_index=b[2],
                        type="bunch",
                    )
                )
    return events


def _interpolate_crossing_time(
    points: list[TrajectoryPoint], target_stop_index: int
) -> datetime | None:
    """Linear interpolate the datetime at which ``stop_index`` first reaches target."""
    for a, b in zip(points, points[1:]):
        lo, hi = (a, b) if a.stop_index <= b.stop_index else (b, a)
        if lo.stop_index <= target_stop_index <= hi.stop_index:
            span = hi.stop_index - lo.stop_index
            if span <= 0:
                return lo.datetime
            frac = (target_stop_index - lo.stop_index) / span
            epoch = lo.datetime.timestamp() + frac * (
                hi.datetime.timestamp() - lo.datetime.timestamp()
            )
            return datetime.fromtimestamp(epoch, tz=lo.datetime.tzinfo)
    return None


def detect_all(
    buses: list[BusTrajectory],
    *,
    bunch_seconds_threshold: float,
    idle_min_threshold: float,
    crowd_pct_threshold: float,
) -> list[AnomalyEvent]:
    """Run all three detectors and return the flat event list."""
    out: list[AnomalyEvent] = []
    for bus in buses:
        out.extend(detect_idle_events(bus, idle_min_threshold=idle_min_threshold))
        out.extend(detect_crowd_events(bus, crowd_pct_threshold=crowd_pct_threshold))
    out.extend(
        detect_bunch_events(buses, bunch_seconds_threshold=bunch_seconds_threshold)
    )
    return out
