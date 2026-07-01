"""Upsample the trip trajectory to a fixed temporal resolution.

``upsample_df`` is carried over verbatim from the legacy
``data_process/clean_and_combine.py`` with one addition — each emitted row is
tagged ``observed=False`` (it is a synthetic boundary point), while source
rows that happen to be carried through the nearer-midpoint logic inherit
``observed=True`` from the input. The algorithm: for each consecutive pair of
real rows, insert one synthetic row at every ``resolution_seconds`` boundary
between them; distance is interpolated via the next row's speed; other columns
are copied from whichever of (current, next) is closer in distance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_FINAL_COLUMN_ORDER = [
    "trip_id", "start_date", "service_date", "route_id", "direction_id", "shape_id",
    "vehicle_id",
    "datetime", "time_offset_seconds",
    "travel_distance_m", "moving_speed_m_s",
    "observed",
    "occupancy_status", "source_vehicle_position_id",
]


def compute_moving_speed(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``moving_speed_m_s`` from distance/time diffs.

    The leading NaN is filled with 0 so upsample can use it on the first gap.
    """
    if df.empty:
        return df.assign(moving_speed_m_s=pd.Series(dtype=float))
    out = df.copy().sort_values("datetime").reset_index(drop=True)
    dist_diff = out["travel_distance_m"].diff()
    time_diff = out["datetime"].diff().dt.total_seconds()
    speed = dist_diff / time_diff
    speed = speed.replace([float("inf"), float("-inf")], pd.NA)
    # Align: speed[i] describes movement from row i-1 -> row i. upsample_df
    # wants the "next row's speed" when bridging row i -> row i+1, which is
    # exactly speed[i+1]. So no shift here — the old pipeline used the same
    # convention (speed stored on the arriving row).
    out["moving_speed_m_s"] = speed.fillna(0.0)
    return out


def upsample_df(df: pd.DataFrame, resolution_seconds: int, max_gap_seconds: float | None = None) -> pd.DataFrame:
    """Insert rows at fixed time boundaries between every consecutive pair.

    Each output row has ``observed`` set to False (it is synthesized). Source
    rows are NOT appended here — the boundary-point logic preserves one row's
    identity at each midpoint, matching the legacy pipeline's behavior.

    Vectorized but semantics-identical to the legacy per-pair loop (guarded by
    a randomized parity test): per consecutive pair (i, i+1) with positive
    time delta, boundaries are the multiples of ``resolution_seconds`` in
    ``[ceil_to_res(floor(t_i)), floor(t_{i+1}))``; each boundary row copies
    the identity columns of whichever endpoint is nearer in *distance*, with
    distance extrapolated from row i at row i+1's speed.

    Requires columns: ``datetime``, ``travel_distance_m``, ``moving_speed_m_s``.
    """
    if len(df) < 2:
        return pd.DataFrame(columns=df.columns)
    
    res = int(resolution_seconds)
    ts_ns = df["datetime"].astype("int64").to_numpy()  # epoch nanoseconds
    travel = df["travel_distance_m"].to_numpy(dtype=float)
    speed = df["moving_speed_m_s"].to_numpy(dtype=float)

    ns_cur, ns_next = ts_ns[:-1], ts_ns[1:]
    # int(Timestamp.timestamp()) truncates toward zero; epochs are positive.
    epoch_cur = ns_cur // 1_000_000_000
    epoch_next = ns_next // 1_000_000_000

    first_boundary = (epoch_cur // res) * res
    first_boundary = np.where(first_boundary < epoch_cur, first_boundary + res, first_boundary)

    # Number of candidates with first_boundary + k*res < epoch_next, k >= 0.
    count = np.ceil((epoch_next - first_boundary) / res).astype(np.int64)

    # If the gap between two rows is larger than max_gap_seconds, do not upsample that pair.
    if max_gap_seconds is not None:
        gap_seconds = (ns_next - ns_cur) / 1e9
        large_gap_mask = gap_seconds > max_gap_seconds
        count = np.where(large_gap_mask, 0, count)

    count = np.where(ns_next > ns_cur, np.maximum(count, 0), 0)
    
    total = int(count.sum())
    if total == 0:
        return pd.DataFrame(columns=df.columns)

    pair_idx = np.repeat(np.arange(len(ns_cur)), count)
    # k = 0..count-1 within each pair: global arange minus each pair's offset.
    starts = np.concatenate(([0], np.cumsum(count)[:-1]))
    k = np.arange(total) - np.repeat(starts, count)

    cand_epoch = first_boundary[pair_idx] + k * res
    # Subtract in the ns domain, divide once — matches Timedelta.total_seconds()
    # bit-for-bit (divide-then-subtract drifts at the 1e-6 level).
    partial = (cand_epoch * 1_000_000_000 - ns_cur[pair_idx]) / 1e9
    dist = travel[:-1][pair_idx] + partial * speed[1:][pair_idx]
    middle = (travel[:-1] + travel[1:]) / 2.0
    use_next = dist >= middle[pair_idx]

    src_iloc = pair_idx + use_next.astype(np.int64)
    out = df.iloc[src_iloc].copy()
    out["moving_speed_m_s"] = speed[1:][pair_idx]
    out["datetime"] = pd.to_datetime(cand_epoch, unit="s", utc=True)
    out["travel_distance_m"] = dist
    out["observed"] = False
    # Candidate epochs are strictly increasing across and within pairs, so this
    # sort is a stability no-op kept for parity with the legacy implementation.
    return out.sort_values("datetime").reset_index(drop=True)


def last_step_clean_up(df: pd.DataFrame) -> pd.DataFrame:
    """Round numeric fields and reorder columns to the canonical output schema."""
    if df.empty:
        return df
    out = df.copy()
    if "travel_distance_m" in out.columns:
        out["travel_distance_m"] = out["travel_distance_m"].round(2)
    if "moving_speed_m_s" in out.columns:
        out["moving_speed_m_s"] = out["moving_speed_m_s"].round(2)
    existing = [c for c in _FINAL_COLUMN_ORDER if c in out.columns]
    return out[existing]
