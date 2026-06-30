import io
import sys
import argparse
import numpy as np
import pandas as pd
from datetime import date
from pathlib import Path
from apps.analytics.pipeline import _EFFECTIVE_START_DATE
from apps.analytics.stop_projection import compute_route_stops
from apps.api.routers.iroam import _group_into_buses
from db.queries.iroam import fetch_trajectories_for_slice
from sqlalchemy import func, select
from apps.analytics.gtfs_static import load_all, resolve_shape_id
from apps.analytics.project_to_shape import project_trajectory
from apps.analytics.shapes import build_linestrings
from apps.analytics.trajectory_extract import build_trip_trajectory
from core.logging import configure_logging
from db.models.vehicle_position import VehiclePosition
from db.models.trip_trajectory import TripTrajectory
from db.session import SessionLocal
from contextlib import redirect_stdout

configure_logging()

def _parse_args(argv):
    p = argparse.ArgumentParser(description="Profile trajectory data quality")
    p.add_argument("--date", required=True, type=date.fromisoformat, help="service date (YYYY-MM-DD)")
    p.add_argument("--route", type=str, default=None, help="filter to a single route_id")
    return p.parse_args(argv)

def _run(argv=None):
    # 1. Write scripts/analysis/profile_quality.py that takes --route and --date and pulls the raw vehicle_positions rows plus the
    #    processed trip_trajectories for that slice.

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    route_id = args.route
    service_date = args.date

    # pull the raw vehicle_positions rows for that route and date
    with SessionLocal() as session:
        yyyymmdd = service_date.strftime("%Y%m%d")
        stmt = (
            select(VehiclePosition)
            .where(VehiclePosition.route_id == route_id)
            .where(_EFFECTIVE_START_DATE == yyyymmdd)
        )
        vehicle_positions = session.execute(stmt).scalars().all()

    vehicle_positions_df = pd.DataFrame([
        {c.name: getattr(vp, c.name) for c in VehiclePosition.__table__.columns}
        for vp in vehicle_positions
    ])
    print(f"Pulled {len(vehicle_positions_df)} vehicle_positions rows for route {route_id} on date {service_date}")

    # pull the processed trip_trajectories for that route and date
    with SessionLocal() as session:
        stmt = (
            select(TripTrajectory)
            .where(TripTrajectory.route_id == route_id)
            .where(TripTrajectory.service_date == service_date)
        )
        trip_trajectories = session.execute(stmt).scalars().all()

    trip_trajectories_df = pd.DataFrame([
        {c.name: getattr(tt, c.name) for c in TripTrajectory.__table__.columns}
        for tt in trip_trajectories
    ])
    print(f"Pulled {len(trip_trajectories_df)} trip_trajectories rows for route {route_id} on date {service_date}")

    # load static gtfs
    static = load_all()
    shape_lines = build_linestrings(static.shapes)

    # get unique trip_ids from vehicle_positions
    unique_trip_ids = vehicle_positions_df["trip_id"].dropna().unique()
    print(f"\nFound {len(unique_trip_ids)} unique trip_ids in vehicle_positions")


    # 2. Compute and print Drop rates at each filter stage : how many raw points enter, and how many are removed by filters

    all_ortho_before = []
    all_ortho_after = []

    for trip_id in unique_trip_ids:
        rows = [vp for vp in vehicle_positions if vp.trip_id == trip_id]
        if not rows:
            continue

        # filter 1: exact-timestamp deduplication
        print(f"\nProcessing trip_id {trip_id} with {len(rows)} raw points before exact-timestamp deduplication")
        df = build_trip_trajectory(rows, static.trips)
        print(f"Processing trip_id {trip_id} with {len(df)} points after exact-timestamp deduplication")
        print(f"trip {trip_id}: {len(rows) - len(df)} points dropped by exact-timestamp deduplication")

        if df.empty:
            continue

        shape_id = resolve_shape_id(static, trip_id)
        if shape_id is None or shape_id not in shape_lines:
            print(f"No shape found for trip_id {trip_id}, skipping")
            continue

        # unfiltered projection — for orthogonal distance distribution
        df_unfiltered = project_trajectory(
            df, shape_lines[shape_id], max_orthogonal_distance_m=999999.0
        )
        all_ortho_before.extend(df_unfiltered["orthogonal_distance_m"].tolist())

        # filtered projection — for drop rate
        df_filtered = project_trajectory(
            df, shape_lines[shape_id], max_orthogonal_distance_m=200.0
        )
        all_ortho_after.extend(df_filtered["orthogonal_distance_m"].tolist())

        # filter 2: orthogonal distance filter (200m)
        print(
            f"\ntrip {trip_id}: {len(df_unfiltered)} points before orthogonal distance filter, "
            f"trip {trip_id}: {len(df_filtered)} points after orthogonal distance filter "
            f"({len(df_unfiltered) - len(df_filtered)} dropped)"
        )

    # output 1: orthogonal distance distribution (before and after stage 1)
    if all_ortho_before and all_ortho_after:
        ortho_before = np.array(all_ortho_before)
        ortho_after = np.array(all_ortho_after)
        print(f"\nOrthogonal distance distribution (before 200m filter):")
        print(f"  p50: {np.percentile(ortho_before, 50):.1f} m")
        print(f"  p90: {np.percentile(ortho_before, 90):.1f} m")
        print(f"  p95: {np.percentile(ortho_before, 95):.1f} m")
        print(f"  p99: {np.percentile(ortho_before, 99):.1f} m")
        print(f"  max: {ortho_before.max():.1f} m")
        print(f"  % dropped by 200m filter: {(ortho_before > 200).mean() * 100:.1f}%")

        print(f"\nOrthogonal distance distribution (after 200m filter):")
        print(f"  p50: {np.percentile(ortho_after, 50):.1f} m")
        print(f"  p90: {np.percentile(ortho_after, 90):.1f} m")
        print(f"  p95: {np.percentile(ortho_after, 95):.1f} m")
        print(f"  p99: {np.percentile(ortho_after, 99):.1f} m")
        print(f"  max: {ortho_after.max():.1f} m")
    else:
        print("No orthogonal distance data collected")

    # filter 3: ghost segement and stale speed filter

    direction_ids = trip_trajectories_df["direction_id"].dropna().unique()

    for direction_id in direction_ids:
        # get route stops for this route and direction
        route_stops = compute_route_stops(route_id, direction_id)

        # get trip_trajectory slice for this route, direction and service date ordered by trip_id, start_date, vehicle_id, datetime
        trip_trajectories_slice = trip_trajectories_df[
            (trip_trajectories_df["route_id"] == route_id) &
            (trip_trajectories_df["direction_id"] == direction_id) &
            (trip_trajectories_df["service_date"] == service_date)
        ].sort_values(by=["trip_id", "start_date", "vehicle_id", "datetime"])

        print(f"\nProcessing trip_trajectories for route {route_id}, direction {direction_id} on date {service_date} through ghost segment and stale speed filter")
        buses = _group_into_buses(list(trip_trajectories_slice.itertuples(index=False)), route_stops)

        # get number of trip_trajectories rows dropped by ghost segment and stale speed filter
        total_points_before = len(trip_trajectories_slice)
        total_points_after = sum(len(bus.points) for bus in buses)
        points_dropped = total_points_before - total_points_after
        pct_dropped = (points_dropped / total_points_before * 100) if total_points_before > 0 else 0

        print(f"  Points before ghost/stale filter: {total_points_before}")
        print(f"  Points after ghost/stale filter:  {total_points_after}")
        print(f"  Points dropped:                   {points_dropped} ({pct_dropped:.1f}%)")
        print(f"  BusTrajectory segments produced:  {len(buses)}")

    # output #2: GPS Cadence (distribution of seconds between consecutive vehicle_position points per trip)
    counter = 0
    for trip_id in unique_trip_ids:
        trip_vp = vehicle_positions_df[vehicle_positions_df["trip_id"] == trip_id].sort_values(by="vehicle_timestamp")
        if len(trip_vp) < 2:
            continue
        trip_vp["time_diff"] = trip_vp["vehicle_timestamp"].diff().dt.total_seconds()
        print(f"\n quantile distribution of Trip {trip_id} GPS cadence (seconds between consecutive points):")
        print(trip_vp["time_diff"].describe(percentiles=[0.25, 0.5, 0.75, 1.00]))

        print(f"count and cummulative size of GPS Cadence for Trip {trip_id} over 2 minutes (120 seconds):")
        over_2min = trip_vp[trip_vp["time_diff"] > 120]
        print(f"  count: {len(over_2min)}")
        print(f"  size: {over_2min['time_diff'].sum()} seconds")

        if len(over_2min) > 0:
            counter += 1

    print(f"\n % of trips with GPS cadence over 2 minutes: {counter / len(unique_trip_ids) * 100:.1f}%")

    # output #3: distribution of vehicle_timestamp - fetched_at (how long after the vehicle_timestamp was the point fetched)
    vehicle_positions_df["fetch_delay"] = (vehicle_positions_df["fetched_at"] - vehicle_positions_df["vehicle_timestamp"]).dt.total_seconds()
    print(f"\nDistribution of vehicle_timestamp - fetched_at (seconds):")
    print(vehicle_positions_df["fetch_delay"].describe(percentiles=[0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.00]))

    # output #4: field availability (% of rows where each of these is non-null: occupancy_status, occupancy_percentage, current_stop_sequence, stop_id, current_status, bearing, speed_mps, odometer, direction_id, vehicle_id.)
    fields = ["occupancy_status", "occupancy_percentage", "current_stop_sequence", "stop_id", "current_status", "bearing", "speed_mps", "odometer", "direction_id", "vehicle_id"]
    print(f"\nField availability (% of rows where each field is non-null):")
    for field in fields:
        availability = vehicle_positions_df[field].notnull().mean() * 100
        print(f"  {field}: {availability:.1f}%")

    return route_id, service_date


def main(argv=None):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        route_id, service_date = _run(argv)
    output = buffer.getvalue()

    sys.stdout.write(output)

    # write to markdown
    out_dir = Path("out/qa")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"route{route_id}_{service_date}_quality_profile.md"

    md_lines = [
        f"# Quality Profile — Route {route_id} — {service_date}",
        "",
        "```",
        output.rstrip(),
        "```",
    ]
    out_path.write_text("\n".join(md_lines))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
