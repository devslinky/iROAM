"""Per-trip-instance orchestration.

Pure-ish: reads the DB (via ``fetch_by_trip_instance``) but does not write.
The runner owns the transaction + analytics_runs lifecycle.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.analytics.gtfs_static import GtfsStatic, resolve_route_id, resolve_shape_id
from apps.analytics.project_to_shape import project_trajectory
from apps.analytics.trajectory_extract import build_trip_trajectory
from apps.analytics.upsample import compute_moving_speed, last_step_clean_up, upsample_df
from db.models.vehicle_position import VehiclePosition
from db.queries.vehicles import fetch_by_trip_instance

# The TTC VehiclePositions feed does not populate TripDescriptor.start_date,
# so analytics derives an "effective start_date" from the observation timestamp
# in Toronto local time. Feeds that do set start_date are honored verbatim.
_EFFECTIVE_START_DATE = func.coalesce(
    VehiclePosition.start_date,
    func.to_char(
        func.timezone("America/Toronto", VehiclePosition.vehicle_timestamp),
        "YYYYMMDD",
    ),
    func.to_char(
        func.timezone("America/Toronto", VehiclePosition.fetched_at),
        "YYYYMMDD",
    ),
)


def list_trip_instances(
    session: Session,
    service_date: date,
    *,
    route_id: str | None = None,
) -> list[tuple[str, str]]:
    """Distinct ``(trip_id, start_date)`` pairs active on ``service_date``.

    Prefers TripDescriptor.start_date when present; otherwise synthesizes it
    from ``vehicle_timestamp`` / ``fetched_at`` at Toronto local time. This
    keeps overnight trips (start_time >= 24:00) scoped to their true service
    day when the feed sets start_date, and still works on TTC's feed which
    doesn't.
    """
    yyyymmdd = service_date.strftime("%Y%m%d")
    stmt = (
        select(VehiclePosition.trip_id, _EFFECTIVE_START_DATE.label("eff_start_date"))
        .where(VehiclePosition.trip_id.is_not(None))
        .where(_EFFECTIVE_START_DATE == yyyymmdd)
        .distinct()
    )
    if route_id is not None:
        stmt = stmt.where(VehiclePosition.route_id == route_id)
    return [(row.trip_id, row.eff_start_date) for row in session.execute(stmt).all()]


def list_changed_trip_instances(
    session: Session,
    service_date: date,
    *,
    since: datetime,
    route_id: str | None = None,
) -> list[tuple[str, str]]:
    """Trip instances with at least one VehiclePosition row newer than ``since``.

    Used by the analytics worker for incremental refresh: only trips whose
    raw observations have grown since the last tick need their trajectory
    re-derived. Safe for idempotent re-runs because the runner
    delete-then-inserts per ``(trip_id, start_date)``.
    """
    yyyymmdd = service_date.strftime("%Y%m%d")
    stmt = (
        select(VehiclePosition.trip_id, _EFFECTIVE_START_DATE.label("eff_start_date"))
        .where(VehiclePosition.trip_id.is_not(None))
        .where(_EFFECTIVE_START_DATE == yyyymmdd)
        .where(VehiclePosition.fetched_at > since)
        .distinct()
    )
    if route_id is not None:
        stmt = stmt.where(VehiclePosition.route_id == route_id)
    return [(row.trip_id, row.eff_start_date) for row in session.execute(stmt).all()]


def process_trip_instance(
    session: Session,
    static: GtfsStatic,
    shape_lines: dict,
    trip_id: str,
    start_date: str,
    *,
    upsample_resolution_s: int = 10,
    max_orthogonal_distance_m: float = 200.0,
) -> pd.DataFrame:
    """Full per-trip transform: fetch -> extract -> project -> speed -> upsample.

    Returns the final upsampled DataFrame (empty if the trip has <2 usable
    points or if its shape can't be resolved). Caller converts to ORM rows
    and commits.
    """
    rows = fetch_by_trip_instance(session, trip_id, start_date)
    if not rows:
        return pd.DataFrame()

    # Stale-feed false-match guard: realtime trip_ids are recycled across GTFS
    # feed versions, so an expired static bundle can resolve this trip_id to an
    # unrelated route. Projecting the bus's GPS onto that route's shape yields
    # garbage travel distances. If the static feed disagrees with the route the
    # realtime feed reported, drop the trip rather than emit nonsense.
    realtime_route = next((r.route_id for r in rows if r.route_id), None)
    static_route = resolve_route_id(static, trip_id)
    if (
        realtime_route is not None
        and static_route is not None
        and static_route != realtime_route
    ):
        return pd.DataFrame()

    df = build_trip_trajectory(rows, static.trips)
    if df.empty:
        return df

    # Overwrite with the effective start_date the caller used to key this
    # instance — the raw VehiclePosition.start_date may be NULL (TTC feed).
    df["start_date"] = start_date

    shape_id = resolve_shape_id(static, trip_id)
    df["shape_id"] = shape_id
    service_date_val = None
    if start_date and len(start_date) == 8:
        from datetime import date as _date
        service_date_val = _date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
    df["service_date"] = service_date_val

    if shape_id is None or shape_id not in shape_lines:
        return pd.DataFrame()

    df = project_trajectory(df, shape_lines[shape_id], max_orthogonal_distance_m=max_orthogonal_distance_m)
    if df.empty or len(df) < 2:
        return pd.DataFrame()

    df = compute_moving_speed(df)
    df["observed"] = True
    df_up = upsample_df(df, upsample_resolution_s)
    if df_up.empty:
        return pd.DataFrame()

    # Re-attach the static identity columns that upsample_df's boundary logic
    # preserves row-wise from (current, next) — these are already carried
    # through, but we recompute time_offset_seconds on the new datetimes.
    if "trip_start_datetime" in df.columns and df["trip_start_datetime"].notna().any():
        trip_start = df["trip_start_datetime"].iloc[0]
        if trip_start is not None:
            df_up["time_offset_seconds"] = (
                (df_up["datetime"] - pd.Timestamp(trip_start)).dt.total_seconds().astype("Int64")
            )

    return last_step_clean_up(df_up)
