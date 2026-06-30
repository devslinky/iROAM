"""Tests for the labels-schema-v2 quality improvements in data_process.bunching.

Synthetic two-bus scenarios on a 60 s grid — no DB, no GTFS. Geometry is
chosen so stop_index ≈ distance / 500 with 21 stops over 10 km.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint
from data_process.bunching.labels import (
    BUNCHING_THRESHOLD_M,
    extract_labelled_examples,
)

_T0 = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
_STOP_SPACING_M = 500.0
_NUM_STOPS = 21


def _bus(bus_index: int, trip_id: str, dist_by_minute: list[float]) -> BusTrajectory:
    points = [
        TrajectoryPoint(
            datetime=_T0 + timedelta(minutes=i),
            travel_distance_m=d,
            moving_speed_m_s=8.0,
            occupancy_status=None,
            stop_index=d / _STOP_SPACING_M,
        )
        for i, d in enumerate(dist_by_minute)
    ]
    return BusTrajectory(
        bus_index=bus_index, trip_id=trip_id, start_date="20260609",
        vehicle_id=f"V{bus_index}", points=points,
    )


def _extract(buses, **kw):
    defaults = dict(
        route_id="29",
        direction_id=0,
        service_date=_T0.date(),
        num_stops=_NUM_STOPS,
        step_seconds=60,
        seq_len=3,
        pred_len=5,
        edge_exclude=2,
    )
    defaults.update(kw)
    return extract_labelled_examples(buses, **defaults)


def _follower_examples(examples):
    return [e for e in examples if e.bus_index == 0]


def test_gap_labels_and_realised_headway() -> None:
    # Leader 480 s (= 4000 m at 8.33 m/s? no — distances given directly) ahead:
    # follower trails leader by exactly 600 m → gap stays 600 m, headway is
    # the time the leader needed to cover those 600 m = 75 s at 8 m/s... we
    # construct distances directly: both advance 480 m/min; offset 600 m.
    n = 25
    leader = _bus(1, "lead", [600.0 + 480.0 * i for i in range(n)])
    follower = _bus(0, "follow", [480.0 * i for i in range(n)])
    examples = _follower_examples(_extract([leader, follower]))
    assert examples, "expected examples for the follower"
    ex = examples[0]
    # Constant 600 m gap → no bunching anywhere.
    finite = np.isfinite(ex.labels)
    assert finite.any()
    assert np.nanmax(ex.labels) == 0.0
    # Realised time headway: leader covers 480 m/min → 600 m gap = 75 s.
    hw = ex.labels_headway_s[np.isfinite(ex.labels_headway_s)]
    assert hw.size > 0
    assert np.allclose(hw, 75.0, atol=1.0)
    # Scheduled headway metadata passes through.
    examples_hw = _follower_examples(
        _extract([leader, follower], sched_headway_by_trip={"follow": 480.0})
    )
    assert examples_hw[0].sched_headway_s == 480.0


def test_persistence_debounces_single_tick_dips() -> None:
    # Follower's gap dips under the threshold for exactly one tick (minute 12)
    # then recovers — classic projection flicker, not real bunching.
    n = 25
    leader_d = [600.0 + 480.0 * i for i in range(n)]
    follower_d = [480.0 * i for i in range(n)]
    dip_minute = 12
    follower_d[dip_minute] = leader_d[dip_minute] - (BUNCHING_THRESHOLD_M - 20.0)
    leader = _bus(1, "lead", leader_d)
    follower = _bus(0, "follow", follower_d)

    examples = _follower_examples(_extract([leader, follower], persist_ticks=2))
    hits_inst = 0
    hits_persist = 0
    for ex in examples:
        finite = np.isfinite(ex.labels)
        hits_inst += int(np.nansum(ex.labels[finite]))
        finite_p = np.isfinite(ex.labels_persist)
        hits_persist += int(np.nansum(ex.labels_persist[finite_p]))
    assert hits_inst > 0, "instantaneous labels should flag the dip"
    assert hits_persist == 0, "debounced labels should suppress a 1-tick dip"


def test_persistence_keeps_sustained_bunching() -> None:
    # Follower closes onto the leader and stays ~50 m behind for the rest of
    # the window — sustained bunching must survive the debounce.
    n = 25
    leader_d = [600.0 + 480.0 * i for i in range(n)]
    follower_d = []
    for i in range(n):
        if i < 8:
            follower_d.append(480.0 * i + i * 68.0)  # closing
        else:
            follower_d.append(leader_d[i] - 50.0)
    leader = _bus(1, "lead", leader_d)
    follower = _bus(0, "follow", follower_d)
    examples = _follower_examples(_extract([leader, follower], persist_ticks=2))
    total_persist = sum(
        int(np.nansum(ex.labels_persist[np.isfinite(ex.labels_persist)]))
        for ex in examples
    )
    assert total_persist > 0


def test_terminal_mask_blanks_labels_near_terminus() -> None:
    # Two buses queueing 40 m apart while crawling through the last stops
    # (stop_index > num_stops - edge_exclude = 19).
    n = 25
    # Both buses near the end of the 10 km route, inching forward.
    leader_d = [9540.0 + 4.0 * i for i in range(n)]
    follower_d = [9500.0 + 4.0 * i for i in range(n)]
    leader = _bus(1, "lead", leader_d)
    follower = _bus(0, "follow", follower_d)

    masked = _extract([leader, follower], terminal_mask=True)
    legacy = _extract([leader, follower], terminal_mask=False)

    # t_ref eligibility already excludes the band, so no examples either way
    # for buses living entirely inside it.
    assert masked == [] and legacy == []

    # Now a follower that enters the band mid-horizon: at minute ~10 it
    # crosses stop 19 (9500 m); horizons before that label normally, after
    # that are masked.
    approach_leader = _bus(1, "lead", [8740.0 + 100.0 * i for i in range(n)])
    approach_follow = _bus(0, "follow", [8700.0 + 100.0 * i for i in range(n)])
    ex_masked = _follower_examples(_extract([approach_leader, approach_follow], terminal_mask=True))
    ex_legacy = _follower_examples(_extract([approach_leader, approach_follow], terminal_mask=False))
    n_lab_masked = sum(int(np.isfinite(e.labels).sum()) for e in ex_masked)
    n_lab_legacy = sum(int(np.isfinite(e.labels).sum()) for e in ex_legacy)
    assert n_lab_legacy > n_lab_masked > 0
    # The 40 m terminal gap counted as bunching under legacy labels.
    bunched_legacy = sum(int(np.nansum(e.labels[np.isfinite(e.labels)])) for e in ex_legacy)
    bunched_masked = sum(int(np.nansum(e.labels[np.isfinite(e.labels)])) for e in ex_masked)
    assert bunched_legacy > bunched_masked


def test_headway_nan_when_no_leader() -> None:
    n = 25
    solo_leader = _bus(1, "lead", [600.0 + 480.0 * i for i in range(n)])
    follower = _bus(0, "follow", [480.0 * i for i in range(n)])
    examples = [e for e in _extract([solo_leader, follower]) if e.bus_index == 1]
    # The leader has nobody ahead → its examples are skipped entirely by the
    # any_neighbour_tick rule (v2 requires a leader), so none exist.
    assert examples == []
