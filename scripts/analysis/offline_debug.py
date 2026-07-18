# IMPORTS ##################################################
from __future__ import annotations
import os
DB_URL = "postgresql+psycopg://ttc:ttc@localhost:5433/ttc_gtfsrt"
os.environ["DATABASE_URL"] = DB_URL
import argparse
import decimal
import hashlib
import json
import pickle
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pandas as pd
from db.queries.iroam import fetch_trajectories_for_slice
from db.queries.vehicles import fetch_by_trip_instance
from sqlalchemy import create_engine, text
from apps.analytics.gtfs_static import load_shape_linestrings, resolve_route_id
from apps.analytics import pipeline
from apps.analytics.gtfs_static import resolve_shape_id
from apps.analytics.project_to_shape import project_trajectory
from apps.analytics.gtfs_static import load_all
from core.config import get_settings
from apps.analytics.upsample import compute_moving_speed, upsample_df
from apps.analytics.stop_projection import compute_route_stops
from apps.api.services.bus_grouping import group_into_buses
from db.models.trip_trajectory import TripTrajectory
from data_process.bunching.labels import extract_for_date, extract_labelled_examples
from apps.analytics.trajectory_extract import build_trip_trajectory
from apps.analytics.schedule_headways import scheduled_headway_s
from apps.api.services.bus_grouping import group_into_buses
import numpy as np
###########################################################

# data engineering/processing functions ###################

def _find_root() -> Path:
    """Locate root of the iROAM repo."""

    env = os.environ.get("IROAM_ROOT")
    if env and (Path(env) / "apps").exists():
        return Path(env)
    for c in [Path.cwd(), *Path.cwd().parents]:
        if (c / "apps").exists() and (c / "notebooks").exists():
            return c
    return Path.cwd()

def live_cache_DATAMODE_switch():
    """Determine whether to use live data or cached data based on the AUDIT_DATA_MODE environment variable."""

    DATA_MODE = os.environ.get("AUDIT_DATA_MODE", "live").lower()
    assert DATA_MODE in {"live", "cache"}, DATA_MODE
    return DATA_MODE

def _probe_db(DATA_MODE, ENGINE) -> bool:
    """Check if the database is reachable. Returns True if reachable, False otherwise."""

    if DATA_MODE == "cache":
        return False
    try:
        with ENGINE.connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception as e:
        print(f"⚠ DB unreachable ({type(e).__name__}) — replaying from cache where possible")
        return False


# cache helper functions
def _norm(v):   # type-stable param normalization so live/cache derive the SAME key
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):              # numpy scalar → python scalar
        return v.item()
    return v

def _key(sql: str, params: dict) -> str:
    canon = " ".join(sql.split()) + "|" + repr(sorted((k, _norm(v)) for k, v in params.items()))
    return hashlib.sha256(canon.encode()).hexdigest()[:24]

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data); os.replace(tmp, path)   # rename is atomic → no torn files



def q(sql: str, CACHE, OFFLINE, ENGINE, CACHE_WRITE_FAILURES, **params) -> pd.DataFrame:
    """Read-only SQL → DataFrame, transparently cached under out/qa/cache/.

    live: query the DB, write the result to cache. offline: replay from cache
    (raises with a hint if this exact query was never primed / cache is corrupt).
    """
    pq = CACHE / f"{_key(sql, params)}.parquet"
    if OFFLINE:
        if pq.exists():
            try:
                return pd.read_parquet(pq)
            except Exception as e:
                raise RuntimeError(f"corrupt cache file {pq.name} ({type(e).__name__}) — "
                                   "delete it and re-prime with DATA_MODE='live'") from e
        raise RuntimeError("offline and no cached result for:\n  "
                           + " ".join(sql.split())[:160] + f"\n  params={params}\n"
                           "→ run the notebook once with DATA_MODE='live' to prime the cache")
    df = pd.read_sql(text(sql), ENGINE, params=params)
    for c in df.columns:   # Decimal (from avg() etc.) → float: parquet-safe + plottable
        if df[c].dtype == object:
            head = df[c].dropna().head(50)
            if len(head) and all(isinstance(v, decimal.Decimal) for v in head):
                df[c] = df[c].astype(float)
    try:
        tmp = pq.with_suffix(".parquet.tmp"); df.to_parquet(tmp); os.replace(tmp, pq)
        (pq.with_suffix(".sql")).write_text(" ".join(sql.split()) + f"\nparams={params!r}\n")
    except Exception as e:
        CACHE_WRITE_FAILURES.append((pq.name, f"{type(e).__name__}: {e}"))
        print(f"⚠ cache write skipped ({pq.name}): {type(e).__name__}: {e}")
    return df

