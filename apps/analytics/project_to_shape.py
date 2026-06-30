"""Project GPS points onto a route shape to get ``travel_distance_m``.

Pure: input DataFrame + LineString -> DataFrame with two added columns
(``travel_distance_m``, ``orthogonal_distance_m``) and outliers dropped.

Two outlier filters run here, in order:

1. **Off-route** — points whose perpendicular distance to the shape exceeds
   ``max_orthogonal_distance_m`` (bad GPS, garage moves, wrong-shape matches).
2. **Along-route teleports** — points whose *implied along-shape speed* from
   the previous kept point exceeds ``max_implied_speed_m_s``. These come from
   GPS glitches and from nearest-point projection snapping to the wrong leg of
   a self-overlapping (out-and-back) shape; left in, they render as the
   long-diagonal artifacts the dashboard segmenter has to patch downstream.
   The filter is a greedy forward pass anchored at the last kept point, so a
   contiguous block of wrong-leg projections is dropped wholesale rather than
   re-anchoring on it.

Projection math is vectorized via shapely 2.x array ops — the per-point
Python loop this replaces was the analytics worker's dominant CPU cost
alongside upsampling.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString

from apps.analytics.shapes import transform_lonlat_to_meters

#: 126 km/h — comfortably above any real TTC bus movement, well below the
#: km-scale jumps produced by GPS glitches and wrong-leg projections.
DEFAULT_MAX_IMPLIED_SPEED_M_S = 35.0


def _implied_speed_keep_mask(
    travel_m: np.ndarray,
    epoch_s: np.ndarray,
    max_speed_m_s: float,
) -> np.ndarray:
    """Greedy forward pass: keep a point only if the along-route speed implied
    from the last *kept* point is plausible.

    Anchoring on the last kept point (not the previous raw point) means one
    teleported block doesn't poison the points after it returns to normal.
    """
    n = len(travel_m)
    keep = np.ones(n, dtype=bool)
    if n == 0 or not np.isfinite(max_speed_m_s):
        return keep
    anchor = 0
    for i in range(1, n):
        dt = epoch_s[i] - epoch_s[anchor]
        jump = abs(travel_m[i] - travel_m[anchor])
        # Identical timestamps can't happen post-dedup; guard anyway.
        if dt <= 0 or jump / dt > max_speed_m_s:
            keep[i] = False
        else:
            anchor = i
    return keep


def project_trajectory(
    df: pd.DataFrame,
    shape_line: LineString,
    *,
    max_orthogonal_distance_m: float = 200.0,
    max_implied_speed_m_s: float | None = DEFAULT_MAX_IMPLIED_SPEED_M_S,
) -> pd.DataFrame:
    """Add ``travel_distance_m`` + ``orthogonal_distance_m``; drop outliers.

    Expects columns ``latitude``/``longitude`` (WGS84) and — when the teleport
    filter is enabled — a ``datetime`` column sorted ascending. Pass
    ``max_implied_speed_m_s=None`` to disable the teleport filter.
    """
    if df.empty:
        return df.assign(travel_distance_m=pd.Series(dtype=float),
                         orthogonal_distance_m=pd.Series(dtype=float))

    xs, ys = transform_lonlat_to_meters(
        df["longitude"].to_numpy(dtype=float), df["latitude"].to_numpy(dtype=float)
    )
    pts = shapely.points(xs, ys)
    travel = shapely.line_locate_point(shape_line, pts)
    orth = shapely.distance(pts, shape_line)

    out = df.copy()
    out["travel_distance_m"] = travel.astype(float)
    out["orthogonal_distance_m"] = orth.astype(float)
    out = out[out["orthogonal_distance_m"] <= max_orthogonal_distance_m]

    if (
        max_implied_speed_m_s is not None
        and len(out) > 1
        and "datetime" in out.columns
    ):
        epoch = out["datetime"].astype("int64").to_numpy() / 1e9
        keep = _implied_speed_keep_mask(
            out["travel_distance_m"].to_numpy(), epoch, float(max_implied_speed_m_s)
        )
        out = out[keep]

    return out.reset_index(drop=True)
