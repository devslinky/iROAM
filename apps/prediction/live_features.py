"""Live feature builder driven by a loaded bundle's metadata.

The deployed predictor's geometry (``seq_len``, ``step_seconds``, ``feature_set``)
varies per bundle:

  * vendor bundle:        seq_len=60, step=10 s, vendor-only 9 channels
  * local v1:             seq_len=20, step=60 s, vendor-only 9 channels
  * local rich:           seq_len=20, step=60 s, vendor (9) + extras (7) = 16

Rather than maintain several forecast-feature builders, this module reads
``metadata.json`` from the bundle and produces a matching per-bus window at
serving time. The eligibility rules (freshness / edge-exclude / contiguous
history / at-least-one-neighbour-tick / finite values) are pinned by
``tests/test_forecast_features.py``.

The "extras" channels match the offline labelling pipeline exactly
(``data_process/bunching/labels.py:EXTRA_FEATURES``) — same order, same units.
That's load-bearing: the trainer and live builder must agree column-by-column.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint, to_minute_of_day as _to_minute_of_day
from data_process.bunching.labels import (
    BUNCHING_THRESHOLD_M,
    EXTRA_FEATURES,
    EXTRAS_SCHEMA_V1,
    EXTRAS_SCHEMA_V2,
    EXTRAS_SCHEMA_V3,
    EXTRAS_SCHEMA_V4,
    NO_LEADER_GAP_M,
    N_CHANNELS,
    N_CHANNELS_V1,
    N_CHANNELS_V2,
    N_EXTRA,
    N_EXTRA_V1_LEGACY,
    TIME_TO_TERMINUS_FLOOR_SPEED_M_S,
    VENDOR_SCHEMA_V1,
    VENDOR_SCHEMA_V2,
    n_channels_for,
    n_extra_for,
)


@dataclass(frozen=True)
class LiveWindow:
    bus_index: int
    window: np.ndarray | None      # (seq_len, n_channels) float32, raw units. None if ineligible.
    extras: np.ndarray | None      # (seq_len, n_extra)    float32, raw units. None if vendor-only.
    reason: str | None
    stop_idx_at_ref: float | None
    forward_gap_at_ref: float | None   # for UI rationale
    gap_closure_m_per_s_at_ref: float | None  # for UI rationale
    travel_distance_m_at_ref: float | None    # for remaining-trip-time estimation
    median_speed_m_s_recent: float | None     # for remaining-trip-time estimation


# ─────────────────────────── helpers ─────────────────────────────────────────


class _Track:
    """A bus's samples sorted by minute-of-day, as parallel arrays for bisect.

    ``_sample_at`` is called O(seq_len × peers) times per scored bus; a linear
    scan over full-day point lists made each /iroam/forecast request cost tens
    of millions of Python iterations. Bisect makes each lookup O(log n).
    """

    __slots__ = ("mods", "pts")

    def __init__(self, bus: BusTrajectory) -> None:
        pairs = sorted(
            ((_to_minute_of_day(p.datetime), p) for p in bus.points),
            key=lambda r: r[0],
        )
        self.mods = [m for m, _ in pairs]
        self.pts = [p for _, p in pairs]


def _points_sorted_by_mod(bus: BusTrajectory) -> _Track:
    return _Track(bus)


def _sample_at(
    track: _Track,
    target_mod: float,
    tol_s: float,
) -> TrajectoryPoint | None:
    """Nearest sample within ±tol of ``target_mod``; earlier sample wins ties."""
    mods = track.mods
    if not mods:
        return None
    tol_min = tol_s / 60.0
    i = bisect.bisect_left(mods, target_mod)
    best: tuple[float, TrajectoryPoint] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(mods):
            dt = abs(mods[j] - target_mod)
            if dt <= tol_min and (best is None or dt < best[0]):
                best = (dt, track.pts[j])
    return best[1] if best else None


def _freshness_min(bus: BusTrajectory, t_ref_min: float) -> float:
    most_recent: float | None = None
    for p in bus.points:
        mod = _to_minute_of_day(p.datetime)
        if mod <= t_ref_min and (most_recent is None or mod > most_recent):
            most_recent = mod
    if most_recent is None:
        return math.inf
    return t_ref_min - most_recent


def _forward_gap(target: TrajectoryPoint, peers: list[TrajectoryPoint]) -> tuple[float, TrajectoryPoint | None]:
    best = NO_LEADER_GAP_M
    leader: TrajectoryPoint | None = None
    td = target.travel_distance_m
    for o in peers:
        if o.travel_distance_m > td:
            gap = o.travel_distance_m - td
            if gap < best:
                best = gap
                leader = o
    return float(best), leader


def _upstream(target: TrajectoryPoint, peers: list[TrajectoryPoint]) -> list[TrajectoryPoint]:
    """Schema v1 helper — 2 nearest buses *behind* the target."""
    behind: list[tuple[float, TrajectoryPoint]] = []
    td = target.travel_distance_m
    for o in peers:
        if o.travel_distance_m < td:
            behind.append((td - o.travel_distance_m, o))
    behind.sort(key=lambda r: r[0])
    return [p for _, p in behind[:2]]


def _downstream_chain(
    target: TrajectoryPoint, peers: list[TrajectoryPoint],
) -> tuple[TrajectoryPoint | None, float, TrajectoryPoint | None, float]:
    """Schema v2 helper — walk the leader chain: target → d1 → d2.

    Returns ``(d1, d1_fwd_gap, d2, d2_fwd_gap)`` where:
      * ``d1`` is the nearest bus ahead of ``target`` (or None if target is the
        head of the route).
      * ``d1_fwd_gap`` is d1's own gap to ITS leader (NO_LEADER_GAP_M if none).
      * ``d2`` is d1's nearest leader (i.e., 2nd link of the chain).
      * ``d2_fwd_gap`` is d2's own gap to its leader.

    "Forward gap of bus X" = the same quantity the target_gap channel
    measures for the target. Surfacing it for d1 and d2 lets the model see
    cascading bunching: if d1 is already closing on d2, target's d1_gap will
    shrink even if target itself doesn't change speed.
    """
    # d1
    d1: TrajectoryPoint | None = None
    d1_gap = NO_LEADER_GAP_M
    td = target.travel_distance_m
    for o in peers:
        diff = o.travel_distance_m - td
        if diff > 0 and diff < d1_gap:
            d1_gap = diff
            d1 = o
    if d1 is None:
        return None, NO_LEADER_GAP_M, None, NO_LEADER_GAP_M

    # d2 = d1's nearest leader (excluding target — already excluded by
    # diff>0 check since target is BEHIND d1).
    d2: TrajectoryPoint | None = None
    d1_to_d2 = NO_LEADER_GAP_M
    for o in peers:
        if o is d1:
            continue
        diff = o.travel_distance_m - d1.travel_distance_m
        if diff > 0 and diff < d1_to_d2:
            d1_to_d2 = diff
            d2 = o
    if d2 is None:
        return d1, NO_LEADER_GAP_M, None, NO_LEADER_GAP_M

    # d2's own forward gap (to its own leader).
    d2_to_d3 = NO_LEADER_GAP_M
    for o in peers:
        if o is d1 or o is d2:
            continue
        diff = o.travel_distance_m - d2.travel_distance_m
        if diff > 0 and diff < d2_to_d3:
            d2_to_d3 = diff
    return d1, d1_to_d2, d2, d2_to_d3


# ─────────────────────────── main builder ────────────────────────────────────


def build_bus_window(
    target: BusTrajectory,
    peers: Sequence[BusTrajectory],
    *,
    t_ref_min: float,
    num_stops: int,
    seq_len: int,
    step_seconds: int,
    feature_set: str,
    n_extra: int = N_EXTRA,
    freshness_s: float = 90.0,
    edge_exclude: int = 2,
    route_shape_length_m: float | None = None,
    # vendor_schema_v selects which vendor-block layout to write:
    # 1 = legacy 9-channel (target/u1/u2 × speed/gap/aux)
    # 2 = current 6-channel (target/d1/d2 × speed/fwd_gap)
    vendor_schema_v: int = VENDOR_SCHEMA_V2,
    # extras_schema_v selects which extras layout to write:
    # 1 = legacy 7 (with leader_speed, no terminus)
    # 2 = 10 (v1 + 3 terminus channels)
    # 3 = trimmed 7 (no leader_speed, only dist_to_terminus_norm)
    # 4 = post-diag 6 (drops redundant location features + dwell;
    #     adds rel_speed_to_d1 and target_accel_3tick)
    extras_schema_v: int = EXTRAS_SCHEMA_V4,
) -> LiveWindow:
    """Build one bus's serving window. Geometry from the loaded bundle."""
    bus_index = target.bus_index

    fresh_min = _freshness_min(target, t_ref_min)
    if fresh_min > freshness_s / 60.0:
        return LiveWindow(
            bus_index=bus_index, window=None, extras=None,
            stop_idx_at_ref=None, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
            reason=(
                f"stale: last sample {fresh_min * 60:.0f}s before t_ref "
                f"(threshold {freshness_s:.0f}s)"
            ),
        )

    target_sorted = _points_sorted_by_mod(target)
    peers_sorted = {
        p.bus_index: _points_sorted_by_mod(p)
        for p in peers if p.bus_index != bus_index
    }

    match_tol_s = max(5.0, step_seconds / 2.0)
    max_gap_s = 2 * step_seconds

    # Identify the t_ref sample (for stop_idx_at_ref).
    ref_sample = _sample_at(target_sorted, t_ref_min, tol_s=match_tol_s)
    if ref_sample is None:
        return LiveWindow(
            bus_index=bus_index, window=None, extras=None,
            stop_idx_at_ref=None, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
            reason=f"no sample within ±{match_tol_s:.0f}s of t_ref",
        )
    stop_at_ref = float(ref_sample.stop_index)
    si = int(round(stop_at_ref))
    if si < edge_exclude or si >= num_stops - edge_exclude:
        return LiveWindow(
            bus_index=bus_index, window=None, extras=None,
            stop_idx_at_ref=stop_at_ref, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
            reason=(
                f"stop_idx={si} inside edge-exclude zone "
                f"[{edge_exclude}, {num_stops - edge_exclude})"
            ),
        )

    # Build the (seq_len, n_channels) window ending at t_ref.
    step_min = step_seconds / 60.0
    grid = [t_ref_min - (seq_len - 1 - k) * step_min for k in range(seq_len)]

    want_extras = feature_set == "rich"
    n_vendor = n_channels_for(vendor_schema_v)
    n_chans_total = n_vendor + (n_extra if want_extras else 0)
    window = np.zeros((seq_len, n_vendor), dtype=np.float32)
    extras = np.zeros((seq_len, n_extra), dtype=np.float32) if want_extras else None
    # Renamed since v2: we just need at least one tick with a usable neighbour
    # set. v1 wanted upstream; v2 wants downstream (a leader). Either way:
    # "some peer of the expected kind was observed at some tick."
    any_neighbour_tick = False
    last_gap_seen: float | None = None
    gap_history: list[float] = []  # for the at-ref gap-closure-rate (UI extra)

    tz = ZoneInfo("America/Toronto")
    # Dwell window length in ticks: ~5 min, same rule as labels.py
    dwell_ticks = max(1, int(round(300 / step_seconds)))
    gap_lookback = max(1, min(3, seq_len - 1))

    speed_hist: list[float] = []  # for dwell rollup

    for k, tick_mod in enumerate(grid):
        t_sample = _sample_at(target_sorted, tick_mod, tol_s=max_gap_s)
        if t_sample is None:
            return LiveWindow(
                bus_index=bus_index, window=None, extras=extras,
                stop_idx_at_ref=stop_at_ref, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
                reason=f"missing target sample at tick {k} (±{max_gap_s:.0f}s)",
            )

        peers_at_tick: list[TrajectoryPoint] = []
        for _, pts in peers_sorted.items():
            q = _sample_at(pts, tick_mod, tol_s=max_gap_s)
            if q is not None:
                peers_at_tick.append(q)

        # Vendor channels.
        t_speed = t_sample.moving_speed_m_s if t_sample.moving_speed_m_s is not None else 0.0
        t_gap, leader = _forward_gap(t_sample, peers_at_tick)
        gap_history.append(t_gap)
        speed_hist.append(t_speed)

        if vendor_schema_v == VENDOR_SCHEMA_V2:
            # Layout: (target, d1, d2) × (speed, fwd_gap) = 6 channels.
            d1, d1_fwd_gap, d2, d2_fwd_gap = _downstream_chain(t_sample, peers_at_tick)
            window[k, 0] = float(t_speed)
            window[k, 1] = float(t_gap)
            window[k, 2] = float(d1.moving_speed_m_s) if d1 is not None and d1.moving_speed_m_s is not None else 0.0
            window[k, 3] = float(d1_fwd_gap)
            window[k, 4] = float(d2.moving_speed_m_s) if d2 is not None and d2.moving_speed_m_s is not None else 0.0
            window[k, 5] = float(d2_fwd_gap)
            if t_gap < NO_LEADER_GAP_M:
                any_neighbour_tick = True
        else:
            # Legacy 9-channel (target, u1, u2) × (speed, gap, aux=0).
            window[k, 0] = float(t_speed)
            window[k, 1] = float(t_gap)
            window[k, 2] = 0.0
            ups = _upstream(t_sample, peers_at_tick)
            if ups:
                any_neighbour_tick = True
            for j in range(2):
                col = 3 + 3 * j
                if j < len(ups):
                    up = ups[j]
                    up_speed = up.moving_speed_m_s if up.moving_speed_m_s is not None else 0.0
                    window[k, col + 0] = float(up_speed)
                    window[k, col + 1] = float(t_sample.travel_distance_m - up.travel_distance_m)
                    window[k, col + 2] = 0.0
                else:
                    window[k, col + 0] = 0.0
                    window[k, col + 1] = NO_LEADER_GAP_M
                    window[k, col + 2] = 0.0

        if extras is not None:
            # ── compute every extras value, then write the right ones by schema ──
            si_k_val = float(t_sample.stop_index)
            stop_index_norm_val = (si_k_val / num_stops) if num_stops > 0 else 0.0

            gap_closure_val = 0.0
            lb = min(gap_lookback, k)
            if lb > 0 and gap_history[k - lb] < NO_LEADER_GAP_M and t_gap < NO_LEADER_GAP_M:
                gap_closure_val = float((gap_history[k - lb] - t_gap) / (lb * step_seconds))

            leader_speed_val = 0.0
            if leader is not None and leader.moving_speed_m_s is not None:
                leader_speed_val = float(leader.moving_speed_m_s)

            window_speeds = speed_hist[max(0, len(speed_hist) - dwell_ticks):]
            dwell_val = float(sum(1 for s in window_speeds if s < 0.5) * step_seconds)

            local = t_sample.datetime.astimezone(tz)
            mod_min = local.hour * 60 + local.minute + local.second / 60.0
            ang = 2 * math.pi * mod_min / 1440.0
            tod_sin_val = float(math.sin(ang))
            tod_cos_val = float(math.cos(ang))

            dist_to_terminus_m_val = 0.0
            dist_to_terminus_norm_val = 0.0
            time_to_terminus_min_val = 0.0
            if route_shape_length_m is not None:
                td_dist = float(t_sample.travel_distance_m)
                remaining_m = max(0.0, float(route_shape_length_m) - td_dist)
                dist_to_terminus_m_val = float(remaining_m)
                dist_to_terminus_norm_val = float(remaining_m / float(route_shape_length_m))
                ws = np.asarray(window_speeds, dtype=np.float64)
                moving = ws[ws >= 0.5]
                if moving.size > 0:
                    med_speed = float(np.median(moving))
                elif ws.size > 0:
                    med_speed = float(np.median(ws))
                else:
                    med_speed = 0.0
                eff_speed = max(med_speed, TIME_TO_TERMINUS_FLOOR_SPEED_M_S)
                time_to_terminus_min_val = float(remaining_m / eff_speed / 60.0)

            # ── v4-only derived values ─────────────────────────────────
            # rel_speed_to_d1 = target_speed − d1_speed (closing rate).
            # d1's speed is the same value we already use for ch2 of the
            # downstream vendor block (leader.moving_speed_m_s).
            d1_speed_now = (
                float(leader.moving_speed_m_s)
                if leader is not None and leader.moving_speed_m_s is not None else 0.0
            )
            rel_speed_to_d1_val = float(t_speed) - d1_speed_now if d1_speed_now != 0.0 else 0.0
            # target_accel_3tick: Δ target_speed over the last 3 ticks.
            # speed_hist[-1] is the current tick (just appended above).
            target_accel_val = 0.0
            if len(speed_hist) >= 4:
                target_accel_val = float(
                    (speed_hist[-1] - speed_hist[-4]) / (3.0 * step_seconds)
                )

            # ── write into the per-tick extras slot by schema version ──
            if extras_schema_v == EXTRAS_SCHEMA_V4:
                # 6 channels: stop_index_norm, gap_closure, tod_sin,
                # tod_cos, rel_speed_to_d1, target_accel_3tick.
                extras[k, 0] = stop_index_norm_val
                extras[k, 1] = gap_closure_val
                extras[k, 2] = tod_sin_val
                extras[k, 3] = tod_cos_val
                extras[k, 4] = rel_speed_to_d1_val
                extras[k, 5] = target_accel_val
            elif extras_schema_v == EXTRAS_SCHEMA_V3:
                extras[k, 0] = si_k_val
                extras[k, 1] = stop_index_norm_val
                extras[k, 2] = gap_closure_val
                extras[k, 3] = dwell_val
                extras[k, 4] = tod_sin_val
                extras[k, 5] = tod_cos_val
                extras[k, 6] = dist_to_terminus_norm_val
            elif extras_schema_v == EXTRAS_SCHEMA_V2:
                extras[k, 0] = si_k_val
                extras[k, 1] = stop_index_norm_val
                extras[k, 2] = gap_closure_val
                extras[k, 3] = leader_speed_val
                extras[k, 4] = dwell_val
                extras[k, 5] = tod_sin_val
                extras[k, 6] = tod_cos_val
                extras[k, 7] = dist_to_terminus_m_val
                extras[k, 8] = dist_to_terminus_norm_val
                extras[k, 9] = time_to_terminus_min_val
            else:
                # EXTRAS_SCHEMA_V1: legacy 7 (with leader_speed, no terminus).
                extras[k, 0] = si_k_val
                extras[k, 1] = stop_index_norm_val
                extras[k, 2] = gap_closure_val
                extras[k, 3] = leader_speed_val
                extras[k, 4] = dwell_val
                extras[k, 5] = tod_sin_val
                extras[k, 6] = tod_cos_val

    if not any_neighbour_tick:
        # Schema-accurate wording: v1 needs a bus *behind* (upstream), v2 a
        # bus *ahead* (leader). forecast._is_running matches both prefixes.
        no_neighbour_reason = (
            "no leader on any tick of the history window"
            if vendor_schema_v == VENDOR_SCHEMA_V2
            else "no upstream bus on any tick of the history window"
        )
        return LiveWindow(
            bus_index=bus_index, window=None, extras=extras,
            stop_idx_at_ref=stop_at_ref, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
            reason=no_neighbour_reason,
        )
    if not np.all(np.isfinite(window)) or (extras is not None and not np.all(np.isfinite(extras))):
        return LiveWindow(
            bus_index=bus_index, window=None, extras=extras,
            stop_idx_at_ref=stop_at_ref, forward_gap_at_ref=None, gap_closure_m_per_s_at_ref=None,
            travel_distance_m_at_ref=None, median_speed_m_s_recent=None,
            reason="non-finite value in feature window",
        )

    # At-ref gap-closure rate (for UI rationale, regardless of feature_set).
    gap_at_ref = float(gap_history[-1])
    lb = min(gap_lookback, seq_len - 1)
    if lb > 0 and gap_history[-1 - lb] < NO_LEADER_GAP_M and gap_at_ref < NO_LEADER_GAP_M:
        closure = float((gap_history[-1 - lb] - gap_at_ref) / (lb * step_seconds))
    else:
        closure = 0.0

    # For the serving-time horizon truncation: we need the bus's current along-
    # route position and a "what speed is it actually moving at" estimate. The
    # ref_sample is the closest observed sample to t_ref; speed_hist holds the
    # last seq_len ticks' speeds. We use the median of the recent NON-IDLE
    # speeds so a brief stop doesn't fool us into thinking the bus is stopped
    # for the rest of its trip.
    travel_dist_at_ref = float(ref_sample.travel_distance_m)
    moving_speeds = [s for s in speed_hist if s >= 0.5]
    if moving_speeds:
        median_speed = float(np.median(moving_speeds))
    elif speed_hist:
        median_speed = float(np.median(speed_hist))
    else:
        median_speed = None

    return LiveWindow(
        bus_index=bus_index,
        window=window,
        extras=extras,
        stop_idx_at_ref=stop_at_ref,
        forward_gap_at_ref=gap_at_ref,
        gap_closure_m_per_s_at_ref=closure,
        travel_distance_m_at_ref=travel_dist_at_ref,
        median_speed_m_s_recent=median_speed,
        reason=None,
    )


