"""Tests for incremental-refresh semantics in apps.analytics.runner.

Covers two guarantees:

1. Re-running ``run_for_date`` for the same ``(trip_id, start_date)`` does
   not duplicate trajectory rows — old rows are replaced atomically
   (delete-then-insert per trip instance).
2. Passing ``only_changed_since`` scopes work to trip instances that have
   gained new VehiclePosition observations since the cutoff; quiet trips
   are skipped.

Both tests reuse the synthetic GTFS bundle and VP seeder from
``test_pipeline_roundtrip``; duplicating the helpers here would be churn.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from pyproj import Transformer
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.analytics import runner
from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.trip_trajectory import AnalyticsRun, TripTrajectory
from db.models.vehicle_position import VehiclePosition
from tests.test_pipeline_roundtrip import (
    _ROUTE_ID,
    _SHAPE_ID,
    _START_DATE,
    _START_TIME,
    _TRIP_ID,
    _seed_vehicle_positions,
    _write_synthetic_gtfs,
    date_from_str,
)


def _wire_static_gtfs(monkeypatch: pytest.MonkeyPatch, gtfs_dir: Path) -> None:
    import apps.analytics.gtfs_static as gs
    import apps.analytics.runner as rn

    monkeypatch.setattr(
        gs,
        "load_all",
        lambda gtfs_dir=gtfs_dir: gs._load(str(gtfs_dir), gs._dir_mtime_key(gtfs_dir)),
    )
    monkeypatch.setattr(rn, "load_all", gs.load_all)


def _seed_additional_vp_batch(
    session: Session,
    *,
    n: int,
    start_seconds_offset: int,
    start_fraction: float,
) -> datetime:
    """Append ``n`` VP rows further along the shape, returning the max fetched_at."""
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    x0, y0 = -8_838_000.0, 5_416_000.0
    base_dt = datetime(2026, 4, 20, 13, 0, 0, tzinfo=timezone.utc) + timedelta(
        seconds=start_seconds_offset
    )

    log = FeedFetchLog(
        feed_name="vehicle-positions",
        feed_url="http://example/feed",
        fetched_at=base_dt,
        http_status=200,
        success=True,
        duration_ms=10,
        response_bytes=100,
        feed_header_timestamp=base_dt,
        entity_count=n,
    )
    session.add(log)
    session.flush()
    snap = RawGtfsrtSnapshot(
        fetch_log_id=log.id,
        feed_name="vehicle-positions",
        fetched_at=base_dt,
        feed_header_timestamp=base_dt,
        content_sha256="1" * 64,
    )
    session.add(snap)
    session.flush()

    last_dt = base_dt
    for i in range(n):
        # Advance from start_fraction -> 1.0 of the 2000m shape.
        frac = start_fraction + (1.0 - start_fraction) * (i / max(n - 1, 1))
        y = y0 + 2000.0 * frac
        lon, lat = transformer.transform(x0, y)
        dt = base_dt + timedelta(seconds=15 * i)
        last_dt = dt
        session.add(
            VehiclePosition(
                snapshot_id=snap.id,
                fetched_at=dt,
                feed_header_timestamp=dt,
                entity_id=f"e2-{i}",
                vehicle_timestamp=dt,
                vehicle_id="V_SMOKE",
                trip_id=_TRIP_ID,
                route_id=_ROUTE_ID,
                direction_id=0,
                start_date=_START_DATE,
                start_time=_START_TIME,
                latitude=lat,
                longitude=lon,
                occupancy_status="FEW_SEATS_AVAILABLE",
                raw_entity={"id": f"e2-{i}"},
            )
        )
    session.commit()
    return last_dt


def test_refresh_replaces_prior_trip_instance_rows(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running twice for the same trip instance must not duplicate rows."""
    gtfs_dir = _write_synthetic_gtfs(tmp_path)
    _wire_static_gtfs(monkeypatch, gtfs_dir)

    _seed_vehicle_positions(db_session, n=20)
    service_date: date = date_from_str(_START_DATE)

    first = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
    )
    assert first.status == "ok"
    assert first.rows_written > 0
    first_run_count = first.rows_written

    # Second run against the SAME raw data.
    second = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
    )
    assert second.status == "ok"
    # Same input → same output row count. Not doubled.
    assert second.rows_written == first_run_count

    # The total rows in the table should equal one run's worth, not two.
    total = db_session.execute(
        select(TripTrajectory).where(TripTrajectory.trip_id == _TRIP_ID)
    ).scalars().all()
    assert len(total) == first_run_count

    # All surviving rows belong to the newer run (old ones were deleted).
    surviving_run_ids = {r.run_id for r in total}
    assert surviving_run_ids == {second.run_id}

    # Sanity: the older run row still exists (we don't cascade-delete the run).
    first_run = db_session.execute(
        select(AnalyticsRun).where(AnalyticsRun.id == first.run_id)
    ).scalars().one()
    assert first_run.status == "ok"


def test_only_changed_since_skips_quiet_trip_instances(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --since in the future, no trip instance should be reprocessed."""
    gtfs_dir = _write_synthetic_gtfs(tmp_path)
    _wire_static_gtfs(monkeypatch, gtfs_dir)

    _seed_vehicle_positions(db_session, n=20)
    service_date: date = date_from_str(_START_DATE)

    # First, a baseline full run so there are trajectory rows in place.
    baseline = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
    )
    assert baseline.rows_written > 0

    # Worker tick AFTER the latest VP observation — no fresh data to act on.
    later_than_any_vp = datetime(2026, 4, 20, 23, 59, 59, tzinfo=timezone.utc)
    quiet = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
        only_changed_since=later_than_any_vp,
    )
    assert quiet.status == "ok"
    assert quiet.trip_instances_processed == 0
    assert quiet.rows_written == 0

    # Existing trajectory rows must be untouched.
    kept = db_session.execute(
        select(TripTrajectory).where(TripTrajectory.trip_id == _TRIP_ID)
    ).scalars().all()
    assert {r.run_id for r in kept} == {baseline.run_id}


def test_only_changed_since_refreshes_trip_with_new_observations(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a new batch of VP rows lands, the worker refreshes that instance."""
    gtfs_dir = _write_synthetic_gtfs(tmp_path)
    _wire_static_gtfs(monkeypatch, gtfs_dir)

    # Baseline: seed first half of the trajectory and run once.
    _seed_vehicle_positions(db_session, n=20)
    service_date: date = date_from_str(_START_DATE)
    baseline = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
    )
    baseline_row_count = baseline.rows_written

    # Worker tick at "now" BEFORE new data arrives.
    tick_started_at = datetime(2026, 4, 20, 13, 20, 0, tzinfo=timezone.utc)

    # More observations later in the trip — these are "newer than since".
    _seed_additional_vp_batch(
        db_session,
        n=10,
        start_seconds_offset=30 * 60,  # 30 minutes after baseline
        start_fraction=0.5,
    )

    refreshed = runner.run_for_date(
        db_session,
        service_date,
        route_id=_ROUTE_ID,
        upsample_resolution_s=10,
        max_orthogonal_distance_m=200.0,
        only_changed_since=tick_started_at,
    )
    assert refreshed.status == "ok"
    assert refreshed.trip_instances_processed == 1
    assert refreshed.rows_written > 0

    # Surviving rows now belong to the refreshed run only (delete-then-insert).
    all_rows = db_session.execute(
        select(TripTrajectory).where(TripTrajectory.trip_id == _TRIP_ID)
    ).scalars().all()
    assert {r.run_id for r in all_rows} == {refreshed.run_id}
    # Trajectory grew because we added later observations.
    assert len(all_rows) >= baseline_row_count
