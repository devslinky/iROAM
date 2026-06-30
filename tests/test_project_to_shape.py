"""Unit tests for ``apps.analytics.project_to_shape.project_trajectory``.

Uses synthetic shapely LineStrings in the pipeline's metric CRS — no GTFS,
no DB. We build a LineString in planar coordinates directly and feed lat/lon
derived through the inverse transform, so projecting them forward again lands
on known points of that line regardless of which metric CRS is configured.
"""

from __future__ import annotations

import pandas as pd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from apps.analytics.project_to_shape import project_trajectory
from apps.analytics.shapes import METRIC_CRS

_TO_4326 = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)


def _to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon, lat = _TO_4326.transform(x, y)
    return float(lon), float(lat)


@pytest.fixture
def shape_line() -> LineString:
    # L-shape anchored near Toronto in metric coords: 100 m east, then 100 m
    # north. Total length 200 m. Test points below are expressed relative to
    # the same anchor (applied in _df_from_xy / _to_lonlat callers).
    return LineString(
        [(_X0, _Y0), (_X0 + 100.0, _Y0), (_X0 + 100.0, _Y0 + 100.0)]
    )


_X0, _Y0 = 630_000.0, 4_833_000.0


def _df_from_xy(points: list[tuple[float, float]]) -> pd.DataFrame:
    rows = []
    for x, y in points:
        lon, lat = _to_lonlat(_X0 + x, _Y0 + y)
        rows.append({"latitude": lat, "longitude": lon})
    return pd.DataFrame(rows)


def test_projection_distances_on_straight_segment(shape_line: LineString) -> None:
    df = _df_from_xy([(0.0, 0.0), (25.0, 0.0), (50.0, 0.0), (100.0, 0.0)])
    out = project_trajectory(df, shape_line, max_orthogonal_distance_m=1.0)
    assert len(out) == 4
    assert out["travel_distance_m"].tolist() == pytest.approx([0.0, 25.0, 50.0, 100.0], abs=0.01)
    assert (out["orthogonal_distance_m"] < 0.01).all()


def test_projection_around_corner(shape_line: LineString) -> None:
    # After the corner, distance-along-shape is 100m (corner) + y coord.
    df = _df_from_xy([(100.0, 10.0), (100.0, 50.0), (100.0, 100.0)])
    out = project_trajectory(df, shape_line, max_orthogonal_distance_m=1.0)
    assert out["travel_distance_m"].tolist() == pytest.approx([110.0, 150.0, 200.0], abs=0.01)


def test_outliers_beyond_max_orthogonal_are_dropped(shape_line: LineString) -> None:
    # Two near-the-line points and one far outlier 500m off.
    df = _df_from_xy([(50.0, 0.0), (50.0, 500.0), (75.0, 0.0)])
    out = project_trajectory(df, shape_line, max_orthogonal_distance_m=200.0)
    assert len(out) == 2
    assert out["travel_distance_m"].tolist() == pytest.approx([50.0, 75.0], abs=0.01)


def test_empty_input_returns_empty_with_columns(shape_line: LineString) -> None:
    df = pd.DataFrame(columns=["latitude", "longitude"])
    out = project_trajectory(df, shape_line)
    assert out.empty
    assert "travel_distance_m" in out.columns
    assert "orthogonal_distance_m" in out.columns


def test_preserves_other_columns(shape_line: LineString) -> None:
    rows = []
    for x, y, tag in [(0.0, 0.0, "a"), (50.0, 0.0, "b"), (100.0, 0.0, "c")]:
        lon, lat = _to_lonlat(_X0 + x, _Y0 + y)
        rows.append({"latitude": lat, "longitude": lon, "tag": tag})
    df = pd.DataFrame(rows)
    out = project_trajectory(df, shape_line, max_orthogonal_distance_m=1.0)
    assert out["tag"].tolist() == ["a", "b", "c"]


def _df_from_xy_timed(points: list[tuple[float, float]], step_s: float = 20.0) -> pd.DataFrame:
    df = _df_from_xy(points)
    df["datetime"] = pd.to_datetime(
        [1_700_000_000 + i * step_s for i in range(len(df))], unit="s", utc=True
    )
    return df


def test_teleport_spike_is_dropped() -> None:
    # 2 km straight shape; bus moves ~10 m/s except one sample teleports 1.5 km
    # ahead and back — both physically impossible at 20 s cadence.
    line = LineString([(_X0, _Y0), (_X0 + 2000.0, _Y0)])
    xs = [0.0, 200.0, 400.0, 1900.0, 600.0, 800.0]
    df = _df_from_xy_timed([(x, 0.0) for x in xs])
    out = project_trajectory(df, line, max_orthogonal_distance_m=50.0)
    assert out["travel_distance_m"].tolist() == pytest.approx(
        [0.0, 200.0, 400.0, 600.0, 800.0], abs=0.01
    )


def test_teleport_block_does_not_reanchor() -> None:
    # A contiguous block of wrong-leg projections must be dropped wholesale —
    # the filter anchors on the last *kept* point, not the previous raw point.
    line = LineString([(_X0, _Y0), (_X0 + 3000.0, _Y0)])
    xs = [0.0, 200.0, 2500.0, 2520.0, 2540.0, 600.0, 800.0]
    df = _df_from_xy_timed([(x, 0.0) for x in xs])
    out = project_trajectory(df, line, max_orthogonal_distance_m=50.0)
    assert out["travel_distance_m"].tolist() == pytest.approx(
        [0.0, 200.0, 600.0, 800.0], abs=0.01
    )


def test_teleport_filter_disabled_keeps_everything() -> None:
    line = LineString([(_X0, _Y0), (_X0 + 2000.0, _Y0)])
    xs = [0.0, 200.0, 1900.0, 400.0]
    df = _df_from_xy_timed([(x, 0.0) for x in xs])
    out = project_trajectory(
        df, line, max_orthogonal_distance_m=50.0, max_implied_speed_m_s=None
    )
    assert len(out) == 4


def test_vectorized_projection_matches_per_point_loop(shape_line: LineString) -> None:
    """The vectorized path must agree with scalar shapely projection."""
    from shapely.geometry import Point

    from apps.analytics.shapes import transform_lonlat_to_meters

    df = _df_from_xy(
        [(3.7, 1.2), (42.0, -7.5), (97.3, 4.4), (100.0, 55.5), (88.8, 99.0)]
    )
    out = project_trajectory(df, shape_line, max_orthogonal_distance_m=1e9)
    for i in range(len(df)):
        x, y = transform_lonlat_to_meters(
            float(df["longitude"].iloc[i]), float(df["latitude"].iloc[i])
        )
        pt = Point(x, y)
        d_ref = shape_line.project(pt)
        orth_ref = pt.distance(shape_line.interpolate(d_ref))
        assert out["travel_distance_m"].iloc[i] == pytest.approx(d_ref, abs=1e-6)
        assert out["orthogonal_distance_m"].iloc[i] == pytest.approx(orth_ref, abs=1e-6)
