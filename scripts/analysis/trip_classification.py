"""
Diagnostic script that classifies every trip instance for a given
route + date and prints each trip with its classification label.

Usage:
    python3 -m scripts.analysis.trip_classification --route 29 --date 2026-06-18
"""

from __future__ import annotations

import os
os.environ["DATABASE_URL"] = "postgresql+psycopg://ttc:ttc@localhost:5433/ttc_gtfsrt"

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd


def _find_root() -> Path:
    env = os.environ.get("IROAM_ROOT")
    if env and (Path(env) / "apps").exists():
        return Path(env)
    for c in [Path.cwd(), *Path.cwd().parents]:
        if (c / "apps").exists() and (c / "notebooks").exists():
            return c
    return Path.cwd()


def print_trip_classification(clsdf: pd.DataFrame) -> None:
    """Print each trip and its classification label distinctly."""
    print(f"\n{'='*60}")
    print(f"TRIP CLASSIFICATION SUMMARY ({len(clsdf)} total instances)")
    print(f"{'='*60}")
    print(f"{'trip_id':<20} {'start_date':<12} {'static_route':<14} {'kind'}")
    print(f"{'-'*60}")
    for _, row in clsdf.iterrows():
        print(
            f"{str(row['trip_id']):<20} "
            f"{str(row['start_date']):<12} "
            f"{str(row['static_route']):<14} "
            f"{row['kind']}"
        )
    print(f"{'='*60}")
    print(
        f"match={len(clsdf[clsdf['kind']=='match'])}  "
        f"mismatch={len(clsdf[clsdf['kind']=='mismatch'])}  "
        f"none={len(clsdf[clsdf['kind']=='none'])}"
    )
    print(f"{'='*60}\n")


def classify_trips(route: str, service_date: date) -> pd.DataFrame:
    """Classify every trip instance for a route+date against the static bundle."""
    from apps.analytics import pipeline
    from apps.analytics.gtfs_static import load_all, resolve_route_id
    from core.config import get_settings
    from db.session import SessionLocal
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine

    # build a local engine pointing at localhost:5433
    get_settings.cache_clear()
    DB_URL = "postgresql+psycopg://ttc:ttc@localhost:5433/ttc_gtfsrt"
    engine = create_engine(DB_URL, connect_args={"connect_timeout": 5})
    _factory = sessionmaker(bind=engine, autoflush=False, autocommit=False,
                            expire_on_commit=False, future=True)

    # load static GTFS
    static = load_all()
    fi = pd.read_csv(Path("Complete GTFS") / "feed_info.txt", dtype=str).iloc[0]
    bundle_tag = str(fi.get("feed_version", "unknown"))
    print(f"Bundle: {bundle_tag}")

    # get all trip instances for this date
    with _factory() as s:
        insts = pipeline.list_trip_instances(s, service_date, route_id=route)

    # classify each one
    rows = []
    for tid, st in insts:
        rr = resolve_route_id(static, tid)
        kind = "match" if str(rr) == route else ("none" if rr is None else "mismatch")
        rows.append({
            "trip_id":      tid,
            "start_date":   st,
            "static_route": str(rr) if rr is not None else None,
            "kind":         kind,
        })

    return pd.DataFrame(rows, columns=["trip_id", "start_date", "static_route", "kind"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m scripts.analysis.trip_classification",
        description="Classify every trip instance for a route+date and print results",
    )
    p.add_argument("--route", required=True, help="GTFS route_id e.g. 29")
    p.add_argument("--date",  required=True, help="service date YYYY-MM-DD")
    p.add_argument("--kind",  default=None,
                   choices=["match", "mismatch", "none"],
                   help="filter output to only show trips of this kind")
    args = p.parse_args(argv)

    # set up repo root
    ROOT = _find_root()
    assert (ROOT / "apps").exists(), (
        f"repo root not found from cwd={Path.cwd()} — set IROAM_ROOT=/path/to/iROAM"
    )
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    service_date = date.fromisoformat(args.date)
    clsdf = classify_trips(args.route, service_date)

    # optionally filter to one kind
    if args.kind:
        clsdf = clsdf[clsdf["kind"] == args.kind].reset_index(drop=True)
        print(f"\n(showing only '{args.kind}' trips)")

    print_trip_classification(clsdf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())