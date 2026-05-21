"""Smoke tests for every documented API endpoint.

Seeds a minimal dataset, then hits each route via FastAPI's ``TestClient`` and
asserts the shape of the response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.deps import get_db
from apps.api.main import create_app
from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.trip_update import TripUpdate
from db.models.trip_update_stop_time import TripUpdateStopTime


@pytest.fixture()
def api_client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient whose ``get_db`` dep returns the test-scoped session."""
    app = create_app()

    def _override() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client


def _seed_fixture(session: Session) -> dict[str, object]:
    """Insert: 1 failure log, 2 success logs with snapshots, 3 trip_updates (2 for T1, 1 for T2)."""
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    fail = FeedFetchLog(
        feed_name="trip-updates",
        feed_url="http://example/feed",
        fetched_at=now - timedelta(minutes=10),
        http_status=503,
        success=False,
        duration_ms=200,
        response_bytes=None,
        feed_header_timestamp=None,
        entity_count=None,
        error_type="HTTPServerError",
        error_message="boom",
    )
    session.add(fail)
    session.flush()

    snapshots: list[RawGtfsrtSnapshot] = []
    for offset_min in (5, 1):
        at = now - timedelta(minutes=offset_min)
        log = FeedFetchLog(
            feed_name="trip-updates",
            feed_url="http://example/feed",
            fetched_at=at,
            http_status=200,
            success=True,
            duration_ms=80,
            response_bytes=1234,
            feed_header_timestamp=at,
            entity_count=2,
        )
        session.add(log)
        session.flush()
        snap = RawGtfsrtSnapshot(
            fetch_log_id=log.id,
            feed_name="trip-updates",
            fetched_at=at,
            feed_header_timestamp=at,
            content_sha256="a" * 64,
        )
        session.add(snap)
        session.flush()
        snapshots.append(snap)

    s_older, s_newer = snapshots

    tu_old = TripUpdate(
        snapshot_id=s_older.id,
        entity_id="tu-1a",
        trip_id="T1",
        route_id="501",
        fetched_at=s_older.fetched_at,
        feed_header_timestamp=s_older.fetched_at,
        delay_seconds=0,
    )
    tu_old.stop_times = [
        TripUpdateStopTime(stop_sequence=1, stop_id="S1", arrival_delay=0),
    ]
    tu_new = TripUpdate(
        snapshot_id=s_newer.id,
        entity_id="tu-1b",
        trip_id="T1",
        route_id="501",
        fetched_at=s_newer.fetched_at,
        feed_header_timestamp=s_newer.fetched_at,
        delay_seconds=60,
    )
    tu_new.stop_times = [
        TripUpdateStopTime(stop_sequence=1, stop_id="S1", arrival_delay=60),
        TripUpdateStopTime(stop_sequence=2, stop_id="S2", arrival_delay=120),
    ]
    tu_other = TripUpdate(
        snapshot_id=s_newer.id,
        entity_id="tu-2",
        trip_id="T2",
        route_id="504",
        fetched_at=s_newer.fetched_at,
        feed_header_timestamp=s_newer.fetched_at,
        delay_seconds=-15,
    )
    session.add_all([tu_old, tu_new, tu_other])
    session.commit()

    return {
        "now": now,
        "snapshot_newer_at": s_newer.fetched_at,
        "snapshot_older_at": s_older.fetched_at,
    }


def test_health(api_client: TestClient) -> None:
    r = api_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db_ok"] is True
    assert body["feed_name"] == "trip-updates"


def test_feed_status_shape(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/feed-status/trip-updates")
    assert r.status_code == 200
    body = r.json()
    assert body["feed_name"] == "trip-updates"
    assert body["fetches_last_hour"] == 3
    assert body["successes_last_hour"] == 2
    assert body["failures_last_hour"] == 1
    assert 0.66 <= body["success_rate_last_hour"] <= 0.67
    assert len(body["recent"]) == 3


def test_latest_trip_updates(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/trip-updates/latest")
    assert r.status_code == 200
    body = r.json()
    assert {row["trip_id"] for row in body} == {"T1", "T2"}
    t1 = next(row for row in body if row["trip_id"] == "T1")
    assert t1["delay_seconds"] == 60  # newer one wins


def test_latest_trip_updates_filtered_by_route(
    api_client: TestClient, db_session: Session
) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/trip-updates/latest", params={"route_id": "504"})
    assert r.status_code == 200
    body = r.json()
    assert [row["trip_id"] for row in body] == ["T2"]


def test_trip_latest_and_404(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/trips/T1/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["delay_seconds"] == 60
    assert len(body["stop_times"]) == 2

    r2 = api_client.get("/trips/does-not-exist/latest")
    assert r2.status_code == 404


def test_trip_history(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/trips/T1/history")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert [row["delay_seconds"] for row in body] == [0, 60]  # oldest-first


def test_route_active_trips(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/routes/501/active-trips", params={"window_minutes": 60})
    assert r.status_code == 200
    body = r.json()
    assert [row["trip_id"] for row in body] == ["T1"]


def test_route_latest(api_client: TestClient, db_session: Session) -> None:
    _seed_fixture(db_session)
    r = api_client.get("/routes/501/trip-updates/latest")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["trip_id"] == "T1"


def test_replay_window(api_client: TestClient, db_session: Session) -> None:
    seeded = _seed_fixture(db_session)
    start = (seeded["now"] - timedelta(minutes=30)).isoformat()
    end = (seeded["now"] + timedelta(minutes=1)).isoformat()
    r = api_client.get(
        "/replay/trips",
        params={"start": start, "end": end, "route_id": "501"},
    )
    assert r.status_code == 200
    body = r.json()
    assert [row["trip_id"] for row in body] == ["T1", "T1"]
