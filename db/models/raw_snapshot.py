"""GTFS-RT snapshot metadata, one row per successful fetch.

A lightweight link target for ``vehicle_positions.snapshot_id``. The raw
protobuf bytes are no longer stored (migration 0005) — re-normalization
relies on the decoded per-entity JSON in ``vehicle_positions.raw_entity``.
``content_sha256`` is retained so an unchanged feed (identical hash across
consecutive polls) stays observable without keeping the payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from db.models.feed_fetch_log import FeedFetchLog


class RawGtfsrtSnapshot(Base):
    __tablename__ = "raw_gtfsrt_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    fetch_log_id: Mapped[int] = mapped_column(
        ForeignKey("feed_fetch_logs.id", ondelete="CASCADE"),
        nullable=False,
    )
    feed_name: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    feed_header_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    gtfs_realtime_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    incrementality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    fetch_log: Mapped["FeedFetchLog"] = relationship(back_populates="snapshot")

    __table_args__ = (
        UniqueConstraint("fetch_log_id", name="uq_snapshots_fetch_log_id"),
        Index("ix_snapshots_feed_fetched_desc", "feed_name", "fetched_at"),
        Index("ix_snapshots_content_sha256", "content_sha256"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<RawGtfsrtSnapshot id={self.id} feed={self.feed_name} "
            f"sha256={self.content_sha256[:12] if self.content_sha256 else None}>"
        )
