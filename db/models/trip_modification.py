"""TripModification — one row per detour-feed FeedEntity per snapshot.

The TTC detour feed pairs each ``TripModifications`` entity (which trips are
detoured, on which service dates, between which stops) with a ``Shape`` entity
carrying the replacement shape as an encoded polyline. Both land here,
discriminated by ``entity_kind``; a modification's ``selected_trips.shape_id``
matches the shape row's ``shape_id``.

The feed is small (~40 entities) and slow-moving, so the full decoded entity
is kept in ``raw_entity`` and only the join/filter paths are pulled into
columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

#: ``entity_kind`` values.
KIND_TRIP_MODIFICATIONS = "trip_modifications"
KIND_SHAPE = "shape"


class TripModification(Base):
    __tablename__ = "trip_modifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("raw_gtfsrt_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    feed_header_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # TTC detour entity ids embed the human-readable detour name and run long.
    entity_id: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(24), nullable=False)

    # TripModifications entities: flattened across selected_trips groups.
    trip_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    service_dates: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    start_times: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    modifications_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # Shape entities: the replacement shape for a detour.
    shape_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    encoded_polyline: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Full per-entity JSON; the feed is tiny so fidelity is cheap here.
    raw_entity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        Index("ix_tm_kind_fetched", "entity_kind", "fetched_at"),
        Index("ix_tm_fetched_at", "fetched_at"),
        Index("ix_tm_snapshot", "snapshot_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<TripModification id={self.id} kind={self.entity_kind} "
            f"entity={self.entity_id[:48]!r}>"
        )