# object saver/loader using pickle files
def save_obj(name: str, obj, CACHE) -> None:
    _atomic_write_bytes(CACHE / f"{name}.pkl", pickle.dumps(obj))

def load_obj(name: str, CACHE, default=None):
    p = CACHE / f"{name}.pkl"
    if not p.exists():
        return default
    try:
        return pickle.loads(p.read_bytes())
    except Exception as e:   # truncated/corrupt or class moved: fall back to recompute path
        print(f"⚠ could not load {p.name} ({type(e).__name__}) — treating as unprimed")
        return default

# ORM data row saver/loader
class _Row:
    def __init__(self, d): self.__dict__.update(d)

def _rows_to_df(rows, cols) -> pd.DataFrame:
    return pd.DataFrame([{c: getattr(r, c) for c in cols} for r in rows])

def _df_to_rows(df: pd.DataFrame) -> list:
    recs = df.to_dict("records")
    for r in recs:
        for k, v in r.items():
            if v is pd.NaT or (isinstance(v, float) and pd.isna(v)):
                r[k] = None
    return [_Row(r) for r in recs]

# manifest read/write
def read_manifest(MANIFEST):
    return json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}

def write_manifest(MANIFEST, d):
    _atomic_write_bytes(MANIFEST, json.dumps(d, indent=2, default=str).encode())

def fetch_trip_rows(tid, st, CACHE, OFFLINE, SessionLocal, pipeline, _VP_COLS) -> list:
    "fetch_by_trip_instance (production) with a parquet-backed offline replay."
    pq = CACHE / f"vprows_{tid}_{st}.parquet"
    if OFFLINE:
        if pq.exists():
            return _df_to_rows(pd.read_parquet(pq))
        raise RuntimeError(f"offline: no cached rows for trip {tid}/{st}")
    # Mirror process_trip_instance: bound the scan by the same fetched_at window
    # (indexed; excludes >6h-skew ghost-broadcast rows production drops).
    window = None
    try:
        window = pipeline._fetched_at_window(date(int(st[:4]), int(st[4:6]), int(st[6:8])))
    except (ValueError, TypeError, IndexError):
        pass
    with SessionLocal() as s:
        rows = fetch_by_trip_instance(s, tid, st, fetched_at_window=window)
    _rows_to_df(rows, _VP_COLS).to_parquet(pq)
    return rows

def classify(sd, route, BUNDLE_TAG, OFFLINE,SessionLocal,pipeline,STATIC, CACHE):
    "Per-trip static resolution for one service date: match / mismatch / none."
    # Key includes BUNDLE_TAG: resolutions are bundle-derived, so a refreshed
    # bundle must not serve the previous board period's pickle (gtfs_static's
    # caching contract). Cache is a pure offline-replay mirror — LIVE always
    # recomputes and overwrites, so fixing resolve_* + re-running reflects it.
    name = f"classify_{route}_{sd}_{BUNDLE_TAG}"
    if OFFLINE:
        cached = load_obj(name, CACHE=CACHE)
        if cached is not None:
            return cached
        raise RuntimeError(f"offline: classify({sd}) not primed for bundle {BUNDLE_TAG} — run once with DATA_MODE='live'")
    with SessionLocal() as s:
        insts = pipeline.list_trip_instances(s, sd, route_id=route)
    rows = []
    for tid, st in insts:
        rr = resolve_route_id(STATIC, tid)
        kind = "match" if str(rr)==route else ("none" if rr is None else "mismatch")
        rows.append((tid, st, rr, kind))
    out = pd.DataFrame(rows, columns=["trip_id","start_date","static_route","kind"])
    save_obj(name, out, CACHE=CACHE)
    return out

