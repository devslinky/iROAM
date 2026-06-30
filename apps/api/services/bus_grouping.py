"""Group trajectory DB rows into per-physical-trip ``BusTrajectory`` objects.

This is the shared front door between the trajectory store and everything
that consumes per-bus tracks (the dashboard endpoints and the forecast
service). It lives in the service layer — the router only adapts HTTP.

Two distinct data pathologies are handled here, both of which would render
as long diagonal artifacts on the dashboard's time-distance chart:

* TTC reuses a scheduled ``trip_id`` for different vehicles over the day
  (block rotation), so the grouping key must include ``vehicle_id``.
* A vehicle sometimes keeps broadcasting the same ``trip_id`` with stale GPS
  long after its physical run ended; those runs are split out and discarded
  by ``segment_vehicle_points``.
"""

from __future__ import annotations

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint
from apps.analytics.stop_projection import RouteStops, distance_to_stop_index

# Segmentation thresholds for ``segment_vehicle_points``. A TTC vehicle
# sometimes broadcasts the same ``trip_id`` long after its physical run has
# ended — the feed keeps emitting stale positions (``|moving_speed_m_s| < 0.5``
# for hours) while the projection slowly drifts ``travel_distance_m``.
# Those points, if left in the trajectory, render as a long shallow diagonal
# across the time axis. We treat any stale run longer than ``_STALE_MIN_SEC``
# as a trip boundary and drop segments whose total distance moved doesn't
# clear ``_MIN_SEGMENT_DISPLACEMENT_M`` — that cutoff is well below any real
# TTC bus trip (minimum ~5 km) and well above GPS drift.
_STALE_SPEED_THRESHOLD_M_S = 0.5
_STALE_MIN_SEC = 20 * 60
_MIN_SEGMENT_DISPLACEMENT_M = 500.0


def segment_vehicle_points(
    points: list[TrajectoryPoint],
) -> list[list[TrajectoryPoint]]:
    """Split one vehicle's points at stale-speed runs; drop ghost segments."""
    if len(points) < 2:
        return [list(points)] if points else []

    # Collect stale ranges — contiguous [i..j] where every sample's |speed|
    # is below threshold AND the elapsed span is at least _STALE_MIN_SEC.
    stale_ranges: list[tuple[int, int]] = []
    n = len(points)
    i = 0
    while i < n:
        if abs(points[i].moving_speed_m_s or 0.0) < _STALE_SPEED_THRESHOLD_M_S:
            j = i
            while (
                j + 1 < n
                and abs(points[j + 1].moving_speed_m_s or 0.0)
                < _STALE_SPEED_THRESHOLD_M_S
            ):
                j += 1
            span_s = (points[j].datetime - points[i].datetime).total_seconds()
            if span_s >= _STALE_MIN_SEC:
                stale_ranges.append((i, j))
            i = j + 1
        else:
            i += 1

    # Slice out the non-stale segments. The stale ranges themselves are
    # discarded — their points would only add the problematic diagonal.
    segments: list[list[TrajectoryPoint]] = []
    prev = 0
    for start, end in stale_ranges:
        if start > prev:
            segments.append(points[prev:start])
        prev = end + 1
    if prev < n:
        segments.append(points[prev:])

    # Drop segments with negligible total displacement (nothing real happened).
    surviving: list[list[TrajectoryPoint]] = []
    for seg in segments:
        if len(seg) < 2:
            continue
        total = sum(
            abs(b.travel_distance_m - a.travel_distance_m)
            for a, b in zip(seg, seg[1:])
        )
        if total >= _MIN_SEGMENT_DISPLACEMENT_M:
            surviving.append(seg)
    return surviving


def group_into_buses(rows: list, route_stops: RouteStops) -> list[BusTrajectory]:
    """Walk the DB rows → one BusTrajectory per (trip_id, start_date, vehicle_id).

    ``vehicle_id`` is part of the key because TTC reuses the same scheduled
    ``trip_id`` for different vehicles over the course of a day (block
    rotation). Keying only on ``(trip_id, start_date)`` would merge two
    physically separate trips into one, and the frontend would then render a
    straight diagonal line connecting their endpoints. Rows are expected to
    arrive sorted by ``(trip_id, start_date, vehicle_id, datetime)`` so each
    key's points remain contiguous even when two vehicles' clock windows
    overlap.

    Within each key group the points are passed through
    ``segment_vehicle_points`` to handle the other diagonal-producing case:
    a single vehicle that keeps broadcasting the same trip_id with stale GPS
    after its physical run has finished.
    """
    buses: list[BusTrajectory] = []
    current_key: tuple[str, str, str | None] | None = None
    current_points: list[TrajectoryPoint] = []
    current_vehicle: str | None = None

    def flush(key: tuple[str, str, str | None] | None) -> None:
        if key is None or not current_points:
            return
        for seg in segment_vehicle_points(current_points):
            buses.append(
                BusTrajectory(
                    bus_index=len(buses),
                    trip_id=key[0],
                    start_date=key[1],
                    vehicle_id=current_vehicle,
                    points=seg,
                )
            )

    for r in rows:
        key = (r.trip_id, r.start_date, r.vehicle_id)
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


__all__ = ["group_into_buses", "segment_vehicle_points"]
