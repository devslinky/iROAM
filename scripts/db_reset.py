"""Destructive data reset — empty every data table in one transaction.

Leaves the schema (and ``alembic_version``) untouched so migrations don't
have to be re-applied. Intended for dev/demo resets and for kicking off
a fresh analytics run after a scope change (e.g. a new route allowlist).

Safety:

* Requires the explicit ``--yes-i-am-sure`` flag. Without it, the script
  prints the row counts it *would* delete and exits with code 2.
* Runs a single ``TRUNCATE ... RESTART IDENTITY CASCADE`` so child rows
  (stop_times on trip_updates, analytics rows on trajectories, etc.) are
  cleared atomically and sequences reset to 1.

Usage (dev):

    docker compose exec api python -m scripts.db_reset            # dry-run
    docker compose exec api python -m scripts.db_reset --yes-i-am-sure
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import text

from core.logging import configure_logging, get_logger
from db.session import SessionLocal

_logger = get_logger(__name__)

# Ordered from most-derived to most-upstream for human readability. The
# TRUNCATE itself uses CASCADE so order does not affect correctness.
DATA_TABLES: tuple[str, ...] = (
    "trip_trajectories",
    "analytics_runs",
    "vehicle_positions",
    "trip_updates",
    "trip_modifications",
    "raw_gtfsrt_snapshots",
    "feed_fetch_logs",
)


def _row_counts(tables: Sequence[str]) -> dict[str, int]:
    with SessionLocal() as session:
        return {
            t: session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
            for t in tables
        }


def _truncate(tables: Sequence[str]) -> None:
    # CASCADE so FK-dependent rows in unlisted tables (if any are added later)
    # don't block the reset. RESTART IDENTITY so re-ingested rows start at id=1.
    stmt = f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"
    with SessionLocal() as session:
        session.execute(text(stmt))
        session.commit()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--yes-i-am-sure",
        action="store_true",
        help="Actually run the TRUNCATE. Without this, the script only prints "
        "current row counts and exits with code 2.",
    )
    args = parser.parse_args(argv)

    before = _row_counts(DATA_TABLES)
    total = sum(before.values())
    for t in DATA_TABLES:
        _logger.info("db_reset_before", extra={"table": t, "rows": before[t]})

    if not args.yes_i_am_sure:
        print("DRY RUN — pass --yes-i-am-sure to actually truncate these tables:")
        for t in DATA_TABLES:
            print(f"  {t:<24} {before[t]:>12,} rows")
        print(f"  {'TOTAL':<24} {total:>12,} rows")
        return 2

    _logger.warning("db_reset_truncate_begin", extra={"total_rows": total})
    _truncate(DATA_TABLES)
    after = _row_counts(DATA_TABLES)
    for t in DATA_TABLES:
        _logger.info("db_reset_after", extra={"table": t, "rows": after[t]})
    _logger.warning("db_reset_truncate_done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
