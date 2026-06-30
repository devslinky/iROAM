"""Tests for the sequence-monotone stop projection in stop_projection."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString

from apps.analytics.stop_projection import _monotone_stop_distances


def test_straight_line_matches_free_projection() -> None:
    line = LineString([(0, 0), (1000, 0)])
    xy = [(100.0, 5.0), (400.0, -3.0), (900.0, 0.0)]
    assert _monotone_stop_distances(line, xy) == pytest.approx([100.0, 400.0, 900.0])


def test_tiny_backward_wobble_clamps_forward() -> None:
    line = LineString([(0, 0), (1000, 0)])
    xy = [(500.0, 0.0), (499.5, 0.0)]  # 0.5 m wobble behind, inside tolerance
    out = _monotone_stop_distances(line, xy)
    assert out == pytest.approx([500.0, 500.0])


def test_out_and_back_shape_reprojects_return_leg_stops() -> None:
    # Out 0→1000 then back along the identical street: free projection snaps
    # every return-leg stop to the outbound leg (first nearest wins).
    line = LineString([(0, 0), (1000, 0), (0, 0)])
    out_stop = (200.0, 0.0)        # outbound, true d = 200
    ret_stop_a = (900.0, 0.0)      # return leg, true d = 1100 — but free d = 900
    ret_stop_b = (400.0, 0.0)      # return leg, true d = 1600 — free d = 400 < prev
    out = _monotone_stop_distances(line, [out_stop, ret_stop_a, ret_stop_b])
    assert out[0] == pytest.approx(200.0)
    # ret_stop_a's wrong-leg projection lands ahead of prev → undetectable
    # from ordering alone (greedy limitation, documented).
    assert out[1] == pytest.approx(900.0)
    # ret_stop_b violates ordering → re-projected onto the remainder.
    assert out[2] == pytest.approx(1600.0)
    assert out == sorted(out)


def test_predecessor_at_line_end_clamps_to_length() -> None:
    line = LineString([(0, 0), (100, 0)])
    out = _monotone_stop_distances(line, [(100.0, 0.0), (10.0, 0.0)])
    assert out == pytest.approx([100.0, 100.0])


def test_distances_always_non_decreasing_property() -> None:
    import numpy as np

    rng = np.random.default_rng(7)
    # Zig-zag self-overlapping shape.
    line = LineString([(0, 0), (500, 0), (250, 2), (750, 2), (400, 4), (1000, 4)])
    for _ in range(20):
        xy = [(float(rng.uniform(0, 1000)), float(rng.uniform(-5, 9))) for _ in range(12)]
        out = _monotone_stop_distances(line, xy)
        assert all(b >= a for a, b in zip(out, out[1:], strict=False))
        assert all(0.0 <= d <= line.length + 1e-9 for d in out)
