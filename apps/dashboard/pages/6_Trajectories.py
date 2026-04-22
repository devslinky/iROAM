"""Trip Trajectories — upsampled 10s-grid output of the analytics worker.

Pick a service date → route → trip instance. Shows:
  * summary metrics (duration, distance, point count, mean speed)
  * map with the shape-interpolated path (points color-graded by time)
  * distance-over-time and speed-over-time charts
  * raw rows expander

The underlying data is append-only-per-trip-instance (delete-then-insert on
refresh); no stale-run de-duplication is needed in the client.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

from apps.dashboard.api_client import (
    APIError,
    trajectory_detail,
    trajectory_routes,
    trajectory_service_dates,
    trajectory_trips,
)

st.set_page_config(page_title="Trip Trajectories", layout="wide")
st.title("Trip Trajectories")
st.caption(
    "Upsampled (10s-grid) analytics output — shape-projected travel distance, "
    "per trip instance. Refreshed incrementally by the analytics-worker."
)

with st.sidebar:
    st.header("Filters")
    show_map = st.checkbox("Interpolate shape onto map", value=True)
    point_limit = st.number_input(
        "Trip list limit", min_value=50, max_value=5000, value=500, step=50
    )

try:
    service_dates = trajectory_service_dates(limit=30)
except APIError as exc:
    st.error(f"Could not load service dates: {exc}")
    st.stop()

if not service_dates:
    st.info("No trajectories yet. Let the analytics-worker run for a bit.")
    st.stop()

service_date = st.selectbox("Service date", options=service_dates, index=0)

try:
    routes = trajectory_routes(service_date)
except APIError as exc:
    st.error(f"Could not load routes: {exc}")
    st.stop()

route_filter = st.selectbox(
    "Route filter",
    options=["(all)"] + sorted(routes),
    index=0,
)
route_id = None if route_filter == "(all)" else route_filter

try:
    trips = trajectory_trips(
        service_date, route_id=route_id, limit=int(point_limit)
    )
except APIError as exc:
    st.error(f"Could not load trip list: {exc}")
    st.stop()

if not trips:
    st.info("No trips match this filter.")
    st.stop()

trips_df = pd.DataFrame(trips)
trips_df["first_datetime"] = pd.to_datetime(trips_df["first_datetime"], utc=True)
trips_df["last_datetime"] = pd.to_datetime(trips_df["last_datetime"], utc=True)
trips_df["duration_min"] = (
    (trips_df["last_datetime"] - trips_df["first_datetime"]).dt.total_seconds() / 60
).round(1)
trips_df["distance_km"] = (trips_df["travel_distance_m"] / 1000.0).round(2)
trips_df = trips_df.sort_values("last_datetime", ascending=False).reset_index(drop=True)

trips_df["label"] = (
    trips_df["trip_id"]
    + "  ·  route "
    + trips_df["route_id"].fillna("—").astype(str)
    + "  ·  "
    + trips_df["first_datetime"].dt.strftime("%H:%M:%S")
    + "→"
    + trips_df["last_datetime"].dt.strftime("%H:%M:%S")
    + "  ·  "
    + trips_df["distance_km"].astype(str)
    + " km"
)

trip_choice = st.selectbox(
    f"Trip instance ({len(trips_df)} available)",
    options=trips_df["label"].tolist(),
)
chosen_row = trips_df.loc[trips_df["label"] == trip_choice].iloc[0]
trip_id = chosen_row["trip_id"]
start_date = chosen_row["start_date"]

try:
    detail = trajectory_detail(
        trip_id, start_date=start_date, include_shape=show_map
    )
except APIError as exc:
    st.error(f"Could not load trajectory: {exc}")
    st.stop()

points = detail.get("points") or []
if not points:
    st.warning("Trip has no points.")
    st.stop()

pts = pd.DataFrame(points)
pts["datetime"] = pd.to_datetime(pts["datetime"], utc=True)
pts = pts.sort_values("datetime").reset_index(drop=True)
pts["speed_kph"] = (pts["moving_speed_m_s"].fillna(0) * 3.6).round(2)
pts["distance_km"] = (pts["travel_distance_m"] / 1000.0).round(3)
pts["age_seconds"] = (
    (pts["datetime"] - pts["datetime"].iloc[0]).dt.total_seconds().astype(int)
)

st.subheader(f"Trip {trip_id}  ·  service {detail.get('service_date')}")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Route", detail.get("route_id") or "—")
c2.metric("Direction", detail.get("direction_id") if detail.get("direction_id") is not None else "—")
c3.metric("Points", detail.get("point_count", 0))
duration_min = (pts["datetime"].iloc[-1] - pts["datetime"].iloc[0]).total_seconds() / 60
c4.metric("Duration", f"{duration_min:.1f} min")
c5.metric("Distance", f"{pts['distance_km'].iloc[-1]:.2f} km")

c6, c7, c8 = st.columns(3)
mean_speed = pts["speed_kph"].replace(0, pd.NA).dropna().mean()
max_speed = pts["speed_kph"].max()
c6.metric("Mean moving speed", f"{mean_speed:.1f} km/h" if pd.notna(mean_speed) else "n/a")
c7.metric("Max speed", f"{max_speed:.1f} km/h")
c8.metric("Vehicle", detail.get("vehicle_id") or "—")

if show_map and "latitude" in pts.columns and pts["latitude"].notna().any():
    st.subheader("Shape-projected path")
    map_df = pts.dropna(subset=["latitude", "longitude"]).copy()
    if not map_df.empty:
        path_points = map_df[["longitude", "latitude"]].values.tolist()
        path_layer = pdk.Layer(
            "PathLayer",
            data=[{"path": path_points, "color": [52, 152, 219]}],
            get_path="path",
            get_color="color",
            get_width=4,
            width_min_pixels=2,
        )
        # Color-grade points by time for direction cue — blue early, red late.
        n = len(map_df)
        map_df["r"] = (map_df.index * 255 // max(n - 1, 1)).astype(int)
        map_df["g"] = 60
        map_df["b"] = (255 - map_df.index * 255 // max(n - 1, 1)).astype(int)
        map_df["time_str"] = map_df["datetime"].dt.strftime("%H:%M:%S")
        scatter = pdk.Layer(
            "ScatterplotLayer",
            data=map_df,
            get_position="[longitude, latitude]",
            get_fill_color="[r, g, b]",
            get_radius=20,
            radius_min_pixels=2,
            radius_max_pixels=6,
            pickable=True,
            opacity=0.85,
        )
        view_state = pdk.ViewState(
            latitude=float(map_df["latitude"].mean()),
            longitude=float(map_df["longitude"].mean()),
            zoom=12,
            pitch=0,
        )
        tooltip = {
            "html": (
                "<b>{time_str}</b><br/>"
                "Distance: {distance_km} km<br/>"
                "Speed: {speed_kph} km/h"
            ),
            "style": {"backgroundColor": "rgba(30,30,30,0.85)", "color": "white"},
        }
        st.pydeck_chart(
            pdk.Deck(
                layers=[path_layer, scatter],
                initial_view_state=view_state,
                tooltip=tooltip,
                map_style=None,
            )
        )
    else:
        st.info("Shape returned no interpolated points (shape_id may be missing in GTFS static).")
elif show_map:
    st.info(
        "Map enabled but no lon/lat returned. "
        "Likely the trip's shape_id couldn't be resolved from the static bundle."
    )

col_left, col_right = st.columns(2)
with col_left:
    st.subheader("Distance over time")
    dist_chart = (
        alt.Chart(pts)
        .mark_line()
        .encode(
            x=alt.X("datetime:T", title="Time (UTC)"),
            y=alt.Y("distance_km:Q", title="Travel distance (km)"),
            tooltip=["datetime:T", "distance_km:Q", "observed:N"],
        )
        .properties(height=260)
    )
    st.altair_chart(dist_chart, use_container_width=True)

with col_right:
    st.subheader("Speed over time")
    speed_chart = (
        alt.Chart(pts)
        .mark_line(point=False)
        .encode(
            x=alt.X("datetime:T", title="Time (UTC)"),
            y=alt.Y("speed_kph:Q", title="Moving speed (km/h)"),
            tooltip=["datetime:T", "speed_kph:Q"],
        )
        .properties(height=260)
    )
    st.altair_chart(speed_chart, use_container_width=True)

with st.expander(f"Raw points ({len(pts)})"):
    display_cols = [
        "datetime",
        "time_offset_seconds",
        "distance_km",
        "speed_kph",
        "observed",
        "occupancy_status",
    ]
    if "latitude" in pts.columns:
        display_cols += ["latitude", "longitude"]
    present = [c for c in display_cols if c in pts.columns]
    st.dataframe(pts[present], use_container_width=True, hide_index=True)
