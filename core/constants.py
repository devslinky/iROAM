"""Shared constants.

VehiclePositions is the canonical feed. Adding another GTFS-RT feed (alerts)
is a one-line change here plus a new ``FeedSpec`` and normalizer in
``apps/collector/feed_specs.py``.
"""

from __future__ import annotations

from typing import Final

FEED_VEHICLE_POSITIONS: Final[str] = "vehicle-positions"
FEED_TRIP_UPDATES: Final[str] = "trip-updates"
FEED_SUBWAY_TRIP_UPDATES: Final[str] = "subway-trip-updates"
FEED_TRIP_MODIFICATIONS: Final[str] = "trip-modifications"
FEED_ALERTS: Final[str] = "alerts"  # future

CANONICAL_FEED: Final[str] = FEED_VEHICLE_POSITIONS

KNOWN_FEEDS: Final[tuple[str, ...]] = (
    FEED_VEHICLE_POSITIONS,
    FEED_TRIP_UPDATES,
    FEED_SUBWAY_TRIP_UPDATES,
    FEED_TRIP_MODIFICATIONS,
    FEED_ALERTS,
)

# VehiclePosition.VehicleStopStatus enum names (stored as strings).
VSS_INCOMING_AT: Final[str] = "INCOMING_AT"
VSS_STOPPED_AT: Final[str] = "STOPPED_AT"
VSS_IN_TRANSIT_TO: Final[str] = "IN_TRANSIT_TO"

# VehiclePosition.OccupancyStatus enum names (stored as strings).
OCC_EMPTY: Final[str] = "EMPTY"
OCC_MANY_SEATS_AVAILABLE: Final[str] = "MANY_SEATS_AVAILABLE"
OCC_FEW_SEATS_AVAILABLE: Final[str] = "FEW_SEATS_AVAILABLE"
OCC_STANDING_ROOM_ONLY: Final[str] = "STANDING_ROOM_ONLY"
OCC_CRUSHED_STANDING_ROOM_ONLY: Final[str] = "CRUSHED_STANDING_ROOM_ONLY"
OCC_FULL: Final[str] = "FULL"
OCC_NOT_ACCEPTING_PASSENGERS: Final[str] = "NOT_ACCEPTING_PASSENGERS"
OCC_NO_DATA_AVAILABLE: Final[str] = "NO_DATA_AVAILABLE"
OCC_NOT_BOARDABLE: Final[str] = "NOT_BOARDABLE"

# CongestionLevel enum names.
CL_UNKNOWN: Final[str] = "UNKNOWN_CONGESTION_LEVEL"
CL_RUNNING_SMOOTHLY: Final[str] = "RUNNING_SMOOTHLY"
CL_STOP_AND_GO: Final[str] = "STOP_AND_GO"
CL_CONGESTION: Final[str] = "CONGESTION"
CL_SEVERE_CONGESTION: Final[str] = "SEVERE_CONGESTION"

# TripDescriptor.ScheduleRelationship enum names.
TRIP_SR_SCHEDULED: Final[str] = "SCHEDULED"
TRIP_SR_ADDED: Final[str] = "ADDED"
TRIP_SR_UNSCHEDULED: Final[str] = "UNSCHEDULED"
TRIP_SR_CANCELED: Final[str] = "CANCELED"
TRIP_SR_DUPLICATED: Final[str] = "DUPLICATED"