def fetch_slice_rows(sd, direction, ROUTE, CACHE, OFFLINE, SessionLocal, _TT_COLS) -> list:
    "fetch_trajectories_for_slice (production) with a parquet-backed offline replay."
    pq = CACHE / f"slice_{ROUTE}_{direction}_{sd}.parquet"
    if OFFLINE:
        if pq.exists():
            return _df_to_rows(pd.read_parquet(pq))
        raise RuntimeError(f"offline: slice {sd}/dir{direction} not primed")
    with SessionLocal() as s:
        rows = fetch_trajectories_for_slice(s, service_date=sd, route_id=ROUTE, direction_id=direction)
    _rows_to_df(rows, _TT_COLS).to_parquet(pq)
    return rows

# Derive the cached column set from the model (minus the internal FK/PK cols) so
# the offline shim can't silently lag a schema change group_into_buses depends on.
def _tt_cols() -> list[str]:
    return [c.key for c in TripTrajectory.__table__.columns if c.key not in {"id", "run_id"}]


def pick_happy_trip(start_date, ROUTE, SessionLocal, pipeline, ctx, min_pts=60):
    misses = 0
    
    m = read_manifest(ctx.MANIFEST)
    BUNDLE_TAG = str(m["bundle_version"])

    cl = classify(sd = start_date,route = ROUTE, BUNDLE_TAG = BUNDLE_TAG, OFFLINE = ctx.OFFLINE, SessionLocal = SessionLocal, STATIC = ctx.STATIC, pipeline = pipeline, CACHE = ctx.CACHE)
    
    for tid, st in cl[cl.kind=="match"][["trip_id","start_date"]].itertuples(index=False):
        sid = resolve_shape_id(ctx.STATIC, tid)

        if sid not in ctx.SHAPE_LINES:
            continue
        try:
            rws = fetch_trip_rows(tid, st, CACHE = ctx.CACHE, OFFLINE = ctx.OFFLINE, SessionLocal = SessionLocal, pipeline = pipeline, _VP_COLS = _VP_COLS)
        except RuntimeError:
            misses += 1; continue      # offline: rows for this candidate weren't primed
        df = build_trip_trajectory(rws, ctx.STATIC.trips)
        if len(df) >= min_pts:
            print(f"picked happy trip {tid} for {ROUTE}/{start_date} ({len(df)} points)")
            return tid
    raise RuntimeError(f"no happy trip found for {ROUTE}/{start_date} (missed {misses} candidates)")

#####################################################
# replay core stages #

def setup(data_mode: str = "cache", db_url: str | None = None) -> SimpleNamespace:

    # set up working directory
    ROOT = _find_root()
    assert (ROOT / "apps").exists(), (
        f"repo root not found from cwd={Path.cwd()} — set IROAM_ROOT=/path/to/iroam_final")
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    # set up Cache and Manifest directories
    OUT = ROOT / "out" / "qa"; CACHE = OUT / "cache"
    OUT.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)
    MANIFEST = CACHE / "manifest.json"

    # set up DB engine
    ENGINE = create_engine(DB_URL, connect_args={"connect_timeout": 5})

    from db.session import SessionLocal as _SessionLocal

    # live/cache data mode switch + probe DB connectivity
    os.environ["AUDIT_DATA_MODE"] = data_mode
    DATA_MODE = live_cache_DATAMODE_switch()
    DB_OK = _probe_db(DATA_MODE = DATA_MODE, ENGINE = ENGINE)
    OFFLINE = (DATA_MODE == "cache") or not DB_OK

    CACHE_WRITE_FAILURES = []

    return SimpleNamespace(ROOT=ROOT, OUT=OUT, CACHE=CACHE, MANIFEST=MANIFEST, ENGINE=ENGINE, DATA_MODE=DATA_MODE, OFFLINE=OFFLINE, CACHE_WRITE_FAILURES=CACHE_WRITE_FAILURES, SessionLocal=_SessionLocal)


