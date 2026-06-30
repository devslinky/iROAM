"""VehiclePosition query helpers.

All "latest" lookups use Postgres ``DISTINCT ON``: with the composite
``(vehicle_id | route_id, fetched_at DESC)`` indexes, the planner picks
the first row per key without a sort.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from db.models.vehicle_position import VehiclePosition


def latest_vehicle_positions(
    session: Session,
    *,
    route_id: str | None = None,
    since: datetime | None = None,
    limit: int = 5000,
) -> list[VehiclePosition]:
    """Latest row per ``vehicle_id`` (optionally filtered by route).

    ``since`` bounds the scan — defaults to now-24h so a stale DB doesn't
    return ancient rows. Use a smaller window for the live map.
    """
    cutoff = since if since is not None else datetime.now(tz=timezone.utc) - timedelta(hours=24)

    inner = (
        select(VehiclePosition)
        .distinct(VehiclePosition.vehicle_id)
        .where(VehiclePosition.vehicle_id.is_not(None))
        .where(VehiclePosition.fetched_at >= cutoff)
    )
    if route_id is not None:
        inner = inner.where(VehiclePosition.route_id == route_id)
    inner = inner.order_by(
        VehiclePosition.vehicle_id, VehiclePosition.fetched_at.desc()
    )

    sub = inner.subquery()
    VP = aliased(VehiclePosition, sub)
    stmt = select(VP).order_by(VP.fetched_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def latest_vehicle_position(
    session: Session, vehicle_id: str
) -> VehiclePosition | None:
    """Single most-recent row for a specific vehicle, or None."""
    stmt = (
        select(VehiclePosition)
        .where(VehiclePosition.vehicle_id == vehicle_id)
        .order_by(VehiclePosition.fetched_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def vehicle_history(
    session: Session,
    vehicle_id: str,
    *,
    start: datetime,
    end: datetime,
    limit: int = 5000,
) -> list[VehiclePosition]:
    """Append-ordered history for a vehicle within [start, end]."""
    stmt = (
        select(VehiclePosition)
        .where(VehiclePosition.vehicle_id == vehicle_id)
        .where(VehiclePosition.fetched_at >= start)
        .where(VehiclePosition.fetched_at < end)
        .order_by(VehiclePosition.fetched_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def fetch_by_trip_instance(
    session: Session,
    trip_id: str,
    start_date: str,
    *,
    fetched_at_window: tuple[datetime, datetime] | None = None,
) -> list[VehiclePosition]:
    """All rows for one trip instance, chronological.

    Keyed on ``(trip_id, start_date)`` because the same ``trip_id`` repeats
    across service days. When the feed populates TripDescriptor.start_date the
    filter is exact; otherwise we fall back to the Toronto-local date derived
    from ``vehicle_timestamp`` / ``fetched_at``. Sort is ASC by observation
    time so analytics can compute diffs directly.

    ``fetched_at_window`` (UTC ``[lo, hi)``) lets callers bound the scan with
    the indexed column — the effective-start-date expression itself is not
    indexable, and trip_ids recur every service day within a board period.
    """
    ts = func.coalesce(VehiclePosition.vehicle_timestamp, VehiclePosition.fetched_at)
    effective_start_date = func.coalesce(
        VehiclePosition.start_date,
        func.to_char(func.timezone("America/Toronto", VehiclePosition.vehicle_timestamp), "YYYYMMDD"),
        func.to_char(func.timezone("America/Toronto", VehiclePosition.fetched_at), "YYYYMMDD"),
    )
    stmt = (
        select(VehiclePosition)
        .where(VehiclePosition.trip_id == trip_id)
        .where(effective_start_date == start_date)
        .order_by(ts.asc(), VehiclePosition.id.asc())
    )
    if fetched_at_window is not None:
        stmt = stmt.where(
            VehiclePosition.fetched_at >= fetched_at_window[0],
            VehiclePosition.fetched_at < fetched_at_window[1],
        )
    return list(session.execute(stmt).scalars().all())


def active_route_ids(
    session: Session,
    *,
    window: timedelta = timedelta(minutes=15),
    limit: int = 500,
) -> list[str]:
    """Distinct route_ids observed within the last ``window``, newest-activity-first."""
    cutoff = datetime.now(tz=timezone.utc) - window
    stmt = (
        select(
            VehiclePosition.route_id,
            func.max(VehiclePosition.fetched_at).label("last_seen"),
        )
        .where(VehiclePosition.route_id.is_not(None))
        .where(VehiclePosition.fetched_at >= cutoff)
        .group_by(VehiclePosition.route_id)
        .order_by(func.max(VehiclePosition.fetched_at).desc())
        .limit(limit)
    )
    return [row.route_id for row in session.execute(stmt).all()]


@dataclass
class RouteMetrics:
    route_id: str
    window_minutes: int
    active_vehicle_count: int
    latest_fetched_at: datetime | None
    avg_speed_mps: float | None
    max_speed_mps: float | None
    status_breakdown: dict[str, int]
    occupancy_breakdown: dict[str, int]
    bbox: dict[str, float] | None  # {min_lat, min_lon, max_lat, max_lon}


def route_metrics(
    session: Session,
    route_id: str,
    *,
    window_minutes: int = 15,
) -> RouteMetrics:
    """Aggregate metrics over the latest observation per vehicle on a route."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=window_minutes)

    # Latest-per-vehicle within the window.
    inner = (
        select(VehiclePosition)
        .distinct(VehiclePosition.vehicle_id)
        .where(VehiclePosition.route_id == route_id)
        .where(VehiclePosition.vehicle_id.is_not(None))
        .where(VehiclePosition.fetched_at >= cutoff)
        .order_by(VehiclePosition.vehicle_id, VehiclePosition.fetched_at.desc())
    )
    sub = inner.subquery()
    VP = aliased(VehiclePosition, sub)
    rows = list(session.execute(select(VP)).scalars().all())

    if not rows:
        return RouteMetrics(
            route_id=route_id,
            window_minutes=window_minutes,
            active_vehicle_count=0,
            latest_fetched_at=None,
            avg_speed_mps=None,
            max_speed_mps=None,
            status_breakdown={},
            occupancy_breakdown={},
            bbox=None,
        )

    speeds = [r.speed_mps for r in rows if r.speed_mps is not None]
    lats = [r.latitude for r in rows if r.latitude is not None]
    lons = [r.longitude for r in rows if r.longitude is not None]

    status_counts: dict[str, int] = {}
    for r in rows:
        key = r.current_status or "UNKNOWN"
        status_counts[key] = status_counts.get(key, 0) + 1

    occupancy_counts: dict[str, int] = {}
    for r in rows:
        key = r.occupancy_status or "UNKNOWN"
        occupancy_counts[key] = occupancy_counts.get(key, 0) + 1

    bbox: dict[str, float] | None = None
    if lats and lons:
        bbox = {
            "min_lat": min(lats),
            "min_lon": min(lons),
            "max_lat": max(lats),
            "max_lon": max(lons),
        }

    return RouteMetrics(
        route_id=route_id,
        window_minutes=window_minutes,
        active_vehicle_count=len(rows),
        latest_fetched_at=max(r.fetched_at for r in rows),
        avg_speed_mps=(sum(speeds) / len(speeds)) if speeds else None,
        max_speed_mps=max(speeds) if speeds else None,
        status_breakdown=status_counts,
        occupancy_breakdown=occupancy_counts,
        bbox=bbox,
    )


def replay_vehicles(
    session: Session,
    *,
    start: datetime,
    end: datetime,
    route_id: str | None = None,
    limit: int = 5000,
) -> list[VehiclePosition]:
    """Append-ordered rows for map replay in a time window."""
    stmt = (
        select(VehiclePosition)
        .where(VehiclePosition.fetched_at >= start)
        .where(VehiclePosition.fetched_at < end)
    )
    if route_id is not None:
        stmt = stmt.where(VehiclePosition.route_id == route_id)
    stmt = stmt.order_by(VehiclePosition.fetched_at.asc()).limit(limit)
    return list(session.execute(stmt).scalars().all())
