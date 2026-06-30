"""Parse raw protobuf bytes into a ``FeedMessage``.

Kept separate from the fetcher and normalizer so each layer is independently
testable against a recorded fixture.

The TTC endpoint can serve either the binary wire format (``?format=binary``)
or protobuf text format by default. We try binary first and transparently
fall back to text if the wire decode fails — this makes the collector robust
to either endpoint configuration.

Uses the vendored ``apps.collector.gtfs_realtime_pb2`` bindings, not the
``gtfs-realtime-bindings`` package: no released version of that package knows
the ``TripModifications`` / ``Shape`` entities the TTC detour feed carries.
Never import both in one process — they declare the same proto symbols and
protobuf's descriptor pool will reject the duplicate. Regenerate with::

    curl -sO https://raw.githubusercontent.com/google/transit/master/gtfs-realtime/proto/gtfs-realtime.proto
    python -m grpc_tools.protoc -I. --python_out=apps/collector gtfs-realtime.proto
"""

from __future__ import annotations

from google.protobuf import text_format

from apps.collector import gtfs_realtime_pb2


class ParseError(Exception):
    """Raised when payload cannot be decoded as a FeedMessage."""


def parse_feed_message(payload: bytes) -> gtfs_realtime_pb2.FeedMessage:
    """Decode ``payload`` into a ``FeedMessage`` (binary first, text fallback).

    Raises ``ParseError`` if neither format decodes cleanly. The caller is
    responsible for recording the failure in ``feed_fetch_logs``.
    """
    if not payload:
        raise ParseError("empty payload")

    message = gtfs_realtime_pb2.FeedMessage()
    binary_exc: Exception | None = None
    try:
        message.ParseFromString(payload)
        if message.HasField("header"):
            return message
        binary_exc = ValueError("FeedMessage missing header after binary parse")
    except Exception as exc:  # protobuf raises various exception types
        binary_exc = exc

    # Binary decode failed — the server may be serving protobuf text format.
    # Detect cheaply by checking for ASCII "header {" near the start.
    try:
        head = payload[:64].decode("ascii", errors="ignore").lstrip()
    except Exception:
        head = ""
    if head.startswith("header"):
        try:
            message.Clear()
            text_format.Parse(payload.decode("utf-8"), message)
            if not message.HasField("header"):
                raise ValueError("parsed text FeedMessage missing header")
            return message
        except Exception as exc:
            raise ParseError(
                f"text-format decode failed after binary decode failed: {exc}"
            ) from exc

    raise ParseError(f"protobuf decode failed: {binary_exc}") from binary_exc
