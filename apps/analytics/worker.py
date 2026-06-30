"""Long-running analytics worker.

Polls the DB on a fixed interval. Each tick:

1. Computes ``today`` in the configured service-date timezone
   (``ANALYTICS_WORKER_SERVICE_DATE_TZ``).
2. Calls ``run_for_date(today, only_changed_since=last_tick_started_at)``
   so only trip instances that gained new VehiclePosition observations
   since the previous tick are reprocessed. On the very first tick the
   cutoff is ``(now - 2 * interval)`` so we catch trips that were active
   just before the worker booted.
3. Sleeps until the next interval boundary, cooperatively honoring
   SIGINT/SIGTERM so ``docker compose stop`` is quick and clean.

The runner itself is idempotent (delete-then-insert per trip instance,
schema-level unique index as safety net), so overlapping with a manual
``python -m apps.analytics.main`` run is safe: the last writer wins per
trip instance, no duplicates.
"""

from __future__ import annotations

import signal
import time
from datetime import date, datetime, timedelta, timezone
from types import FrameType
from zoneinfo import ZoneInfo

from apps.analytics.runner import run_for_date
from core.config import get_settings
from core.logging import configure_logging, get_logger
from db.session import SessionLocal

_logger = get_logger(__name__)

# How long after local midnight the worker keeps refreshing *yesterday* too.
# Observations of trips that straddle midnight land with yesterday's
# effective start_date; a worker that only ever processes "today" would
# permanently miss the final pre-midnight observations that arrived after
# its last tick of the old day.
_MIDNIGHT_GRACE = timedelta(hours=1)


def _service_dates_for_tick(
    now_local: datetime, *, grace: timedelta = _MIDNIGHT_GRACE
) -> list[date]:
    """Service dates this tick must refresh, yesterday-first near midnight."""
    today = now_local.date()
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    if now_local - midnight < grace:
        return [today - timedelta(days=1), today]
    return [today]


def _tick(since: datetime, tz_name: str) -> int:
    """Run one analytics cycle. Returns trip_instances_processed."""
    now_local = datetime.now(tz=ZoneInfo(tz_name))
    settings = get_settings()
    total_processed = 0
    for service_date in _service_dates_for_tick(now_local):
        with SessionLocal() as session:
            outcome = run_for_date(
                session,
                service_date,
                upsample_resolution_s=settings.analytics_upsample_resolution_s,
                max_orthogonal_distance_m=settings.analytics_max_orthogonal_distance_m,
                max_implied_speed_m_s=settings.analytics_max_implied_speed_m_s,
                only_changed_since=since,
            )
        _logger.info(
            "analytics_tick_done",
            extra={
                "service_date": service_date.isoformat(),
                "since": since.isoformat(),
                "status": outcome.status,
                "trip_instances_processed": outcome.trip_instances_processed,
                "rows_written": outcome.rows_written,
            },
        )
        total_processed += outcome.trip_instances_processed
    return total_processed


def main() -> int:
    configure_logging()
    settings = get_settings()
    interval = settings.analytics_worker_interval_seconds
    tz_name = settings.analytics_worker_service_date_tz

    stopping = False

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        _logger.info("signal_received", extra={"signal": signum})
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Cold-start cutoff: look back 2 intervals so a restart doesn't miss
    # observations from just-before-boot without re-processing yesterday.
    last_tick_started_at = datetime.now(tz=timezone.utc) - timedelta(seconds=2 * interval)

    _logger.info(
        "analytics_worker_start",
        extra={
            "interval_seconds": interval,
            "service_date_tz": tz_name,
            "initial_since": last_tick_started_at.isoformat(),
        },
    )

    while not stopping:
        tick_started_at = datetime.now(tz=timezone.utc)
        try:
            _tick(last_tick_started_at, tz_name)
        except Exception:  # noqa: BLE001 — loop must not die on one bad cycle
            _logger.exception("analytics_tick_unhandled_error")
        # Advance the cutoff regardless of success; next tick's since is this
        # tick's start. Failed ticks just mean the next one has a slightly
        # bigger window to catch up.
        last_tick_started_at = tick_started_at

        elapsed = (datetime.now(tz=timezone.utc) - tick_started_at).total_seconds()
        remaining = max(0.0, interval - elapsed)
        # Poll stop flag 10x/sec so compose stop is snappy.
        for _ in range(int(remaining * 10)):
            if stopping:
                break
            time.sleep(0.1)

    _logger.info("analytics_worker_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
