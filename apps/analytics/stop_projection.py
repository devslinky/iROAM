"""Project GTFS stops onto the route shape → ordered stop list with distance-along-shape.

For the iROAM dashboard we need a fractional ``stop_index`` for each trajectory
point. The cheapest way to compute it is to precompute, per ``(route_id,
direction_id)``, the ordered list of stops along the canonical shape plus each
stop's distance along that shape (in meters). A trajectory point's
``travel_distance_m`` then binary-searches into that distance array.

The canonical shape is picked as the most-common ``shape_id`` for the
(route_id, direction_id) pair; ties broken by the trip with the most
stop_times, so we don't pick a short-turn variant.

Cached per (bundle, route_id, direction_id). The bundle component of the key
matters: ``Complete GTFS/`` is refreshed in place every board period, and a
cache keyed only on route+direction would keep serving the previous period's
shape after a swap (stop list subtly misaligned with newly written
trajectories) until the process restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from shapely.geometry import LineString, Point
from shapely.ops import substring

from apps.analytics.gtfs_static import GtfsStatic, bundle_token, load_all, load_shape_linestrings
from apps.analytics.shapes import transform_lonlat_to_meters
from core.logging import get_logger

_logger = get_logger(__name__)

# A stop projecting more than this *behind* its predecessor is treated as a
# wrong-leg projection (self-overlapping shape) and re-projected onto the
# remainder of the line; anything inside the tolerance is ordinary projection
# wobble and is clamped forward.
_MONOTONE_TOL_M = 1.0


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


def compute_route_stops(route_id: str, direction_id: int) -> RouteStops | None:
    """Return the ordered stops along the canonical shape for this route+direction.

    Returns ``None`` if the route+direction has no shape or no stop_times.
    """
    return _compute_route_stops(bundle_token(), route_id, direction_id)


@lru_cache(maxsize=128)
def _compute_route_stops(
    token: tuple[str, float], route_id: str, direction_id: int
) -> RouteStops | None:
    static = load_all()
    canonical_trip_id = _pick_canonical_trip(static, route_id, direction_id)
    if canonical_trip_id is None:
        return None

    trip_row = static.trips.loc[static.trips["trip_id"] == canonical_trip_id].iloc[0]
    shape_id = str(trip_row["shape_id"])

    line = load_shape_linestrings().get(shape_id)
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
    visit: list[tuple[str, str, float, float, int, float, float]] = []
    for row in ordered_st.itertuples(index=False):
        sid = str(row.stop_id)
        if sid in seen or sid not in stops.index:
            continue
        seen.add(sid)
        s = stops.loc[sid]
        lon, lat = float(s["stop_lon"]), float(s["stop_lat"])
        x, y = transform_lonlat_to_meters(lon, lat)
        visit.append((sid, str(s["stop_name"]), lat, lon, int(row.stop_sequence), x, y))

    distances = _monotone_stop_distances(
        line, [(x, y) for *_head, x, y in visit], shape_id=shape_id
    )
    stops_out = [
        StopOnRoute(
            stop_id=sid,
            stop_name=name,
            stop_lat=lat,
            stop_lon=lon,
            stop_sequence=seq,
            distance_m=dist_m,
        )
        for (sid, name, lat, lon, seq, _x, _y), dist_m in zip(visit, distances, strict=False)
    ]

    # The monotone projection guarantees non-decreasing distances, so this sort
    # is a no-op safety net keeping the binary search well-defined regardless.
    stops_out.sort(key=lambda s: s.distance_m)

    return RouteStops(
        route_id=route_id,
        direction_id=direction_id,
        shape_id=shape_id,
        shape_length_m=float(line.length),
        stops=tuple(stops_out),
    )


def _monotone_stop_distances(
    line: LineString,
    stop_xy: list[tuple[float, float]],
    *,
    shape_id: str | None = None,
) -> list[float]:
    """Project stops onto ``line`` in visit order, distances non-decreasing.

    A canonical trip visits its stops in shape order, so distance-along-shape
    must be non-decreasing by definition. Unconstrained nearest-point
    projection breaks that on self-overlapping (out-and-back) shapes: a stop
    on the return leg can snap to the outbound leg, and the old sort-by-
    distance then silently reordered stops. Here a stop whose free projection
    lands more than ``_MONOTONE_TOL_M`` behind its predecessor is re-projected
    onto the *remainder* of the line; sub-tolerance backward wobble (almost-
    straight segments) is clamped forward.

    Greedy by construction: a wrong-leg projection that happens to land
    *ahead* of its predecessor is not detectable from ordering alone. Those
    cases are logged when a later stop exposes them via a re-projection.
    """
    out: list[float] = []
    prev = 0.0
    length = float(line.length)
    n_reprojected = 0
    for x, y in stop_xy:
        pt = Point(x, y)
        d_free = float(line.project(pt))
        if d_free >= prev:
            d = d_free
        elif d_free >= prev - _MONOTONE_TOL_M:
            d = prev
        elif prev >= length:
            d = length
        else:
            rest = substring(line, prev, length)
            d = prev + float(rest.project(pt))
            n_reprojected += 1
        out.append(d)
        prev = d
    if n_reprojected:
        _logger.warning(
            "stop_projection_nonmonotone_fixed",
            extra={
                "shape_id": shape_id,
                "stops_reprojected": n_reprojected,
                "detail": (
                    "free nearest-point projection violated stop order — the "
                    "shape likely self-overlaps; affected stops were re-projected "
                    "onto the remainder of the shape"
                ),
            },
        )
    return out


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
