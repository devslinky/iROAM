"""Query helpers for the iROAM dashboard route-and-day view.

The dashboard picks a ``(route_id, service_date, direction_id)`` triple and
needs:

  * catalog — which routes have any trajectory data, and on which dates per
    route / direction combo, for the sidebar dropdowns;
  * bulk trajectories — every point of every trip instance in the picked
    slice, in one round-trip (per-instance fetching would hammer the DB
    when the dashboard refreshes every few seconds).

We deliberately avoid joining to ``vehicle_positions`` etc.; trajectories
already carry everything the UI renders. Sort order is ``(trip_id,
start_date, datetime)`` so the caller can walk the list linearly and group
on change-of-key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models.trip_trajectory import TripTrajectory


@dataclass(frozen=True)
class RouteDateDir:
    route_id: str
    direction_id: int
    service_dates: list[date]
    trip_instance_count: int


def list_route_catalog(session: Session) -> list[dict[str, Any]]:
    """One entry per distinct ``(route_id, direction_id)`` with the dates it ran on."""
    stmt = (
        select(
            TripTrajectory.route_id,
            TripTrajectory.direction_id,
            TripTrajectory.service_date,
            TripTrajectory.trip_id,
            TripTrajectory.start_date,
        )
        .where(TripTrajectory.route_id.is_not(None))
        .where(TripTrajectory.direction_id.is_not(None))
        .distinct()
    )
    rows = session.execute(stmt).all()

    catalog: dict[tuple[str, int], dict[str, Any]] = {}
    for r in rows:
        key = (r.route_id, r.direction_id)
        bucket = catalog.setdefault(
            key,
            {
                "route_id": r.route_id,
                "direction_id": r.direction_id,
                "service_dates": set(),
                "trip_instances": set(),
            },
        )
        bucket["service_dates"].add(r.service_date)
        bucket["trip_instances"].add((r.trip_id, r.start_date))

    out = []
    for (route_id, direction_id), v in catalog.items():
        out.append(
            {
                "route_id": route_id,
                "direction_id": direction_id,
                "service_dates": sorted(v["service_dates"], reverse=True),
                "trip_instance_count": len(v["trip_instances"]),
            }
        )
    out.sort(key=lambda r: (r["route_id"], r["direction_id"]))
    return out


def fetch_trajectories_for_slice(
    session: Session,
    *,
    service_date: date,
    route_id: str,
    direction_id: int,
) -> list[TripTrajectory]:
    """Every trajectory point for the given slice, ordered for linear grouping."""
    stmt = (
        select(TripTrajectory)
        .where(TripTrajectory.service_date == service_date)
        .where(TripTrajectory.route_id == route_id)
        .where(TripTrajectory.direction_id == direction_id)
        .order_by(
            TripTrajectory.trip_id.asc(),
            TripTrajectory.start_date.asc(),
            TripTrajectory.datetime.asc(),
        )
    )
    return list(session.execute(stmt).scalars().all())
