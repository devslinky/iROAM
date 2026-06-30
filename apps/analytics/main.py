"""Analytics CLI.

Usage::

    python -m apps.analytics.main --date 2026-04-20
    python -m apps.analytics.main --date 2026-04-20 --route 29
    python -m apps.analytics.main --date 2026-04-20 --export-csv ./out/2026-04-20
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from apps.analytics.runner import run_for_date
from core.config import get_settings
from core.logging import configure_logging, get_logger
from db.session import SessionLocal

_logger = get_logger(__name__)


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _parse_since(s: str) -> datetime:
    """Accept ISO-8601 datetimes; a bare date is treated as midnight UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TTC trip-trajectory analytics run")
    p.add_argument("--date", required=True, type=_parse_date, help="service date (YYYY-MM-DD)")
    p.add_argument("--route", type=str, default=None, help="filter to a single route_id")
    p.add_argument(
        "--upsample-seconds",
        type=int,
        default=None,
        help="upsample resolution (overrides ANALYTICS_UPSAMPLE_RESOLUTION_S)",
    )
    p.add_argument(
        "--max-orthogonal-distance-m",
        type=float,
        default=None,
        help="drop points farther than this from the shape (overrides config)",
    )
    p.add_argument(
        "--export-csv",
        type=Path,
        default=None,
        help="also write {route}_{date}_dir{N}.csv files into this directory",
    )
    p.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="only refresh trip instances with VehiclePosition observations "
        "newer than this ISO-8601 timestamp (used by the worker loop)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = get_settings()

    upsample_s = args.upsample_seconds or settings.analytics_upsample_resolution_s
    max_orth = (
        args.max_orthogonal_distance_m
        if args.max_orthogonal_distance_m is not None
        else settings.analytics_max_orthogonal_distance_m
    )

    with SessionLocal() as session:
        outcome = run_for_date(
            session,
            args.date,
            route_id=args.route,
            upsample_resolution_s=upsample_s,
            max_orthogonal_distance_m=max_orth,
            max_implied_speed_m_s=settings.analytics_max_implied_speed_m_s,
            export_csv_dir=args.export_csv,
            only_changed_since=args.since,
        )

    print(
        f"run_id={outcome.run_id} status={outcome.status} "
        f"trip_instances={outcome.trip_instances_processed} "
        f"rows={outcome.rows_written}"
    )
    return 0 if outcome.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
