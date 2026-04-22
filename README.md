# TTC GTFS-Realtime VehiclePositions Platform

A production-structured MVP that continuously ingests the [Toronto Transit Commission
GTFS-Realtime VehiclePositions feed](https://gtfsrt.ttc.ca/vehicles/position), persists
both raw protobuf snapshots and normalized relational rows **append-only** in PostgreSQL
(with PostGIS for geom), exposes query APIs via FastAPI, and visualizes feed health +
vehicle movement in a Streamlit dashboard. An `apps/analytics/` pipeline joins the live
feed against static GTFS to produce per-trip upsampled trajectories.

The layout is intentionally structured so that later additions (VehiclePositions, Alerts,
static GTFS joins, map views, analytics) drop in without touching the existing data
model.

---

## Architecture

```
 ┌─────────────┐   every 30 s (HTTP GET)    ┌──────────────┐
 │  Collector  │ ─────── protobuf ─────────▶│   TTC feed   │
 │  (asyncio)  │◀────────   bytes   ─────── └──────────────┘
 └──────┬──────┘
        │ single txn per fetch:
        │   1. feed_fetch_logs           (always; success or failure)
        │   2. raw_gtfsrt_snapshots      (on success)
        │   3. trip_updates              (normalized)
        │   4. trip_update_stop_times
        ▼
 ┌───────────────┐         ┌──────────────┐         ┌─────────────────┐
 │  PostgreSQL   │◀── SQL ─│   FastAPI    │◀─ HTTP ─│   Streamlit     │
 │  (TIMESTAMPTZ │         │  (SQLAlchemy │   JSON  │   dashboard     │
 │   + BYTEA)    │         │     2.x)     │         │  (4 pages)      │
 └───────────────┘         └──────────────┘         └─────────────────┘
```

Three long-running processes plus Postgres, orchestrated by `docker-compose`.

- **Append-only** — every poll writes new rows; "latest" is a *query*, not a mutation.
- **Raw + normalized** — raw protobuf bytes are preserved in
  `raw_gtfsrt_snapshots.payload` so the feed can be re-normalized later if the parser
  changes, or replayed into tests.
- **Clean module seams** — `db/`, `core/`, `apps/api`, `apps/collector`,
  `apps/dashboard`. Each `apps/*` entry depends on `core` + `db` but not on each
  other.
- **Sync SQLAlchemy 2.x** with the typed `Mapped[X]` style. Async adds transaction
  complexity with no latency benefit at 2 polls / minute.

---

## Quickstart

```bash
cp .env.example .env
make up            # builds images, starts postgres + migrator + api + collector + dashboard
```

Then:

- API  http://localhost:8000/health
- Dashboard  http://localhost:8501
- Postgres  `localhost:5433` (user `ttc` / pwd `ttc` / db `ttc_gtfsrt`)

Shut down with `make down`. Wipe data with `docker compose down -v`.

### Running tests

The test suite expects a Postgres instance reachable via `TEST_DATABASE_URL`
(falls back to `DATABASE_URL`). The default `.env` points at the compose-exposed
port 5433, which works once `make up` is running:

```bash
pip install -e '.[dev]'
make test
```

Tests marked as requiring the DB are auto-skipped if the server is unreachable.

---

## Repo layout

```
apps/
  api/        FastAPI app, routers, pydantic schemas
  collector/  httpx fetcher, protobuf parser, pure normalizer, runner (txn owner), CLI
  dashboard/  Streamlit multipage app + thin API client
core/         settings, logging, time helpers, constants
db/           Base, session factory, ORM models, query helpers, alembic migrations
tests/        parser / normalizer / query / API smoke tests + recorded fixture
scripts/      capture_sample.py (record a live feed payload for fixtures)
```

---

## Data model

Five tables. The raw-ingest trio is created by `0001_initial_schema.py`; the
`vehicle_positions` pivot is `0002_pivot_to_vehicle_positions.py`; the analytics
output tables land in `0003_trip_trajectories.py`:

| table | purpose | key columns |
|-|-|-|
| `feed_fetch_logs` | every fetch attempt — success or failure | `feed_name`, `fetched_at`, `success`, `http_status`, `duration_ms`, `entity_count`, `error_type`, `error_message` |
| `raw_gtfsrt_snapshots` | raw protobuf payload per successful fetch (1:1 with the log row) | `fetch_log_id` UNIQUE, `content_sha256`, `payload` BYTEA |
| `vehicle_positions` | one row per `FeedEntity.vehicle` per snapshot | `snapshot_id`, `trip_id`, `route_id`, `vehicle_id`, `latitude`, `longitude`, `speed_mps`, `occupancy_status`, **denormalized** `fetched_at`, PostGIS `geom(Point, 4326)` GENERATED |
| `analytics_runs` | one row per `apps/analytics` invocation | `service_date`, `route_id`, `status`, `rows_written`, `config_json`, `started_at`, `finished_at` |
| `trip_trajectories` | upsampled per-trip trajectory points | `run_id`, `trip_id`, `start_date`, `service_date`, `datetime`, `travel_distance_m`, `moving_speed_m_s`, `observed` |

### Why `fetched_at` is denormalized onto `vehicle_positions`

The hottest query is "most recent row per `vehicle_id`" or "per `route_id`":

```sql
SELECT DISTINCT ON (vehicle_id) *
FROM vehicle_positions
ORDER BY vehicle_id, fetched_at DESC;
```

Joining every lookup back to `raw_gtfsrt_snapshots` to pull `fetched_at` would
destroy the `(vehicle_id, fetched_at DESC)` index's value. Denormalization is
safe because both tables are append-only — `fetched_at` cannot drift.

### Why no dedup on `content_sha256`

Each poll is a distinct *observation*, even when the payload bytes are identical.
Preserving every poll keeps the monitoring signal "feed is reachable but hasn't
changed" — dedup would silently hide that.

---

## API

All routes return JSON. Timestamps are ISO-8601 UTC.

| route | purpose |
|-|-|
| `GET /health` | liveness + db-ok |
| `GET /feed-status/trip-updates` | last fetch time, success rate (1h), current lag vs feed header |
| `GET /trip-updates/latest?route_id=&limit=` | `DISTINCT ON (trip_id)` most recent observation, optionally route-filtered |
| `GET /trips/{trip_id}/latest` | latest row for a trip + its stop-time updates |
| `GET /trips/{trip_id}/history?start=&end=&limit=` | append-ordered history for a trip in a time window |
| `GET /routes/{route_id}/active-trips?window_minutes=&limit=` | trips seen on a route within a recent window |
| `GET /routes/{route_id}/trip-updates/latest?limit=` | `/trip-updates/latest` scoped to one route |
| `GET /replay/trips?start=&end=&route_id=&limit=` | raw append-ordered rows over an arbitrary window |

Example:

```bash
curl 'http://localhost:8000/feed-status/trip-updates' | jq
curl 'http://localhost:8000/trip-updates/latest?limit=5' | jq
curl --data-urlencode 'start=2026-04-21T22:00:00+00:00' \
     --data-urlencode 'end=2026-04-21T22:30:00+00:00'   \
     --get 'http://localhost:8000/replay/trips' | jq '.[0]'
```

---

## Dashboard

Streamlit multipage app at http://localhost:8501, backed entirely by the API
(no direct DB reads from the dashboard):

1. **Home** — platform summary: fetches today, success rate, current lag.
2. **Feed Health** — per-minute success/failure bar chart and recent fetch log rows.
3. **Route Explorer** — pick a route, see active trips and a delay distribution.
4. **Trip Detail** — latest stop-time updates table + delay-over-time line chart.
5. **Replay** — date-range + optional route filter; rows-per-minute chart + paginated
   table.

---

## Configuration

Configured entirely via environment variables (see `.env.example`). Important knobs:

| var | default | note |
|-|-|-|
| `DATABASE_URL` | `postgresql+psycopg://ttc:ttc@postgres:5432/ttc_gtfsrt` | psycopg v3 dialect |
| `GTFS_RT_VEHICLE_POSITIONS_URL` | `https://gtfsrt.ttc.ca/vehicles/position?format=binary` | `?format=binary` matters — see below |
| `COLLECTOR_INTERVAL_SECONDS` | `20` | TTC vehicles update ~every 20 s |
| `COLLECTOR_HTTP_RETRIES` | `2` | retry connect/timeout/5xx |
| `COLLECTOR_ROUTE_ALLOWLIST` | *(empty)* | Comma-separated `route_id` list. Empty ⇒ ingest every route. When set, the normalizer drops entities whose `trip.route_id` is missing or not in the list. |
| `ACTIVE_VEHICLE_WINDOW_MINUTES` | `5` | staleness bound for "active" vehicle queries |
| `MAX_PAGE_SIZE` | `5000` | hard cap on any `limit=` parameter |
| `ANALYTICS_UPSAMPLE_RESOLUTION_S` | `10` | upsampling grid for trip trajectories |
| `ANALYTICS_MAX_ORTHOGONAL_DISTANCE_M` | `200` | drop trajectory points farther than this from the shape |
| `ANALYTICS_WORKER_INTERVAL_SECONDS` | `120` | analytics worker tick; smaller = fresher trajectories, higher load |
| `ANALYTICS_WORKER_SERVICE_DATE_TZ` | `America/Toronto` | used by the worker to compute "today" |
| `LOG_JSON` | `true` | structured JSON logging |

### Note on the TTC feed URL

Without `?format=binary`, the TTC endpoint returns protobuf **text** format
(~5.6 MB `text/plain`) rather than binary (~730 KB `application/x-protobuf`).
The collector defaults to the binary form; the parser also falls back to text
format if the server ever switches back, so the system remains resilient either
way.

---

## Key engineering decisions

1. **Append-only schema** — "latest" is always a query, never an upsert. Implemented
   with `DISTINCT ON (key) ORDER BY key, fetched_at DESC`, served by a composite index.
   Upgradable to a materialized view later without an API change.
2. **Raw protobuf stored in Postgres BYTEA** — one blob per successful fetch, FK-linked
   to its fetch log. Object storage (S3/MinIO) is the obvious Phase-2 swap and only
   touches the collector + a future replay endpoint.
3. **Failed fetches log a row, no snapshot row** — keeps the FK clean (no dangling
   snapshot without bytes) while still preserving the monitoring signal.
4. **Normalization is a pure function** — `apps/collector/normalizer.py` has no DB
   side effects; the transaction is opened in `runner.py`. Makes unit tests trivial.
5. **Sync SQLAlchemy** over async — polling rate is ~2/min; API is low-QPS internal.
6. **TIMESTAMPTZ everywhere** (UTC). Only the dashboard converts to `America/Toronto`
   for display.
7. **Streamlit calls the FastAPI** (never the DB directly) — a future React/Next
   rewrite doesn't require data-layer changes.

---

## Known limits

- `raw_gtfsrt_snapshots.payload` is the dominant on-disk cost
  (~700 KB per poll × 2 polls/min ≈ 2 GB/day). Retention / partitioning comes
  in Phase 2 (see `0004_partition_trip_updates_by_month.py` placeholder in the
  migrations plan).
- No auth on the API. Suitable for internal use or behind a reverse proxy.
- No Prometheus metrics or tracing yet.
- Static GTFS (under `Complete GTFS/`) is consumed by `apps/analytics/` but
  not yet materialized into dedicated reference tables — joins happen in
  pandas at runtime.

---

## Extension roadmap

Each bullet drops in without touching the existing model:

- **VehiclePositions** — new `feed_name="vehicle-positions"` constant, reuse
  `raw_gtfsrt_snapshots`, add `vehicle_positions` table mirroring `trip_updates`
  structure.
- **Alerts** — new `feed_name="alerts"`, new `alerts` table.
- **Static GTFS join** — load `routes` / `stops` / `trips` from `Complete GTFS/`
  and add name-resolution endpoints; the string keys already match.
- **Map view** — blocked only on VehiclePositions; the dashboard's `api_client.py`
  is already ready to feed pydeck / folium.
- **Partitioning + retention** — `0004_partition_trip_updates_by_month.py`
  declarative partitioning once retention pressure appears.
- **Materialized latest view** — `0005_latest_view.py` if the `DISTINCT ON` query
  becomes a bottleneck.

---

## Trajectories (apps/analytics)

A batch pipeline that projects every `vehicle_positions` GPS point onto the
trip's GTFS static shape, derives `travel_distance_m`/`moving_speed_m_s`, and
upsamples to a fixed resolution. Output lands in `trip_trajectories` (one row
per upsampled point, `observed=True/False`) with a parent `analytics_runs` row
per invocation.

```bash
make analytics-run DATE=2026-04-20                       # all routes (full refresh)
make analytics-run DATE=2026-04-20 ROUTE=29              # one route
python -m apps.analytics.main --date 2026-04-20 \
  --export-csv ./out/2026-04-20                          # also emit legacy-style CSVs
python -m apps.analytics.main --date 2026-04-20 \
  --since 2026-04-20T12:00:00+00:00                      # incremental: only trips with new VP data
```

### Continuous refresh — `analytics-worker`

An optional long-running service in compose that re-runs the analytics for
"today" every `ANALYTICS_WORKER_INTERVAL_SECONDS`, scoped to trip instances
whose raw `vehicle_positions` rows have grown since the last tick. Output
lands in the same `trip_trajectories` table, overwriting each trip
instance's prior rows atomically (delete-then-insert per `(trip_id,
start_date)`, enforced by a schema-level unique index). Manual
`make analytics-run` and the worker can coexist — last writer wins per
trip instance, and no duplicates are possible.

```bash
make up                        # brings up the analytics-worker too
make analytics-worker-logs     # tail the worker loop
```

Read them back from a notebook (host port 5433):

```python
import pandas as pd
from sqlalchemy import create_engine
e = create_engine("postgresql+psycopg://ttc:ttc@localhost:5433/ttc_gtfsrt")
pd.read_sql("""
    SELECT datetime, travel_distance_m, moving_speed_m_s, observed
    FROM trip_trajectories
    WHERE route_id = '29' AND service_date = '2026-04-20'
    ORDER BY trip_id, start_date, datetime
""", e)
```

---

## Common commands

```bash
make up              # build + start the full stack
make down            # stop the full stack (keeps data)
make ps              # service status
make logs            # tail all compose logs
make migrate         # alembic upgrade head (via compose)
make collect-once    # run a single fetch + persist cycle
make capture-sample  # record a fresh protobuf fixture
make test            # pytest (host)
make fmt             # ruff format + fix
make lint            # ruff check
make api             # run the API locally (no docker)
make dashboard       # run the dashboard locally (no docker)
make analytics-run DATE=2026-04-20 [ROUTE=29]  # batch analytics pipeline
make analytics-worker-logs                     # tail the analytics-worker loop
make db-reset                                  # dry-run — print row counts that would be truncated
make db-reset-confirm                          # DESTRUCTIVE — TRUNCATE every data table
```

---

## Runbook: resetting and re-ingesting

When the data scope changes (e.g. editing `COLLECTOR_ROUTE_ALLOWLIST`) or
the trajectory pipeline needs a clean slate, follow this sequence. The
data-reset is separate from schema migrations so migrations don't
implicitly destroy data.

```bash
# 1. Verify what would be wiped.
make db-reset                      # dry-run: prints row counts per table

# 2. Actually wipe. Leaves schema + alembic_version intact.
make db-reset-confirm

# 3. Apply any new migrations (e.g. 0004 adds a unique index on
#    trip_trajectories; its guard refuses to run if duplicates exist,
#    so db-reset first).
make migrate

# 4. Restart services so the collector picks up the new allowlist and
#    the analytics worker starts its loop against the fresh data.
docker compose restart collector analytics-worker

# 5. Watch the streams.
make logs                          # everything
make analytics-worker-logs         # just the trajectory refresh loop
```
