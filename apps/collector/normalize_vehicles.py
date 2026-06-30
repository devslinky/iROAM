"""Pure translation from a parsed ``FeedMessage`` to ``VehiclePosition`` rows.

No DB access happens here. The runner is responsible for attaching a
``snapshot_id`` and committing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from google.protobuf.json_format import MessageToDict

from apps.collector import gtfs_realtime_pb2

from core.time import epoch_to_utc
from db.models.vehicle_position import VehiclePosition

# Enum-name lookup tables.
_TRIP_SR_NAMES = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name
_VSS_NAMES = gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name
_OCC_NAMES = gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name
_CL_NAMES = gtfs_realtime_pb2.VehiclePosition.CongestionLevel.Name
_INCR_NAMES = gtfs_realtime_pb2.FeedHeader.Incrementality.Name


@dataclass
class HeaderInfo:
    """Header-level fields extracted from a FeedMessage."""

    gtfs_realtime_version: str | None
    incrementality: str | None
    feed_header_timestamp: datetime | None
    entity_count: int


def extract_header(message: gtfs_realtime_pb2.FeedMessage) -> HeaderInfo:
    """Extract header fields for the fetch log and snapshot row."""
    header = message.header
    return HeaderInfo(
        gtfs_realtime_version=header.gtfs_realtime_version or None,
        incrementality=_INCR_NAMES(header.incrementality)
        if header.HasField("incrementality")
        else None,
        feed_header_timestamp=epoch_to_utc(header.timestamp)
        if header.HasField("timestamp")
        else None,
        entity_count=len(message.entity),
    )


def normalize_vehicle_positions(
    message: gtfs_realtime_pb2.FeedMessage,
    *,
    fetched_at: datetime,
    feed_header_timestamp: datetime | None,
    route_allowlist: Iterable[str] | None = None,
) -> list[VehiclePosition]:
    """Convert every ``FeedEntity`` carrying a ``vehicle`` into a row.

    Returns detached ``VehiclePosition`` instances; the caller attaches
    ``snapshot_id`` before adding them to the session.

    When ``route_allowlist`` is non-empty, entities whose ``trip.route_id``
    is not in the set are dropped — including entities that have no
    ``route_id`` at all, since they cannot be attributed to an allowed
    route. Passing ``None`` or an empty iterable disables filtering
    (every vehicle entity is kept, as before).
    """
    allow: frozenset[str] | None = (
        frozenset(route_allowlist) if route_allowlist else None
    )
    rows: list[VehiclePosition] = []

    for entity in message.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle

        trip = vp.trip if vp.HasField("trip") else None
        descriptor = vp.vehicle if vp.HasField("vehicle") else None
        pos = vp.position if vp.HasField("position") else None

        if allow is not None:
            route_id = trip.route_id if trip and trip.route_id else None
            if route_id is None or route_id not in allow:
                continue

        trip_sr: str | None = None
        if trip is not None and trip.HasField("schedule_relationship"):
            trip_sr = _TRIP_SR_NAMES(trip.schedule_relationship)

        current_status: str | None = None
        if vp.HasField("current_status"):
            current_status = _VSS_NAMES(vp.current_status)

        occupancy_status: str | None = None
        if vp.HasField("occupancy_status"):
            occupancy_status = _OCC_NAMES(vp.occupancy_status)

        congestion_level: str | None = None
        if vp.HasField("congestion_level"):
            congestion_level = _CL_NAMES(vp.congestion_level)

        rows.append(
            VehiclePosition(
                fetched_at=fetched_at,
                feed_header_timestamp=feed_header_timestamp,
                entity_id=entity.id or "",
                vehicle_timestamp=epoch_to_utc(vp.timestamp) if vp.HasField("timestamp") else None,
                vehicle_id=(descriptor.id if descriptor and descriptor.id else None),
                vehicle_label=(descriptor.label if descriptor and descriptor.label else None),
                trip_id=(trip.trip_id if trip and trip.trip_id else None),
                route_id=(trip.route_id if trip and trip.route_id else None),
                direction_id=(
                    trip.direction_id if trip and trip.HasField("direction_id") else None
                ),
                start_date=(trip.start_date if trip and trip.start_date else None),
                start_time=(trip.start_time if trip and trip.start_time else None),
                schedule_relationship=trip_sr,
                latitude=(pos.latitude if pos else None),
                longitude=(pos.longitude if pos else None),
                bearing=(pos.bearing if pos and pos.HasField("bearing") else None),
                odometer=(pos.odometer if pos and pos.HasField("odometer") else None),
                speed_mps=(pos.speed if pos and pos.HasField("speed") else None),
                current_status=current_status,
                current_stop_sequence=(
                    vp.current_stop_sequence if vp.HasField("current_stop_sequence") else None
                ),
                stop_id=(vp.stop_id or None) if vp.HasField("stop_id") or vp.stop_id else None,
                occupancy_status=occupancy_status,
                occupancy_percentage=(
                    vp.occupancy_percentage if vp.HasField("occupancy_percentage") else None
                ),
                congestion_level=congestion_level,
                raw_entity=MessageToDict(entity, preserving_proto_field_name=True),
            )
        )

    return rows
