"""Unit tests for live feature-window construction and the running-bus
eligibility rules (``apps.prediction.live_features`` — what production
serves; the old ``apps.api.services.forecast_features`` duplicate is gone).

Exercises the legacy vendor schema v1 (60×9, target/u1/u2 triples) because
its eligibility rules — freshness, edge exclusion, contiguous history,
at-least-one-upstream-tick, finite values — are the contract these tests pin.

Pure-Python: no DB, no model. Synthesises ``BusTrajectory`` objects with
hand-crafted geometry so we can assert exact outcomes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint
from apps.prediction.live_features import LiveWindow, build_bus_window

SEQ_LEN = 60
STEP_SECONDS = 10
N_CHANNELS = 9  # vendor schema v1: (target, u1, u2) × (speed, gap, aux)


def _build_v1(target: BusTrajectory, peers: list[BusTrajectory], *, t_ref_min: float, num_stops: int) -> LiveWindow:
    return build_bus_window(
        target,
        peers,
        t_ref_min=t_ref_min,
        num_stops=num_stops,
        seq_len=SEQ_LEN,
        step_seconds=STEP_SECONDS,
        feature_set="vendor",
        vendor_schema_v=1,
    )


def _mk_points(
    start_utc: datetime,
    *,
    count: int,
    start_dist: float,
    speed: float,
    step_s: int = STEP_SECONDS,
    occupancy: str | None = None,
    stop_stride_m: float = 300.0,
) -> list[TrajectoryPoint]:
    """Produce ``count`` trajectory points with constant speed; stop_idx = dist/stride."""
    out: list[TrajectoryPoint] = []
    for i in range(count):
        t = start_utc + timedelta(seconds=i * step_s)
        d = start_dist + speed * (i * step_s)
        out.append(
            TrajectoryPoint(
                datetime=t,
                travel_distance_m=d,
                moving_speed_m_s=speed,
                occupancy_status=occupancy,
                stop_index=d / stop_stride_m,
            )
        )
    return out


def _bus(idx: int, points: list[TrajectoryPoint]) -> BusTrajectory:
    return BusTrajectory(
        bus_index=idx,
        trip_id=f"T{idx}",
        start_date="20260422",
        vehicle_id=f"V{idx}",
        points=points,
    )


def _minute_of_day(dt_utc: datetime) -> float:
    from zoneinfo import ZoneInfo
    local = dt_utc.astimezone(ZoneInfo("America/Toronto"))
    return local.hour * 60 + local.minute + local.second / 60.0


# ─── happy path ────────────────────────────────────────────────────────────

def test_eligible_bus_produces_60x9_float32_window():
    # 12:00 Toronto; 80 points at 10 s cadence → bus has 13 min of history.
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)  # 12:00 EDT
    target_points = _mk_points(t0, count=80, start_dist=2000.0, speed=8.0)
    # Upstream #1 is 400 m behind at the same speed
    u1_points = _mk_points(t0, count=80, start_dist=1600.0, speed=8.0)
    # Upstream #2 is 900 m behind
    u2_points = _mk_points(t0, count=80, start_dist=1100.0, speed=8.0)

    target = _bus(0, target_points)
    u1 = _bus(1, u1_points)
    u2 = _bus(2, u2_points)

    # t_ref = minute-of-day at the 75th point (so we have ≥60 ticks before it).
    t_ref = _minute_of_day(target_points[75].datetime)

    result = _build_v1(target, [target, u1, u2], t_ref_min=t_ref, num_stops=40)

    assert result.reason is None, result.reason
    assert result.window is not None
    assert result.window.shape == (SEQ_LEN, N_CHANNELS)
    assert result.window.dtype == np.float32
    # Target speed ≈ 8 m/s everywhere
    assert np.allclose(result.window[:, 0], 8.0, atol=1e-3)
    # Upstream gaps > 0 (both behind)
    assert (result.window[:, 4] > 0).all()
    assert (result.window[:, 7] > 0).all()


# ─── freshness ──────────────────────────────────────────────────────────────

def test_stale_bus_rejected():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    target_points = _mk_points(t0, count=80, start_dist=2000.0, speed=8.0)
    u1 = _bus(1, _mk_points(t0, count=80, start_dist=1600.0, speed=8.0))

    # Ask for forecast 10 minutes after the last sample.
    t_last = target_points[-1].datetime
    t_ref = _minute_of_day(t_last + timedelta(minutes=10))

    result = _build_v1(
        _bus(0, target_points), [_bus(0, target_points), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "stale" in (result.reason or "")


# ─── edge exclude ───────────────────────────────────────────────────────────

def test_edge_exclude_near_origin_rejected():
    # Target is parked at the first stop for the entire window.
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    target_points = _mk_points(t0, count=80, start_dist=100.0, speed=0.0)
    u1 = _bus(1, _mk_points(t0, count=80, start_dist=50.0, speed=0.0))

    t_ref = _minute_of_day(target_points[75].datetime)
    result = _build_v1(
        _bus(0, target_points), [_bus(0, target_points), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "edge-exclude" in (result.reason or "")


def test_edge_exclude_near_terminus_rejected():
    # Near the end of a 40-stop route: stop_idx ≈ 38.7 → rejected.
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    # distance / 300 stride = 38.7 → distance = 11600
    target_points = _mk_points(t0, count=80, start_dist=11600.0, speed=0.0)
    u1 = _bus(1, _mk_points(t0, count=80, start_dist=11500.0, speed=0.0))

    t_ref = _minute_of_day(target_points[75].datetime)
    result = _build_v1(
        _bus(0, target_points), [_bus(0, target_points), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "edge-exclude" in (result.reason or "")


# ─── history length / gaps ──────────────────────────────────────────────────

def test_short_history_rejected():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    target_points = _mk_points(t0, count=40, start_dist=2000.0, speed=8.0)  # only 6.7 min
    u1 = _bus(1, _mk_points(t0, count=40, start_dist=1600.0, speed=8.0))

    t_ref = _minute_of_day(target_points[-1].datetime)
    result = _build_v1(
        _bus(0, target_points), [_bus(0, target_points), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "missing target sample" in (result.reason or "")


def test_gap_in_history_rejected():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    pts = _mk_points(t0, count=80, start_dist=2000.0, speed=8.0)
    # Drop a 60-second span mid-window so one grid tick has no nearby sample.
    pts = pts[:30] + pts[36:]
    u1 = _bus(1, _mk_points(t0, count=80, start_dist=1600.0, speed=8.0))

    t_ref = _minute_of_day(t0 + timedelta(seconds=75 * STEP_SECONDS))
    result = _build_v1(
        _bus(0, pts), [_bus(0, pts), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "missing target sample" in (result.reason or "")


# ─── upstream availability ─────────────────────────────────────────────────

def test_no_upstream_anywhere_rejected():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    target = _bus(0, _mk_points(t0, count=80, start_dist=2000.0, speed=8.0))
    # Only a leader (higher distance) exists — no upstream.
    leader = _bus(1, _mk_points(t0, count=80, start_dist=3000.0, speed=8.0))

    t_ref = _minute_of_day(target.points[75].datetime)
    result = _build_v1(target, [target, leader], t_ref_min=t_ref, num_stops=40)
    assert result.window is None
    assert "no upstream" in (result.reason or "")


def test_partial_upstream_fills_sentinel_and_succeeds():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    target = _bus(0, _mk_points(t0, count=80, start_dist=2000.0, speed=8.0))
    # Upstream only for the last 30 ticks (appears at t0+500s).
    late_start = t0 + timedelta(seconds=500)
    u1 = _bus(1, _mk_points(late_start, count=30, start_dist=1600.0, speed=8.0))

    t_ref = _minute_of_day(target.points[75].datetime)
    result = _build_v1(target, [target, u1], t_ref_min=t_ref, num_stops=40)
    assert result.reason is None, result.reason
    assert result.window is not None
    # Early ticks should show the no-upstream sentinel speed = 0.
    assert result.window[0, 3] == 0.0
    # A tick that actually has the upstream should have speed > 0.
    assert result.window[-1, 3] > 0.0


def test_nan_speed_rejected():
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    pts = _mk_points(t0, count=80, start_dist=2000.0, speed=8.0)
    # Corrupt one sample's speed to NaN. Speed goes into the window directly,
    # so the finite-value guard must reject this.
    bad = TrajectoryPoint(
        datetime=pts[40].datetime,
        travel_distance_m=pts[40].travel_distance_m,
        moving_speed_m_s=float("nan"),
        occupancy_status=pts[40].occupancy_status,
        stop_index=pts[40].stop_index,
    )
    pts = pts[:40] + [bad] + pts[41:]
    u1 = _bus(1, _mk_points(t0, count=80, start_dist=1600.0, speed=8.0))

    t_ref = _minute_of_day(t0 + timedelta(seconds=75 * STEP_SECONDS))
    result = _build_v1(
        _bus(0, pts), [_bus(0, pts), u1],
        t_ref_min=t_ref, num_stops=40,
    )
    assert result.window is None
    assert "non-finite" in (result.reason or "")
