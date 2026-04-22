"""Trip trajectory endpoints — upsampled analytics output.

  GET /trajectories/service-dates                        — dates with data
  GET /trajectories/routes?service_date=                 — routes on a date
  GET /trajectories/trips?service_date=[&route_id=]      — picker list
  GET /trajectories/trips/{trip_id}?start_date=[&include=shape]
      — full points, optionally enriched with shape-interpolated lon/lat

The ``include=shape`` option loads the GTFS static bundle (cached per-process
via ``apps.analytics.gtfs_static.load_all``) and interpolates lon/lat for
every upsampled point. It is opt-in so clients that only need a chart don't
pay the shapes-load cost.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pyproj import Transformer
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from apps.api.schemas import (
    TrajectoryPoint,
    TripInstanceSummary,
    TripTrajectoryResponse,
)
from db.queries.trajectories import (
    fetch_trip_trajectory,
    list_active_routes_on_date,
    list_service_dates,
    list_trip_instances,
)

router = APIRouter(prefix="/trajectories", tags=["trajectories"])

_FROM_3857 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)


@lru_cache(maxsize=1)
def _shape_linestrings_cached():
    # Deferred import so the router module can be collected even if pandas/
    # shapely/pyproj aren't installed (e.g. in the parser-only test container).
    from apps.analytics.gtfs_static import load_all
    from apps.analytics.shapes import build_linestrings

    static = load_all()
    return build_linestrings(static.shapes)


def _interpolate_lonlat(shape_id: str, travel_m: float) -> tuple[float, float] | None:
    """Return (lon, lat) at ``travel_m`` along ``shape_id``, or None if unknown."""
    try:
        shapes = _shape_linestrings_cached()
    except Exception:
        return None
    line = shapes.get(shape_id)
    if line is None:
        return None
    d = max(0.0, min(travel_m, line.length))
    point = line.interpolate(d)
    lon, lat = _FROM_3857.transform(point.x, point.y)
    return float(lon), float(lat)


@router.get("/service-dates")
def trajectory_service_dates(
    limit: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[date]:
    """Distinct service dates with any trajectory rows, newest first."""
    return list_service_dates(db, limit=limit)


@router.get("/routes")
def trajectory_routes(
    service_date: date = Query(...),
    db: Session = Depends(get_db),
) -> list[str]:
    """Routes that have at least one trip trajectory on ``service_date``."""
    return list_active_routes_on_date(db, service_date)


@router.get("/trips", response_model=list[TripInstanceSummary])
def trajectory_trips(
    service_date: date = Query(...),
    route_id: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> list[TripInstanceSummary]:
    """Picker list: one summary per trip instance on a given service date."""
    summaries = list_trip_instances(
        db, service_date, route_id=route_id, limit=limit
    )
    return [TripInstanceSummary.model_validate(s) for s in summaries]


@router.get("/trips/{trip_id}", response_model=TripTrajectoryResponse)
def trajectory_detail(
    trip_id: str,
    start_date: str = Query(..., min_length=8, max_length=8, pattern=r"^\d{8}$"),
    include: Literal["shape"] | None = Query(
        default=None,
        description="Set to 'shape' to enrich each point with interpolated lon/lat.",
    ),
    db: Session = Depends(get_db),
) -> TripTrajectoryResponse:
    """Full trajectory for ``(trip_id, start_date)`` in time order."""
    rows = fetch_trip_trajectory(db, trip_id, start_date)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no trajectory for trip_id={trip_id} start_date={start_date}",
        )
    first = rows[0]
    points: list[TrajectoryPoint] = []
    for row in rows:
        lon: float | None = None
        lat: float | None = None
        if include == "shape" and first.shape_id:
            ll = _interpolate_lonlat(first.shape_id, row.travel_distance_m)
            if ll is not None:
                lon, lat = ll
        points.append(
            TrajectoryPoint(
                datetime=row.datetime,
                time_offset_seconds=row.time_offset_seconds,
                travel_distance_m=row.travel_distance_m,
                moving_speed_m_s=row.moving_speed_m_s,
                observed=row.observed,
                occupancy_status=row.occupancy_status,
                latitude=lat,
                longitude=lon,
            )
        )
    return TripTrajectoryResponse(
        trip_id=first.trip_id,
        start_date=first.start_date,
        service_date=first.service_date,
        route_id=first.route_id,
        direction_id=first.direction_id,
        shape_id=first.shape_id,
        vehicle_id=first.vehicle_id,
        point_count=len(points),
        points=points,
    )
