"""VehiclePosition normalizer unit tests — pure FeedMessage→ORM mapping."""

from __future__ import annotations

from datetime import datetime, timezone

from google.transit import gtfs_realtime_pb2

from apps.collector.normalize_vehicles import (
    extract_header,
    normalize_vehicle_positions,
)


def _now() -> datetime:
    return datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)


def _make_vp_message(
    *,
    feed_timestamp: int = 1_700_000_000,
    entities: list[dict] | None = None,
) -> gtfs_realtime_pb2.FeedMessage:
    """Build a minimal ``FeedMessage`` carrying VehiclePosition entities.

    Each entity dict accepts: id, trip_id, route_id, vehicle_id, latitude,
    longitude, timestamp, include_vehicle (bool, default True). Omitting
    ``route_id`` produces an entity with no route attribution.
    """
    m = gtfs_realtime_pb2.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    m.header.timestamp = feed_timestamp

    for spec in entities or []:
        e = m.entity.add()
        e.id = spec["id"]
        if not spec.get("include_vehicle", True):
            continue
        vp = e.vehicle
        if "trip_id" in spec:
            vp.trip.trip_id = spec["trip_id"]
        if "route_id" in spec:
            vp.trip.route_id = spec["route_id"]
        if "vehicle_id" in spec:
            vp.vehicle.id = spec["vehicle_id"]
        if "latitude" in spec:
            vp.position.latitude = spec["latitude"]
        if "longitude" in spec:
            vp.position.longitude = spec["longitude"]
        if "timestamp" in spec:
            vp.timestamp = spec["timestamp"]
    return m


def test_extract_header_basic() -> None:
    msg = _make_vp_message(feed_timestamp=1_700_000_000)
    h = extract_header(msg)
    assert h.gtfs_realtime_version == "2.0"
    assert h.incrementality == "FULL_DATASET"
    assert h.feed_header_timestamp is not None
    assert int(h.feed_header_timestamp.timestamp()) == 1_700_000_000
    assert h.entity_count == 0


def test_normalize_maps_expected_fields() -> None:
    msg = _make_vp_message(
        entities=[
            {
                "id": "e1",
                "trip_id": "T1",
                "route_id": "29",
                "vehicle_id": "V1",
                "latitude": 43.65,
                "longitude": -79.38,
                "timestamp": 1_700_000_000,
            }
        ]
    )
    rows = normalize_vehicle_positions(msg, fetched_at=_now(), feed_header_timestamp=_now())
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_id == "e1"
    assert row.trip_id == "T1"
    assert row.route_id == "29"
    assert row.vehicle_id == "V1"
    # Protobuf uses float32 for lat/lon, so compare with tolerance.
    assert row.latitude is not None and abs(row.latitude - 43.65) < 1e-4
    assert row.longitude is not None and abs(row.longitude - (-79.38)) < 1e-4
    assert row.vehicle_timestamp is not None


def test_normalize_skips_entities_without_vehicle() -> None:
    msg = _make_vp_message(entities=[{"id": "no-vp", "include_vehicle": False}])
    rows = normalize_vehicle_positions(msg, fetched_at=_now(), feed_header_timestamp=None)
    assert rows == []


def test_no_filter_keeps_every_vehicle() -> None:
    msg = _make_vp_message(
        entities=[
            {"id": "a", "trip_id": "T1", "route_id": "29"},
            {"id": "b", "trip_id": "T2", "route_id": "504"},
            {"id": "c", "trip_id": "T3"},  # no route_id at all
        ]
    )

    # None → disabled
    rows_none = normalize_vehicle_positions(
        msg, fetched_at=_now(), feed_header_timestamp=None, route_allowlist=None
    )
    assert {r.entity_id for r in rows_none} == {"a", "b", "c"}

    # Empty iterable → also disabled (back-compat with unset env var).
    rows_empty = normalize_vehicle_positions(
        msg, fetched_at=_now(), feed_header_timestamp=None, route_allowlist=[]
    )
    assert {r.entity_id for r in rows_empty} == {"a", "b", "c"}


def test_filter_keeps_only_matching_routes() -> None:
    msg = _make_vp_message(
        entities=[
            {"id": "a", "trip_id": "T1", "route_id": "29"},
            {"id": "b", "trip_id": "T2", "route_id": "929"},
            {"id": "c", "trip_id": "T3", "route_id": "504"},
        ]
    )
    rows = normalize_vehicle_positions(
        msg,
        fetched_at=_now(),
        feed_header_timestamp=None,
        route_allowlist={"29", "929"},
    )
    assert {r.entity_id for r in rows} == {"a", "b"}
    assert all(r.route_id in {"29", "929"} for r in rows)


def test_filter_drops_entities_without_route_id() -> None:
    msg = _make_vp_message(
        entities=[
            {"id": "has-route", "trip_id": "T1", "route_id": "29"},
            {"id": "no-route", "trip_id": "T2"},  # trip set but no route_id
        ]
    )
    rows = normalize_vehicle_positions(
        msg,
        fetched_at=_now(),
        feed_header_timestamp=None,
        route_allowlist={"29"},
    )
    assert {r.entity_id for r in rows} == {"has-route"}
