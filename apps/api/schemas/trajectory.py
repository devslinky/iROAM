"""Trip-trajectory response schemas (analytics output).

Distinct from ``vehicle.py`` — those expose raw observations; these expose
the upsampled, shape-projected trajectory produced by ``apps/analytics``.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class TripInstanceSummary(BaseModel):
    """One ``(trip_id, start_date)`` pair with coarse metadata for the picker."""

    model_config = ConfigDict(from_attributes=True)

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


class TrajectoryPoint(BaseModel):
    """One upsampled row, optionally enriched with interpolated lon/lat."""

    model_config = ConfigDict(from_attributes=True)

    datetime: datetime
    time_offset_seconds: int | None
    travel_distance_m: float
    moving_speed_m_s: float | None
    observed: bool
    occupancy_status: str | None
    # Present only when ``?include=shape`` and the shape resolves.
    latitude: float | None = None
    longitude: float | None = None


class TripTrajectoryResponse(BaseModel):
    """Envelope for a single trip instance's full trajectory."""

    trip_id: str
    start_date: str
    service_date: date
    route_id: str | None
    direction_id: int | None
    shape_id: str | None
    vehicle_id: str | None
    point_count: int
    points: list[TrajectoryPoint]
