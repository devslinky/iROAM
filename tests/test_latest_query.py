"""Integration tests for the latest / history / active-trips queries.

Requires a reachable Postgres (see conftest.py). Skipped otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.trip_update import TripUpdate
from db.queries.latest import (
    active_trips_on_route,
    latest_trip_update,
    latest_trip_updates,
    replay_trip_updates,
    trip_history,
)


def _seed_snapshot(session: Session, *, at: datetime) -> RawGtfsrtSnapshot:
    log = FeedFetchLog(
        feed_name="trip-updates",
        feed_url="http://example/feed",
        fetched_at=at,
        http_status=200,
        success=True,
        duration_ms=50,
        response_bytes=100,
        feed_header_timestamp=at,
        entity_count=0,
    )
    session.add(log)
    session.flush()
    snap = RawGtfsrtSnapshot(
        fetch_log_id=log.id,
        feed_name="trip-updates",
        fetched_at=at,
        feed_header_timestamp=at,
        content_sha256="0" * 64,
    )
    session.add(snap)
    session.flush()
    return snap


def _seed_tu(
    session: Session,
    snap: RawGtfsrtSnapshot,
    *,
    trip_id: str,
    route_id: str,
    fetched_at: datetime,
    delay: int | None = None,
) -> TripUpdate:
    tu = TripUpdate(
        snapshot_id=snap.id,
        entity_id=f"e-{trip_id}-{int(fetched_at.timestamp())}",
        trip_id=trip_id,
        route_id=route_id,
        fetched_at=fetched_at,
        feed_header_timestamp=fetched_at,
        delay_seconds=delay,
    )
    session.add(tu)
    session.flush()
    return tu


def test_latest_returns_only_newest_per_trip(db_session: Session) -> None:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    t_old = now - timedelta(minutes=5)
    t_mid = now - timedelta(minutes=3)
    t_new = now - timedelta(minutes=1)

    s1 = _seed_snapshot(db_session, at=t_old)
    s2 = _seed_snapshot(db_session, at=t_mid)
    s3 = _seed_snapshot(db_session, at=t_new)

    _seed_tu(db_session, s1, trip_id="T1", route_id="R1", fetched_at=t_old, delay=0)
    _seed_tu(db_session, s2, trip_id="T1", route_id="R1", fetched_at=t_mid, delay=60)
    _seed_tu(db_session, s3, trip_id="T1", route_id="R1", fetched_at=t_new, delay=120)
    _seed_tu(db_session, s3, trip_id="T2", route_id="R1", fetched_at=t_new, delay=-15)
    db_session.commit()

    latest = latest_trip_updates(db_session)
    trip_ids = sorted((t.trip_id, t.delay_seconds) for t in latest)
    assert trip_ids == [("T1", 120), ("T2", -15)]

    only_r1 = latest_trip_updates(db_session, route_id="R1")
    assert {t.trip_id for t in only_r1} == {"T1", "T2"}
    assert latest_trip_updates(db_session, route_id="R999") == []


def test_latest_trip_update_and_history(db_session: Session) -> None:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    t1, t2, t3 = (now - timedelta(minutes=k) for k in (9, 5, 1))

    s1 = _seed_snapshot(db_session, at=t1)
    s2 = _seed_snapshot(db_session, at=t2)
    s3 = _seed_snapshot(db_session, at=t3)
    _seed_tu(db_session, s1, trip_id="T1", route_id="R", fetched_at=t1, delay=0)
    _seed_tu(db_session, s2, trip_id="T1", route_id="R", fetched_at=t2, delay=30)
    _seed_tu(db_session, s3, trip_id="T1", route_id="R", fetched_at=t3, delay=60)
    db_session.commit()

    latest = latest_trip_update(db_session, "T1", with_stop_times=False)
    assert latest is not None
    assert latest.delay_seconds == 60

    history = trip_history(db_session, "T1")
    assert [tu.delay_seconds for tu in history] == [0, 30, 60]

    windowed = trip_history(db_session, "T1", start=t2, end=t3)
    assert [tu.delay_seconds for tu in windowed] == [30, 60]


def test_active_trips_window(db_session: Session) -> None:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    old = now - timedelta(minutes=30)
    recent = now - timedelta(minutes=2)

    s_old = _seed_snapshot(db_session, at=old)
    s_recent = _seed_snapshot(db_session, at=recent)
    _seed_tu(db_session, s_old, trip_id="T_old", route_id="R1", fetched_at=old)
    _seed_tu(db_session, s_recent, trip_id="T_recent", route_id="R1", fetched_at=recent)
    db_session.commit()

    active = active_trips_on_route(db_session, "R1", window_minutes=15)
    assert {t.trip_id for t in active} == {"T_recent"}


def test_replay_window(db_session: Session) -> None:
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    t1, t2, t3 = (now - timedelta(minutes=k) for k in (9, 5, 1))
    s1 = _seed_snapshot(db_session, at=t1)
    s2 = _seed_snapshot(db_session, at=t2)
    s3 = _seed_snapshot(db_session, at=t3)
    _seed_tu(db_session, s1, trip_id="A", route_id="R1", fetched_at=t1)
    _seed_tu(db_session, s2, trip_id="B", route_id="R2", fetched_at=t2)
    _seed_tu(db_session, s3, trip_id="C", route_id="R1", fetched_at=t3)
    db_session.commit()

    rows = replay_trip_updates(db_session, start=t2, end=t3)
    assert [r.trip_id for r in rows] == ["B", "C"]

    rows_r1 = replay_trip_updates(db_session, start=t1, end=t3, route_id="R1")
    assert [r.trip_id for r in rows_r1] == ["A", "C"]


@pytest.mark.usefixtures("db_session")
def test_tables_are_append_only(db_session: Session) -> None:
    """Inserting 'updates' for the same trip must create rows, not overwrite."""
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    s1 = _seed_snapshot(db_session, at=now)
    s2 = _seed_snapshot(db_session, at=now + timedelta(seconds=30))
    _seed_tu(db_session, s1, trip_id="T", route_id="R", fetched_at=now, delay=1)
    _seed_tu(db_session, s2, trip_id="T", route_id="R", fetched_at=now + timedelta(seconds=30), delay=2)
    db_session.commit()

    rows = trip_history(db_session, "T")
    assert len(rows) == 2
    assert [r.delay_seconds for r in rows] == [1, 2]
