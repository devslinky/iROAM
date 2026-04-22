"""Project GTFS stops onto the route shape → ordered stop list with distance-along-shape.

For the iROAM dashboard we need a fractional ``stop_index`` for each trajectory
point. The cheapest way to compute it is to precompute, per ``(route_id,
direction_id)``, the ordered list of stops along the canonical shape plus each
stop's distance along that shape (in meters). A trajectory point's
``travel_distance_m`` then binary-searches into that distance array.

The canonical shape is picked as the most-common ``shape_id`` for the
(route_id, direction_id) pair; ties broken by the trip with the most
stop_times, so we don't pick a short-turn variant.

Cached per (route_id, direction_id); safe because the static bundle is
process-local and rarely changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from shapely.geometry import Point

from apps.analytics.gtfs_static import GtfsStatic, load_all
from apps.analytics.shapes import build_linestrings, transform_lonlat_to_3857


@dataclass(frozen=True)
class StopOnRoute:
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    stop_sequence: int
    distance_m: float  # distance along the shape, from shape start


@dataclass(frozen=True)
class RouteStops:
    route_id: str
    direction_id: int
    shape_id: str
    shape_length_m: float
    stops: tuple[StopOnRoute, ...]


def _pick_canonical_trip(
    static: GtfsStatic, route_id: str, direction_id: int
) -> str | None:
    trips = static.trips
    mask = (trips["route_id"] == route_id) & (trips["direction_id"] == direction_id)
    candidates = trips.loc[mask, ["trip_id", "shape_id"]].dropna()
    if candidates.empty:
        return None

    # Most-common shape_id wins; ties → the trip with the most stop_times.
    top_shape = candidates["shape_id"].value_counts().idxmax()
    trip_ids = candidates.loc[candidates["shape_id"] == top_shape, "trip_id"].tolist()
    st = static.stop_times
    st_counts = st.loc[st["trip_id"].isin(trip_ids), "trip_id"].value_counts()
    if st_counts.empty:
        return None
    return str(st_counts.idxmax())


@lru_cache(maxsize=64)
def compute_route_stops(route_id: str, direction_id: int) -> RouteStops | None:
    """Return the ordered stops along the canonical shape for this route+direction.

    Returns ``None`` if the route+direction has no shape or no stop_times.
    """
    static = load_all()
    canonical_trip_id = _pick_canonical_trip(static, route_id, direction_id)
    if canonical_trip_id is None:
        return None

    trip_row = static.trips.loc[static.trips["trip_id"] == canonical_trip_id].iloc[0]
    shape_id = str(trip_row["shape_id"])

    lines = build_linestrings(static.shapes.loc[static.shapes["shape_id"] == shape_id])
    line = lines.get(shape_id)
    if line is None:
        return None

    ordered_st = (
        static.stop_times.loc[static.stop_times["trip_id"] == canonical_trip_id]
        .sort_values("stop_sequence")
    )
    if ordered_st.empty:
        return None

    stops = static.stops.set_index("stop_id")
    seen: set[str] = set()
    stops_out: list[StopOnRoute] = []
    for row in ordered_st.itertuples(index=False):
        sid = str(row.stop_id)
        if sid in seen or sid not in stops.index:
            continue
        seen.add(sid)
        s = stops.loc[sid]
        lon, lat = float(s["stop_lon"]), float(s["stop_lat"])
        x, y = transform_lonlat_to_3857(lon, lat)
        dist_m = float(line.project(Point(x, y)))
        stops_out.append(
            StopOnRoute(
                stop_id=sid,
                stop_name=str(s["stop_name"]),
                stop_lat=lat,
                stop_lon=lon,
                stop_sequence=int(row.stop_sequence),
                distance_m=dist_m,
            )
        )

    # Enforce monotonic distance — interpolated projections occasionally wobble
    # by a few cm when a stop sits on an almost-straight segment; sort by distance
    # so the binary search remains well-defined.
    stops_out.sort(key=lambda s: s.distance_m)

    return RouteStops(
        route_id=route_id,
        direction_id=direction_id,
        shape_id=shape_id,
        shape_length_m=float(line.length),
        stops=tuple(stops_out),
    )


def distance_to_stop_index(distance_m: float, route_stops: RouteStops) -> float:
    """Map ``travel_distance_m`` to a fractional stop index in ``[0, N-1]``."""
    n = len(route_stops.stops)
    if n == 0:
        return 0.0
    if distance_m <= route_stops.stops[0].distance_m:
        return 0.0
    if distance_m >= route_stops.stops[-1].distance_m:
        return float(n - 1)

    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if route_stops.stops[mid].distance_m <= distance_m:
            lo = mid
        else:
            hi = mid
    d_lo = route_stops.stops[lo].distance_m
    d_hi = route_stops.stops[hi].distance_m
    if d_hi <= d_lo:
        return float(lo)
    return lo + (distance_m - d_lo) / (d_hi - d_lo)
