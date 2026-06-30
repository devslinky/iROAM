"""Turn a list of ``VehiclePosition`` rows into an analysis-ready DataFrame.

Pure: no DB access. The caller passes in the ORM rows (loaded via
``db.queries.vehicles.fetch_by_trip_instance``) and the ``trips_df`` from the
GTFS static bundle.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from core.time import TORONTO_TZ
from db.models.vehicle_position import VehiclePosition


def _parse_start_datetime(start_date: str, start_time: str | None) -> datetime | None:
    """Combine GTFS-RT ``start_date`` (YYYYMMDD) + ``start_time`` (HH:MM:SS, may
    exceed 24h for overnight trips) into an absolute UTC datetime.

    The GTFS service day starts at local *noon minus 12 h* — in practice,
    local midnight — and schedule times are agency wall-clock, so the anchor
    must use the real Toronto zone (UTC-5/-4 across DST), not a fixed offset:
    a fixed UTC-5 shifts every summer trip's ``time_offset_seconds`` by 1 h.
    ``start_time`` like ``27:15:00`` (overnight trips) is handled by adding a
    plain timedelta to the local-midnight anchor.
    """
    if not start_date:
        return None
    try:
        d = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
    except (ValueError, IndexError):
        return None
    base = datetime(d.year, d.month, d.day, tzinfo=TORONTO_TZ)
    if not start_time:
        return base.astimezone(timezone.utc)
    try:
        hh, mm, ss = (int(p) for p in start_time.split(":"))
    except (ValueError, AttributeError):
        return base.astimezone(timezone.utc)
    return (base + timedelta(hours=hh, minutes=mm, seconds=ss)).astimezone(timezone.utc)


def build_trip_trajectory(
    rows: Iterable[VehiclePosition],
    trips_df: pd.DataFrame,
) -> pd.DataFrame:
    """Build the per-trip DataFrame on which projection + upsample operate.

    Columns: ``datetime``, ``latitude``, ``longitude``, ``vehicle_id``,
    ``occupancy_status``, ``source_vehicle_position_id``, ``trip_id``,
    ``start_date``, ``route_id``, ``direction_id``, ``shape_id``,
    ``trip_start_datetime``.
    """
    records = []
    for r in rows:
        if r.latitude is None or r.longitude is None:
            continue
        dt = r.vehicle_timestamp or r.fetched_at
        records.append(
            {
                "datetime": dt,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "vehicle_id": r.vehicle_id,
                "occupancy_status": r.occupancy_status,
                "source_vehicle_position_id": r.id,
                "trip_id": r.trip_id,
                "start_date": r.start_date,
                "start_time": r.start_time,
                "route_id": r.route_id,
                "direction_id": r.direction_id,
            }
        )

    if not records:
        return pd.DataFrame(
            columns=[
                "datetime", "latitude", "longitude", "vehicle_id",
                "occupancy_status", "source_vehicle_position_id",
                "trip_id", "start_date", "route_id", "direction_id",
                "shape_id", "trip_start_datetime", "time_offset_seconds",
            ]
        )

    df = pd.DataFrame.from_records(records)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values(["datetime", "source_vehicle_position_id"]).reset_index(drop=True)
    # Deduplicate exact-timestamp collisions — keep the most recently ingested row.
    df = df.drop_duplicates(subset=["datetime"], keep="last").reset_index(drop=True)

    # Attach shape_id (and fill direction_id when missing) from GTFS static.
    trips_sub = trips_df[["trip_id", "shape_id", "direction_id"]].rename(
        columns={"direction_id": "direction_id_static"}
    )
    df = df.merge(trips_sub, how="left", on="trip_id")
    df["direction_id"] = df["direction_id"].where(df["direction_id"].notna(), df["direction_id_static"])
    df = df.drop(columns=["direction_id_static"])

    # trip_start_datetime + time_offset_seconds
    start_date_val = df["start_date"].iloc[0] if not df.empty else None
    start_time_val = df["start_time"].iloc[0] if not df.empty else None
    trip_start = _parse_start_datetime(start_date_val, start_time_val) if start_date_val else None
    df["trip_start_datetime"] = trip_start
    if trip_start is not None:
        df["time_offset_seconds"] = (
            (df["datetime"] - pd.Timestamp(trip_start)).dt.total_seconds().astype("Int64")
        )
    else:
        df["time_offset_seconds"] = pd.Series([pd.NA] * len(df), dtype="Int64")

    return df