def merge_for_predictor(
    window: np.ndarray, extras: np.ndarray | None, scaler: Mapping,
) -> tuple[np.ndarray, bool]:
    """Pre-scale the vendor block when extras are present.

    The shipped ``BunchingPredictor.scale_window`` only handles (speed, gap,
    aux) triples — it can't scale a 16-channel rich window directly. For rich
    bundles we z-score the first 9 channels here and pass ``is_scaled=True``
    so the predictor leaves the merged tensor alone.

    Returns ``(merged_window, is_scaled)``.
    """
    if extras is None:
        return window.astype(np.float32), False
    # Per-channel scaling. Dispatch on n_ch so this works for v1 (9-channel
    # vendor block, 3-channel triples with aux) AND v2 (6-channel, 2-channel
    # pairs of speed/fwd_gap). Single source of truth lives in
    # apps.prediction.data._speed_gap_offsets so train+serve stay aligned.
    from apps.prediction.data import _speed_gap_offsets

    s_mean = float(scaler["speed_mean"]); s_std = float(scaler["speed_std"]) or 1.0
    g_mean = float(scaler["gap_mean"]); g_std = float(scaler["gap_std"]) or 1.0
    scaled = window.astype(np.float32).copy()
    seq_len, n_ch = scaled.shape
    speed_cols, gap_cols = _speed_gap_offsets(n_ch)
    speed_set = set(speed_cols); gap_set = set(gap_cols)
    for i in range(n_ch):
        if i in speed_set:
            scaled[:, i] = (scaled[:, i] - s_mean) / s_std
        elif i in gap_set:
            scaled[:, i] = (scaled[:, i] - g_mean) / g_std
        # else: passthrough (covers v1 aux columns)
    merged = np.concatenate([scaled, extras.astype(np.float32)], axis=1)
    return merged.astype(np.float32), True


__all__ = ["LiveWindow", "build_bus_window", "merge_for_predictor"]
