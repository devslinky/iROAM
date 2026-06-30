"""TripUpdate — one row per FeedEntity.trip_update per snapshot.

Append-only, shared by the bus feed (``trip-updates``) and the subway feed
(``subway-trip-updates``); both carry the same GTFS-RT ``TripUpdate`` message,
so they land in one table discriminated by ``feed_name``.

Per-stop arrival/departure predictions are kept as a compact JSONB array in
``stop_time_updates`` rather than a child table: TTC publishes ~25k stop-time
updates per bus-feed poll, which would be >100M child rows/day at a 20s poll
interval. Epoch times are stored as JSON integers so SQL can cast directly,
e.g. ``(stu->'arrival'->>'time')::bigint``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TripUpdate(Base):
    __tablename__ = "trip_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("raw_gtfsrt_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    feed_name: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    feed_header_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trip_update_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    delay_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # TripDescriptor
    trip_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction_id: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String(8), nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(8), nullable=True)
    schedule_relationship: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # VehicleDescriptor
    vehicle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # StopTimeUpdates, decoded: [{stop_sequence, stop_id, schedule_relationship,
    # arrival: {time, delay, uncertainty}, departure: {...}}, ...]
    stop_time_update_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_time_updates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_tu_trip_fetched", "trip_id", "fetched_at"),
        Index("ix_tu_route_fetched", "route_id", "fetched_at"),
        Index("ix_tu_feed_fetched", "feed_name", "fetched_at"),
        Index("ix_tu_fetched_at", "fetched_at"),
        Index("ix_tu_snapshot", "snapshot_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<TripUpdate id={self.id} feed={self.feed_name} trip={self.trip_id} "
            f"route={self.route_id} stus={self.stop_time_update_count}>"
        )
