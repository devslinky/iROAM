"""Build labelled bunching-prediction examples from local trip_trajectories.

Given a (route, direction, service_date), reads the upsampled trajectories from
Postgres, groups into buses (same logic the API uses), then for every "valid"
reference time t_ref builds:

    * a (seq_len, N_CHANNELS) raw-unit history window — same channel layout as
      the deployed vendor bundle so we can drop new boosters into the existing
      ``BunchingPredictor`` while still re-using the existing live feature
      builder when convenient;
    * a (seq_len, N_EXTRA) richer-feature window;
    * a ``pred_len``-element label vector
      ``y[h] = 1 if forward_gap_at(t_ref + (h+1) * step_seconds) < 100 m``;
    * a small ``meta`` row.

Geometry is fully parameterised (``step_seconds``, ``seq_len``, ``pred_len``) so
the same code drives both a short-horizon (vendor-compatible, 10 s / 5 min) and
a long-horizon (60 s / 30 min) dataset. The "richer feature" channel set is
computed too and stored alongside as a parallel array — when we train the
richer model variant we feed those instead of the 9-channel raw window. Labels
are identical either way.

Pure-Python; pulls everything from the local DB. Roughly 1-3 minutes per
service date for route 29 on a laptop, dominated by per-tick Python feature
math.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from apps.analytics.anomalies import BusTrajectory
from apps.analytics.stop_projection import compute_route_stops
from db.queries.iroam import fetch_trajectories_for_slice

# ── default geometry ─────────────────────────────────────────────────────────
# 30-min horizon at 1-minute granularity, with 20 minutes of input history.
# Matches the user goal "predict bunching in the next 5-30 min". The vendor
# bundle (10 s / 5 min) is still supported by passing different kwargs.
DEFAULT_STEP_SECONDS = 60
DEFAULT_SEQ_LEN = 20
DEFAULT_PRED_LEN = 30

# Vendor-block schema versioning. v1 = legacy upstream layout that ships with
# the bundled 2024 vendor model: 3 buses × (speed, gap, aux=0) = 9 channels.
# v2 = aux removed, upstream replaced by the downstream leader chain
# (target → d1 → d2), each contributing (speed, that-bus's-forward-gap) =
# 6 channels. Pick a value with ``VENDOR_SCHEMA_V`` below; bundle metadata
# carries the version forward so the live builder can serve either schema.
VENDOR_SCHEMA_V1 = 1
VENDOR_SCHEMA_V2 = 2
VENDOR_SCHEMA_V = VENDOR_SCHEMA_V2   # new datasets default to v2

VENDOR_CHANNELS_V1 = (
    "target_speed", "target_gap", "target_aux",
    "u1_speed",     "u1_gap",     "u1_aux",
    "u2_speed",     "u2_gap",     "u2_aux",
)
VENDOR_CHANNELS_V2 = (
    "target_speed", "target_fwd_gap",
    "d1_speed",     "d1_fwd_gap",
    "d2_speed",     "d2_fwd_gap",
)

N_CHANNELS_V1 = len(VENDOR_CHANNELS_V1)   # 9
N_CHANNELS_V2 = len(VENDOR_CHANNELS_V2)   # 6
N_CHANNELS = N_CHANNELS_V2 if VENDOR_SCHEMA_V == VENDOR_SCHEMA_V2 else N_CHANNELS_V1

BUNCHING_THRESHOLD_M = 100.0
NO_LEADER_GAP_M = 20_000.0
EDGE_EXCLUDE_DEFAULT = 2

# Label-quality extensions (labels schema v2). The instantaneous spatial-gap
# label (gap < BUNCHING_THRESHOLD_M) is kept bit-identical for compatibility;
# v2 adds, per example:
#   * labels_persist — debounced 0/1: requires the gap to stay under the
#     threshold for PERSIST_TICKS_DEFAULT consecutive ticks, suppressing
#     single-tick flicker around the threshold (label noise).
#   * labels_headway_s — realised *time* headway at each future tick: how long
#     ago the leader passed the target's current along-route position. This is
#     the AVL-standard quantity bunching is defined on in the literature
#     (headway ≤ 0.25 × scheduled headway, Moreira-Matias et al. 2012; TCQSM
#     headway adherence), generalized off-stop. Stored continuous so trainers
#     can threshold freely against sched_headway_s.
#   * terminal masking — future ticks where the target sits inside the
#     edge-exclusion band (layover zones) get NaN labels: gaps there reflect
#     terminal queueing, not service bunching.
PERSIST_TICKS_DEFAULT = 2
HEADWAY_RATIO_BUNCHED = 0.25  # h ≤ 0.25 × scheduled ⇒ bunched (literature rule)


def n_channels_for(schema_v: int) -> int:
    """Channel count for the given schema. Use this everywhere instead of the
    module-level ``N_CHANNELS`` when serving multiple schemas in one process."""
    return N_CHANNELS_V2 if int(schema_v) == VENDOR_SCHEMA_V2 else N_CHANNELS_V1

# Extra context features the richer-model variant uses, stacked into a
# (seq_len, N_EXTRA) array per example. Kept narrow on purpose — every new
# channel is one more thing to maintain at serving time.
#
# The last three were added in v2 to fix a selection-bias artifact at long
# horizons: the original schema let the model condition on stop_index, but the
# label population at high stop_index + long horizon is dominated by bunched
# survivors (buses that DIDN'T finish their trip in time). Adding explicit
# "how far to terminus" features lets the model condition on the actual
# remaining route geometry instead of the misleading absolute stop position.
# Extras schema versioning. v1 = legacy 7 (with leader_speed, no terminus).
# v2 = 10 (v1 + 3 terminus channels). v3 = 7 (drops the redundant
# leader_speed, drops the raw and the time-scaled terminus channels —
# keeps only the normalized distance-to-terminus). Trees are scale-invariant
# so the normalized form alone is sufficient.
EXTRAS_SCHEMA_V1 = 1
EXTRAS_SCHEMA_V2 = 2
EXTRAS_SCHEMA_V3 = 3
# v4 is the current default. Empirical diagnostics on v5 (see
# out/diag/v5_features.md) showed three drops were safe (redundant or
# unused) and two physically-motivated derived features were worth
# adding. See ``EXTRA_FEATURES_V4`` for the resulting layout.
EXTRAS_SCHEMA_V4 = 4
EXTRAS_SCHEMA_V = EXTRAS_SCHEMA_V4

EXTRA_FEATURES_V1 = (
    "stop_index",
    "stop_index_norm",
    "gap_closure_m_per_s",
    "leader_speed",
    "dwell_recent_s",
    "tod_sin",
    "tod_cos",
)
EXTRA_FEATURES_V2 = EXTRA_FEATURES_V1 + (
    "dist_to_terminus_m",
    "dist_to_terminus_norm",
    "time_to_terminus_min",
)
EXTRA_FEATURES_V3 = (
    "stop_index",
    "stop_index_norm",
    "gap_closure_m_per_s",
    "dwell_recent_s",
    "tod_sin",
    "tod_cos",
    "dist_to_terminus_norm",
)
EXTRA_FEATURES_V4 = (
    # Kept from v3 — these all carry independent signal:
    "stop_index_norm",        # route position (single representative; drops
                              # stop_index + dist_to_terminus_norm which are
                              # all ρ ≈ ±0.998 with this one).
    "gap_closure_m_per_s",    # ⏶ of target's forward gap (sample-efficient
                              # finite-difference feature).
    "tod_sin",                # cyclical time-of-day.
    "tod_cos",
    # New v4 — derived physical features the trees can't easily compose:
    "rel_speed_to_d1",        # target_speed - d1_speed (closing rate vs the
                              # leader; trees need a 2-level split to encode
                              # this without it as an explicit channel).
    "target_accel_3tick",     # (target_speed[t] - target_speed[t-3]) /
                              # (3 * step_seconds), m/s² (recent speed trend).
)


def extra_features_for(schema_v: int) -> tuple[str, ...]:
    """List of extras feature names for the given schema version."""
    if int(schema_v) == EXTRAS_SCHEMA_V1: return EXTRA_FEATURES_V1
    if int(schema_v) == EXTRAS_SCHEMA_V2: return EXTRA_FEATURES_V2
    if int(schema_v) == EXTRAS_SCHEMA_V3: return EXTRA_FEATURES_V3
    if int(schema_v) == EXTRAS_SCHEMA_V4: return EXTRA_FEATURES_V4
    raise ValueError(f"unknown extras_schema_v {schema_v!r}")


def n_extra_for(schema_v: int) -> int:
    return len(extra_features_for(schema_v))


# Module-level defaults track the current schema version.
EXTRA_FEATURES = extra_features_for(EXTRAS_SCHEMA_V)
N_EXTRA = len(EXTRA_FEATURES)
N_EXTRA_V1_LEGACY = 7         # kept so older bundles can still be loaded


# Floor for the speed used in time_to_terminus estimation. Below ~1 m/s the
# estimate explodes; we cap at 1 m/s as a "the bus is essentially stopped"
# fallback, matching what the serving truncator does.
TIME_TO_TERMINUS_FLOOR_SPEED_M_S = 1.0


@dataclass(frozen=True)
class LabelledExample:
    # identity / debug
    service_date: str
    route_id: str
    direction_id: int
    trip_id: str
    start_date: str
    vehicle_id: str | None
    bus_index: int
    t_ref_min: float           # minute-of-day, local TZ
    stop_idx_at_ref: float

    # features
    window: np.ndarray         # (seq_len, N_CHANNELS) float32, vendor schema
    extras: np.ndarray         # (seq_len, N_EXTRA)    float32, richer features
    forward_gap_at_ref: float  # m

    # labels: 0/1 at +1..+pred_len ticks (NaN if outside data window)
    labels: np.ndarray         # (pred_len,) float32 (0/1 or NaN)
    label_gaps: np.ndarray     # (pred_len,) float32 — realised gap; NaN if outside

    # ── labels schema v2 (None on legacy paths) ────────────────────────────
    labels_persist: np.ndarray | None = None     # (pred_len,) debounced 0/1/NaN
    labels_headway_s: np.ndarray | None = None   # (pred_len,) realised time headway
    sched_headway_s: float | None = None         # trip's scheduled headway
    headway_at_ref_s: float | None = None        # realised time headway at t_ref


# ───────────────────────── geometry helpers ──────────────────────────────────


def _bus_points_on_grid(
    bus: BusTrajectory, grid_utc: np.ndarray, *, max_gap_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Linear-interpolate ``bus`` onto a sorted UTC-second grid.

    Returns four arrays of length ``len(grid_utc)``:
        valid (bool)  — point was within ``max_gap_s`` of an observed sample
        dist_m, speed_m_s, stop_idx — interpolated values (NaN where !valid)
    """
    n = len(grid_utc)
    valid = np.zeros(n, dtype=bool)
    dist = np.full(n, np.nan, dtype=np.float64)
    speed = np.full(n, np.nan, dtype=np.float64)
    stop = np.full(n, np.nan, dtype=np.float64)

    ts = np.array([p.datetime.timestamp() for p in bus.points], dtype=np.float64)
    ds = np.array([p.travel_distance_m for p in bus.points], dtype=np.float64)
    ss = np.array(
        [p.moving_speed_m_s if p.moving_speed_m_s is not None else np.nan for p in bus.points],
        dtype=np.float64,
    )
    si = np.array([p.stop_index for p in bus.points], dtype=np.float64)
    if ts.size == 0:
        return valid, dist, speed, stop

    idx = np.searchsorted(ts, grid_utc)
    for k in range(n):
        t = grid_utc[k]
        if t < ts[0] or t > ts[-1]:
            continue
        i = idx[k]
        left = i - 1 if i > 0 else 0
        right = i if i < len(ts) else len(ts) - 1
        dl = abs(t - ts[left])
        dr = abs(t - ts[right])
        j = left if dl <= dr else right
        if abs(t - ts[j]) > max_gap_s:
            continue
        if right > left and ts[right] > ts[left]:
            frac = (t - ts[left]) / (ts[right] - ts[left])
            dist[k] = ds[left] + frac * (ds[right] - ds[left])
            stop[k] = si[left] + frac * (si[right] - si[left])
            if np.isfinite(ss[left]) and np.isfinite(ss[right]):
                speed[k] = ss[left] + frac * (ss[right] - ss[left])
            elif np.isfinite(ss[j]):
                speed[k] = ss[j]
        else:
            dist[k] = ds[j]
            stop[k] = si[j]
            if np.isfinite(ss[j]):
                speed[k] = ss[j]
        valid[k] = True
    return valid, dist, speed, stop