def _load_static(ctx: SimpleNamespace):
    """Lazy-load GtfsStatic + shape linestrings (expensive; load once)."""
    STATIC = load_all()
    SHAPE_LINES = load_shape_linestrings()
    ctx.STATIC      = STATIC
    ctx.SHAPE_LINES = SHAPE_LINES
    return STATIC, SHAPE_LINES

_VP_COLS = ["id","trip_id","route_id","direction_id","start_date","start_time","vehicle_id",
            "vehicle_timestamp","fetched_at","latitude","longitude","occupancy_status"]

def _stage_extract(tid: str, st: str, ctx: SimpleNamespace) -> pd.DataFrame:
    """extract: vehicle_positions rows → build_trip_trajectory DataFrame."""

    rows = fetch_trip_rows(
        tid, st,
        CACHE=ctx.CACHE, OFFLINE=ctx.OFFLINE,
        SessionLocal=ctx.SessionLocal, pipeline=pipeline,
        _VP_COLS=_VP_COLS,
    )
    if not rows:
        raise RuntimeError(f"no vehicle_positions rows for {tid}/{st}")
    
    STATIC, _ = _load_static(ctx) if not hasattr(ctx, "STATIC") else (ctx.STATIC, ctx.SHAPE_LINES)

    df = build_trip_trajectory(rows,STATIC.trips)

    if df.empty:
        raise RuntimeError(f"build_trip_trajectory returned empty DataFrame for {tid}/{st}")
    return df

def _stage_project(tid: str, st: str, ctx: SimpleNamespace, trip_trajectory: pd.DataFrame | None = None) -> pd.DataFrame:
    """project: project_trajectory."""

    if trip_trajectory is None:
        trip_trajectory = _stage_extract(tid, st, ctx)

    STATIC, SHAPE_LINES = _load_static(ctx) if not hasattr(ctx, "STATIC") else (ctx.STATIC, ctx.SHAPE_LINES)
 
    sid = resolve_shape_id(STATIC, tid)
    if sid is None or sid not in SHAPE_LINES:
        raise RuntimeError(f"no shape for {tid}")
    
    return project_trajectory(trip_trajectory, SHAPE_LINES[sid])

def _stage_upsample(tid: str, st: str, ctx: SimpleNamespace, res = None, projected_trajectory = None) -> pd.DataFrame:
    """upsample: project → compute_moving_speed → upsample_df."""

    if res is None:
        res = get_settings().analytics_upsample_resolution_s
 
    df = projected_trajectory if projected_trajectory is not None else _stage_project(tid, st, ctx)
    df = compute_moving_speed(df)
    df["observed"] = True
    return upsample_df(df, res)


def _stage_group(sd: date, route: str, direction: int, ctx: SimpleNamespace, view_buses_as_flat_df: bool = False) -> pd.DataFrame:
    """group: fetch_slice_rows → group_into_buses → flat DataFrame."""
 
    rows = fetch_slice_rows(
        sd, direction, route,
        CACHE=ctx.CACHE, OFFLINE=ctx.OFFLINE,
        SessionLocal=ctx.SessionLocal,
        _TT_COLS=_tt_cols(),
    )
    route_stops = compute_route_stops(route, direction)

    assert rows and route_stops is not None, f"no slice rows or route stops for {sd}/{route}/dir{direction}"

    buses = group_into_buses(rows, route_stops)

    # if we want a flat DataFrame of all the bus points, we can construct it here
    if view_buses_as_flat_df:
        records = []
        for bus in buses:
            for pt in bus.points:
                records.append({
                    "trip_id":          bus.trip_id,
                    "vehicle_id":       bus.vehicle_id,
                    "datetime":         pt.datetime,
                    "stop_index":       pt.stop_index,
                    "moving_speed_m_s": pt.moving_speed_m_s,
                })
        return pd.DataFrame(records)
    else: return buses

