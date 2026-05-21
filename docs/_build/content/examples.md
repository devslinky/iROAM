# Examples

Concrete commands for the workflows researchers ask about most. All commands assume the working directory is the repo root and that `make up` is running.

## Quickstart with docker-compose

```bash
cp .env.example .env
make up               # postgres + migrator + api + collector + dashboard + analytics-worker
```

After ~10 seconds:

- API: <http://localhost:8000/health> → `{"status":"ok","db_ok":true}`
- Dashboard: <http://localhost:8501>
- iROAM time-distance UI: <http://localhost:8000/ui>
- Postgres: `psql postgresql://ttc:ttc@localhost:5433/ttc_gtfsrt`

Stop with `make down` (keeps data) or `docker compose down -v` (wipes volumes).

## Running the collector once

For debugging fetcher/parser changes without spinning up the loop:

```bash
python -m apps.collector.main --once --feed vehicle-positions
```

The runner writes one `feed_fetch_logs` row, one `raw_gtfsrt_snapshots` row, and ~1500 `vehicle_positions` rows (one per active TTC vehicle).

## Inspecting feed health

```bash
curl -s 'http://localhost:8000/feed-status/vehicle-positions' | python -m json.tool
```

Expected shape:

```json
{
  "feed_name": "vehicle-positions",
  "last_fetched_at": "2026-04-21T17:43:20+00:00",
  "success_rate_1h": 0.996,
  "lag_seconds_vs_header": 4.2,
  "recent_fetches": [
    {"fetched_at": "...", "success": true, "http_status": 200, "duration_ms": 187, "entity_count": 1612}
  ]
}
```

## Latest vehicle positions

```bash
# All routes, 100 most-recent vehicles
curl -s 'http://localhost:8000/vehicles/latest?limit=100' | python -m json.tool | head -40

# Single route
curl -s 'http://localhost:8000/routes/29/vehicles/latest' | python -m json.tool
```

## Replaying a time window

```bash
curl -s --get \
  --data-urlencode 'start=2026-04-21T17:00:00+00:00' \
  --data-urlencode 'end=2026-04-21T17:30:00+00:00' \
  'http://localhost:8000/replay/vehicles' | python -m json.tool | head
```

Returns append-ordered `vehicle_positions` rows in the window. Capped at `MAX_PAGE_SIZE`.

## Running the analytics pipeline

Batch (full refresh for a date):

```bash
make analytics-run DATE=2026-04-20                                  # every route
make analytics-run DATE=2026-04-20 ROUTE=29                         # one route
python -m apps.analytics.main --date 2026-04-20 \
    --export-csv ./out/2026-04-20                                   # also emit legacy-format CSVs
python -m apps.analytics.main --date 2026-04-20 \
    --since 2026-04-20T12:00:00+00:00                               # incremental
```

The worker daemon (started by `make up` in docker-compose) re-runs the analytics for "today" every `ANALYTICS_WORKER_INTERVAL_SECONDS` (default 120 s), scoped to trip instances with new `vehicle_positions` rows since the last tick.

Tail it:

```bash
make analytics-worker-logs
```

## Reading trajectories from a notebook

```python
import pandas as pd
from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg://ttc:ttc@localhost:5433/ttc_gtfsrt")

df = pd.read_sql("""
    SELECT datetime, travel_distance_m, moving_speed_m_s, observed
    FROM trip_trajectories
    WHERE route_id = '29'
      AND service_date = '2026-04-20'
    ORDER BY trip_id, start_date, datetime
""", engine)
df.head()
```

The bundled `notebooks/database_access.ipynb` has more variations.

## Fetching the iROAM trajectory slice

The richest single API call. Returns every running bus on a route × date × direction, plus anomaly events at user-chosen thresholds:

```bash
curl -s --get \
  --data-urlencode 'route_id=29' \
  --data-urlencode 'service_date=2026-04-20' \
  --data-urlencode 'direction_id=0' \
  --data-urlencode 'bunch_sec=240' \
  --data-urlencode 'idle_min=2' \
  --data-urlencode 'crowd_pct=80' \
  --data-urlencode 'bunch_method=distance' \
  --data-urlencode 'bunch_dist=500' \
  'http://localhost:8000/iroam/buses' | python -m json.tool | head -80
```

The iROAM static HTML at `/ui` calls this on every slider change.

## Forecasting bunching risk

```bash
curl -s --get \
  --data-urlencode 'route_id=29' \
  --data-urlencode 'service_date=2026-04-20' \
  --data-urlencode 'direction_id=0' \
  --data-urlencode 't_ref=1080'   `# minutes since local-day midnight = 18:00` \
  'http://localhost:8000/iroam/forecast' | python -m json.tool | head -40
```

Returns a per-bus risk vector (`prob[0..29]`, one probability per future minute), a `first_alert_horizon`, and an aggregate `horizon_summary` across all eligible buses. Internally:

1. `services/forecast_features.build_bus_window` pulls the last ~10 minutes of upsampled trajectory for every running bus on the slice, finds two upstream neighbours, builds a `60 × 9` feature window.
2. `services/bunching_predictor.get_predictor` lazy-loads the 30 LightGBM boosters from `deployment/bunching_lightgbm/model/`.
3. `services/forecast.run_forecast` batches eligible windows through all 30 boosters, applies the per-horizon F2-optimal thresholds from `thresholds.json`, returns probabilities and counts.

If the model bundle is missing, the endpoint returns 503 with `{"detail": "predictor unavailable: ..."}`.

## Resetting the database

```bash
make db-reset                  # dry-run: prints row counts per table
make db-reset-confirm          # actually TRUNCATE every data table (schema kept)
make migrate                   # apply any new migrations
docker compose restart collector analytics-worker
```

Useful when `COLLECTOR_ROUTE_ALLOWLIST` changes or when the trajectory pipeline needs a clean slate. Data reset is intentionally **separate** from schema migrations so `alembic upgrade head` never destroys data.

## Running tests

```bash
pip install -e '.[dev]'
make test
```

Tests that require Postgres auto-skip if it's unreachable; the rest are pure. Fixtures live under `tests/fixtures/` (recorded protobuf payloads) and `tests/_factories.py` (ORM object factories).
