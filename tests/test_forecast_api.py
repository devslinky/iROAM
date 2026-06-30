"""FastAPI TestClient tests for ``GET /iroam/forecast``.

Unlike ``test_api_smoke.py`` (which covers older trip-update endpoints) this
file uses the live-dataset trajectory store. We avoid DB dependencies by
monkeypatching the router's query helpers + the predictor itself so the test
runs on any machine where FastAPI is installable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.analytics.stop_projection import RouteStops, StopOnRoute
from apps.api.main import create_app
# Stub-bundle geometry used throughout these tests (matches the vendor
# bundle: 60 ticks x 10 s).
STEP_SECONDS = 10


class _FakeTripRow:
    """Minimal duck-type for ``_group_into_buses``."""

    def __init__(
        self,
        trip_id: str,
        start_date: str,
        vehicle_id: str,
        dt: datetime,
        dist: float,
        speed: float,
    ) -> None:
        self.trip_id = trip_id
        self.start_date = start_date
        self.vehicle_id = vehicle_id
        self.datetime = dt
        self.travel_distance_m = dist
        self.moving_speed_m_s = speed
        self.occupancy_status = None


def _seed_rows() -> list[_FakeTripRow]:
    """Three buses trailing each other so the front two both have upstream."""
    t0 = datetime(2026, 4, 22, 16, 0, 0, tzinfo=timezone.utc)
    rows: list[_FakeTripRow] = []
    for trip, start in [("T0", 2000.0), ("T1", 1600.0), ("T2", 1200.0)]:
        for i in range(80):
            rows.append(
                _FakeTripRow(
                    trip_id=trip,
                    start_date="20260422",
                    vehicle_id=f"V{trip}",
                    dt=t0 + timedelta(seconds=i * STEP_SECONDS),
                    dist=start + 8.0 * (i * STEP_SECONDS),
                    speed=8.0,
                )
            )
    return rows


def _fake_route_stops() -> RouteStops:
    stops = tuple(
        StopOnRoute(
            stop_id=f"S{i}",
            stop_name=f"Stop {i}",
            stop_lat=43.6 + 0.001 * i,
            stop_lon=-79.4,
            stop_sequence=i,
            distance_m=i * 300.0,
        )
        for i in range(40)
    )
    return RouteStops(
        route_id="29",
        direction_id=0,
        shape_id="fake",
        shape_length_m=40 * 300.0,
        stops=stops,
    )


class _StubPredictor:
    pred_len = 30
    thresholds = {
        h: {"threshold": 0.5, "f2": 1.0, "precision": 1.0, "recall": 1.0}
        for h in range(30)
    }
    # Vendor-schema geometry matches the test bus generator (10 s ticks).
    metadata = {
        "seq_len": 60,
        "step_seconds": STEP_SECONDS,
        "feature_set": "vendor",
        "n_channels": 9,
    }
    scaler = {"speed_mean": 0.0, "speed_std": 1.0, "gap_mean": 0.0, "gap_std": 1.0}

    def predict_proba(self, batch: np.ndarray, *, is_scaled: bool) -> np.ndarray:
        n = batch.shape[0]
        ramp = np.linspace(0.1, 0.9, self.pred_len, dtype=np.float32)
        return np.broadcast_to(ramp, (n, self.pred_len)).astype(np.float32)

    def alert(self, batch: np.ndarray, *, is_scaled: bool) -> list[dict]:
        probs = self.predict_proba(batch, is_scaled=is_scaled)
        thrs = np.array([self.thresholds[h]["threshold"] for h in range(self.pred_len)])
        out = []
        for i in range(probs.shape[0]):
            exceed = probs[i] >= thrs
            any_hit = bool(exceed.any())
            out.append(
                {
                    "any_alert": any_hit,
                    "first_alert_step": int(np.argmax(exceed)) if any_hit else None,
                    "max_prob": float(probs[i].max()),
                    "max_prob_step": int(np.argmax(probs[i])),
                    "per_horizon": probs[i].tolist(),
                }
            )
        return out


@pytest.fixture()
def client(monkeypatch) -> Iterator[TestClient]:
    # Patch out DB-dependent hooks so we can run without Postgres.
    from apps.api import deps
    from apps.api.routers import iroam as router_mod

    def _fake_get_db() -> Iterator[None]:
        yield None

    monkeypatch.setattr(router_mod, "fetch_trajectories_for_slice", lambda *a, **kw: _seed_rows())
    monkeypatch.setattr(router_mod, "compute_route_stops", lambda *a, **kw: _fake_route_stops())
    monkeypatch.setattr(router_mod, "list_route_catalog", lambda *a, **kw: [])
    monkeypatch.setattr(
        "apps.api.services.forecast.get_predictor", lambda: _StubPredictor()
    )

    app = create_app()
    app.dependency_overrides[deps.get_db] = _fake_get_db
    with TestClient(app) as c:
        yield c


def test_forecast_200_happy_path(client: TestClient):
    # t_ref = 12:12:30 EDT on 2026-04-22 → minute-of-day ≈ 12*60 + 12.5 = 732.5
    # Pick t_ref so buses have ≥60 samples before it.
    t_ref = 12 * 60 + 12.5
    resp = client.get(
        "/iroam/forecast",
        params={
            "route_id": "29",
            "service_date": "2026-04-22",
            "direction_id": 0,
            "t_ref_min": t_ref,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["route_id"] == "29"
    assert body["horizon_steps"] == 30
    assert body["step_seconds"] == 10
    assert len(body["horizon_summary"]["any_alert_rate"]) == 30
    assert len(body["per_bus"]) == 3
    # With the stub ramp (0.1→0.9), the two buses with upstream should alert;
    # the trailing bus has no upstream → ineligible.
    eligible = [b for b in body["per_bus"] if b["eligible"]]
    assert len(eligible) == 2
    for e in eligible:
        assert e["any_alert"] is True
        assert e["first_alert_step"] is not None
        assert 0.0 <= e["max_prob"] <= 1.0


def test_forecast_404_when_route_has_no_shape(client: TestClient, monkeypatch):
    from apps.api.routers import iroam as router_mod

    monkeypatch.setattr(router_mod, "compute_route_stops", lambda *a, **kw: None)

    resp = client.get(
        "/iroam/forecast",
        params={
            "route_id": "zzz",
            "service_date": "2026-04-22",
            "direction_id": 0,
            "t_ref_min": 732.5,
        },
    )
    assert resp.status_code == 404
    assert "no static-GTFS shape" in resp.json()["detail"]


def test_forecast_422_when_t_ref_missing(client: TestClient):
    resp = client.get(
        "/iroam/forecast",
        params={
            "route_id": "29",
            "service_date": "2026-04-22",
            "direction_id": 0,
        },
    )
    assert resp.status_code == 422


def test_forecast_returns_all_ineligible_when_edge_exclude_kills_everyone(client: TestClient):
    # Near end of route — everyone in the first-2/last-2 edge zone.
    resp = client.get(
        "/iroam/forecast",
        params={
            "route_id": "29",
            "service_date": "2026-04-22",
            "direction_id": 0,
            "t_ref_min": 12 * 60 + 12.5,
            "edge_exclude": 20,   # excludes middle too
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["num_eligible"] == 0
    assert all(not b["eligible"] for b in body["per_bus"])
