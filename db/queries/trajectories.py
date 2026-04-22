"""Read-side helpers for the ``trip_trajectories`` analytics output.

All queries are bounded by ``service_date`` (+ optionally ``route_id``) and
sorted by ``datetime``. The table is refreshed delete-then-insert per trip
instance so callers don't need to think about multiple analytics runs — the
``(trip_id, start_date)`` unique-on-datetime index guarantees one row per
upsampled tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from db.models.trip_trajectory import TripTrajectory


@dataclass(frozen=True)
class TripInstanceSummary:
    trip_id: str
    start_date: str
    service_date: date
    route_id: str | None
    direction_id: int | None
    shape_id: str | None
    vehicle_id: str | None
    first_datetime: datetime
    last_datetime: datetime
    point_count: int
    travel_distance_m: float


def list_service_dates(session: Session, *, limit: int = 30) -> list[date]:
    """Distinct service_dates present in ``trip_trajectories``, newest first."""
    stmt = (
        select(TripTrajectory.service_date)
        .distinct()
        .order_by(TripTrajectory.service_date.desc())
        .limit(limit)
    )
    return [row[0] for row in session.execute(stmt).all()]


def list_trip_instances(
    session: Session,
    service_date: date,
    *,
    route_id: str | None = None,
    limit: int = 500,
) -> list[TripInstanceSummary]:
    """One summary row per ``(trip_id, start_date)`` for a service date.

    ``vehicle_id`` and ``route_id`` are picked from the row with the largest
    travel_distance_m so a trip instance that changed vehicles mid-trip gets a
    stable representative; this is rare but non-zero in the feed.
    """
    point_count = func.count(TripTrajectory.id).label("point_count")
    first_dt = func.min(TripTrajectory.datetime).label("first_datetime")
    last_dt = func.max(TripTrajectory.datetime).label("last_datetime")
    max_dist = func.max(TripTrajectory.travel_distance_m).label("travel_distance_m")

    stmt = (
        select(
            TripTrajectory.trip_id,
            TripTrajectory.start_date,
            TripTrajectory.service_date,
            func.max(TripTrajectory.route_id).label("route_id"),
            func.max(TripTrajectory.direction_id).label("direction_id"),
            func.max(TripTrajectory.shape_id).label("shape_id"),
            func.max(TripTrajectory.vehicle_id).label("vehicle_id"),
            first_dt,
            last_dt,
            point_count,
            max_dist,
        )
        .where(TripTrajectory.service_date == service_date)
        .group_by(TripTrajectory.trip_id, TripTrajectory.start_date, TripTrajectory.service_date)
        .order_by(last_dt.desc())
        .limit(limit)
    )
    if route_id is not None:
        # Filter before the group-by so the group stays valid.
        stmt = stmt.where(TripTrajectory.route_id == route_id)

    return [
        TripInstanceSummary(
            trip_id=row.trip_id,
            start_date=row.start_date,
            service_date=row.service_date,
            route_id=row.route_id,
            direction_id=row.direction_id,
            shape_id=row.shape_id,
            vehicle_id=row.vehicle_id,
            first_datetime=row.first_datetime,
            last_datetime=row.last_datetime,
            point_count=int(row.point_count),
            travel_distance_m=float(row.travel_distance_m or 0.0),
        )
        for row in session.execute(stmt).all()
    ]


def fetch_trip_trajectory(
    session: Session,
    trip_id: str,
    start_date: str,
) -> list[TripTrajectory]:
    """All upsampled points for one trip instance, time-ascending.

    Served by the ``ux_trip_trajectories_instance_dt`` unique index
    (``trip_id, start_date, datetime``).
    """
    stmt = (
        select(TripTrajectory)
        .where(TripTrajectory.trip_id == trip_id)
        .where(TripTrajectory.start_date == start_date)
        .order_by(TripTrajectory.datetime.asc())
    )
    return list(session.execute(stmt).scalars().all())


def list_active_routes_on_date(
    session: Session, service_date: date, *, limit: int = 500
) -> list[str]:
    """Distinct ``route_id`` values present in trajectories for a service_date."""
    stmt = (
        select(TripTrajectory.route_id)
        .where(TripTrajectory.service_date == service_date)
        .where(TripTrajectory.route_id.is_not(None))
        .distinct()
        .order_by(TripTrajectory.route_id.asc())
        .limit(limit)
    )
    return [row[0] for row in session.execute(stmt).all() if row[0] is not None]
