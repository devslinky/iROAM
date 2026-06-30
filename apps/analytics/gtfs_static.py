"""Lazy cached loader for the GTFS static bundle under ``Complete GTFS/``.

The four tables the pipeline actually uses are ``trips``, ``stops``,
``stop_times``, and ``shapes``. ``routes`` is loaded for completeness.
Only the columns the pipeline reads are loaded — ``stop_times.txt`` alone is
~280 MB on disk and the unused columns (headsigns, timepoints, ...) would
multiply the resident memory of every process that calls ``load_all``.

The load is cached by directory mtime so a refreshed bundle is picked up
without a process restart. Anything derived from the bundle (shape
linestrings here, per-route stop projections in ``stop_projection``) must be
cached against ``bundle_token()`` rather than unkeyed — an unkeyed cache
keeps serving the *previous* board period after a bundle swap, which is
exactly the failure mode the staleness guard exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from core.config import get_settings
from core.logging import get_logger

_logger = get_logger(__name__)

# Columns actually consumed downstream (resolve_* helpers, trajectory_extract,
# stop_projection, shapes). Loading is tolerant to absent columns so older or
# vendor-variant bundles still load.
_TRIPS_COLS = {"trip_id", "route_id", "service_id", "direction_id", "shape_id"}
_STOPS_COLS = {"stop_id", "stop_name", "stop_lat", "stop_lon"}
_STOP_TIMES_COLS = {"trip_id", "stop_id", "stop_sequence"}
_SHAPES_COLS = {"shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"}


@dataclass(frozen=True)
class GtfsStatic:
    trips: pd.DataFrame          # trip_id, route_id, service_id, direction_id, shape_id
    stops: pd.DataFrame          # stop_id, stop_name, stop_lat, stop_lon
    stop_times: pd.DataFrame     # trip_id, stop_id, stop_sequence
    shapes: pd.DataFrame         # shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence
    routes: pd.DataFrame         # route_id, route_short_name, ...
    # From feed_info.txt (YYYYMMDD strings); None when the bundle omits it.
    feed_version: str | None = None
    feed_start_date: str | None = None
    feed_end_date: str | None = None
    # trip_id → (route_id, shape_id, direction_id), built once at load.
    # The resolve_* helpers are called several times per trip instance per
    # worker tick; a linear scan over ~150k trips each call dominated their
    # cost. None when a GtfsStatic is hand-built (tests) — resolvers fall
    # back to the scan path.
    trip_index: dict[str, tuple[str | None, str | None, int | None]] | None = field(
        default=None, repr=False, compare=False
    )


def _dir_mtime_key(path: Path) -> float:
    """Max mtime across every .txt in the bundle, for cache invalidation."""
    if not path.is_dir():
        return 0.0
    mtimes = [p.stat().st_mtime for p in path.glob("*.txt")]
    return max(mtimes) if mtimes else 0.0


def bundle_token(gtfs_dir: Path | None = None) -> tuple[str, float]:
    """Identity of the *current* bundle on disk: ``(path, max .txt mtime)``.

    Derived caches (shape linestrings, per-route stop projections) must key on
    this so a bundle refresh invalidates them without a process restart.
    """
    path = gtfs_dir if gtfs_dir is not None else get_settings().gtfs_static_dir
    return (str(path), _dir_mtime_key(path))


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


def _read_subset(path: Path, wanted: set[str], dtypes: dict[str, type]) -> pd.DataFrame:
    """Read only ``wanted`` columns (those present) from a GTFS .txt file."""
    return pd.read_csv(
        path,
        usecols=lambda c: c in wanted,
        dtype={k: v for k, v in dtypes.items() if k in wanted},
    )


def _build_trip_index(
    trips: pd.DataFrame,
) -> dict[str, tuple[str | None, str | None, int | None]] | None:
    """One pass over trips → trip_id keyed lookup used by the resolvers.

    Tolerant to absent optional columns (the loader only reads what exists);
    returns None when there's no trip_id column to key on at all.
    """
    if "trip_id" not in trips.columns:
        return None
    index: dict[str, tuple[str | None, str | None, int | None]] = {}
    for row in trips.itertuples(index=False):
        trip_id = str(row.trip_id)
        if trip_id in index:  # first occurrence wins, matching .iloc[0] scans
            continue
        route = getattr(row, "route_id", None)
        route = None if route is None or pd.isna(route) else str(route)
        shape = getattr(row, "shape_id", None)
        shape = None if shape is None or pd.isna(shape) else str(shape)
        direction = getattr(row, "direction_id", None)
        direction = None if direction is None or pd.isna(direction) else int(direction)
        index[trip_id] = (route, shape, direction)
    return index


@lru_cache(maxsize=2)
def _load(path_str: str, mtime_key: float) -> GtfsStatic:
    path = Path(path_str)
    trips = _read_subset(
        path / "trips.txt", _TRIPS_COLS,
        {"trip_id": str, "route_id": str, "shape_id": str, "service_id": str},
    )
    # A duplicated trip_id would silently multiply trajectory rows through the
    # left-merge in trajectory_extract; GTFS requires uniqueness but vendor
    # bundles have shipped violations before. Keep the first, log the rest.
    n_dups = int(trips["trip_id"].duplicated().sum()) if "trip_id" in trips.columns else 0
    if n_dups:
        _logger.warning(
            "gtfs_duplicate_trip_ids",
            extra={"path": path_str, "duplicate_rows_dropped": n_dups},
        )
        trips = trips.drop_duplicates(subset="trip_id", keep="first").reset_index(drop=True)
    stops = _read_subset(path / "stops.txt", _STOPS_COLS, {"stop_id": str})
    stop_times = _read_subset(
        path / "stop_times.txt", _STOP_TIMES_COLS, {"trip_id": str, "stop_id": str}
    )
    shapes = _read_subset(path / "shapes.txt", _SHAPES_COLS, {"shape_id": str})
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
        trip_index=_build_trip_index(trips),
    )


@lru_cache(maxsize=2)
def _shape_linestrings(token: tuple[str, float]):
    """All shape LineStrings for the bundle identified by ``token``.

    Building every linestring costs seconds (44 MB shapes.txt + reprojection);
    the analytics worker would otherwise pay it on every 2-minute tick.
    """
    from apps.analytics.shapes import build_linestrings

    static = _load(token[0], token[1])
    return build_linestrings(static.shapes)


def load_shape_linestrings(gtfs_dir: Path | None = None):
    """Cached ``dict[shape_id, LineString]`` for the current bundle."""
    return _shape_linestrings(bundle_token(gtfs_dir))


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
    token = bundle_token(gtfs_dir)
    return _load(token[0], token[1])


def resolve_shape_id(static: GtfsStatic, trip_id: str) -> str | None:
    """Return the ``shape_id`` for a given ``trip_id`` via ``trips.txt``, or None."""
    if static.trip_index is not None:
        hit = static.trip_index.get(str(trip_id))
        return hit[1] if hit else None
    hit = static.trips.loc[static.trips["trip_id"] == trip_id, "shape_id"]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if pd.isna(value):
        return None
    return str(value)


def resolve_direction_id(static: GtfsStatic, trip_id: str) -> int | None:
    """Return ``direction_id`` (0 or 1) for a trip, or None."""
    if static.trip_index is not None:
        hit = static.trip_index.get(str(trip_id))
        return hit[2] if hit else None
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
    if static.trip_index is not None:
        hit = static.trip_index.get(str(trip_id))
        return hit[0] if hit else None
    hit = static.trips.loc[static.trips["trip_id"] == trip_id, "route_id"]
    if hit.empty:
        return None
    value = hit.iloc[0]
    if pd.isna(value):
        return None
    return str(value)


def validate_bundle(static: GtfsStatic) -> list[str]:
    """Structural sanity checks on a loaded bundle. Returns problem strings.

    Complements ``feed_covers`` (which only checks the date window): a bundle
    can be in-window yet truncated or internally inconsistent — e.g. a partial
    download, or a vendor export missing shape rows. Each finding is a human-
    readable string; an empty list means the bundle looks structurally sound.
    Checks are set-based and cost milliseconds, so callers can run this once
    per load without budget concerns.
    """
    problems: list[str] = []

    for name, frame, required in (
        ("trips", static.trips, ("trip_id", "route_id", "shape_id")),
        ("stops", static.stops, ("stop_id", "stop_lat", "stop_lon")),
        ("stop_times", static.stop_times, ("trip_id", "stop_id", "stop_sequence")),
        ("shapes", static.shapes, ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence")),
    ):
        if frame.empty:
            problems.append(f"{name}.txt is empty")
            continue
        missing = [c for c in required if c not in frame.columns]
        if missing:
            problems.append(f"{name}.txt missing columns: {', '.join(missing)}")

    # Referential integrity (skipped when the relevant columns are absent).
    if "shape_id" in static.trips.columns and "shape_id" in static.shapes.columns:
        trip_shapes = set(static.trips["shape_id"].dropna().astype(str))
        known_shapes = set(static.shapes["shape_id"].dropna().astype(str))
        orphans = trip_shapes - known_shapes
        if trip_shapes and orphans:
            pct = 100.0 * len(orphans) / len(trip_shapes)
            problems.append(
                f"{len(orphans)} shape_ids referenced by trips are absent from "
                f"shapes.txt ({pct:.1f}% — projection will drop those trips)"
            )

    if "stop_id" in static.stop_times.columns and "stop_id" in static.stops.columns:
        st_stops = set(static.stop_times["stop_id"].dropna().astype(str))
        known_stops = set(static.stops["stop_id"].dropna().astype(str))
        orphans = st_stops - known_stops
        if st_stops and orphans:
            pct = 100.0 * len(orphans) / len(st_stops)
            problems.append(
                f"{len(orphans)} stop_ids referenced by stop_times are absent "
                f"from stops.txt ({pct:.1f}%)"
            )

    if "trip_id" in static.trips.columns and "trip_id" in static.stop_times.columns:
        trips_ids = set(static.trips["trip_id"].astype(str))
        st_trips = set(static.stop_times["trip_id"].astype(str))
        uncovered = trips_ids - st_trips
        if trips_ids and len(uncovered) > 0.05 * len(trips_ids):
            pct = 100.0 * len(uncovered) / len(trips_ids)
            problems.append(
                f"{len(uncovered)} trips ({pct:.1f}%) have no stop_times rows "
                f"(truncated stop_times.txt?)"
            )

    return problems
