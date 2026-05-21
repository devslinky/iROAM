# Operations

Everything you need to start, stop, debug, and configure the stack. The iROAM repository ships a `Makefile`, a `docker-compose.yml`, a multi-stage `Dockerfile`, and a generously-commented `.env.example`. This page is a runtime reference.

## One-line start

```bash
cp .env.example .env
make up                # build images, start every service in the background
```

`make up` wraps `docker compose up -d --build`. After ~30 seconds, five services are running plus the database; a one-shot `migrator` exits cleanly.

## Service topology

```mermaid
flowchart TB
    PG[(postgres<br/>postgis/postgis:16-3.4<br/>host port 5433)]
    MIG[migrator<br/>alembic upgrade head<br/>one-shot]
    API[api<br/>uvicorn :8000]
    COLL[collector<br/>polling loop]
    AW[analytics-worker<br/>polling loop]
    DASH[dashboard<br/>Streamlit :8501]

    PG -- healthy --> MIG
    MIG -- completed --> API
    MIG -- completed --> COLL
    MIG -- completed --> AW
    PG --> API
    PG --> COLL
    PG --> AW
    API --> DASH
```

Every long-running service depends on `migrator` having finished and on `postgres` being healthy (`pg_isready`). The dashboard depends on the API (it never reads Postgres directly).

| Service | Image stage | Process | Host port | Volumes |
| --- | --- | --- | --- | --- |
| `postgres` | `postgis/postgis:16-3.4` | postgres | 5433 → 5432 | `pg_data` |
| `migrator` | `migrator` | `alembic upgrade head` | — | — |
| `api` | `api` | `uvicorn apps.api.main:app` | 8000 → 8000 | `./Complete GTFS` (ro) |
| `collector` | `collector` | `python -m apps.collector.main --loop` | — | — |
| `analytics-worker` | `analytics-worker` | `python -m apps.analytics.worker` | — | `./Complete GTFS` (ro) |
| `dashboard` | `dashboard` | `streamlit run apps/dashboard/Home.py` | 8501 → 8501 | — |

The Postgres volume `pg_data` survives `make down`; it is wiped only by `docker compose down -v`.

## Makefile cheatsheet

| Target | What it does |
| --- | --- |
| `make up` | `docker compose up -d --build` |
| `make down` | `docker compose down` (keeps volumes) |
| `make logs` | `docker compose logs -f --tail=200` |
| `make ps` | service status |
| `make migrate` | `docker compose run --rm migrator alembic upgrade head` |
| `make collect-once` | one fetch+persist cycle (via compose) |
| `make analytics-run DATE=2026-04-20 [ROUTE=29]` | batch analytics pipeline |
| `make analytics-worker-logs` | tail the analytics-worker logs |
| `make capture-sample` | save a fresh protobuf fixture under `tests/fixtures/` |
| `make api` | run the API locally on port 8000 (no docker) |
| `make dashboard` | run the dashboard locally on port 8501 (no docker) |
| `make test` | `pytest` (host) |
| `make fmt` / `make lint` | `ruff format --fix .` / `ruff check .` |
| `make db-reset` | dry-run: row counts that would be truncated |
| `make db-reset-confirm` | DESTRUCTIVE: `TRUNCATE` every data table (schema preserved) |
| `make install` | `pip install -e '.[dev]'` |

## Dockerfile stages

One `Dockerfile`, five build targets. Source layout under `/app`:

```mermaid
flowchart LR
    BASE[base<br/>python:3.12-slim<br/>+libpq5 libgomp1 curl<br/>+all deps via pip] --> MIG[migrator<br/>CMD: alembic upgrade head]
    BASE --> API[api<br/>EXPOSE 8000<br/>CMD: uvicorn]
    BASE --> COLL[collector<br/>CMD: python -m apps.collector.main --loop]
    BASE --> AW[analytics-worker<br/>CMD: python -m apps.analytics.worker]
    BASE --> DASH[dashboard<br/>EXPOSE 8501<br/>CMD: streamlit run]
```

Dependencies install once in the `base` layer; source is copied last, so a code edit doesn't bust the pip cache. Health checks: `api` on `GET /health`, `dashboard` on Streamlit's `/_stcore/health`.

## Environment variables

Configured entirely via `.env` (`.env.example` is the documented template). The runtime values are read by `core.config.Settings` (Pydantic `BaseSettings`).

### Database

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://ttc:ttc@postgres:5432/ttc_gtfsrt` | psycopg v3 dialect. For host-side runs, switch to `localhost:5433`. |

### Realtime feeds

