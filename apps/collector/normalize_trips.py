"""Pure translation from a parsed ``FeedMessage`` to trip-feed rows.

Covers the two trip-side feeds:

* ``TripUpdate`` entities (bus ``trips/update`` and ``trips/subway``) →
  ``TripUpdate`` rows. Stop-time predictions are decoded into a compact
  JSONB-ready list instead of a child table — TTC ships ~25k stop-time
  updates per bus poll, which would explode a per-stop table.
* ``TripModifications`` + ``Shape`` entities (``trips/detour``) →
  ``TripModification`` rows, one per entity, kind-discriminated.

No DB access happens here. The runner is responsible for attaching a
``snapshot_id`` and committing.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from google.protobuf.json_format import MessageToDict

from apps.collector import gtfs_realtime_pb2
from core.time import epoch_to_utc
from db.models.trip_modification import (
    KIND_SHAPE,
    KIND_TRIP_MODIFICATIONS,
    TripModification,
)
from db.models.trip_update import TripUpdate

_TRIP_SR_NAMES = gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name
_STU_SR_NAMES = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name


def _stop_time_event_dict(event: Any) -> dict[str, int]:
    """Decode a ``StopTimeEvent`` keeping epoch times as JSON integers."""
    out: dict[str, int] = {}
    if event.HasField("time"):
        out["time"] = int(event.time)
    if event.HasField("delay"):
        out["delay"] = event.delay
    if event.HasField("uncertainty"):
        out["uncertainty"] = event.uncertainty
    return out


def _stop_time_update_dict(stu: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if stu.HasField("stop_sequence"):
        out["stop_sequence"] = stu.stop_sequence
    if stu.stop_id:
        out["stop_id"] = stu.stop_id
    if stu.HasField("arrival"):
        out["arrival"] = _stop_time_event_dict(stu.arrival)
    if stu.HasField("departure"):
        out["departure"] = _stop_time_event_dict(stu.departure)
    if stu.HasField("schedule_relationship"):
        out["schedule_relationship"] = _STU_SR_NAMES(stu.schedule_relationship)
    return out


def normalize_trip_updates(
    message: gtfs_realtime_pb2.FeedMessage,
    *,
    fetched_at: datetime,
    feed_header_timestamp: datetime | None,
    feed_name: str,
    route_allowlist: Iterable[str] | None = None,
) -> list[TripUpdate]:
    """Convert every ``FeedEntity`` carrying a ``trip_update`` into a row.

    ``feed_name`` discriminates the bus and subway feeds, which share the
    ``trip_updates`` table. ``route_allowlist`` follows the same semantics
    as the vehicle normalizer: when non-empty, entities whose
    ``trip.route_id`` is missing or not in the set are dropped.
    """
    allow: frozenset[str] | None = (
        frozenset(route_allowlist) if route_allowlist else None
    )
    rows: list[TripUpdate] = []

    for entity in message.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update

        trip = tu.trip if tu.HasField("trip") else None
        descriptor = tu.vehicle if tu.HasField("vehicle") else None

        if allow is not None:
            route_id = trip.route_id if trip and trip.route_id else None
            if route_id is None or route_id not in allow:
                continue

        trip_sr: str | None = None
        if trip is not None and trip.HasField("schedule_relationship"):
            trip_sr = _TRIP_SR_NAMES(trip.schedule_relationship)

        stop_time_updates = [_stop_time_update_dict(stu) for stu in tu.stop_time_update]

        rows.append(
            TripUpdate(
                feed_name=feed_name,
                fetched_at=fetched_at,
                feed_header_timestamp=feed_header_timestamp,
                entity_id=entity.id or "",
                trip_update_timestamp=(
                    epoch_to_utc(tu.timestamp) if tu.HasField("timestamp") else None
                ),
                delay_seconds=tu.delay if tu.HasField("delay") else None,
                trip_id=(trip.trip_id if trip and trip.trip_id else None),
                route_id=(trip.route_id if trip and trip.route_id else None),
                direction_id=(
                    trip.direction_id if trip and trip.HasField("direction_id") else None
                ),
                start_date=(trip.start_date if trip and trip.start_date else None),
                start_time=(trip.start_time if trip and trip.start_time else None),
                schedule_relationship=trip_sr,
                vehicle_id=(descriptor.id if descriptor and descriptor.id else None),
                vehicle_label=(descriptor.label if descriptor and descriptor.label else None),
                stop_time_update_count=len(stop_time_updates),
                stop_time_updates=stop_time_updates,
            )
        )

    return rows


def normalize_trip_modifications(
    message: gtfs_realtime_pb2.FeedMessage,
    *,
    fetched_at: datetime,
    feed_header_timestamp: datetime | None,
) -> list[TripModification]:
    """Convert detour-feed entities into rows.

    ``TripModifications`` entities and their companion ``Shape`` entities
    (replacement polylines, matched via ``shape_id``) both become rows;
    anything else in the feed is ignored.
    """
    rows: list[TripModification] = []

    for entity in message.entity:
        common = dict(
            fetched_at=fetched_at,
            feed_header_timestamp=feed_header_timestamp,
            entity_id=entity.id or "",
            raw_entity=MessageToDict(entity, preserving_proto_field_name=True),
        )
        if entity.HasField("trip_modifications"):
            tm = entity.trip_modifications
            rows.append(
                TripModification(
                    entity_kind=KIND_TRIP_MODIFICATIONS,
                    trip_ids=[
                        trip_id
                        for group in tm.selected_trips
                        for trip_id in group.trip_ids
                    ],
                    service_dates=list(tm.service_dates),
                    start_times=list(tm.start_times),
                    modifications_count=len(tm.modifications),
                    **common,
                )
            )
        elif entity.HasField("shape"):
            shape = entity.shape
            rows.append(
                TripModification(
                    entity_kind=KIND_SHAPE,
                    shape_id=shape.shape_id or None,
                    encoded_polyline=shape.encoded_polyline or None,
                    **common,
                )
            )

    return rows
