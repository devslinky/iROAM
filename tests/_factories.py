"""Synthetic FeedMessage factories for tests — no network, no fixture files."""

from __future__ import annotations

from apps.collector import gtfs_realtime_pb2


def make_feed_message(
    *,
    feed_timestamp: int = 1_700_000_000,
    version: str = "2.0",
    entities: list[dict] | None = None,
) -> gtfs_realtime_pb2.FeedMessage:
    """Build a minimal ``FeedMessage`` with the given entities.

    Each ``entities`` dict accepts:
        id, trip_id, route_id, direction_id, start_date, start_time,
        schedule_relationship, vehicle_id, vehicle_label,
        timestamp, delay, stop_time_updates (list of dicts with
        stop_sequence, stop_id, arrival{time,delay},
        departure{time,delay}, schedule_relationship).
    """
    m = gtfs_realtime_pb2.FeedMessage()
    m.header.gtfs_realtime_version = version
    m.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    m.header.timestamp = feed_timestamp

    for spec in entities or []:
        e = m.entity.add()
        e.id = spec["id"]
        tu = e.trip_update
        trip = tu.trip
        if "trip_id" in spec:
            trip.trip_id = spec["trip_id"]
        if "route_id" in spec:
            trip.route_id = spec["route_id"]
        if "direction_id" in spec:
            trip.direction_id = spec["direction_id"]
        if "start_date" in spec:
            trip.start_date = spec["start_date"]
        if "start_time" in spec:
            trip.start_time = spec["start_time"]
        if "schedule_relationship" in spec:
            trip.schedule_relationship = spec["schedule_relationship"]
        if "vehicle_id" in spec:
            tu.vehicle.id = spec["vehicle_id"]
        if "vehicle_label" in spec:
            tu.vehicle.label = spec["vehicle_label"]
        if "timestamp" in spec:
            tu.timestamp = spec["timestamp"]
        if "delay" in spec:
            tu.delay = spec["delay"]

        for stu_spec in spec.get("stop_time_updates", []):
            stu = tu.stop_time_update.add()
            if "stop_sequence" in stu_spec:
                stu.stop_sequence = stu_spec["stop_sequence"]
            if "stop_id" in stu_spec:
                stu.stop_id = stu_spec["stop_id"]
            if "arrival" in stu_spec:
                arr = stu_spec["arrival"]
                if "time" in arr:
                    stu.arrival.time = arr["time"]
                if "delay" in arr:
                    stu.arrival.delay = arr["delay"]
                if "uncertainty" in arr:
                    stu.arrival.uncertainty = arr["uncertainty"]
            if "departure" in stu_spec:
                dep = stu_spec["departure"]
                if "time" in dep:
                    stu.departure.time = dep["time"]
                if "delay" in dep:
                    stu.departure.delay = dep["delay"]
                if "uncertainty" in dep:
                    stu.departure.uncertainty = dep["uncertainty"]
            if "schedule_relationship" in stu_spec:
                stu.schedule_relationship = stu_spec["schedule_relationship"]

    return m
