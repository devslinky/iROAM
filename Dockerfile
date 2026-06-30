# syntax=docker/dockerfile:1.6

# ── Base: shared Python + dependency install ────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps: psycopg needs libpq; curl is handy for container healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so source changes don't bust the dependency layer.
COPY pyproject.toml ./
RUN pip install \
      'fastapi>=0.115,<0.120' \
      'uvicorn[standard]>=0.30,<0.35' \
      'sqlalchemy>=2.0.30,<2.1' \
      'alembic>=1.13,<1.15' \
      'psycopg[binary]>=3.2,<3.3' \
      'geoalchemy2>=0.15,<0.18' \
      'httpx>=0.27,<0.29' \
      'protobuf>=6.33.5,<7' \
      'pydantic>=2.7,<3' \
      'pydantic-settings>=2.3,<3' \
      'python-dateutil>=2.9,<3' \
      'streamlit>=1.36,<2' \
      'requests>=2.32,<3' \
      'pandas>=2.2,<3' \
      'altair>=5.3,<6' \
      'shapely>=2.0,<3' \
      'pyproj>=3.6,<4' \
      'lightgbm>=4,<5' \
      'numpy>=1.24'

COPY apps ./apps
COPY core ./core
COPY db ./db
COPY deployment ./deployment
COPY scripts ./scripts
COPY data_process ./data_process
COPY alembic.ini ./alembic.ini

# ── Migrator: one-shot alembic upgrade head ────────────────────────────────
FROM base AS migrator
CMD ["alembic", "upgrade", "head"]

# ── API: FastAPI via uvicorn ───────────────────────────────────────────────
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Collector: polling loop ────────────────────────────────────────────────
FROM base AS collector
CMD ["python", "-m", "apps.collector.main", "--loop"]

# ── Analytics worker: polling loop that incrementally re-runs analytics ────
FROM base AS analytics-worker
CMD ["python", "-m", "apps.analytics.worker"]

# ── Dashboard: Streamlit ───────────────────────────────────────────────────
FROM base AS dashboard
EXPOSE 8501
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -sf http://localhost:8501/_stcore/health || exit 1
CMD ["streamlit", "run", "apps/dashboard/Home.py", \
     "--server.port=8501", "--server.address=0.0.0.0", \
     "--server.headless=true", "--browser.gatherUsageStats=false"]
