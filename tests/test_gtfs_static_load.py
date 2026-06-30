"""Smoke test for ``apps.analytics.gtfs_static`` against the real feed.

Skips gracefully if the ``Complete GTFS/`` directory is absent. The row-count
assertions are coarse — they catch a feed swap or truncated file, not small
schedule drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analytics.gtfs_static import (
    load_all,
    resolve_direction_id,
    resolve_shape_id,
)
from apps.analytics.shapes import build_linestrings

_GTFS_DIR = Path(__file__).resolve().parent.parent / "Complete GTFS"


@pytest.fixture(scope="module")
def static():
    if not _GTFS_DIR.is_dir():
        pytest.skip(f"GTFS bundle not found at {_GTFS_DIR}")
    return load_all(gtfs_dir=_GTFS_DIR)


def test_row_counts_roughly_match_current_feed(static) -> None:
    # Sanity-only: catches a feed swap or corrupt file, not real drift.
    assert 200 <= len(static.routes) <= 400
    assert 9000 <= len(static.stops) <= 12000
    assert 100_000 <= len(static.trips) <= 200_000


def test_trips_have_required_columns(static) -> None:
    for col in ("trip_id", "route_id", "shape_id", "direction_id"):
        assert col in static.trips.columns


def test_resolve_shape_id_for_known_trip(static) -> None:
    sample = static.trips.iloc[0]
    shape_id = resolve_shape_id(static, sample["trip_id"])
    assert shape_id is not None
    assert shape_id == str(sample["shape_id"])


def test_resolve_shape_id_unknown_trip_returns_none(static) -> None:
    assert resolve_shape_id(static, "not-a-real-trip-id") is None


def test_resolve_direction_id(static) -> None:
    sample = static.trips.iloc[0]
    direction = resolve_direction_id(static, sample["trip_id"])
    assert direction in (0, 1, None)


def test_build_linestrings_returns_one_per_shape(static) -> None:
    lines = build_linestrings(static.shapes)
    n_unique_shapes = static.shapes["shape_id"].nunique()
    # Every shape with >= 2 vertices becomes a LineString; TTC shapes all qualify.
    assert len(lines) >= n_unique_shapes - 5
    first_key = next(iter(lines))
    line = lines[first_key]
    assert line.length > 0
    xs, ys = zip(*line.coords)
    # UTM 17N Toronto meters: x ≈ 0.58–0.66M, y ≈ 4.79–4.89M. Guard against
    # unit / CRS drift (EPSG:3857 would put y at ~5.4M and x at ~-8.8M).
    assert 500_000 < min(xs) and max(xs) < 700_000
    assert 4_700_000 < min(ys) and max(ys) < 4_950_000
