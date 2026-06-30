"""ORM row factories for VehiclePosition-era DB tests.

Every ``VehiclePosition`` needs a ``RawGtfsrtSnapshot`` parent which needs a
``FeedFetchLog`` parent; these helpers wire that chain so tests can focus on
the rows they actually assert about.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.vehicle_position import VehiclePosition

FEED = "vehicle-positions"


def make_fetch_log(
    session: Session,
    *,
    fetched_at: datetime,
    success: bool = True,
    http_status: int | None = 200,
    error_type: str | None = None,
) -> FeedFetchLog:
    log = FeedFetchLog(
        feed_name=FEED,
        feed_url="http://example/feed",
        fetched_at=fetched_at,
        http_status=http_status,
        success=success,
        duration_ms=80 if success else 200,
        response_bytes=1234 if success else None,
        feed_header_timestamp=fetched_at if success else None,
        entity_count=2 if success else None,
        error_type=error_type,
        error_message="boom" if error_type else None,
    )
    session.add(log)
    session.flush()
    return log


def make_snapshot(session: Session, *, fetched_at: datetime) -> RawGtfsrtSnapshot:
    log = make_fetch_log(session, fetched_at=fetched_at)
    snap = RawGtfsrtSnapshot(
        fetch_log_id=log.id,
        feed_name=FEED,
        fetched_at=fetched_at,
        feed_header_timestamp=fetched_at,
        content_sha256="a" * 64,
    )
    session.add(snap)
    session.flush()
    return snap


def make_vp(
    session: Session,
    snapshot: RawGtfsrtSnapshot,
    *,
    vehicle_id: str | None,
    fetched_at: datetime,
    route_id: str | None = None,
    trip_id: str | None = None,
    start_date: str | None = None,
    vehicle_timestamp: datetime | None = None,
    latitude: float | None = 43.65,
    longitude: float | None = -79.38,
    speed_mps: float | None = None,
    current_status: str | None = None,
    occupancy_status: str | None = None,
) -> VehiclePosition:
    vp = VehiclePosition(
        snapshot_id=snapshot.id,
        fetched_at=fetched_at,
        feed_header_timestamp=fetched_at,
        entity_id=f"e-{vehicle_id}-{fetched_at.timestamp():.0f}",
        vehicle_timestamp=vehicle_timestamp,
        vehicle_id=vehicle_id,
        trip_id=trip_id,
        route_id=route_id,
        start_date=start_date,
        latitude=latitude,
        longitude=longitude,
        speed_mps=speed_mps,
        current_status=current_status,
        occupancy_status=occupancy_status,
        raw_entity={"id": f"e-{vehicle_id}"},
    )
    session.add(vp)
    session.flush()
    return vp