def _stage_label(sd: date, route: str, direction: int, ctx: SimpleNamespace, view_examples_as_df:bool = False):
    """label: extract_for_date from data_process/bunching/labels.py."""
   
    route_stops,buses = return_group(sd, route, direction, ctx, view_buses_as_flat_df=False)

    sched_by_trip: dict[str, float] = {}
    for bus in buses:
        if bus.trip_id in sched_by_trip:
            continue
        hw = scheduled_headway_s(bus.trip_id, route, direction, sd)
        if hw is not None:
            sched_by_trip[bus.trip_id] = hw
    
    examples = extract_labelled_examples(buses,route_id=route,direction_id=direction,service_date=sd,num_stops=len(route_stops.stops),
        step_seconds=60,seq_len=20,pred_len= 30,edge_exclude=2,route_shape_length_m=float(route_stops.shape_length_m),
        extras_schema_v=4,terminal_mask=True,persist_ticks=2,sched_headway_by_trip=sched_by_trip or None)
        
    # if we want a flat DataFrame of the labelled examples, we can construct it here
    if view_examples_as_df:
        rows = []
        for ex in examples:
            rows.append({
                "service_date":       ex.service_date,
                "route_id":           ex.route_id,
                "direction_id":       ex.direction_id,
                "trip_id":            ex.trip_id,
                "start_date":         ex.start_date,
                "vehicle_id":         ex.vehicle_id,
                "bus_index":          ex.bus_index,
                "t_ref_min":          ex.t_ref_min,
                "stop_idx_at_ref":    ex.stop_idx_at_ref,
                "forward_gap_at_ref": ex.forward_gap_at_ref,
                "sched_headway_s":    ex.sched_headway_s,
                "headway_at_ref_s":   ex.headway_at_ref_s,
                "labels":             ex.labels.tolist() if ex.labels is not None else None,
                "labels_persist":     ex.labels_persist.tolist() if ex.labels_persist is not None else None,
                "labels_headway_s":   ex.labels_headway_s.tolist() if ex.labels_headway_s is not None else None,
            })
        return pd.DataFrame(rows)
    else:
        return examples

def return_group(sd: date, route: str, direction: int, ctx: SimpleNamespace, view_buses_as_flat_df: bool = False) -> pd.DataFrame:
    """ returns grouped buses + route stops"""
 
    rows = fetch_slice_rows(
        sd, direction, route,
        CACHE=ctx.CACHE, OFFLINE=ctx.OFFLINE,
        SessionLocal=ctx.SessionLocal,
        _TT_COLS=_tt_cols(),
    )
    route_stops = compute_route_stops(route, direction)

    assert rows and route_stops is not None, f"no slice rows or route stops for {sd}/{route}/dir{direction}"

    buses = group_into_buses(rows, route_stops)

    return route_stops, buses

##########################################
# A/B comparator for each stage for different functions #

def compare_stage(stage: str, old_stg_fn, new_stg_fn, service_date: str = "2026-06-18", route: str = "29", direction: int = 0, trip_id: str | None = None) -> None:
    """Compare the output of two stage functions for the same input parameters."""

    # set up the context for the comparison
    ctx = setup(data_mode="cache")

    # ensure static GTFS is loaded before any stage runs
    if not hasattr(ctx, "STATIC"):
        _load_static(ctx)

    service_date = date.fromisoformat(service_date)
    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")


    if stage in ("extract", "project", "upsample"):
        # pick a happy trip if trip_id is not provided
        if not trip_id:
            print(f"stage '{stage}' requires a trip_id; picking a happy trip instead")
            trip_id = pick_happy_trip(sd, route, SessionLocal=ctx.SessionLocal, pipeline=pipeline, ctx=ctx)
        
        old_output = old_stg_fn(trip_id, st, ctx)
        new_output = new_stg_fn(trip_id, st, ctx)

    elif stage in ("group", "label"):
        old_output = old_stg_fn(sd, route, direction, ctx,True)
        new_output = new_stg_fn(sd, route, direction, ctx, True)
    
    # A/B comparison or before/after comparison of the outputs
    try:
        pd.testing.assert_frame_equal(old_output, new_output, check_exact=False, rtol=1e-5)
        print(f"✓ stage '{stage}' output is identical between old and new function")
    except AssertionError as e:
        print("new stage function returned different output than old stage function :\n", e)
        try:
            print("\nCell-level diff:\n", old_output.compare(new_output))
        except ValueError:
            print("\n(shapes/labels differ — can't use .compare() directly)")
    return

