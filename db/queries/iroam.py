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

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models.trip_trajectory import TripTrajectory


@dataclass(frozen=True)
class RouteDateDir:
    route_id: str
    direction_id: int
    service_dates: list[date]
    trip_instance_count: int


# The catalog only changes when the analytics worker writes (every ~2 min),
# but the dashboard requests it on every page load. A process-local TTL cache
# absorbs that fan-out; the underlying aggregation still seq-scans the table
# (~2 s at 32M rows, growing ~1M rows/day — if cold-cache latency becomes a
# problem, the next step is a runner-maintained summary table, not an index:
# no btree helps a 5-column distinct over the whole table).
_CATALOG_TTL_S = 120.0
_catalog_cache: tuple[float, list[dict[str, Any]]] | None = None


def invalidate_catalog_cache() -> None:
    """Test hook / manual invalidation."""
    global _catalog_cache
    _catalog_cache = None


def list_route_catalog(session: Session, *, use_cache: bool = True) -> list[dict[str, Any]]:
    """One entry per distinct ``(route_id, direction_id)`` with the dates it ran on.

    Aggregation happens in SQL — one output row per (route, direction, date) —
    instead of streaming every distinct trip instance into Python.
    """
    global _catalog_cache
    if use_cache and _catalog_cache is not None:
        if time.monotonic() - _catalog_cache[0] < _CATALOG_TTL_S:
            return _catalog_cache[1]

    # Two-level aggregation: DISTINCT down to trip instances first, then a
    # plain count per (route, dir, date). Postgres parallel-hash-aggregates
    # this in ~2 s at 32M rows; the single-level count(DISTINCT ROW(...))
    # form forces a serial per-group sort and takes ~40 s. Don't "simplify"
    # it back.
    instances = (
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
        .subquery()
    )
    stmt = select(
        instances.c.route_id,
        instances.c.direction_id,
        instances.c.service_date,
        func.count().label("instances"),
    ).group_by(
        instances.c.route_id,
        instances.c.direction_id,
        instances.c.service_date,
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
                "service_dates": [],
                # Each trip instance's start_date pins it to exactly one
                # service_date, so summing per-date distinct counts equals
                # the all-dates distinct count.
                "trip_instance_count": 0,
            },
        )
        bucket["service_dates"].append(r.service_date)
        bucket["trip_instance_count"] += int(r.instances)

    out = []
    for v in catalog.values():
        v["service_dates"] = sorted(v["service_dates"], reverse=True)
        out.append(v)
    out.sort(key=lambda r: (r["route_id"], r["direction_id"]))

    # Stamp after the query so a slow aggregation can't expire its own entry.
    _catalog_cache = (time.monotonic(), out)
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
            TripTrajectory.vehicle_id.asc(),
            TripTrajectory.datetime.asc(),
        )
    )
    return list(session.execute(stmt).scalars().all())
