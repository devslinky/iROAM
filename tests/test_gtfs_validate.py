"""Tests for ``validate_bundle`` and the trip_index fast path in gtfs_static."""

from __future__ import annotations

import pandas as pd

from apps.analytics.gtfs_static import (
    GtfsStatic,
    _build_trip_index,
    resolve_direction_id,
    resolve_route_id,
    resolve_shape_id,
    validate_bundle,
)


def _static(**overrides) -> GtfsStatic:
    base = dict(
        trips=pd.DataFrame(
            {
                "trip_id": ["t1", "t2"],
                "route_id": ["29", "29"],
                "service_id": ["wk", "wk"],
                "direction_id": [0, 1],
                "shape_id": ["s1", "s2"],
            }
        ),
        stops=pd.DataFrame(
            {
                "stop_id": ["a", "b"],
                "stop_name": ["A", "B"],
                "stop_lat": [43.7, 43.71],
                "stop_lon": [-79.4, -79.41],
            }
        ),
        stop_times=pd.DataFrame(
            {
                "trip_id": ["t1", "t1", "t2"],
                "stop_id": ["a", "b", "a"],
                "stop_sequence": [1, 2, 1],
            }
        ),
        shapes=pd.DataFrame(
            {
                "shape_id": ["s1", "s1", "s2", "s2"],
                "shape_pt_lat": [43.7, 43.71, 43.7, 43.72],
                "shape_pt_lon": [-79.4, -79.41, -79.4, -79.42],
                "shape_pt_sequence": [1, 2, 1, 2],
            }
        ),
        routes=pd.DataFrame({"route_id": ["29"]}),
    )
    base.update(overrides)
    return GtfsStatic(**base)


def test_validate_bundle_clean() -> None:
    assert validate_bundle(_static()) == []


def test_validate_bundle_flags_empty_and_missing_columns() -> None:
    problems = validate_bundle(
        _static(
            shapes=pd.DataFrame(columns=["shape_id"]),
            stops=pd.DataFrame({"stop_id": ["a"], "stop_name": ["A"]}),
        )
    )
    assert any("shapes.txt is empty" in p for p in problems)
    assert any("stops.txt missing columns" in p for p in problems)


def test_validate_bundle_flags_orphan_shapes_and_stops() -> None:
    s = _static(
        trips=pd.DataFrame(
            {
                "trip_id": ["t1", "t2"],
                "route_id": ["29", "29"],
                "service_id": ["wk", "wk"],
                "direction_id": [0, 1],
                "shape_id": ["s1", "missing-shape"],
            }
        ),
        stop_times=pd.DataFrame(
            {
                "trip_id": ["t1", "t2"],
                "stop_id": ["a", "ghost-stop"],
                "stop_sequence": [1, 1],
            }
        ),
    )
    problems = validate_bundle(s)
    assert any("absent from shapes.txt" in p for p in problems)
    assert any("absent from stops.txt" in p for p in problems)


def test_validate_bundle_flags_trips_without_stop_times() -> None:
    s = _static(stop_times=pd.DataFrame({"trip_id": ["t1"], "stop_id": ["a"], "stop_sequence": [1]}))
    problems = validate_bundle(s)
    # 1 of 2 trips uncovered (50% > 5% tolerance).
    assert any("no stop_times rows" in p for p in problems)


def test_trip_index_resolvers_match_scan_path() -> None:
    plain = _static()  # trip_index=None → scan path
    indexed = GtfsStatic(
        trips=plain.trips,
        stops=plain.stops,
        stop_times=plain.stop_times,
        shapes=plain.shapes,
        routes=plain.routes,
        trip_index=_build_trip_index(plain.trips),
    )
    for trip_id in ["t1", "t2", "nope"]:
        assert resolve_route_id(plain, trip_id) == resolve_route_id(indexed, trip_id)
        assert resolve_shape_id(plain, trip_id) == resolve_shape_id(indexed, trip_id)
        assert resolve_direction_id(plain, trip_id) == resolve_direction_id(indexed, trip_id)
    assert resolve_route_id(indexed, "t1") == "29"
    assert resolve_shape_id(indexed, "t2") == "s2"
    assert resolve_direction_id(indexed, "t1") == 0


def test_trip_index_first_occurrence_wins_on_duplicates() -> None:
    trips = pd.DataFrame(
        {
            "trip_id": ["t1", "t1"],
            "route_id": ["29", "999"],
            "shape_id": ["s1", "s9"],
            "direction_id": [0, 1],
        }
    )
    idx = _build_trip_index(trips)
    assert idx["t1"] == ("29", "s1", 0)