##########################################
# Prime slice + CLI commands #

def clear_offline_cache(ctx: SimpleNamespace) -> None:
    """Delete all cached parquet/pickle files and manifest.json."""
    for p in ctx.CACHE.glob("*"):
        p.unlink()
    print(f"cleared {ctx.CACHE} (cache + manifest)")


def _cmd_prime(route: str, service_date: date, ctx: SimpleNamespace) -> None:
    """Pull one route+date slice into cache and freeze manifest."""

    if ctx.OFFLINE:
        raise SystemExit("prime requires a live DB connection — run with --mode live")

    STATIC, _ = _load_static(ctx)
    BUNDLE_VERSION = pd.read_csv(Path("Complete GTFS") / "feed_info.txt", dtype=str).iloc[0].get("feed_version")
    BUNDLE_TAG = str(BUNDLE_VERSION)

    print(f"Priming cache for route={route} date={service_date} bundle={BUNDLE_TAG} ...")

    # identify trips whose information matches static data (no stale feeds, mismatches etc.)
    clsdf = classify(
        service_date, route, BUNDLE_TAG,
        OFFLINE=False, SessionLocal=ctx.SessionLocal,
        pipeline=pipeline, STATIC=STATIC, CACHE=ctx.CACHE,
    )
    match_trips = clsdf[clsdf["kind"] == "match"]
    print(f"  classify: {len(clsdf)} instances — "
          f"match={len(match_trips)} "
          f"mismatch={len(clsdf[clsdf['kind']=='mismatch'])} "
          f"none={len(clsdf[clsdf['kind']=='none'])}")

    # fetch VP rows for every matched trip (keeps cache lean: no need to fetch mismatches/none)
    for _, row in match_trips.iterrows():
        fetch_trip_rows(
            row["trip_id"], row["start_date"],
            CACHE=ctx.CACHE, OFFLINE=False,
            SessionLocal=ctx.SessionLocal, pipeline=pipeline,
            _VP_COLS=_VP_COLS,
        ) 
    print(f"  fetched vehicle_positions rows for {len(match_trips)} matched trips")

    # 3. fetch trajectory slice for both directions
    tt_cols = _tt_cols()
    for direction in [0, 1]:
        fetch_slice_rows(
            service_date, direction, route,
            CACHE=ctx.CACHE, OFFLINE=False,
            SessionLocal=ctx.SessionLocal,
            _TT_COLS=tt_cols,
        )
    print("  fetched trip_trajectories slices for direction 0 and 1")

    # 4. freeze manifest
    write_manifest(ctx.MANIFEST, dict(
        primed_at=str(pd.Timestamp.now()),
        bundle_version=BUNDLE_VERSION,
        route=route,
        date=str(service_date),
    ))
    print(f"  manifest written → {ctx.MANIFEST}")
    print("Done.")


