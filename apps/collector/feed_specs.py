"""Feed registry.

One ``FeedSpec`` per GTFS-RT feed the collector knows how to ingest. Adding
a new feed (e.g. alerts) is: write a normalizer, register a spec here. The
runner is feed-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Callable

from apps.collector import gtfs_realtime_pb2
from apps.collector.normalize_trips import (
    normalize_trip_modifications,
    normalize_trip_updates,
)
from apps.collector.normalize_vehicles import normalize_vehicle_positions
from core.config import get_settings
from core.constants import (
    FEED_SUBWAY_TRIP_UPDATES,
    FEED_TRIP_MODIFICATIONS,
    FEED_TRIP_UPDATES,
    FEED_VEHICLE_POSITIONS,
)
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

    The route allowlist applies to the bus-side feeds (vehicle positions and
    bus trip updates). The subway feed is left unfiltered: its route_ids
    (1/2/4/5) name subway lines, not bus routes, so a bus allowlist would
    silently drop every subway entity.
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
        FEED_TRIP_UPDATES: FeedSpec(
            name=FEED_TRIP_UPDATES,
            url=settings.gtfs_rt_trip_updates_url,
            normalize=partial(
                normalize_trip_updates,
                feed_name=FEED_TRIP_UPDATES,
                route_allowlist=allowlist or None,
            ),
        ),
        FEED_SUBWAY_TRIP_UPDATES: FeedSpec(
            name=FEED_SUBWAY_TRIP_UPDATES,
            url=settings.gtfs_rt_subway_trip_updates_url,
            normalize=partial(
                normalize_trip_updates,
                feed_name=FEED_SUBWAY_TRIP_UPDATES,
            ),
        ),
        FEED_TRIP_MODIFICATIONS: FeedSpec(
            name=FEED_TRIP_MODIFICATIONS,
            url=settings.gtfs_rt_trip_modifications_url,
            normalize=normalize_trip_modifications,
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
    "normalize_trip_updates",
    "normalize_trip_modifications",
    "datetime",  # typing re-export
]
