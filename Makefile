.PHONY: help install up down logs ps migrate test fmt lint capture-sample collect-once api dashboard analytics-run analytics-worker-logs db-reset db-reset-confirm

help:
	@echo "Common targets:"
	@echo "  install         - pip install -e '.[dev]'"
	@echo "  up              - docker compose up -d (postgres, migrator, api, collector, dashboard)"
	@echo "  down            - docker compose down"
	@echo "  logs            - tail compose logs"
	@echo "  ps              - docker compose ps"
	@echo "  migrate         - alembic upgrade head (in api container)"
	@echo "  test            - pytest (host)"
	@echo "  fmt             - ruff format + fix"
	@echo "  lint            - ruff check"
	@echo "  capture-sample  - save a fresh sample protobuf to tests/fixtures/"
	@echo "  collect-once    - run one fetch+persist cycle (via compose)"
	@echo "  api             - run API locally (uvicorn)"
	@echo "  dashboard       - run dashboard locally (streamlit)"
	@echo "  analytics-run   - run analytics for a service date (DATE=YYYY-MM-DD [ROUTE=X])"
	@echo "  analytics-worker-logs - tail the analytics-worker container logs"
	@echo "  db-reset        - print row counts that would be truncated (dry run)"
	@echo "  db-reset-confirm- actually TRUNCATE every data table (DESTRUCTIVE)"

install:
	pip install -e '.[dev]'

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

ps:
	docker compose ps

migrate:
	docker compose run --rm migrator alembic upgrade head

test:
	pytest

fmt:
	ruff format .
	ruff check --fix .

lint:
	ruff check .

capture-sample:
	python -m scripts.capture_sample

collect-once:
	docker compose run --rm collector python -m apps.collector.main --once

api:
	uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

dashboard:
	streamlit run apps/dashboard/Home.py

analytics-run:
	@if [ -z "$(DATE)" ]; then echo "DATE=YYYY-MM-DD required"; exit 2; fi
	docker compose run --rm api python -m apps.analytics.main --date $(DATE) $(if $(ROUTE),--route $(ROUTE),)

analytics-worker-logs:
	docker compose logs -f --tail=200 analytics-worker

db-reset:
	docker compose exec api python -m scripts.db_reset

db-reset-confirm:
	docker compose exec api python -m scripts.db_reset --yes-i-am-sure
