"""Scheduled headways from the GTFS static bundle.

The bunching literature defines bunching against the *scheduled* headway —
Moreira-Matias et al. flag a bunching event when the actual headway falls to
≤ 0.25 × scheduled, and the TCQSM's headway-adherence LOS is the coefficient
of variation of headway deviations against mean scheduled headway. The
labelled-dataset builder therefore needs "what headway was this trip supposed
to have?", which is exactly the gap between consecutive scheduled trip start
times on the same (route, direction) for the service ids active on a date.

Loading is tolerant (absent calendar files → unknown service, callers get
None) and cached per bundle token like every other static-derived structure;
stop_times.txt is re-read here with only the three columns needed to find
each trip's first departure (~seconds, once per bundle refresh).
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import pandas as pd

from apps.analytics.gtfs_static import bundle_token, load_all
from core.config import get_settings
from core.logging import get_logger

_logger = get_logger(__name__)

_WEEKDAY_COLS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _parse_gtfs_time_s(value: str) -> int | None:
    """GTFS HH:MM:SS (hours may exceed 24 for overnight trips) → seconds."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hh, mm, ss = (int(p) for p in parts)
    except ValueError:
        return None
    return hh * 3600 + mm * 60 + ss


@lru_cache(maxsize=2)
def _calendar_frames(token: tuple[str, float]) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    path = Path(token[0])
    cal = None
    cal_dates = None
    cal_path = path / "calendar.txt"
    if cal_path.is_file():
        cal = pd.read_csv(cal_path, dtype={"service_id": str, "start_date": str, "end_date": str})
    dates_path = path / "calendar_dates.txt"
    if dates_path.is_file():
        cal_dates = pd.read_csv(dates_path, dtype={"service_id": str, "date": str})
    return cal, cal_dates


def active_service_ids(service_date: date, gtfs_dir: Path | None = None) -> frozenset[str] | None:
    """Service ids running on ``service_date`` per calendar + exceptions.

    Returns ``None`` when the bundle ships neither calendar file — "unknown",
    which callers must treat differently from "no service".
    """
    token = bundle_token(gtfs_dir)
    cal, cal_dates = _calendar_frames(token)
    if cal is None and cal_dates is None:
        return None

    ymd = service_date.strftime("%Y%m%d")
    active: set[str] = set()
    if cal is not None and not cal.empty:
        weekday_col = _WEEKDAY_COLS[service_date.weekday()]
        if weekday_col in cal.columns:
            mask = (
                (cal[weekday_col] == 1)
                & (cal["start_date"] <= ymd)
                & (cal["end_date"] >= ymd)
            )
            active.update(cal.loc[mask, "service_id"].astype(str))
    if cal_dates is not None and not cal_dates.empty:
        day = cal_dates.loc[cal_dates["date"] == ymd]
        active.update(day.loc[day["exception_type"] == 1, "service_id"].astype(str))
        active.difference_update(day.loc[day["exception_type"] == 2, "service_id"].astype(str))
    return frozenset(active)


@lru_cache(maxsize=2)
def _trip_starts(token: tuple[str, float]) -> pd.DataFrame:
    """Per-trip first scheduled departure: trip_id, route_id, direction_id,
    service_id, start_s. Empty frame when stop_times lacks departure_time."""
    path = Path(token[0]) / "stop_times.txt"
    try:
        st = pd.read_csv(
            path,
            usecols=lambda c: c in {"trip_id", "departure_time", "stop_sequence"},
            dtype={"trip_id": str, "departure_time": str},
        )
    except (FileNotFoundError, ValueError):
        return pd.DataFrame(columns=["trip_id", "route_id", "direction_id", "service_id", "start_s"])
    if "departure_time" not in st.columns or st.empty:
        return pd.DataFrame(columns=["trip_id", "route_id", "direction_id", "service_id", "start_s"])

    first = st.loc[st.groupby("trip_id")["stop_sequence"].idxmin(), ["trip_id", "departure_time"]]
    first["start_s"] = first["departure_time"].map(_parse_gtfs_time_s)
    first = first.dropna(subset=["start_s"])

    trips = load_all(Path(token[0])).trips
    cols = [c for c in ("trip_id", "route_id", "direction_id", "service_id") if c in trips.columns]
    out = first.merge(trips[cols], on="trip_id", how="left")
    out["start_s"] = out["start_s"].astype(int)
    return out[["trip_id", "route_id", "direction_id", "service_id", "start_s"]]


@lru_cache(maxsize=64)
def _headways_for_slice(
    token: tuple[str, float],
    route_id: str,
    direction_id: int,
    service_ids: frozenset[str] | None,
) -> dict[str, float]:
    """trip_id → scheduled headway seconds (gap to the previous scheduled trip
    of the same route+direction on the active services). First trip of the
    span has no predecessor and is omitted."""
    starts = _trip_starts(token)
    if starts.empty:
        return {}
    mask = (starts["route_id"] == route_id) & (starts["direction_id"] == direction_id)
    if service_ids is not None:
        mask &= starts["service_id"].astype(str).isin(service_ids)
    day = starts.loc[mask, ["trip_id", "start_s"]].sort_values("start_s")
    if len(day) < 2:
        return {}
    headways: dict[str, float] = {}
    prev_s: int | None = None
    for row in day.itertuples(index=False):
        if prev_s is not None:
            headways[str(row.trip_id)] = float(row.start_s - prev_s)
        prev_s = int(row.start_s)
    return headways


def scheduled_headway_s(
    trip_id: str,
    route_id: str,
    direction_id: int,
    service_date: date,
    gtfs_dir: Path | None = None,
) -> float | None:
    """Scheduled headway for ``trip_id`` on ``service_date``, in seconds.

    None when unknown: trip absent from the bundle, first trip of the day,
    no departure times, or the trip's service isn't active that date.
    """
    if gtfs_dir is None:
        gtfs_dir = get_settings().gtfs_static_dir
    token = bundle_token(gtfs_dir)
    services = active_service_ids(service_date, gtfs_dir)
    headways = _headways_for_slice(token, route_id, int(direction_id), services)
    return headways.get(str(trip_id))
