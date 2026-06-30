"""Build ``dict[shape_id, shapely.LineString]`` from ``shapes.txt``.

Geometry is projected to EPSG:32617 (UTM zone 17N — Toronto's zone) so that
``LineString.project`` / ``.length`` return *true ground meters*. The system
previously used EPSG:3857 (Web Mercator), whose "meters" are inflated by
1/cos(latitude) ≈ 1.38× at Toronto — every stored ``travel_distance_m`` and
derived speed carried that bias. Within the TTC service area UTM 17N is
accurate to well under 0.1%.

NOTE for model bundles: bunching predictors trained on data produced under
EPSG:3857 expect that inflated unit. The serving shim in
``apps.api.services.forecast`` rescales inputs for such bundles — see
``distance_units`` handling there before retraining or changing this CRS.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString

# UTM zone 17N: true-meter planar CRS valid for Toronto (84°W–78°W).
METRIC_CRS = "EPSG:32617"

_TO_METRIC = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
_FROM_METRIC = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)


def build_linestrings(shapes_df: pd.DataFrame) -> dict[str, LineString]:
    """One ``LineString`` per ``shape_id`` in true-meter units.

    Vertices are ordered by ``shape_pt_sequence``.
    """
    if shapes_df.empty:
        return {}
    df = shapes_df.sort_values(["shape_id", "shape_pt_sequence"])
    result: dict[str, LineString] = {}
    for shape_id, group in df.groupby("shape_id", sort=False):
        lons = group["shape_pt_lon"].to_numpy()
        lats = group["shape_pt_lat"].to_numpy()
        xs, ys = _TO_METRIC.transform(lons, lats)
        if len(xs) >= 2:
            result[str(shape_id)] = LineString(zip(xs, ys, strict=False))
    return result


def transform_lonlat_to_meters(lon, lat):
    """Transform (lon, lat) to the metric CRS.

    Accepts scalars (returns a float pair) or numpy arrays (returns an array
    pair) — pyproj broadcasts either way.
    """
    x, y = _TO_METRIC.transform(lon, lat)
    if np.ndim(x) == 0:
        return float(x), float(y)
    return x, y


def transform_meters_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Inverse of ``transform_lonlat_to_meters``."""
    lon, lat = _FROM_METRIC.transform(x, y)
    return float(lon), float(lat)
