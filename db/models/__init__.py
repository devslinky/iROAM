"""ORM model package.

Importing this package registers every table on ``Base.metadata``.
"""

from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot
from db.models.trip_modification import TripModification
from db.models.trip_trajectory import AnalyticsRun, TripTrajectory
from db.models.trip_update import TripUpdate
from db.models.vehicle_position import VehiclePosition

__all__ = [
    "AnalyticsRun",
    "FeedFetchLog",
    "RawGtfsrtSnapshot",
    "TripModification",
    "TripTrajectory",
    "TripUpdate",
    "VehiclePosition",
]
