"""Unit tests for the along-route distance bunching detector.

Covers:
  * Basic positive case: two buses running 80 m apart for 10 min → one event
    tagged on the follower at the midpoint, ``method="distance"``.
  * Negative case: 500 m separation with a 150 m threshold → no event.
  * Three-bus case where proximity flips from (leader, mid) to (mid, tail) —
    each active pair produces exactly one event, each tagged on its follower.
  * Regression guard: ``detect_all(bunch_method="time")`` output matches a
    direct ``detect_bunch_events`` call, ignoring the added ``method`` tag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.analytics.anomalies import (
    AnomalyEvent,
    BusTrajectory,
    TrajectoryPoint,
    detect_all,
    detect_bunch_events,
    detect_bunch_events_distance,
)

UTC = timezone.utc
EPOCH = datetime(2026, 4, 24, 13, 0, 0, tzinfo=UTC)  # ~09:00 America/Toronto


def _trajectory(
    bus_index: int,
    *,
    start_dist_m: float,
    speed_m_s: float,
    duration_s: int,
    step_s: int = 10,
    stop_spacing_m: float = 250.0,
) -> BusTrajectory:
    """Build a straight-line bus whose ``travel_distance_m`` advances linearly.

    ``stop_index`` is ``travel_distance_m / stop_spacing_m`` — any monotonic
    mapping is fine for these tests.
    """
    points = []
    for s in range(0, duration_s + 1, step_s):
        d = start_dist_m + speed_m_s * s
        points.append(
            TrajectoryPoint(
                datetime=EPOCH + timedelta(seconds=s),
                travel_distance_m=d,
                moving_speed_m_s=speed_m_s,
                occupancy_status="MANY_SEATS_AVAILABLE",
                stop_index=d / stop_spacing_m,
            )
        )
    return BusTrajectory(
        bus_index=bus_index,
        trip_id=f"t{bus_index}",
        start_date="2026-04-24",
        vehicle_id=f"v{bus_index}",
        points=points,
    )


def test_distance_detector_emits_single_event_for_close_pair() -> None:
    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    follower = _trajectory(1, start_dist_m=920.0, speed_m_s=8.0, duration_s=600)

    events = detect_bunch_events_distance(
        [leader, follower], bunch_distance_threshold_m=150.0
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.type == "bunch"
    assert ev.method == "distance"
    assert ev.bus_index == 1  # follower has lower travel_distance_m
    # Window spans 0..600s → midpoint ≈ 300s → 13:05 UTC = 09:05 Toronto = 545 min.
    assert 540 <= ev.minute_of_day <= 550
    # Follower's distance midpoint is 920 + 8*300 = 3320 m → stop_index = 13.28.
    assert abs(ev.stop_index - 3320.0 / 250.0) < 0.2


def test_distance_detector_ignores_well_separated_buses() -> None:
    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    follower = _trajectory(1, start_dist_m=500.0, speed_m_s=8.0, duration_s=600)

    events = detect_bunch_events_distance(
        [leader, follower], bunch_distance_threshold_m=150.0
    )

    assert events == []


def test_distance_detector_handles_three_bus_pairwise_flip() -> None:
    # Three buses running in the same direction. (0) is ahead, (2) trails far
    # behind until its speed closes the gap to (1). Over the 20-minute window
    # both (1,0) and (2,1) spend a stretch inside the threshold.
    leader = _trajectory(0, start_dist_m=2000.0, speed_m_s=6.0, duration_s=1200)
    mid = _trajectory(1, start_dist_m=1920.0, speed_m_s=6.0, duration_s=1200)
    # Tail starts 400 m behind mid but catches up at a faster pace.
    tail = _trajectory(2, start_dist_m=1520.0, speed_m_s=6.5, duration_s=1200)

    events = detect_bunch_events_distance(
        [leader, mid, tail], bunch_distance_threshold_m=150.0
    )

    pairs = sorted({ev.bus_index for ev in events if ev.method == "distance"})
    # Each event is tagged on the follower; we expect the follower of each
    # consecutive pair to appear at least once.
    assert 1 in pairs  # follower of (leader=0, mid=1)
    assert 2 in pairs  # follower of (mid=1, tail=2) once tail has caught up
    assert all(ev.type == "bunch" and ev.method == "distance" for ev in events)


def test_distance_detector_empty_inputs() -> None:
    assert detect_bunch_events_distance([], bunch_distance_threshold_m=150.0) == []
    lone = _trajectory(0, start_dist_m=0.0, speed_m_s=5.0, duration_s=100)
    assert (
        detect_bunch_events_distance([lone], bunch_distance_threshold_m=150.0) == []
    )


def test_detect_all_time_method_is_regression_safe() -> None:
    # Two buses close enough in time at a shared stop to trigger the existing
    # time-gap detector but far enough apart to NOT trigger the distance one.
    # With bunch_method="time" (the default), detect_all must return exactly
    # what detect_bunch_events returns (aside from the added method tag).
    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    follower = _trajectory(1, start_dist_m=900.0, speed_m_s=8.0, duration_s=600)

    direct = detect_bunch_events([leader, follower], bunch_seconds_threshold=60)
    via_all = [
        ev
        for ev in detect_all(
            [leader, follower],
            bunch_seconds_threshold=60,
            idle_min_threshold=1e6,  # effectively disabled
            crowd_pct_threshold=200,  # effectively disabled
            bunch_method="time",
        )
        if ev.type == "bunch"
    ]

    def _key(ev: AnomalyEvent) -> tuple:
        return (ev.bus_index, round(ev.minute_of_day, 3), round(ev.stop_index, 3))

    assert sorted(_key(e) for e in direct) == sorted(_key(e) for e in via_all)
    # Every time-based event carries method="time" after the refactor.
    assert all(ev.method == "time" for ev in via_all)


def test_detect_all_both_methods_tags_events_distinctly() -> None:
    # Same slice as above but with method="both": we expect time-events and
    # distance-events to coexist, each correctly tagged. We don't assert the
    # exact counts (synthetic data) — just that the tag split is clean.
    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    follower = _trajectory(1, start_dist_m=920.0, speed_m_s=8.0, duration_s=600)

    events = detect_all(
        [leader, follower],
        bunch_seconds_threshold=60,
        idle_min_threshold=1e6,
        crowd_pct_threshold=200,
        bunch_distance_threshold_m=150.0,
        bunch_method="both",
    )
    bunch_events = [ev for ev in events if ev.type == "bunch"]
    assert any(ev.method == "distance" for ev in bunch_events)
    assert all(ev.method in ("time", "distance") for ev in bunch_events)


def _oscillating_pair(amplitude_m: float, period_s: int = 120) -> list[BusTrajectory]:
    """Leader fixed-speed; follower's gap oscillates 130↔190 m around a 150 m
    threshold (crosses it every half period)."""
    import math

    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    points = []
    for s in range(0, 601, 10):
        gap = 160.0 + amplitude_m * math.sin(2 * math.pi * s / period_s)
        d = 1000.0 + 8.0 * s - gap
        points.append(
            TrajectoryPoint(
                datetime=EPOCH + timedelta(seconds=s),
                travel_distance_m=d,
                moving_speed_m_s=8.0,
                occupancy_status=None,
                stop_index=d / 250.0,
            )
        )
    follower = BusTrajectory(
        bus_index=1, trip_id="t1", start_date="2026-04-24", vehicle_id="v1", points=points
    )
    return [leader, follower]


def test_hysteresis_merges_oscillating_runs() -> None:
    buses = _oscillating_pair(amplitude_m=30.0)  # gap swings 130..190 m

    flickery = detect_bunch_events_distance(buses, bunch_distance_threshold_m=150.0)
    merged = detect_bunch_events_distance(
        buses, bunch_distance_threshold_m=150.0, bunch_distance_exit_m=200.0
    )
    # Without hysteresis each dip below 150 m is its own event; with a 200 m
    # exit threshold the whole oscillation reads as one continuous run.
    assert len(flickery) > 1
    assert len(merged) == 1


def test_min_duration_drops_brief_brushes() -> None:
    # Follower dips inside the threshold for a single ~30 s tick window.
    leader = _trajectory(0, start_dist_m=1000.0, speed_m_s=8.0, duration_s=600)
    points = []
    for s in range(0, 601, 10):
        gap = 80.0 if 290 <= s <= 310 else 400.0
        d = 1000.0 + 8.0 * s - gap
        points.append(
            TrajectoryPoint(
                datetime=EPOCH + timedelta(seconds=s),
                travel_distance_m=d,
                moving_speed_m_s=8.0,
                occupancy_status=None,
                stop_index=d / 250.0,
            )
        )
    follower = BusTrajectory(
        bus_index=1, trip_id="t1", start_date="2026-04-24", vehicle_id="v1", points=points
    )

    no_filter = detect_bunch_events_distance(
        [leader, follower], bunch_distance_threshold_m=150.0
    )
    filtered = detect_bunch_events_distance(
        [leader, follower], bunch_distance_threshold_m=150.0, min_duration_s=120.0
    )
    assert len(no_filter) >= 1
    assert filtered == []


def test_defaults_preserve_pre_hysteresis_behaviour() -> None:
    buses = _oscillating_pair(amplitude_m=30.0)
    legacy = detect_bunch_events_distance(buses, bunch_distance_threshold_m=150.0)
    explicit = detect_bunch_events_distance(
        buses,
        bunch_distance_threshold_m=150.0,
        bunch_distance_exit_m=None,
        min_duration_s=0.0,
    )
    assert [(e.bus_index, e.minute_of_day, e.stop_index) for e in legacy] == [
        (e.bus_index, e.minute_of_day, e.stop_index) for e in explicit
    ]
