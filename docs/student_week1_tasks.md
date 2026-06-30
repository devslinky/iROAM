# Summer project — Week 1: measure first, fix the cheap correctness bugs

This week is about **seeing** what the preprocessing actually does
before we change anything. By next Monday we want (a) two suspected bugs confirmed or
cleared, (b) a data-quality report, and (c) a small hand-checked validation set.
Don't optimize anything yet — just measure and verify.

**Background reading (you may already finished):**
- Skim the pipeline so you know the data flow:
  `apps/analytics/pipeline.py` → `trajectory_extract.py` → `project_to_shape.py`
  → `upsample.py` → `stop_projection.py` → `apps/api/services/bus_grouping.py`.
- Ask for DB access and run one route (e.g. route 29) for one service date
  end-to-end so you've seen real rows.

**General rules:**
- Work on a branch, one task per commit. Don't edit production logic in Week 1
  except where a task explicitly says so (Tasks 1–4 are read-only analysis;
  Task 5 is the only code change, and it's small).
- Every script you write goes in `scripts/analysis/` and writes its output to
  `out/qa/`. Keep them re-runnable (take `--route` and `--date` args).
- When you find something surprising, write it down in `out/qa/findings.md` with
  a number and a one-line summary. We'll review it together.

---

## Task 1 — Confirm or clear bug #1: shape-variant coordinate mismatch

**Why:** We suspect `travel_distance_m` (measured on each trip's *own* shape) and
`stop_index` (read off the *canonical* shape) can be in different coordinate
systems for short-turn / diversion trips — which would make cross-bus gap and
bunching comparisons wrong.

**Steps:**
1. In `scripts/analysis/`, for one route+direction, list every distinct
   `shape_id` seen across trips and how many trips used each. (Pull from
   `static.trips`.)
2. Compare each trip's actual `shape_id` (`resolve_shape_id`, `pipeline.py:180`)
   against the canonical shape chosen by `_pick_canonical_trip`
   (`stop_projection.py:60`). Count: what % of trips ran a *non-canonical* shape?
3. For one non-canonical trip, show concretely that the same
   `travel_distance_m` maps to a different real-world location on the trip shape
   vs the canonical shape (e.g. compare shape lengths, or project a known stop on
   both).

**Deliverable:** `out/qa/shape_variant_report.md` answering: how often does this
happen, and does it actually move `stop_index`? Add a numbered entry to
`findings.md`.

**Done when:** we know whether this is a rare edge case or a routine source of
error for our target routes. **Do not fix it yet** — we'll design the fix
together once we know the size of the problem.

**Watch out for:** "canonical == trip shape for most trips" would make this a
non-issue for that route; that's a perfectly good (and likely) finding. The
point is to *measure*, not assume it's broken.

---

## Task 2 — Confirm or clear bug #2: is the teleport filter even on?

**Why:** `project_to_shape.py` documents a "teleport" outlier filter at length,
but `process_trip_instance` defaults `max_implied_speed_m_s=None`
(`pipeline.py:127`), which **disables** it. We need to know what production
actually runs.

**Steps:**
1. Trace who calls `process_trip_instance` (search `apps/analytics/runner.py`
   and the worker). Find what value, if any, is passed for
   `max_implied_speed_m_s`.
2. Write down the answer plainly: is the teleport filter ON or OFF in the real
   pipeline? If ON, with what threshold?
3. If it's OFF, estimate how many points it *would* remove by re-running
   `project_trajectory` with the filter enabled on a few trips — so we know what
   we're missing.

**Deliverable:** a numbered entry in `out/qa/findings.md` stating ON/OFF, the
threshold, and the would-be drop rate if off. No code change.

**Done when:** there's a one-line, sourced answer with the `file:line` where the
value is set.

---

## Task 3 — Build a data-quality profiler

**Why:** Every threshold in the code (200 m, 35 m/s, 0.5 m/s, …) is a guess
nobody has checked against real data. We can't tune what we can't see.

**Steps:**
1. Write `scripts/analysis/profile_quality.py` that takes `--route` and `--date`
   and pulls the raw `vehicle_positions` rows plus the processed
   `trip_trajectories` for that slice.
2. Compute and print (and save a CSV/markdown to `out/qa/`):
   - **Drop rates** at each filter stage — how many raw points enter, and how
     many are removed by:
     - off-route filter (`orthogonal_distance_m > 200`, `project_to_shape.py:92`),
     - teleport filter (`project_to_shape.py:103`),
     - exact-timestamp dedup (`trajectory_extract.py:92`),
     - ghost-segment drop (`bus_grouping.py:84`).
     (Add temporary counters/logging or re-run the functions in the script.)
   - **Orthogonal distance** distribution: p50 / p90 / p95 / p99 / max.
   - **GPS cadence**: distribution of seconds between consecutive raw points per
     trip, and the count + size of internal gaps > 2 min.
   - **Clock skew**: distribution of `vehicle_timestamp − fetched_at`.
   - **Field availability** — % of rows where each of these is non-null:
     `occupancy_status`, `occupancy_percentage`, `current_stop_sequence`,
     `stop_id`, `current_status`, `bearing`, `speed_mps`, `odometer`,
     `direction_id`, `vehicle_id`.
3. Run it for at least 3 routes × 3 dates so we see variation.

**Deliverable:** `scripts/analysis/profile_quality.py` + `out/qa/profile_*.md`.

**Done when:** I can read one page and know, for a given route/date, how much
data each filter removes and which feed fields are actually populated.

**Watch out for:** the off-route and teleport filters live *inside*
`project_trajectory`, so to count them separately you'll need to call the steps
manually or add counters — don't just diff input/output totals.

---

## Task 4 — Build a small ground-truth validation set

**Why:** We need a fixed yardstick to judge every later change. Right now nobody
can say whether a projection or a bunching event is correct.

**Steps:**
1. Pick ~10 trips across 2–3 routes. For each, plot the time–distance chart and
   the GPS points on a map (reuse whatever the dashboard / `iroam.html` already
   draws, or a quick matplotlib + folium notebook).
2. For each trip, **eyeball and record** in `out/qa/groundtruth_projection.csv`:
   - Did the projection pick the right leg of the route (no obvious wrong-leg
     jumps / diagonals)? yes / no / partial + a note.
3. From the dashboard, pick **~20 bunching events** and **~20 idle events** and
   record in `out/qa/groundtruth_events.csv` whether each looks real
   (`true` / `false` / `unsure`) with a one-line reason.
4. Keep the trip IDs / dates fixed and committed so we can re-evaluate against
   the same set later.

**Deliverable:** two CSVs in `out/qa/` + a short `out/qa/groundtruth_README.md`
explaining how you chose the examples and what each column means.

**Done when:** we have a committed, re-usable set of ~10 checked projections and
~40 checked events with a yes/no/unsure verdict.

**Watch out for:** keep it small and honest. 10 carefully-checked trips beat 100
rushed ones. Mark anything you're unsure about as `unsure`, don't guess.

---

## Task 5 — Add a max-gap cap to upsampling *(the one code change this week)*

**Why:** `upsample_df` inserts a synthetic point every 10 s between *any* two
real rows with no limit (`upsample.py`). A 6-minute GPS outage becomes ~36
fabricated points on a straight ramp, which later causes false idle/bunch
events. The labels code already caps gaps (`labels.py`, `max_gap_s`); upsampling
should too.

**Steps:**
1. Add a `max_gap_seconds: float | None = None` parameter to `upsample_df`. When
   the gap between a consecutive pair exceeds it, **don't** emit synthetic points
   across that pair (leave the real gap as a gap).
2. Thread a config value through `process_trip_instance` (sensible default:
   ~`3 × upsample_resolution_s`, e.g. 30 s — but confirm against the cadence
   distribution from Task 3).
3. **Tests:** add a case in `tests/test_upsample_parity.py` (or a new test file)
   proving (a) normal short gaps still upsample identically to before, and
   (b) a long gap produces no synthetic points across it.
4. Run the existing upsample parity tests — they must still pass with the
   default (`max_gap_seconds=None` = old behavior).

**Deliverable:** the edit + tests, on a branch, with parity tests green.

**Done when:** old behavior is bit-identical when the cap is unset, and a long
gap is provably not bridged when it's set.

**Watch out for:** this changes stored data, so default to *off* (`None`) and
only enable via config. Don't backfill the table this week — just land the
capability and the test.

---

## End-of-week handoff

Write `out/qa/week1_summary.md` (half a page) covering:
- The verdict on both suspected bugs (Tasks 1 & 2).
- The headline numbers from the profiler (drop rates, field availability).
- Anything surprising in `findings.md`.

We'll use this to lock the Week 2 plan. Don't start Week 2 tasks until we've
reviewed Week 1 together — the measurements may change what's worth doing.
