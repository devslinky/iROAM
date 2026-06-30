"""Trip-feed normalizer unit tests — pure FeedMessage→ORM mapping."""

from __future__ import annotations

from datetime import datetime, timezone

from apps.collector import gtfs_realtime_pb2
from apps.collector.normalize_trips import (
    normalize_trip_modifications,
    normalize_trip_updates,
)
from db.models.trip_modification import KIND_SHAPE, KIND_TRIP_MODIFICATIONS
from tests._factories import make_feed_message


def _now() -> datetime:
    return datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)


def test_trip_update_field_mapping() -> None:
    m = make_feed_message(
        entities=[
            {
                "id": "1",
                "trip_id": "75273020",
                "route_id": "14",
                "direction_id": 1,
                "start_date": "20260611",
                "start_time": "08:15:00",
                "schedule_relationship": gtfs_realtime_pb2.TripDescriptor.SCHEDULED,
                "vehicle_id": "9001",
                "vehicle_label": "Glencairn",
                "timestamp": 1_700_000_100,
                "delay": -30,
                "stop_time_updates": [
                    {
                        "stop_sequence": 1,
                        "stop_id": "5969",
                        "departure": {"time": 1_700_000_500},
                        "schedule_relationship": (
                            gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SCHEDULED
                        ),
                    },
                    {
                        "stop_sequence": 2,
                        "stop_id": "2108",
                        "arrival": {"time": 1_700_000_560, "delay": 60, "uncertainty": 30},
                    },
                ],
            }
        ]
    )

    rows = normalize_trip_updates(
        m, fetched_at=_now(), feed_header_timestamp=_now(), feed_name="trip-updates"
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.feed_name == "trip-updates"
    assert row.trip_id == "75273020"
    assert row.route_id == "14"
    assert row.direction_id == 1
    assert row.start_date == "20260611"
    assert row.schedule_relationship == "SCHEDULED"
    assert row.vehicle_id == "9001"
    assert row.vehicle_label == "Glencairn"
    assert row.trip_update_timestamp == datetime.fromtimestamp(1_700_000_100, tz=timezone.utc)
    assert row.delay_seconds == -30
    assert row.stop_time_update_count == 2
    # Epoch times must land as JSON integers, not strings.
    assert row.stop_time_updates[0] == {
        "stop_sequence": 1,
        "stop_id": "5969",
        "departure": {"time": 1_700_000_500},
        "schedule_relationship": "SCHEDULED",
    }
    assert row.stop_time_updates[1]["arrival"] == {
        "time": 1_700_000_560,
        "delay": 60,
        "uncertainty": 30,
    }


def test_trip_update_skips_non_trip_entities_and_absent_fields() -> None:
    m = make_feed_message(entities=[{"id": "bare", "trip_id": "t9"}])
    m.entity.add().id = "vehicle-only"
    m.entity[-1].vehicle.vehicle.id = "v1"

    rows = normalize_trip_updates(
        m, fetched_at=_now(), feed_header_timestamp=None, feed_name="subway-trip-updates"
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.feed_name == "subway-trip-updates"
    assert row.vehicle_id is None
    assert row.delay_seconds is None
    assert row.trip_update_timestamp is None
    assert row.stop_time_update_count == 0
    assert row.stop_time_updates == []


def test_trip_update_route_allowlist() -> None:
    m = make_feed_message(
        entities=[
            {"id": "1", "trip_id": "t1", "route_id": "14"},
            {"id": "2", "trip_id": "t2", "route_id": "504"},
            {"id": "3", "trip_id": "t3"},  # no route attribution → dropped
        ]
    )

    rows = normalize_trip_updates(
        m,
        fetched_at=_now(),
        feed_header_timestamp=None,
        feed_name="trip-updates",
        route_allowlist={"14"},
    )

    assert [r.trip_id for r in rows] == ["t1"]


def _make_detour_message() -> gtfs_realtime_pb2.FeedMessage:
    m = gtfs_realtime_pb2.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"

    e1 = m.entity.add()
    e1.id = "detour_x"
    tm = e1.trip_modifications
    group = tm.selected_trips.add()
    group.trip_ids.extend(["t1", "t2"])
    group.shape_id = "detour_x_shape"
    tm.service_dates.extend(["20260611", "20260612"])
    mod = tm.modifications.add()
    mod.start_stop_selector.stop_sequence = 6
    mod.end_stop_selector.stop_sequence = 7

    e2 = m.entity.add()
    e2.id = "detour_x_shape"
    e2.shape.shape_id = "detour_x_shape"
    e2.shape.encoded_polyline = "uvqiGrexdN"

    m.entity.add().id = "neither"  # unknown-kind entity → ignored
    return m


def test_trip_modifications_mapping() -> None:
    rows = normalize_trip_modifications(
        _make_detour_message(), fetched_at=_now(), feed_header_timestamp=None
    )

    assert [r.entity_kind for r in rows] == [KIND_TRIP_MODIFICATIONS, KIND_SHAPE]

    mod, shape = rows
    assert mod.entity_id == "detour_x"
    assert mod.trip_ids == ["t1", "t2"]
    assert mod.service_dates == ["20260611", "20260612"]
    assert mod.start_times == []
    assert mod.modifications_count == 1
    assert mod.raw_entity["trip_modifications"]["selected_trips"][0]["shape_id"] == "detour_x_shape"

    assert shape.shape_id == "detour_x_shape"
    assert shape.encoded_polyline == "uvqiGrexdN"
    assert shape.raw_entity["shape"]["shape_id"] == "detour_x_shape"


def test_rows_roundtrip_through_db(db_session) -> None:
    """Insert one row of each new table through the ORM against real Postgres."""
    from db.models.feed_fetch_log import FeedFetchLog
    from db.models.raw_snapshot import RawGtfsrtSnapshot

    log = FeedFetchLog(
        feed_name="trip-updates",
        feed_url="http://example/tu",
        fetched_at=_now(),
        success=True,
    )
    db_session.add(log)
    db_session.flush()
    snapshot = RawGtfsrtSnapshot(
        fetch_log_id=log.id,
        feed_name="trip-updates",
        fetched_at=_now(),
        content_sha256="0" * 64,
    )
    db_session.add(snapshot)
    db_session.flush()

    tu_rows = normalize_trip_updates(
        make_feed_message(
            entities=[{"id": "1", "trip_id": "t1", "route_id": "14",
                       "stop_time_updates": [{"stop_id": "5969", "arrival": {"time": 1}}]}]
        ),
        fetched_at=_now(),
        feed_header_timestamp=None,
        feed_name="trip-updates",
    )
    tm_rows = normalize_trip_modifications(
        _make_detour_message(), fetched_at=_now(), feed_header_timestamp=None
    )
    for row in [*tu_rows, *tm_rows]:
        row.snapshot_id = snapshot.id
        db_session.add(row)
    db_session.commit()

    from db.models import TripModification, TripUpdate

    assert db_session.query(TripUpdate).count() == 1
    assert db_session.query(TripModification).count() == 2
    stored = db_session.query(TripUpdate).one()
    assert stored.stop_time_updates[0]["arrival"]["time"] == 1
