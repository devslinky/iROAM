"""Lazy cached loader for the GTFS static bundle under ``Complete GTFS/``.

The four tables the pipeline actually uses are ``trips``, ``stops``,
``stop_times``, and ``shapes``. ``routes`` is loaded for completeness.

The load is cached by directory mtime so dev iterations on the GTFS bundle are
picked up without a process restart, but production reuses a single copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from core.config import get_settings


@dataclass(frozen=True)
class GtfsStatic:
    trips: pd.DataFrame          # trip_id, route_id, service_id, direction_id, shape_id, ...
    stops: pd.DataFrame          # stop_id, stop_lat, stop_lon, ...
    stop_times: pd.DataFrame     # trip_id, stop_id, stop_sequence, arrival_time, ...
    shapes: pd.DataFrame         # shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence, ...
    routes: pd.DataFrame         # route_id, route_short_name, ...
    # From feed_info.txt (YYYYMMDD strings); None when the bundle omits it.
    feed_version: str | None = None
    feed_start_date: str | None = None
    feed_end_date: str | None = None


def _dir_mtime_key(path: Path) -> float:
    """Max mtime across every .txt in the bundle, for cache invalidation."""
    if not path.is_dir():
        return 0.0
    mtimes = [p.stat().st_mtime for p in path.glob("*.txt")]
    return max(mtimes) if mtimes else 0.0


def _read_feed_info(path: Path) -> tuple[str | None, str | None, str | None]:
    """Return ``(feed_version, feed_start_date, feed_end_date)`` from feed_info.txt.

    All None when the file is absent (some GTFS bundles omit it).
    """
    feed_info_path = path / "feed_info.txt"
    if not feed_info_path.is_file():
        return None, None, None
    fi = pd.read_csv(feed_info_path, dtype=str)
    if fi.empty:
        return None, None, None
    row = fi.iloc[0]

    def _get(col: str) -> str | None:
        if col not in fi.columns or pd.isna(row.get(col)):
            return None
        return str(row[col]).strip()

    return _get("feed_version"), _get("feed_start_date"), _get("feed_end_date")


@lru_cache(maxsize=4)
def _load(path_str: str, mtime_key: float) -> GtfsStatic:
    path = Path(path_str)
    trips = pd.read_csv(path / "trips.txt", dtype={"trip_id": str, "route_id": str, "shape_id": str})
    stops = pd.read_csv(path / "stops.txt", dtype={"stop_id": str})
    stop_times = pd.read_csv(
        path / "stop_times.txt",
        dtype={"trip_id": str, "stop_id": str},
    )
    shapes = pd.read_csv(path / "shapes.txt", dtype={"shape_id": str})
    routes = pd.read_csv(path / "routes.txt", dtype={"route_id": str})
    feed_version, feed_start, feed_end = _read_feed_info(path)
    return GtfsStatic(
        trips=trips,
        stops=stops,
        stop_times=stop_times,
        shapes=shapes,
        routes=routes,
        feed_version=feed_version,
        feed_start_date=feed_start,
        feed_end_date=feed_end,
    )


def feed_covers(static: GtfsStatic, service_date: date) -> bool:
    """True if ``service_date`` falls inside the feed's declared validity window.

    Returns True when the bundle has no feed_info.txt — absence of the file
    can't disprove coverage, so callers should not treat that as an error.
    """
    if static.feed_start_date is None or static.feed_end_date is None:
        return True
    ymd = service_date.strftime("%Y%m%d")
    return static.feed_start_date <= ymd <= static.feed_end_date


def load_all(gtfs_dir: Path | None = None) -> GtfsStatic:
    """Return the cached ``GtfsStatic`` bundle."""
    path = gtfs_dir if gtfs_dir is not None else get_settings().gtfs_static_dir
    return _load(str(path), _dir_mtime_key(path))


def resolve_shape_id(static: GtfsStatic, trip_id: str) -> str | None:
    """Return the ``shape_id`` for a given ``trip_id`` via ``trips.txt``, or None."""
    hit = static.trips.loc[static.trips["trip_id"] == trip_id, "shape_id"]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if pd.isna(value):
        return None
    return str(value)


def resolve_direction_id(static: GtfsStatic, trip_id: str) -> int | None:
    """Return ``direction_id`` (0 or 1) for a trip, or None."""
    hit = static.trips.loc[static.trips["trip_id"] == trip_id, "direction_id"]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if pd.isna(value):
        return None
    return int(value)


def resolve_route_id(static: GtfsStatic, trip_id: str) -> str | None:
    """Return the ``route_id`` a trip_id maps to in ``trips.txt``, or None.

    Used to detect stale-feed false matches: when a realtime trip_id collides
    with an unrelated trip from an expired feed version, this returns the wrong
    route and the caller can drop the trip instead of projecting garbage.
    """
    hit = static.trips.loc[static.trips["trip_id"] == trip_id, "route_id"]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if pd.isna(value):
        return None
    return str(value)
