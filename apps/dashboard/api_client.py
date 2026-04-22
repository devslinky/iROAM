"""Thin HTTP client the Streamlit pages use to talk to the FastAPI backend.

Keeping all HTTP access here means individual pages stay focused on rendering,
and the client is the only thing that needs to change if the API contract
moves.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import requests


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


def _base_url() -> str:
    return os.environ.get("DASHBOARD_API_BASE_URL", "http://localhost:8000").rstrip("/")


def _get(path: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
    url = f"{_base_url()}{path}"
    response = requests.get(url, params=params, timeout=timeout)
    if response.status_code >= 400:
        try:
            msg = response.json().get("detail", response.text)
        except Exception:
            msg = response.text
        raise APIError(response.status_code, str(msg))
    return response.json()


def health() -> dict[str, Any]:
    return _get("/health")


def feed_status_vehicle_positions() -> dict[str, Any]:
    return _get("/feed-status/vehicle-positions")


def vehicles_latest(
    *,
    route_id: str | None = None,
    minutes: int = 5,
    limit: int | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"minutes": minutes}
    if route_id:
        params["route_id"] = route_id
    if limit is not None:
        params["limit"] = limit
    if include_raw:
        params["include"] = "raw"
    return _get("/vehicles/latest", params=params)


def vehicle_latest(vehicle_id: str, *, include_raw: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if include_raw:
        params["include"] = "raw"
    return _get(f"/vehicles/{vehicle_id}/latest", params=params or None)


def vehicle_history(
    vehicle_id: str,
    *,
    start: datetime,
    end: datetime,
    limit: int | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if limit is not None:
        params["limit"] = limit
    if include_raw:
        params["include"] = "raw"
    return _get(f"/vehicles/{vehicle_id}/history", params=params)


def routes_index(minutes: int = 15) -> list[str]:
    return _get("/routes", params={"minutes": minutes})


def route_vehicles_latest(
    route_id: str,
    *,
    minutes: int = 5,
    limit: int | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"minutes": minutes}
    if limit is not None:
        params["limit"] = limit
    if include_raw:
        params["include"] = "raw"
    return _get(f"/routes/{route_id}/vehicles/latest", params=params)


def route_metrics(route_id: str, *, window_minutes: int = 15) -> dict[str, Any]:
    return _get(
        f"/routes/{route_id}/metrics",
        params={"window_minutes": window_minutes},
    )


def trajectory_service_dates(limit: int = 30) -> list[str]:
    return _get("/trajectories/service-dates", params={"limit": limit})


def trajectory_routes(service_date: str) -> list[str]:
    return _get("/trajectories/routes", params={"service_date": service_date})


def trajectory_trips(
    service_date: str,
    *,
    route_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"service_date": service_date, "limit": limit}
    if route_id:
        params["route_id"] = route_id
    return _get("/trajectories/trips", params=params)


def trajectory_detail(
    trip_id: str,
    *,
    start_date: str,
    include_shape: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {"start_date": start_date}
    if include_shape:
        params["include"] = "shape"
    return _get(f"/trajectories/trips/{trip_id}", params=params)


def replay_vehicles(
    *,
    start: datetime,
    end: datetime,
    route_id: str | None = None,
    limit: int | None = None,
    include_raw: bool = False,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if route_id:
        params["route_id"] = route_id
    if limit is not None:
        params["limit"] = limit
    if include_raw:
        params["include"] = "raw"
    return _get("/replay/vehicles", params=params)