def _cmd_run_stage(
    stage: str,
    service_date: date,
    ctx: SimpleNamespace,
    trip_id: str | None = None,
    route: str = "29",
    direction: int = 0,
) -> None:
    """Run one production stage fully offline and print + save output."""

    # ensure static GTFS is loaded before any stage runs
    if not hasattr(ctx, "STATIC"):
        _load_static(ctx)

    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    if stage in ("extract", "project", "upsample"):
        if not trip_id:
            print(f"stage '{stage}' requires a trip_id; picking a happy trip instead")
            trip_id = pick_happy_trip(sd, route, SessionLocal=ctx.SessionLocal, pipeline=pipeline, ctx=ctx)
        if stage == "extract":
            df = _stage_extract(trip_id, st, ctx)
        elif stage == "project":
            df = _stage_project(trip_id, st, ctx)
        else:
            df = _stage_upsample(trip_id, st, ctx)

    elif stage == "group":
        result = _stage_group(sd, route, direction, ctx, view_buses_as_flat_df=True)
        df = result

    elif stage == "label":
        result = _stage_label(sd, route, direction, ctx, view_examples_as_df=True)
        df = result

    else:
        raise SystemExit(f"unknown stage '{stage}'")

    print(df.to_string(max_rows=40))

    out_path = ctx.OUT / f"{stage}_{trip_id or route}_{st}.parquet"
    df.to_parquet(out_path)
    print(f"\n→ saved {len(df)} rows to {out_path}")

def _cmd_diff(stage: str, service_date: date, baseline_pth:str, ctx: SimpleNamespace, trip_id: str | None = None, route: str = "29", direction: int = 0) -> None:
    """Run one production stage fully offline and diff against a baseline parquet file."""

    # Run the stage and return current output
    current_df = _print_run_stage(stage, service_date, ctx, trip_id, route, direction)

    # Load baseline parquet file
    baseline_df = pd.read_parquet(baseline_pth)

    # Compare the two DataFrames
    diff_dataframes(current_df, baseline_df)

def _print_run_stage(
    stage: str,
    service_date: date,
    ctx: SimpleNamespace,
    trip_id: str | None = None,
    route: str = "29",
    direction: int = 0,
) -> None:
    """Run one production stage fully offline and return output."""

    # ensure static GTFS is loaded before any stage runs
    if not hasattr(ctx, "STATIC"):
        _load_static(ctx)

    sd = pd.to_datetime(service_date).date()
    st = sd.strftime("%Y%m%d")

    if stage in ("extract", "project", "upsample"):
        if not trip_id:
            print(f"stage '{stage}' requires a trip_id; picking a happy trip instead")
            trip_id = pick_happy_trip(sd, route, SessionLocal=ctx.SessionLocal, pipeline=pipeline, ctx=ctx)
        if stage == "extract":
            df = _stage_extract(trip_id, st, ctx)
        elif stage == "project":
            df = _stage_project(trip_id, st, ctx)
        else:
            df = _stage_upsample(trip_id, st, ctx)

    elif stage == "group":
        result = _stage_group(sd, route, direction, ctx, view_buses_as_flat_df=True)
        df = result

    elif stage == "label":
        result = _stage_label(sd, route, direction, ctx, view_examples_as_df=True)
        df = result

    else:
        raise SystemExit(f"unknown stage '{stage}'")

    return df

def diff_dataframes(current: pd.DataFrame, baseline: pd.DataFrame) -> pd.DataFrame:
    """Compare two DataFrames and return the differences."""
    
    # compare shape
    print(f"Shape of current trip: {current.shape}, Shape of baseline trip: {baseline.shape}, Difference in shape: {current.shape[0] - baseline.shape[0]} rows, {current.shape[1] - baseline.shape[1]} columns")

    # if shapes are same, run a detailed comparison
    if current.shape == baseline.shape:
        comparison_result = current.compare(baseline)
        # print first 40 rows of the comparison result
        print(comparison_result.head(40))

    # if row counts differ, slice up to minimum row length and print the difference row by row
    elif current.shape[0] != baseline.shape[0]:
        print(f"Row count differs: current (self) trip has {current.shape[0]} rows, baseline (other) trip has {baseline.shape[0]} rows")
        # line by line comparison for rows
        min_rows = min(current.shape[0], baseline.shape[0])
        # slice and compare only the overlapping rows
        comparison_result = current.iloc[:min_rows].compare(baseline.iloc[:min_rows])
        print(comparison_result.head(40))

    # if column counts differ, that shouldnt happen and raise an error
    elif current.shape[1] != baseline.shape[1]:
        raise ValueError(f"Column count differs: current trip has {current.shape[1]} columns, baseline trip has {baseline.shape[1]} columns")
    return

