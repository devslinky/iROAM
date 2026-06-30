"""add trip_updates and trip_modifications

``trip_updates`` stores TripUpdate entities from both the bus
(``trips/update``) and subway (``trips/subway``) feeds, discriminated by
``feed_name``; per-stop predictions live in a JSONB array (a per-stop child
table would be >100M rows/day at the bus feed's volume). ``trip_modifications``
stores the detour feed's TripModifications entities and their companion
replacement-shape entities, discriminated by ``entity_kind``.

Revision ID: 0006_trip_feeds
Revises: 0005_drop_payload
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_trip_feeds"
down_revision = "0005_drop_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trip_updates",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger,
            sa.ForeignKey("raw_gtfsrt_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feed_name", sa.String(32), nullable=False),
        sa.Column("fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("feed_header_timestamp", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("trip_update_timestamp", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delay_seconds", sa.Integer, nullable=True),
        sa.Column("trip_id", sa.String(64), nullable=True),
        sa.Column("route_id", sa.String(32), nullable=True),
        sa.Column("direction_id", sa.SmallInteger, nullable=True),
        sa.Column("start_date", sa.String(8), nullable=True),
        sa.Column("start_time", sa.String(8), nullable=True),
        sa.Column("schedule_relationship", sa.String(16), nullable=True),
        sa.Column("vehicle_id", sa.String(64), nullable=True),
        sa.Column("vehicle_label", sa.String(64), nullable=True),
        sa.Column("stop_time_update_count", sa.Integer, nullable=False),
        sa.Column("stop_time_updates", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_tu_trip_fetched", "trip_updates", ["trip_id", "fetched_at"])
    op.create_index("ix_tu_route_fetched", "trip_updates", ["route_id", "fetched_at"])
    op.create_index("ix_tu_feed_fetched", "trip_updates", ["feed_name", "fetched_at"])
    op.create_index("ix_tu_fetched_at", "trip_updates", ["fetched_at"])
    op.create_index("ix_tu_snapshot", "trip_updates", ["snapshot_id"])

    op.create_table(
        "trip_modifications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.BigInteger,
            sa.ForeignKey("raw_gtfsrt_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("feed_header_timestamp", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("entity_id", sa.String(256), nullable=False),
        sa.Column("entity_kind", sa.String(24), nullable=False),
        sa.Column("trip_ids", postgresql.JSONB, nullable=True),
        sa.Column("service_dates", postgresql.JSONB, nullable=True),
        sa.Column("start_times", postgresql.JSONB, nullable=True),
        sa.Column("modifications_count", sa.SmallInteger, nullable=True),
        sa.Column("shape_id", sa.String(256), nullable=True),
        sa.Column("encoded_polyline", sa.Text, nullable=True),
        sa.Column("raw_entity", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_tm_kind_fetched", "trip_modifications", ["entity_kind", "fetched_at"])
    op.create_index("ix_tm_fetched_at", "trip_modifications", ["fetched_at"])
    op.create_index("ix_tm_snapshot", "trip_modifications", ["snapshot_id"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trip_modifications CASCADE")
    op.execute("DROP TABLE IF EXISTS trip_updates CASCADE")
