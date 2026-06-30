import sys

from apps.analytics.stop_projection import _pick_canonical_trip
sys.path.insert(0, "/Users/devrajsolanki/Documents/iROAM")
from apps.analytics.gtfs_static import GtfsStatic, load_all, resolve_route_id, resolve_shape_id
import pandas as pd
from apps.analytics.project_to_shape import project_trajectory
from apps.analytics.shapes import build_linestrings
from shapely.geometry import Point
from apps.analytics.shapes import transform_lonlat_to_meters

# load static gtfs trips data

static = load_all()
trips = static.trips

# 1. In scripts/analysis/, for one route+direction, list every distinct shape_id seen across trips and how many trips used each. (Pull from static.trips.)

# for each route and direction combination, list every shape.id used and the number of trips that use that shape.id
shape_variant_report = trips.groupby(["route_id", "direction_id", "shape_id"]).size().reset_index(name="trip_count")
#print(shape_variant_report)

# 2. Compare each trip's actual shape_id (resolve_shape_id, pipeline.py:180) against the canonical shape chosen by _pick_canonical_trip (stop_projection.py:60). 
#    Count: what % of trips ran a non-canonical shape?

# create a set of unique route_id and direction_id combinations
route_direction_combinations = set(zip(trips["route_id"], trips["direction_id"]))
#print(f"Unique route_id and direction_id combinations: {len(route_direction_combinations)}")

# determing canonical shape chosen by _pick_canonical_trip for each route_id and direction_id combination
canonical_shapes = {}
for route_id, direction_id in route_direction_combinations:
    canonical_trip_id = _pick_canonical_trip(static, route_id, direction_id)
    if canonical_trip_id is not None:
        trip_row = static.trips.loc[static.trips["trip_id"] == canonical_trip_id].iloc[0]
        shape_id = str(trip_row["shape_id"])
        canonical_shapes[(route_id, direction_id)] = shape_id


# determining actual shape for each trip (dont necessarily have to call resolve_shape_id, can just use the shape_id column in trips.txt)
actual_shapes = {}
for index, row in trips.iterrows():
    trip_id = row["trip_id"]
    route_id = row["route_id"]
    direction_id = row["direction_id"]
    shape_id = row["shape_id"]
    actual_shapes[(trip_id, route_id, direction_id)] = shape_id


# print percentage of trips that ran a non-canonical shape
non_canonical_count = 0
for (trip_id, route_id, direction_id), actual_shape_id in actual_shapes.items():
    canonical_shape_id = canonical_shapes.get((route_id, direction_id))
    if actual_shape_id != canonical_shape_id:
        non_canonical_count += 1

total_trips = len(actual_shapes)
non_canonical_percentage = (non_canonical_count / total_trips) * 100 if total_trips > 0 else 0
print(f"Percentage of trips that ran a non-canonical shape: {non_canonical_percentage:.2f}%")

# 3. For one non-canonical trip, show concretely that the same travel_distance_m maps to a different real-world location on the trip shape vs the canonical shape 

# find a non-canonical trip example that shares stops with its canonical trip (using sets)

for (trip_id, route_id, direction_id), actual_shape_id in actual_shapes.items():
    canonical_shape_id = canonical_shapes.get((route_id, direction_id))
    if actual_shape_id != canonical_shape_id:
        canonical_trip_id = _pick_canonical_trip(static, route_id, direction_id)
        canonical_stop_times = static.stop_times.loc[static.stop_times["trip_id"] == canonical_trip_id]
        non_canonical_stop_times = static.stop_times.loc[static.stop_times["trip_id"] == trip_id]

        # find a stop that is shared between the two trips
        shared_stops = set(canonical_stop_times["stop_id"]).intersection(set(non_canonical_stop_times["stop_id"]))
        if shared_stops:
            # print non-canonical trip info
            print(f"Non-canonical trip_id: {trip_id}, route_id: {route_id}, direction_id: {direction_id}, actual_shape_id: {actual_shape_id}")
            # print canonical trip info
            print(f"Canonical trip_id: {canonical_trip_id}, route_id: {route_id}, direction_id: {direction_id}, canonical_shape_id: {canonical_shape_id}")
            shared_stop_id = shared_stops.pop()
            stop_info = static.stops.loc[static.stops["stop_id"] == shared_stop_id].iloc[0]
            # print shared stop info
            print(f"Shared stop_id: {shared_stop_id}, stop_lat: {stop_info['stop_lat']}, stop_lon: {stop_info['stop_lon']}")
            break

if stop_info is None:
    print("No non-canonical trip with shared stops found")
else:
    # get the shape lines for both the canonical and non-canonical shapes
    canonical_shape_lines = build_linestrings(static.shapes.loc[static.shapes["shape_id"] == canonical_shape_id])
    non_canonical_shape_lines = build_linestrings(static.shapes.loc[static.shapes["shape_id"] == actual_shape_id])

    # project the same stop onto the canonical shape and the non-canonical shape to see the difference in travel distance (travel_distance_m)
    stop_x, stop_y = transform_lonlat_to_meters(stop_info["stop_lon"], stop_info["stop_lat"])
    stop_point = Point(stop_x, stop_y)

    canonical_distance = canonical_shape_lines[canonical_shape_id].project(stop_point)
    non_canonical_distance = non_canonical_shape_lines[actual_shape_id].project(stop_point)

    print(f"Stop {stop_info['stop_id']} projected onto canonical shape: {canonical_distance:.1f} m")
    print(f"Stop {stop_info['stop_id']} projected onto non-canonical shape: {non_canonical_distance:.1f} m")
    print(f"Difference: {abs(canonical_distance - non_canonical_distance):.1f} m")

# print out table of canonical trip stops
#canonical_stop_times = static.stop_times.loc[static.stop_times["trip_id"] == canonical_trip_id]
#print(canonical_stop_times[["stop_sequence", "stop_id", "arrival_time", "departure_time"]].sort_values("stop_sequence"))

# print out table of non-canonical trip stops
#non_canonical_stop_times = static.stop_times.loc[static.stop_times["trip_id"] == trip_id]
#print(non_canonical_stop_times[["stop_sequence", "stop_id", "arrival_time", "departure_time"]].sort_values("stop_sequence"))