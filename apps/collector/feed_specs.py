"""Feed registry.

One ``FeedSpec`` per GTFS-RT feed the collector knows how to ingest. Adding
a new feed (e.g. trip-updates, alerts) is: write a normalizer, register a
spec here. The runner is feed-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Callable

from google.transit import gtfs_realtime_pb2

from apps.collector.normalize_vehicles import normalize_vehicle_positions
from core.config import get_settings
from core.constants import FEED_VEHICLE_POSITIONS
from db.base import Base

#: Shape of a feed-specific normalizer. Pure — no DB side effects.
Normalizer = Callable[
    [gtfs_realtime_pb2.FeedMessage],
    list[Base],
]


@dataclass(frozen=True)
class FeedSpec:
    """Declarative wiring for one feed: how to name it, where to get it, how to normalize it."""

    name: str
    url: str
    normalize: Callable[..., list[Base]]


def build_registry() -> dict[str, FeedSpec]:
    """Build the spec registry from current settings.

    Re-reading settings on each call keeps tests and env-override flows
    simple without having to invalidate module-level state.
    """
    settings = get_settings()
    allowlist = settings.route_allowlist_set
    vp_normalize = (
        partial(normalize_vehicle_positions, route_allowlist=allowlist)
        if allowlist
        else normalize_vehicle_positions
    )
    return {
        FEED_VEHICLE_POSITIONS: FeedSpec(
            name=FEED_VEHICLE_POSITIONS,
            url=settings.gtfs_rt_vehicle_positions_url,
            normalize=vp_normalize,
        ),
    }


def get_spec(feed_name: str) -> FeedSpec:
    """Look up a spec or raise ``KeyError`` with the known-feed list."""
    registry = build_registry()
    try:
        return registry[feed_name]
    except KeyError as exc:
        known = ", ".join(sorted(registry))
        raise KeyError(f"unknown feed {feed_name!r}; known: {known}") from exc


__all__ = [
    "FeedSpec",
    "Normalizer",
    "build_registry",
    "get_spec",
    # Re-export for callers that want to pass the callable directly.
    "normalize_vehicle_positions",
    "datetime",  # typing re-export
]
