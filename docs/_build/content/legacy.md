# Legacy code — `data_process/arch_legacy/`

::: warning Archived
This directory holds **proof-of-concept code** from the original iROAM pipeline, before the system was restructured around the live GTFS-Realtime collector and the append-only Postgres schema. It is not imported or executed by any live process. Kept in the repo for reference only.
:::

The equivalent live functionality is in [`apps/analytics`](apps-analytics.html), which superseded these files. The legacy code is pandas/CSV-centric and was never integrated with the database.

## Files

| File | Successor in `apps/analytics` |
| --- | --- |
| `agent_schedule_check.py` | (no direct equivalent; was a schedule audit script) |
| `clean_and_combine.py` | `upsample.py` + `csv_export.py` |
| `data_processing_pipe.py` | `pipeline.py` + `runner.py` |
| `location_to_shape_projection.py` | `project_to_shape.py` |
| `travel_distance_calculation.py` | folded into `project_to_shape.py` |
| `trip_filtering.py` | `pipeline.list_trip_instances` |
| `trip_stop_schedule_check.py` | (no direct equivalent) |

If you are reading the codebase to understand what runs today, skip this directory.
