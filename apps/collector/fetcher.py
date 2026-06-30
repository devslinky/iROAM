"""HTTP fetcher for GTFS-RT endpoints.

Thin wrapper around ``httpx`` that encapsulates timeout, retry on transient
errors, and User-Agent headers. Returns a ``FetchResult`` that carries the
raw bytes, timing, and status — enough for the runner to build a
``FeedFetchLog`` regardless of success or failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from core.config import get_settings
from core.logging import get_logger

_logger = get_logger(__name__)


class FetchError(Exception):
    """Raised when a fetch ultimately fails after all retries."""

    def __init__(self, message: str, *, error_type: str, http_status: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.http_status = http_status


@dataclass
class FetchResult:
    url: str
    http_status: int
    content: bytes
    duration_ms: int


def fetch_bytes(url: str) -> FetchResult:
    """GET ``url`` and return the raw body.

    Retries ``COLLECTOR_HTTP_RETRIES`` times on connect/timeout errors and
    on 5xx responses (with linear backoff). 4xx is treated as terminal.
    """
    settings = get_settings()
    last_exc: Exception | None = None

    headers = {
        "User-Agent": settings.collector_user_agent,
        "Accept": "application/x-google-protobuf, application/octet-stream",
    }
    timeout = httpx.Timeout(settings.collector_http_timeout_seconds)
    attempts = settings.collector_http_retries + 1

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            try:
                response = client.get(url)
                duration_ms = int((time.monotonic() - t0) * 1000)
                if 500 <= response.status_code < 600:
                    _logger.warning(
                        "fetch_server_error",
                        extra={
                            "url": url,
                            "status": response.status_code,
                            "attempt": attempt,
                        },
                    )
                    last_exc = FetchError(
                        f"Server error {response.status_code}",
                        error_type="HTTPServerError",
                        http_status=response.status_code,
                    )
                    if attempt < attempts:
                        time.sleep(0.5 * attempt)
                        continue
                    raise last_exc
                response.raise_for_status()
                return FetchResult(
                    url=url,
                    http_status=response.status_code,
                    content=response.content,
                    duration_ms=duration_ms,
                )
            except httpx.HTTPStatusError as exc:
                raise FetchError(
                    f"HTTP {exc.response.status_code}: {exc}",
                    error_type="HTTPStatusError",
                    http_status=exc.response.status_code,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # TimeoutException covers connect/read/write/pool timeouts;
                # TransportError covers connect/read/write errors and protocol
                # hiccups — all transient classes worth retrying.
                last_exc = exc
                _logger.warning(
                    "fetch_transient_error",
                    extra={
                        "url": url,
                        "error_type": type(exc).__name__,
                        "attempt": attempt,
                    },
                )
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise FetchError(
                    f"Transient error after {attempts} attempts: {exc}",
                    error_type=type(exc).__name__,
                ) from exc
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"HTTP error: {exc}",
                    error_type=type(exc).__name__,
                ) from exc

    # Unreachable: loop either returns or raises.
    raise FetchError(
        "fetch loop exhausted without result",
        error_type="UnknownFetchError",
    )