| Var | Default | Notes |
| --- | --- | --- |
| `GTFS_RT_VEHICLE_POSITIONS_URL` | `https://gtfsrt.ttc.ca/vehicles/position?format=binary` | `?format=binary` matters — text format is ~7× larger. |
| `GTFS_RT_TRIP_UPDATES_URL` | `https://gtfsrt.ttc.ca/trips/update?format=binary` | reserved; unused today |
| `GTFS_RT_ALERTS_URL` | `https://gtfsrt.ttc.ca/alerts?format=binary` | reserved; unused today |

### Collector

| Var | Default | Notes |
| --- | --- | --- |
| `COLLECTOR_INTERVAL_SECONDS` | `20` | TTC vehicles update ~every 20 s. |
| `COLLECTOR_HTTP_TIMEOUT_SECONDS` | `10` | per-request timeout |
| `COLLECTOR_HTTP_RETRIES` | `2` | connect / 5xx / timeout retries |
| `COLLECTOR_USER_AGENT` | `ttc-gtfsrt-platform/0.2 (+...)` | what the TTC sees |
| `COLLECTOR_ROUTE_ALLOWLIST` | `29,929,25,925,36,501,506` | comma-separated `route_id`s; empty → ingest every route |

### API + dashboard

| Var | Default | Notes |
| --- | --- | --- |
| `API_HOST` | `0.0.0.0` | |
| `API_PORT` | `8000` | |
| `API_CORS_ORIGINS` | `*` | comma-separated list in prod |
| `DASHBOARD_API_BASE_URL` | `http://api:8000` | for host-side dashboard, switch to `http://localhost:8000` |

### Tuning / pagination

| Var | Default | Notes |
| --- | --- | --- |
| `ACTIVE_VEHICLE_WINDOW_MINUTES` | `5` | staleness bound for "active" queries |
| `MAX_PAGE_SIZE` | `5000` | hard cap on any `limit=` parameter |

### Analytics

| Var | Default | Notes |
| --- | --- | --- |
| `ANALYTICS_UPSAMPLE_RESOLUTION_S` | `10` | upsampling grid for trajectories |
| `ANALYTICS_MAX_ORTHOGONAL_DISTANCE_M` | `200` | drop trajectory points off-shape by more than this |
| `ANALYTICS_WORKER_INTERVAL_SECONDS` | `120` | worker tick; smaller = fresher trajectories, more load |
| `ANALYTICS_WORKER_SERVICE_DATE_TZ` | `America/Toronto` | for "today" calculations |

### Logging

| Var | Default | Notes |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | |
| `LOG_JSON` | `true` | structured JSON logging |

## Reset and re-ingest

```bash
make db-reset                  # dry-run: row counts per table
make db-reset-confirm          # actually TRUNCATE every data table
make migrate                   # apply any new migrations
docker compose restart collector analytics-worker
make logs                      # everything
make analytics-worker-logs     # just the trajectory refresh loop
```

The data reset is intentionally **separate** from `alembic upgrade head`; migrations never destroy data. Trigger this when you change `COLLECTOR_ROUTE_ALLOWLIST` or when the trajectory pipeline needs a clean slate.

## Tests

Test suite under `tests/`:

- `conftest.py` — DB engine, scoped session fixtures.
- `_factories.py` — ORM object factories for test data.
- `fixtures/` — recorded protobuf payloads (generated by `make capture-sample`).
- Test modules cover the parser, normalizer, latest-row queries, analytics incremental refresh, anomaly detection (distance + time methods), iROAM endpoint grouping, forecast features, and the DB reset script.

Tests that require Postgres auto-skip when `TEST_DATABASE_URL` is unreachable:

```bash
pip install -e '.[dev]'
make test
# or with verbose output:
pytest -v
```

`make up` exposes Postgres on host port 5433, which matches the `TEST_DATABASE_URL` fallback in `core/config.py`.

## Common gotchas

- **iROAM SPA missing or 404** — the API serves `/ui` from `apps/api/static/iroam.html`. Confirm `apps/api/main.create_app` mounts the static dir. If the file is empty or moved, the route 404s silently.
- **Streamlit can't reach API** — Inside Docker `DASHBOARD_API_BASE_URL=http://api:8000` (compose DNS). Locally, set it to `http://localhost:8000`.
- **`COLLECTOR_ROUTE_ALLOWLIST` change has no effect** — the normalizer reads it at process start. Restart the collector container.
- **Tests pass locally but fail in CI** — most likely Postgres isn't reachable; `TEST_DATABASE_URL` should point at the test instance.
- **Migrations refuse to run** — `0004_trip_trajectory_unique.py` guards against pre-existing duplicates. `make db-reset-confirm` first, then `make migrate`.