def _passage_track(
    grid_utc: np.ndarray, valid: np.ndarray, dist: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """(times, cummax distances) over a bus's valid ticks, for inverting
    "when was this bus at distance d?". The cummax flattens projection wobble
    so the search array is non-decreasing."""
    ts = grid_utc[valid]
    ds = dist[valid]
    if ds.size:
        ds = np.maximum.accumulate(ds)
    return ts, ds


def _time_at_distance(track_t: np.ndarray, track_d: np.ndarray, d: float) -> float | None:
    """Time the tracked bus first reached along-route distance ``d``.

    None when ``d`` is outside the recorded span (the bus was already past it
    before its window, or never got there)."""
    if track_t.size < 2 or not np.isfinite(d):
        return None
    if d < track_d[0] or d > track_d[-1]:
        return None
    i = int(np.searchsorted(track_d, d))
    if i == 0:
        return float(track_t[0])
    d0, d1 = track_d[i - 1], track_d[i]
    t0, t1 = track_t[i - 1], track_t[i]
    if d1 <= d0:
        return float(t0)
    frac = (d - d0) / (d1 - d0)
    return float(t0 + frac * (t1 - t0))


def _forward_gap_row(target_dist: float, others_dist: np.ndarray) -> tuple[float, int | None]:
    """Smallest positive (other - target). Returns (gap_m, leader_local_idx)."""
    if not np.isfinite(target_dist):
        return NO_LEADER_GAP_M, None
    diffs = others_dist - target_dist
    diffs[~np.isfinite(diffs)] = -1.0
    mask = diffs > 0
    if not mask.any():
        return NO_LEADER_GAP_M, None
    j = int(np.argmin(np.where(mask, diffs, np.inf)))
    return float(diffs[j]), j


def _upstream_indices(target_dist: float, others_dist: np.ndarray) -> list[int]:
    """Up to 2 buses just behind ``target`` (closest first)."""
    if not np.isfinite(target_dist):
        return []
    diffs = target_dist - others_dist
    diffs[~np.isfinite(diffs)] = -1.0
    mask = diffs > 0
    if not mask.any():
        return []
    order = np.argsort(np.where(mask, diffs, np.inf))
    out: list[int] = []
    for k in order[:2]:
        if not mask[k]:
            break
        out.append(int(k))
    return out


# ───────────────────────── main extractor ────────────────────────────────────


def extract_labelled_examples(
    buses: Sequence[BusTrajectory],
    *,
    route_id: str,
    direction_id: int,
    service_date: date,
    num_stops: int,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    seq_len: int = DEFAULT_SEQ_LEN,
    pred_len: int = DEFAULT_PRED_LEN,
    edge_exclude: int = EDGE_EXCLUDE_DEFAULT,
    max_gap_factor: float = 2.0,
    # v2+: pass the route's total along-shape length so we can emit the
    # terminus-aware extras. When ``None`` (legacy callers), terminus
    # channels are written as zeros so the array shape stays stable.
    route_shape_length_m: float | None = None,
    # extras_schema_v: 1 = legacy 7, 2 = 10 (with terminus), 3 = trimmed
    # 7 (current default, drops redundant leader_speed + scale-redundant
    # terminus columns). Vendor block schema is selected separately via
    # the module-level ``VENDOR_SCHEMA_V``.
    extras_schema_v: int = EXTRAS_SCHEMA_V,
    # ── labels schema v2 ─────────────────────────────────────────────────
    # Mask future-tick labels while the target sits inside the edge-exclusion
    # band: terminal-queueing gaps are not service bunching. Disable to
    # reproduce legacy label populations exactly.
    terminal_mask: bool = True,
    # Consecutive sub-threshold ticks required for labels_persist.
    persist_ticks: int = PERSIST_TICKS_DEFAULT,
    # trip_id → scheduled headway seconds (see apps.analytics.schedule_headways);
    # None disables the per-example sched_headway_s metadata.
    sched_headway_by_trip: dict[str, float] | None = None,
) -> list[LabelledExample]:
    """Build labelled examples for every (bus, valid t_ref) pair on this slice.

    Geometry kwargs are configurable so the same code produces both the
    short-horizon vendor-compatible dataset and a 30-min-out dataset.
    """
    if not buses:
        return []
    if seq_len <= 0 or pred_len <= 0 or step_seconds <= 0:
        raise ValueError("seq_len, pred_len, step_seconds must all be positive")

    max_gap_s = max_gap_factor * step_seconds

    all_ts = [p.datetime.timestamp() for b in buses for p in b.points]
    if not all_ts:
        return []
    t_lo = min(all_ts)
    t_hi = max(all_ts)
    t_lo_g = math.floor(t_lo / step_seconds) * step_seconds
    t_hi_g = math.ceil(t_hi / step_seconds) * step_seconds
    grid_utc = np.arange(t_lo_g, t_hi_g + step_seconds, step_seconds, dtype=np.float64)
    n_ticks = len(grid_utc)

    # Per-bus interpolated arrays on the grid.
    interp: list[dict] = []
    for bus in buses:
        valid, dist, speed, stop = _bus_points_on_grid(bus, grid_utc, max_gap_s=max_gap_s)
        interp.append({"bus": bus, "valid": valid, "dist": dist, "speed": speed, "stop": stop})

    n_bus = len(interp)
    fwd_gap = np.full((n_bus, n_ticks), NO_LEADER_GAP_M, dtype=np.float32)
    leader_idx_arr = np.full((n_bus, n_ticks), -1, dtype=np.int32)
    leader_speed = np.zeros((n_bus, n_ticks), dtype=np.float32)
    # Schema v1 (legacy): u1/u2 are the two buses *behind* target.
    up_speed = np.zeros((n_bus, 2, n_ticks), dtype=np.float32)
    up_gap = np.full((n_bus, 2, n_ticks), NO_LEADER_GAP_M, dtype=np.float32)
    # Schema v2 (current): d1/d2 are the two buses *ahead* — d1 is target's
    # nearest leader (so d1_fwd_gap_from_target == target_fwd_gap); d2 is
    # d1's own nearest leader (the "lead-of-lead"). Each row stores
    # (speed, that-bus's-own-forward-gap), so the model sees the cascade
    # geometry: target's gap to d1 AND d1's gap to d2.
    down_speed = np.zeros((n_bus, 2, n_ticks), dtype=np.float32)
    down_fwd_gap = np.full((n_bus, 2, n_ticks), NO_LEADER_GAP_M, dtype=np.float32)

    for k in range(n_ticks):
        valid_k = np.array([interp[b]["valid"][k] for b in range(n_bus)], dtype=bool)
        dist_k = np.array([interp[b]["dist"][k] for b in range(n_bus)], dtype=np.float64)
        speed_k = np.array([interp[b]["speed"][k] for b in range(n_bus)], dtype=np.float64)
        dist_k = np.where(valid_k, dist_k, np.nan)
        for b in range(n_bus):
            if not valid_k[b]:
                continue
            td = dist_k[b]
            others = dist_k.copy()
            others[b] = np.nan
            gap, leader_idx = _forward_gap_row(td, others)
            fwd_gap[b, k] = gap
            if leader_idx is not None:
                leader_idx_arr[b, k] = leader_idx
            if leader_idx is not None and np.isfinite(speed_k[leader_idx]):
                leader_speed[b, k] = float(speed_k[leader_idx])
            # ── schema v1 helper arrays ──────────────────────────────────
            ups = _upstream_indices(td, others)
            for j, ui in enumerate(ups):
                up_gap[b, j, k] = float(td - dist_k[ui])
                if np.isfinite(speed_k[ui]):
                    up_speed[b, j, k] = float(speed_k[ui])
            # ── schema v2 helper arrays (downstream chain) ────────────────
            # d1 = nearest leader of target = leader_idx (already computed).
            # d2 = nearest leader of d1 (we walk the chain). For each link
            # we record (speed, fwd_gap_from_THAT_bus_to_ITS_leader).
            if leader_idx is not None:
                d1_idx = leader_idx
                if np.isfinite(speed_k[d1_idx]):
                    down_speed[b, 0, k] = float(speed_k[d1_idx])
                # d1's own forward gap = distance from d1 to its nearest
                # leader. Compute by masking out target AND d1 from peers.
                others_d1 = dist_k.copy()
                others_d1[b] = np.nan
                others_d1[d1_idx] = np.nan
                d1_gap, d2_idx = _forward_gap_row(dist_k[d1_idx], others_d1)
                down_fwd_gap[b, 0, k] = float(d1_gap)
                if d2_idx is not None:
                    if np.isfinite(speed_k[d2_idx]):
                        down_speed[b, 1, k] = float(speed_k[d2_idx])
                    others_d2 = others_d1.copy()
                    others_d2[d2_idx] = np.nan
                    d2_gap, _ = _forward_gap_row(dist_k[d2_idx], others_d2)
                    down_fwd_gap[b, 1, k] = float(d2_gap)

    # Per-bus (times, cummax distance) tracks for inverting "when was bus j at
    # distance d" — the realised-time-headway computation below.
    passage_tracks = [
        _passage_track(grid_utc, interp[j]["valid"], interp[j]["dist"]) for j in range(n_bus)
    ]

    def _headway_at(b: int, k: int) -> float:
        """Realised time headway of bus ``b`` at tick ``k`` (seconds), NaN if
        the leader's passage of b's position isn't inside the data window."""
        j = int(leader_idx_arr[b, k])
        if j < 0:
            return float("nan")
        d_b = interp[b]["dist"][k]
        t_pass = _time_at_distance(passage_tracks[j][0], passage_tracks[j][1], float(d_b))
        if t_pass is None:
            return float("nan")
        return float(grid_utc[k] - t_pass)

    si_lo = float(edge_exclude)
    si_hi = float(num_stops - edge_exclude)
    # Look-back length for the gap-closure rate: ~3 ticks or 3 min, whichever
    # is smaller, so it scales sensibly with step_seconds.
    gap_lookback = max(1, min(3, seq_len - 1))
    # Dwell window length in ticks: ~5 minutes regardless of step.
    dwell_ticks = max(1, int(round(300 / step_seconds)))

    tz = ZoneInfo("America/Toronto")

    examples: list[LabelledExample] = []
    for b in range(n_bus):
        bus = interp[b]["bus"]
        valid = interp[b]["valid"]
        speed = interp[b]["speed"]
        stop = interp[b]["stop"]

        for k_ref in range(seq_len - 1, n_ticks):
            if not valid[k_ref]:
                continue
            si_ref = stop[k_ref]
            if not np.isfinite(si_ref) or si_ref < si_lo or si_ref >= si_hi:
                continue
            if not bool(np.all(valid[k_ref - seq_len + 1 : k_ref + 1])):
                continue

            n_chan = n_channels_for(VENDOR_SCHEMA_V)
            n_extra_local = n_extra_for(extras_schema_v)
            window = np.zeros((seq_len, n_chan), dtype=np.float32)
            extras = np.zeros((seq_len, n_extra_local), dtype=np.float32)
            # Eligibility: at least one tick with the target's expected
            # neighbour set populated. v1 wants ≥1 upstream tick (the
            # legacy rule). v2 wants ≥1 downstream (leader) tick — without
            # a leader anywhere in the history there's nothing to bunch
            # into, so the example provides no signal.
            any_neighbour_tick = False

            for kk in range(seq_len):
                t_k = k_ref - (seq_len - 1 - kk)
                t_speed = speed[t_k] if np.isfinite(speed[t_k]) else 0.0
                t_gap = fwd_gap[b, t_k]

                if VENDOR_SCHEMA_V == VENDOR_SCHEMA_V2:
                    # 6-channel layout: (target, d1, d2) × (speed, fwd_gap).
                    window[kk, 0] = float(t_speed)
                    window[kk, 1] = float(t_gap)
                    window[kk, 2] = float(down_speed[b, 0, t_k])
                    window[kk, 3] = float(down_fwd_gap[b, 0, t_k])
                    window[kk, 4] = float(down_speed[b, 1, t_k])
                    window[kk, 5] = float(down_fwd_gap[b, 1, t_k])
                    if t_gap < NO_LEADER_GAP_M:
                        any_neighbour_tick = True
                else:
                    # Legacy 9-channel layout (target + 2 upstream + aux).
                    window[kk, 0] = float(t_speed)
                    window[kk, 1] = float(t_gap)
                    window[kk, 2] = 0.0
                    for j in range(2):
                        col = 3 + 3 * j
                        g = up_gap[b, j, t_k]
                        s = up_speed[b, j, t_k]
                        window[kk, col + 0] = float(s)
                        window[kk, col + 1] = float(g)
                        window[kk, col + 2] = 0.0
                        if g < NO_LEADER_GAP_M:
                            any_neighbour_tick = True

                # ─── compute every extras value, then write by schema ────
                si_k_val = stop[t_k]
                stop_index_val = float(si_k_val) if np.isfinite(si_k_val) else 0.0
                stop_index_norm_val = (stop_index_val / num_stops) if num_stops > 0 else 0.0

                gap_closure_val = 0.0
                lb = min(gap_lookback, kk)
                if lb > 0:
                    g_prev = fwd_gap[b, t_k - lb]
                    if g_prev < NO_LEADER_GAP_M and t_gap < NO_LEADER_GAP_M:
                        gap_closure_val = float((g_prev - t_gap) / (lb * step_seconds))

                lo = max(0, kk - dwell_ticks + 1)
                window_speeds = np.array(
                    [speed[t_k - (kk - kkk)] for kkk in range(lo, kk + 1)],
                    dtype=np.float64,
                )
                window_speeds = window_speeds[np.isfinite(window_speeds)]
                dwell_val = 0.0
                if window_speeds.size > 0:
                    dwell_val = float(int(np.sum(window_speeds < 0.5)) * step_seconds)

                local = datetime.fromtimestamp(grid_utc[t_k], tz=timezone.utc).astimezone(tz)
                mod = local.hour * 60 + local.minute + local.second / 60.0
                ang = 2 * math.pi * mod / 1440.0
                tod_sin_val = float(math.sin(ang))
                tod_cos_val = float(math.cos(ang))

                # Terminus-aware values (only computed when route length known).
                dist_to_terminus_m_val = 0.0
                dist_to_terminus_norm_val = 0.0
                time_to_terminus_min_val = 0.0
                td_dist = interp[b]["dist"][t_k]
                if route_shape_length_m and np.isfinite(td_dist):
                    remaining_m = max(0.0, float(route_shape_length_m) - float(td_dist))
                    dist_to_terminus_m_val = float(remaining_m)
                    dist_to_terminus_norm_val = float(remaining_m / float(route_shape_length_m))
                    if window_speeds.size > 0:
                        moving = window_speeds[window_speeds >= 0.5]
                        if moving.size > 0:
                            med_speed = float(np.median(moving))
                        else:
                            med_speed = float(np.median(window_speeds))
                    else:
                        med_speed = 0.0
                    eff_speed = max(med_speed, TIME_TO_TERMINUS_FLOOR_SPEED_M_S)
                    time_to_terminus_min_val = float(remaining_m / eff_speed / 60.0)

                # ─── v4-only derived features ──────────────────────────
                # rel_speed_to_d1: positive = target moving faster than d1
                # (closing); negative = target falling further behind.
                # When d1 doesn't exist we report 0 (matches "no closing").
                d1_speed_now = float(down_speed[b, 0, t_k])
                rel_speed_d1_val = float(t_speed) - d1_speed_now if d1_speed_now != 0.0 else 0.0
                # target_accel_3tick: (speed[t] - speed[t-3]) / (3*step_s).
                # Falls back to 0 on edge ticks (kk < 3) — same convention
                # used for gap_closure when the lookback is too short.
                accel_val = 0.0
                if kk >= 3:
                    s_prev = speed[t_k - 3] if np.isfinite(speed[t_k - 3]) else 0.0
                    accel_val = float((float(t_speed) - float(s_prev)) / (3.0 * step_seconds))

                # ─── write into the per-tick extras slot by schema version ──
                if extras_schema_v == EXTRAS_SCHEMA_V4:
                    # 6 channels: stop_index_norm, gap_closure, tod_sin,
                    # tod_cos, rel_speed_to_d1, target_accel_3tick.
                    extras[kk, 0] = stop_index_norm_val
                    extras[kk, 1] = gap_closure_val
                    extras[kk, 2] = tod_sin_val
                    extras[kk, 3] = tod_cos_val
                    extras[kk, 4] = rel_speed_d1_val
                    extras[kk, 5] = accel_val
                elif extras_schema_v == EXTRAS_SCHEMA_V3:
                    # 7 channels: stop_index, stop_index_norm, gap_closure,
                    # dwell, tod_sin, tod_cos, dist_to_terminus_norm.
                    extras[kk, 0] = stop_index_val
                    extras[kk, 1] = stop_index_norm_val
                    extras[kk, 2] = gap_closure_val
                    extras[kk, 3] = dwell_val
                    extras[kk, 4] = tod_sin_val
                    extras[kk, 5] = tod_cos_val
                    extras[kk, 6] = dist_to_terminus_norm_val
                elif extras_schema_v == EXTRAS_SCHEMA_V2:
                    # 10 channels: v1 (7) + 3 terminus.
                    extras[kk, 0] = stop_index_val
                    extras[kk, 1] = stop_index_norm_val
                    extras[kk, 2] = gap_closure_val
                    extras[kk, 3] = float(leader_speed[b, t_k])
                    extras[kk, 4] = dwell_val
                    extras[kk, 5] = tod_sin_val
                    extras[kk, 6] = tod_cos_val
                    extras[kk, 7] = dist_to_terminus_m_val
                    extras[kk, 8] = dist_to_terminus_norm_val
                    extras[kk, 9] = time_to_terminus_min_val
                else:
                    # EXTRAS_SCHEMA_V1: legacy 7 (with leader_speed, no terminus).
                    extras[kk, 0] = stop_index_val
                    extras[kk, 1] = stop_index_norm_val
                    extras[kk, 2] = gap_closure_val
                    extras[kk, 3] = float(leader_speed[b, t_k])
                    extras[kk, 4] = dwell_val
                    extras[kk, 5] = tod_sin_val
                    extras[kk, 6] = tod_cos_val

            if not any_neighbour_tick:
                continue
            if not np.all(np.isfinite(window)) or not np.all(np.isfinite(extras)):
                continue

            labels = np.full(pred_len, np.nan, dtype=np.float32)
            label_gaps = np.full(pred_len, np.nan, dtype=np.float32)
            labels_persist = np.full(pred_len, np.nan, dtype=np.float32)
            labels_headway = np.full(pred_len, np.nan, dtype=np.float32)
            for h in range(pred_len):
                k_fut = k_ref + (h + 1)
                if k_fut >= n_ticks:
                    break
                if not valid[k_fut]:
                    break
                # Terminal masking: inside the edge-exclusion band the forward
                # gap reflects layover queueing, not service bunching — leave
                # this horizon NaN (excluded from training) but keep walking
                # so post-terminal horizons of overlapping windows still label.
                si_fut = stop[k_fut]
                if terminal_mask and (
                    not np.isfinite(si_fut) or si_fut < si_lo or si_fut >= si_hi
                ):
                    continue
                g_fut = fwd_gap[b, k_fut]
                label_gaps[h] = float(g_fut)
                labels[h] = 1.0 if g_fut < BUNCHING_THRESHOLD_M else 0.0
                labels_headway[h] = _headway_at(b, k_fut)
                # Debounced label: bunched only when the gap held below the
                # threshold for the trailing ``persist_ticks`` ticks.
                if persist_ticks <= 1:
                    labels_persist[h] = labels[h]
                else:
                    lo_k = k_fut - persist_ticks + 1
                    if lo_k < 0 or not bool(np.all(valid[lo_k : k_fut + 1])):
                        labels_persist[h] = labels[h]
                    else:
                        held = bool(
                            np.all(fwd_gap[b, lo_k : k_fut + 1] < BUNCHING_THRESHOLD_M)
                        )
                        labels_persist[h] = 1.0 if held else 0.0

            sched_hw: float | None = None
            if sched_headway_by_trip is not None:
                sched_hw = sched_headway_by_trip.get(str(bus.trip_id))

            local_ref = datetime.fromtimestamp(grid_utc[k_ref], tz=timezone.utc).astimezone(tz)
            t_ref_min = local_ref.hour * 60 + local_ref.minute + local_ref.second / 60.0

            examples.append(
                LabelledExample(
                    service_date=service_date.isoformat(),
                    route_id=route_id,
                    direction_id=direction_id,
                    trip_id=bus.trip_id,
                    start_date=bus.start_date,
                    vehicle_id=bus.vehicle_id,
                    bus_index=bus.bus_index,
                    t_ref_min=float(t_ref_min),
                    stop_idx_at_ref=float(si_ref),
                    window=window,
                    extras=extras,
                    forward_gap_at_ref=float(fwd_gap[b, k_ref]),
                    labels=labels,
                    label_gaps=label_gaps,
                    labels_persist=labels_persist,
                    labels_headway_s=labels_headway,
                    sched_headway_s=sched_hw,
                    headway_at_ref_s=_headway_at(b, k_ref),
                )
            )

    return examples


def extract_for_date(
    session,
    *,
    route_id: str,
    direction_id: int,
    service_date: date,
    step_seconds: int = DEFAULT_STEP_SECONDS,
    seq_len: int = DEFAULT_SEQ_LEN,
    pred_len: int = DEFAULT_PRED_LEN,
    edge_exclude: int = EDGE_EXCLUDE_DEFAULT,
    extras_schema_v: int = EXTRAS_SCHEMA_V,
    terminal_mask: bool = True,
    persist_ticks: int = PERSIST_TICKS_DEFAULT,
) -> list[LabelledExample]:
    """End-to-end: pull from DB, group into buses, extract labelled examples."""
    from apps.analytics.schedule_headways import scheduled_headway_s
    from apps.api.services.bus_grouping import group_into_buses

    route_stops = compute_route_stops(route_id, direction_id)
    if route_stops is None:
        return []
    rows = fetch_trajectories_for_slice(
        session, service_date=service_date, route_id=route_id, direction_id=direction_id
    )
    buses = group_into_buses(rows, route_stops)

    sched_by_trip: dict[str, float] = {}
    for bus in buses:
        if bus.trip_id in sched_by_trip:
            continue
        hw = scheduled_headway_s(bus.trip_id, route_id, direction_id, service_date)
        if hw is not None:
            sched_by_trip[bus.trip_id] = hw

    return extract_labelled_examples(
        buses,
        route_id=route_id,
        direction_id=direction_id,
        service_date=service_date,
        num_stops=len(route_stops.stops),
        step_seconds=step_seconds,
        seq_len=seq_len,
        pred_len=pred_len,
        edge_exclude=edge_exclude,
        route_shape_length_m=float(route_stops.shape_length_m),
        extras_schema_v=extras_schema_v,
        terminal_mask=terminal_mask,
        persist_ticks=persist_ticks,
        sched_headway_by_trip=sched_by_trip or None,
    )


__all__ = [
    "DEFAULT_STEP_SECONDS",
    "DEFAULT_SEQ_LEN",
    "DEFAULT_PRED_LEN",
    "N_CHANNELS",
    "BUNCHING_THRESHOLD_M",
    "NO_LEADER_GAP_M",
    "EDGE_EXCLUDE_DEFAULT",
    "EXTRA_FEATURES",
    "N_EXTRA",
    "HEADWAY_RATIO_BUNCHED",
    "PERSIST_TICKS_DEFAULT",
    "LabelledExample",
    "extract_labelled_examples",
    "extract_for_date",
]
