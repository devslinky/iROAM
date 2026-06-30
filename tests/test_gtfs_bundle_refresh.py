"""Bundle-refresh cache invalidation.

``Complete GTFS/`` is operational state replaced in place every TTC board
period. Everything derived from it (parsed frames, shape linestrings,
per-route stop projections) is cached — these tests pin that a refreshed
bundle is picked up *without a process restart*. Regression for the
2026-06-10 incident follow-up: the API kept serving the previous board
period's stops because ``compute_route_stops`` was cached without a bundle
key.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from pyproj import Transformer

from apps.analytics import gtfs_static, stop_projection
from apps.analytics.shapes import METRIC_CRS

_TO_4326 = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)
_X0, _Y0 = 630_000.0, 4_833_000.0


def _lonlat(dx: float, dy: float) -> tuple[float, float]:
    lon, lat = _TO_4326.transform(_X0 + dx, _Y0 + dy)
    return float(lon), float(lat)


def _write_bundle(gtfs: Path, *, shape_id: str, length_m: float, version: str) -> None:
    gtfs.mkdir(parents=True, exist_ok=True)

    shape_rows = []
    for seq, dy in enumerate((0.0, length_m / 2, length_m), start=1):
        lon, lat = _lonlat(0.0, dy)
        shape_rows.append(
            {
                "shape_id": shape_id,
                "shape_pt_lat": lat,
                "shape_pt_lon": lon,
                "shape_pt_sequence": seq,
            }
        )
    pd.DataFrame(shape_rows).to_csv(gtfs / "shapes.txt", index=False)

    pd.DataFrame(
        [
            {
                "trip_id": "T1",
                "route_id": "R",
                "service_id": "svc",
                "direction_id": 0,
                "shape_id": shape_id,
            }
        ]
    ).to_csv(gtfs / "trips.txt", index=False)

    stops = []
    for i, dy in enumerate((0.0, length_m)):
        lon, lat = _lonlat(0.0, dy)
        stops.append({"stop_id": f"s{i}", "stop_name": f"Stop {i}", "stop_lat": lat, "stop_lon": lon})
    pd.DataFrame(stops).to_csv(gtfs / "stops.txt", index=False)

    pd.DataFrame(
        [
            {"trip_id": "T1", "stop_id": "s0", "stop_sequence": 1},
            {"trip_id": "T1", "stop_id": "s1", "stop_sequence": 2},
        ]
    ).to_csv(gtfs / "stop_times.txt", index=False)

    pd.DataFrame([{"route_id": "R", "route_short_name": "R"}]).to_csv(
        gtfs / "routes.txt", index=False
    )
    pd.DataFrame(
        [
            {
                "feed_publisher_name": "t",
                "feed_publisher_url": "http://t",
                "feed_lang": "en",
                "feed_start_date": "20260101",
                "feed_end_date": "20261231",
                "feed_version": version,
            }
        ]
    ).to_csv(gtfs / "feed_info.txt", index=False)


@pytest.fixture()
def bundle_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    gtfs = tmp_path / "gtfs"
    fake_settings = SimpleNamespace(gtfs_static_dir=gtfs)
    monkeypatch.setattr(gtfs_static, "get_settings", lambda: fake_settings)
    return gtfs


def _bump_mtimes(gtfs: Path, offset_s: float) -> None:
    """Force-advance mtimes — same-second rewrites must still invalidate."""
    for p in gtfs.glob("*.txt"):
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + offset_s))


def test_load_all_reflects_in_place_refresh(bundle_dir: Path) -> None:
    _write_bundle(bundle_dir, shape_id="shpA", length_m=2000.0, version="V1")
    static1 = gtfs_static.load_all()
    assert static1.feed_version == "V1"

    _write_bundle(bundle_dir, shape_id="shpB", length_m=3000.0, version="V2")
    _bump_mtimes(bundle_dir, 10.0)
    static2 = gtfs_static.load_all()
    assert static2.feed_version == "V2"
    assert set(static2.shapes["shape_id"]) == {"shpB"}


def test_route_stops_and_linestrings_reflect_refresh(bundle_dir: Path) -> None:
    _write_bundle(bundle_dir, shape_id="shpA", length_m=2000.0, version="V1")
    rs1 = stop_projection.compute_route_stops("R", 0)
    assert rs1 is not None and rs1.shape_id == "shpA"
    assert rs1.shape_length_m == pytest.approx(2000.0, rel=0.01)
    assert "shpA" in gtfs_static.load_shape_linestrings()

    _write_bundle(bundle_dir, shape_id="shpB", length_m=3000.0, version="V2")
    _bump_mtimes(bundle_dir, 10.0)
    rs2 = stop_projection.compute_route_stops("R", 0)
    assert rs2 is not None and rs2.shape_id == "shpB"
    assert rs2.shape_length_m == pytest.approx(3000.0, rel=0.01)
    lines = gtfs_static.load_shape_linestrings()
    assert "shpB" in lines and "shpA" not in lines
