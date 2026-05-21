"""drop raw_gtfsrt_snapshots.payload

Removes the raw protobuf bytes column. Re-normalization now relies on the
decoded per-entity JSON kept in ``vehicle_positions.raw_entity``; fetch
metadata lives in ``feed_fetch_logs``. The snapshot row is retained as the
lightweight link target for ``vehicle_positions.snapshot_id`` and still
carries ``content_sha256`` so the "feed reachable but stuck" signal
(identical hash across consecutive polls) remains observable.

``payload`` was the dominant on-disk cost (~700 KB/row, ~2 GB/day). Dropping
it trades byte-identical raw replay — a feature that was never wired up — for
a flat, lean snapshots table.

Revision ID: 0005_drop_payload
Revises: 0004_tt_unique
Create Date: 2026-05-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_drop_payload"
down_revision = "0004_tt_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("raw_gtfsrt_snapshots", "payload")


def downgrade() -> None:
    # Re-added nullable — historical bytes cannot be recovered.
    op.add_column(
        "raw_gtfsrt_snapshots",
        sa.Column("payload", sa.LargeBinary(), nullable=True),
    )
