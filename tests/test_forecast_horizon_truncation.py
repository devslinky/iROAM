"""Regression tests for the May-2026 horizon-truncation fix.

Two distinct properties to lock in:

1. ``_useful_horizon_steps`` returns ``pred_len`` when any input is missing
   (graceful degradation) and a sensible truncation when all inputs are present.
   We don't pin the exact formula — just the contract that a near-terminus bus
   gets a small number and a fresh-out-of-yard bus gets the full prediction.

2. ``run_forecast`` truncates per-bus predictions when ``route_shape_length_m``
   is supplied: the bus at stop 38 of 40 with low speed should have
   ``useful_horizon_steps`` strictly less than ``pred_len``, while a bus at
   stop 5 of 40 should be allowed the full ``pred_len``. Recomputed
   ``max_prob`` / ``first_alert_step`` are over the truncated horizons only.

The forecast service is exercised with a stub predictor (no LightGBM
dependency) that returns a controlled probability ramp — so the test asserts
plumbing, not model behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint
from apps.api.services.forecast import (
    _useful_horizon_steps,
    run_forecast,
)
# Stub-bundle geometry used throughout these tests (matches the vendor
# bundle: 60 ticks x 10 s).
SEQ_LEN = 60
STEP_SECONDS = 10


# ───────────────────── _useful_horizon_steps unit tests ─────────────────────


def test_useful_horizon_returns_full_when_inputs_missing():
    # Graceful degradation: any None input falls back to pred_len so the fix
    # is a no-op for old callers (e.g. the existing forecast tests).
    assert _useful_horizon_steps(None, 12000.0, 6.0, step_seconds=60, pred_len=30) == 30
    assert _useful_horizon_steps(1000.0, None, 6.0, step_seconds=60, pred_len=30) == 30
    assert _useful_horizon_steps(1000.0, 12000.0, None, step_seconds=60, pred_len=30) == 30


def test_useful_horizon_truncates_for_end_of_route_bus():
    # Bus at 90% of a 12 km route, moving 6 m/s → 1200 m left → ~200 s left.
    # With safety_factor=1.5 → 300 s of headroom = 5 steps at 60 s each.
    out = _useful_horizon_steps(
        travel_distance_m=10800.0,
        route_shape_length_m=12000.0,
        median_speed_m_s=6.0,
        step_seconds=60,
        pred_len=30,
    )
    assert out < 30, "should truncate for end-of-route bus"
    assert out >= 3, "minimum_steps floor"


def test_useful_horizon_returns_full_for_fresh_start_bus():
    # Bus 100 m into a 12 km route, moving 6 m/s → plenty of time for 30 min.
    out = _useful_horizon_steps(
        travel_distance_m=100.0,
        route_shape_length_m=12000.0,
        median_speed_m_s=6.0,
        step_seconds=60,
        pred_len=30,
    )
    assert out == 30


def test_useful_horizon_minimum_steps_floor():
    # Bus right at the terminus with no room — should still report ``minimum_steps``
    # so the UI doesn't go blank in edge cases.
    out = _useful_horizon_steps(
        travel_distance_m=12000.0, route_shape_length_m=12000.0, median_speed_m_s=6.0,
        step_seconds=60, pred_len=30, minimum_steps=3,
    )
    assert out == 3


# ─────────────────── integration: run_forecast end-to-end ───────────────────


def _mk_bus(idx: int, t0: datetime, *, start_dist: float, speed: float = 8.0,
            n_points: int = 320, step_s: int = 10) -> BusTrajectory:
    """Synthesise a bus with constant speed over ``n_points`` ticks.

    Default of 240 ticks × 10 s = 40 min of history is enough for the
    production geometry (20 ticks × 60 s history) to find SEQ_LEN samples
    within the matching tolerance.
    """
    pts: list[TrajectoryPoint] = []
    for i in range(n_points):
        t = t0 + timedelta(seconds=i * step_s)
        d = start_dist + speed * (i * step_s)
        pts.append(
            TrajectoryPoint(
                datetime=t, travel_distance_m=d,
                moving_speed_m_s=speed, occupancy_status=None,
                stop_index=d / 300.0,
            )
        )
    return BusTrajectory(
        bus_index=idx, trip_id=f"T{idx}", start_date="20260422",
        vehicle_id=f"V{idx}", points=pts,
    )


def _mod(dt_utc: datetime) -> float:
    from zoneinfo import ZoneInfo
    local = dt_utc.astimezone(ZoneInfo("America/Toronto"))
    return local.hour * 60 + local.minute + local.second / 60.0


class _RampPredictor:
    """Production-geometry stub (20 ticks × 60 s = 20 min history, 30-step
    horizon = 30 min ahead) so the truncation logic is exercised the same way
    as the deployed bundle. Outputs a monotonic 0.6→0.95 ramp; with
    truncation the per-bus ``max_prob_step`` will land at the truncation
    boundary instead of horizon 29."""

    pred_len = 30
    thresholds = {h: {"threshold": 0.5} for h in range(30)}
    metadata = {"seq_len": 20, "step_seconds": 60,
                "feature_set": "vendor", "n_channels": 9}
    scaler = {"speed_mean": 0.0, "speed_std": 1.0, "gap_mean": 0.0, "gap_std": 1.0}

    def predict_proba(self, batch: np.ndarray, *, is_scaled: bool) -> np.ndarray:
        n = batch.shape[0]
        ramp = np.linspace(0.6, 0.95, self.pred_len, dtype=np.float32)
        return np.broadcast_to(ramp, (n, self.pred_len)).astype(np.float32)

    def alert(self, batch, *, is_scaled): return []  # unused; run_forecast bypasses


def test_run_forecast_truncates_end_of_route_bus():
    """End-of-route bus should get useful_horizon_steps < pred_len; a
    mid-route bus alongside it should still get full pred_len.

    Geometry: route 60 stops × 300 m = 18000 m. Stub geometry is
    seq_len=20 × step=60 s ⇒ we need t_ref at least 20 min into each bus's
    life so the live builder finds 20 samples within tolerance. We place
    t_ref at the synthesised bus's 250th tick (≈2500 s = 41 min) and pick
    start_dist / speed so each bus is where we want it at that ref time.
    """
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    # We pick t_ref at i=150 (t0 + 1500 s) so each bus has ≥ 20 min of history
    # before t_ref (the stub needs seq_len=20 × 60 s).
    fresh_leader = _mk_bus(0, t0, start_dist=3000.0,  speed=2.0)   # dist@ref = 6000  → si ≈ 20
    fresh_tail   = _mk_bus(1, t0, start_dist=2500.0,  speed=2.0)   # dist@ref = 5500  → upstream of leader
    # Near-terminus pair. Speed is high enough that the remaining-trip-time
    # estimate isn't capped by the floor_speed in _useful_horizon_steps —
    # otherwise the bus would always look like it has plenty of time left.
    near_terminus         = _mk_bus(2, t0, start_dist=10800.0, speed=4.0)  # dist@ref = 16800 → si ≈ 56
    upstream_for_terminus = _mk_bus(3, t0, start_dist=10400.0, speed=4.0)  # dist@ref = 16400 → upstream

    t_ref = _mod(fresh_leader.points[150].datetime)
    result = run_forecast(
        [fresh_leader, fresh_tail, near_terminus, upstream_for_terminus],
        num_stops=60,
        t_ref_min=t_ref,
        predictor=_RampPredictor(),
        route_shape_length_m=60 * 300.0,  # 18000 m
    )

    by_bus = {r["bus_id"]: r for r in result.per_bus if r["eligible"]}
    assert 0 in by_bus or 1 in by_bus, "at least one mid-route bus eligible"
    assert 2 in by_bus, f"near-terminus bus not eligible; reasons: " + \
        ", ".join(f"{r['bus_id']}={r.get('ineligible_reason')}" for r in result.per_bus if not r['eligible'])

    near = by_bus[2]
    assert near["useful_horizon_steps"] < 30, f"near-terminus not truncated: {near}"
    assert len(near["per_horizon"]) == near["useful_horizon_steps"]
    assert near["max_prob_step"] < 30

    for bus_id in (0, 1):
        if bus_id in by_bus:
            mid = by_bus[bus_id]
            assert mid["useful_horizon_steps"] == 30, (
                f"bus {bus_id} should not be truncated: useful={mid['useful_horizon_steps']}"
            )


def test_run_forecast_old_signature_still_works():
    """Without ``route_shape_length_m``, no truncation is applied — preserves
    the existing test-fixture API."""
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    a = _mk_bus(0, t0, start_dist=3000.0, speed=2.0)
    b = _mk_bus(1, t0, start_dist=2500.0, speed=2.0)
    t_ref = _mod(a.points[150].datetime)

    result = run_forecast(
        [a, b], num_stops=60, t_ref_min=t_ref, predictor=_RampPredictor(),
    )
    elig = [r for r in result.per_bus if r["eligible"]]
    assert elig, "should have at least one eligible bus"
    # When shape length isn't passed, useful_horizon_steps falls back to pred_len.
    for r in elig:
        assert r["useful_horizon_steps"] == 30
