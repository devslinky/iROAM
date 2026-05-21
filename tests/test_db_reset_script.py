"""Smoke tests for scripts.db_reset.

Verifies:

* Dry-run exits with code 2 and leaves data intact.
* --yes-i-am-sure empties the listed tables.
* The listed tables match the current ORM registry, so adding a model
  without updating the script fails loudly here rather than silently
  leaving old rows behind on reset.

These tests commit rows (the script opens its own Session and won't see
SAVEPOINT-local seeds), so they manage their own cleanup via ``_clean``
rather than the savepoint-based ``db_session`` fixture.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from db.base import Base
from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.vehicle_position import VehiclePosition
from scripts import db_reset


def _now() -> datetime:
    return datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)


def _clean(engine: Engine) -> None:
    """Hard-reset every data table so leftovers don't leak between tests."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"TRUNCATE TABLE {', '.join(db_reset.DATA_TABLES)} "
                f"RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture()
def clean_engine(db_engine: Engine) -> Iterator[Engine]:
    _clean(db_engine)
    try:
        yield db_engine
    finally:
        _clean(db_engine)


def _seed_one_row_per_table(engine: Engine) -> None:
    with Session(engine) as s:
        log = FeedFetchLog(
            feed_name="vehicle-positions",
            feed_url="http://example.test/vp",
            fetched_at=_now(),
            http_status=200,
            success=True,
            duration_ms=12,
            response_bytes=100,
            entity_count=1,
        )
        s.add(log)
        s.flush()

        snap = RawGtfsrtSnapshot(
            fetch_log_id=log.id,
            feed_name="vehicle-positions",
            fetched_at=_now(),
            content_sha256="0" * 64,
        )
        s.add(snap)
        s.flush()

        vp = VehiclePosition(
            snapshot_id=snap.id,
            fetched_at=_now(),
            entity_id="e1",
            vehicle_id="V1",
            trip_id="T1",
            route_id="29",
            raw_entity={"id": "e1"},
        )
        s.add(vp)
        s.commit()


def test_db_reset_table_list_matches_orm(db_engine: Engine) -> None:
    orm_tables = {
        t.name
        for t in Base.metadata.sorted_tables
        if t.name != "alembic_version"
    }
    assert orm_tables == set(db_reset.DATA_TABLES), (
        f"DATA_TABLES drifted from ORM metadata: "
        f"missing={orm_tables - set(db_reset.DATA_TABLES)}, "
        f"extra={set(db_reset.DATA_TABLES) - orm_tables}"
    )


def test_dry_run_exits_2_and_preserves_data(
    clean_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_row_per_table(clean_engine)
    monkeypatch.setattr(db_reset, "SessionLocal", lambda: Session(clean_engine))

    rc = db_reset.main([])
    assert rc == 2

    with Session(clean_engine) as s:
        assert s.execute(text("SELECT COUNT(*) FROM vehicle_positions")).scalar_one() >= 1
        assert s.execute(text("SELECT COUNT(*) FROM raw_gtfsrt_snapshots")).scalar_one() >= 1
        assert s.execute(text("SELECT COUNT(*) FROM feed_fetch_logs")).scalar_one() >= 1


def test_confirmed_run_empties_every_table(
    clean_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_one_row_per_table(clean_engine)
    monkeypatch.setattr(db_reset, "SessionLocal", lambda: Session(clean_engine))

    rc = db_reset.main(["--yes-i-am-sure"])
    assert rc == 0

    with Session(clean_engine) as s:
        for table in db_reset.DATA_TABLES:
            count = s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            assert count == 0, f"{table} still has {count} rows after reset"