def _build_parser() -> argparse.ArgumentParser:

    p = argparse.ArgumentParser(
        prog="python -m scripts.analysis.offline_debug",
        description="iROAM offline pipeline debugger",
    )

    p.add_argument("--mode", choices=["live", "cache"], default="cache", help="'live' queries the DB and primes cache; 'cache' replays from cache only (default: cache)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # prime
    pp = sub.add_parser("prime", help="Pull route+date slice into cache (requires live DB)")
    pp.add_argument("--route", required=True, help="GTFS route_id e.g. 29")
    pp.add_argument("--date",  required=True, help="service date YYYY-MM-DD; current static feed range: 2026-06-14 : 2026-06-20")

    # run-stage
    rp = sub.add_parser("run-stage", help="Run one pipeline stage fully offline")
    rp.add_argument("--stage",required=True, choices=["extract", "project", "upsample", "group", "label"])
    rp.add_argument("--trip", default=None, help="trip_id — if not provided, a happy trip will be picked from cache")
    rp.add_argument("--date", required=True, help="service date YYYY-MM-DD")
    rp.add_argument("--route", default="29",  help="route_id (for group / label)")
    rp.add_argument("--direction", default="0",   help="direction_id (for group / label)")

    # diff
    dp = sub.add_parser("diff", help="Diff current stage output vs a saved baseline snapshot")
    dp.add_argument("--stage",    required=True, choices=["extract", "project", "upsample", "group", "label"])
    dp.add_argument("--baseline", required=True, help="path to baseline .parquet file")
    dp.add_argument("--date",     required=True, help="service date YYYY-MM-DD")
    dp.add_argument("--trip",     default=None,  help="trip_id (for extract/project/upsample)")
    dp.add_argument("--route",    default="29",  help="route_id (for group/label)")
    dp.add_argument("--direction",default="0",   help="direction_id (for group/label)")

    # clear cache
    sub.add_parser("clear-cache", help="Delete all cached files and manifest")

    return p

def main(argv=None) -> int:

    parser = _build_parser()
    args = parser.parse_args(argv)
    ctx = setup(data_mode=args.mode)

    if args.cmd == "prime":
        _cmd_prime(
            route=args.route,
            service_date=date.fromisoformat(args.date),
            ctx=ctx,
        )

    elif args.cmd == "run-stage":
        _cmd_run_stage(
            stage=args.stage,
            service_date=date.fromisoformat(args.date),
            ctx=ctx,
            trip_id =args.trip,
            route=args.route,
            direction=int(args.direction),
        )
    
    elif args.cmd == "diff":
        _cmd_diff(
            stage=args.stage,
            service_date=date.fromisoformat(args.date),
            baseline_pth=args.baseline,
            ctx=ctx,
            trip_id=args.trip,
            route=args.route,
            direction=int(args.direction),
        )

    elif args.cmd == "clear-cache":
        clear_offline_cache(ctx)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# usage:
""" # prime 
python3 -m scripts.analysis.offline_debug --mode live prime --route 29 --date 2026-06-18

# run stages 
python3 -m scripts.analysis.offline_debug --mode cache run-stage --stage extract --date 2026-06-18 --route 29 --direction 0
python3 -m scripts.analysis.offline_debug --mode cache run-stage --stage project --date 2026-06-18 --route 29 --direction 0
python3 -m scripts.analysis.offline_debug --mode cache run-stage --stage upsample --date 2026-06-18 --route 29 --direction 0
python3 -m scripts.analysis.offline_debug --mode cache run-stage --stage group --date 2026-06-18 --route 29 --direction 0
python3 -m scripts.analysis.offline_debug --mode cache run-stage --stage label --date 2026-06-18 --route 29 --direction 0 

# clear cache
python3 -m scripts.analysis.offline_debug --mode cache clear-cache

# diff
baseline_path="out/qa/upsample_10020_20260618.parquet" 
python3 -m scripts.analysis.offline_debug --mode cache diff --stage upsample --baseline $baseline_path --date 2026-06-18 --trip 93304020 --route 29 --direction 0
"""

