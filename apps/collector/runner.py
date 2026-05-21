"""One fetch→persist cycle.

The runner is **feed-agnostic**: it takes a ``FeedSpec`` and does:

  1. Fetch bytes (fetcher).
  2. On HTTP/timeout failure → write a failure ``FeedFetchLog`` row and return.
  3. On success → parse bytes. On parse failure → write a failure log row.
  4. Success → single transaction:
       feed_fetch_logs → raw_gtfsrt_snapshots → normalized rows (via spec.normalize).

Sync SQLAlchemy: polling rate doesn't need async, and a sync session makes
transaction boundaries explicit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from apps.collector.feed_specs import FeedSpec
from apps.collector.fetcher import FetchError, fetch_bytes
from apps.collector.normalize_vehicles import extract_header
from apps.collector.parser import ParseError, parse_feed_message
from core.logging import get_logger
from db.models.feed_fetch_log import FeedFetchLog
from db.models.raw_snapshot import RawGtfsrtSnapshot

_logger = get_logger(__name__)


@dataclass
class RunOutcome:
    success: bool
    fetch_log_id: int
    snapshot_id: int | None
    rows_inserted: int
    error_type: str | None = None
    error_message: str | None = None


def run_once(session: Session, spec: FeedSpec) -> RunOutcome:
    """Execute one fetch+persist cycle for a given feed.

    Commits exactly one transaction on return — success or failure.
    """
    fetched_at = datetime.now(tz=timezone.utc)

    # ── 1. Fetch ─────────────────────────────────────────────────────────
    try:
        result = fetch_bytes(spec.url)
    except FetchError as exc:
        log = FeedFetchLog(
            feed_name=spec.name,
            feed_url=spec.url,
            fetched_at=fetched_at,
            http_status=exc.http_status,
            success=False,
            duration_ms=None,
            response_bytes=None,
            feed_header_timestamp=None,
            entity_count=None,
            error_type=exc.error_type,
            error_message=str(exc)[:8000],
        )
        session.add(log)
        session.commit()
        _logger.error(
            "fetch_failed",
            extra={
                "feed": spec.name,
                "error_type": exc.error_type,
                "http_status": exc.http_status,
            },
        )
        return RunOutcome(
            success=False,
            fetch_log_id=log.id,
            snapshot_id=None,
            rows_inserted=0,
            error_type=exc.error_type,
            error_message=str(exc),
        )

    # ── 2. Parse ────────────────────────────────────────────────────────
    try:
        message = parse_feed_message(result.content)
    except ParseError as exc:
        log = FeedFetchLog(
            feed_name=spec.name,
            feed_url=spec.url,
            fetched_at=fetched_at,
            http_status=result.http_status,
            success=False,
            duration_ms=result.duration_ms,
            response_bytes=len(result.content),
            feed_header_timestamp=None,
            entity_count=None,
            error_type="ParseError",
            error_message=str(exc)[:8000],
        )
        session.add(log)
        session.commit()
        _logger.error("parse_failed", extra={"feed": spec.name, "err": str(exc)})
        return RunOutcome(
            success=False,
            fetch_log_id=log.id,
            snapshot_id=None,
            rows_inserted=0,
            error_type="ParseError",
            error_message=str(exc),
        )

    # ── 3. Normalize ────────────────────────────────────────────────────
    header = extract_header(message)
    rows = spec.normalize(
        message,
        fetched_at=fetched_at,
        feed_header_timestamp=header.feed_header_timestamp,
    )

    # ── 4. Persist (single transaction) ─────────────────────────────────
    log = FeedFetchLog(
        feed_name=spec.name,
        feed_url=spec.url,
        fetched_at=fetched_at,
        http_status=result.http_status,
        success=True,
        duration_ms=result.duration_ms,
        response_bytes=len(result.content),
        feed_header_timestamp=header.feed_header_timestamp,
        entity_count=header.entity_count,
    )
    session.add(log)
    session.flush()  # assign log.id

    snapshot = RawGtfsrtSnapshot(
        fetch_log_id=log.id,
        feed_name=spec.name,
        fetched_at=fetched_at,
        feed_header_timestamp=header.feed_header_timestamp,
        gtfs_realtime_version=header.gtfs_realtime_version,
        incrementality=header.incrementality,
        content_sha256=hashlib.sha256(result.content).hexdigest(),
    )
    session.add(snapshot)
    session.flush()  # assign snapshot.id

    for row in rows:
        # Every row model in the current design has a snapshot_id column.
        row.snapshot_id = snapshot.id  # type: ignore[attr-defined]
        session.add(row)

    session.commit()

    _logger.info(
        "fetch_ok",
        extra={
            "feed": spec.name,
            "bytes": len(result.content),
            "entities": header.entity_count,
            "rows": len(rows),
            "duration_ms": result.duration_ms,
            "snapshot_id": snapshot.id,
        },
    )

    return RunOutcome(
        success=True,
        fetch_log_id=log.id,
        snapshot_id=snapshot.id,
        rows_inserted=len(rows),
    )
