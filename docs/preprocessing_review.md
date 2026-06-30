# Preprocessing review: vehicle projection & anomaly labelling

A working-engineer + data-analyst pass over the preprocessing stack
(`apps/collector`, `apps/analytics`, `apps/api/services/bus_grouping.py`,
`data_process/bunching/labels.py`). The current code is well-documented and the
recent hardening (UTM meters, teleport filter, monotone stop projection,
headway labels) is real progress. But the chain still rests on a handful of
load-bearing **assumptions** that are not measured, and several strong signals
the feed already gives us are thrown away. This note is organized as:

- **§0 Cross-cutting** — do these first; they make everything else measurable.
- **§1–§4** — vehicle projection / resampling / speed / stop mapping.
- **§5–§7** — grouping, anomaly detection, bunching labels.
- **§8** — prioritized roadmap.

Each item is tagged **[check]** (verify an assumption / add a metric),
**[improve]** (harden existing logic), or **[method]** (a different technique
worth trying). 🔺 marks the highest-leverage items.

---

## §0. Cross-cutting — the things that block everything else

### 🔺 0.1 You are tuning blind. Build a data-quality profiler first. **[check]**
There is no instrumentation that tells you *how often* each assumption fails.
Before changing any threshold, emit a per-day / per-route QA report covering:

- % of raw points dropped by each filter: off-route (`orthogonal > 200 m`),
  teleport (`project_to_shape.py:92`, `:103`), ghost segments
  (`bus_grouping.py:84`), exact-timestamp dedup (`trajectory_extract.py:92`).
- Distribution of `orthogonal_distance_m` (p50/p95/p99/max) — this tells you
  whether 200 m is even close to right.
- GPS sampling cadence: distribution of Δt between consecutive raw points, and
  the size/number of internal gaps per trip.
- Clock skew: `vehicle_timestamp − fetched_at` distribution.
- Feed-field availability: % rows with non-null `occupancy_status`,
  `current_stop_sequence`, `current_status`, `bearing`, `speed_mps`, `odometer`,
  `direction_id`. Several recommendations below are only worth it if the field
  is actually populated — measure first.
- % of trips whose actual `shape_id` ≠ the canonical shape for their
  (route, direction) (see §4.1 — this is a correctness bug, not just a metric).

Without this you cannot tell a real improvement from a lucky threshold.

### 🔺 0.2 No ground truth → no way to know if any of this is correct. **[check]**
Hand-label a small validation set and keep it in the repo:
- ~50 projected trajectories visually checked against the route on a map
  (correct leg? plausible distance monotonicity?).
- ~30 bunching events and ~30 idle events confirmed/refuted by eye on the
  time–distance chart.

This becomes the regression target for every change below. Right now every
threshold (200 m, 35 m/s, 0.5 m/s, 100 m, 150 m, 20 min, 500 m) is asserted, not
validated.

### 🔺 0.3 You are discarding signals that are already in the row. **[method]**
`VehiclePosition` stores these and the analytics path ignores all of them:
- **`bearing`** — direction of travel. The single cheapest fix for wrong-leg
  snapping on out-and-back shapes: a bus on the return leg has ~opposite bearing
  to the outbound tangent. Use it in projection (§1).
- **`current_stop_sequence` / `stop_id` / `current_status`** — a near-ground-truth
  anchor for *which stop* the bus is at/approaching. This can constrain
  projection and gives `stop_index` almost directly, independent of geometry
  (§1.4, §4).
- **`speed_mps`** (feed-reported) — cross-check or replace the noisy
  finite-difference speed (§3).
- **`odometer`** — monotone true distance; cross-check projected distance,
  detect wrong-leg jumps.

Auditing availability (§0.1) tells you which are usable on the TTC feed.

### 0.4 Thresholds are hard-coded and scattered. **[improve]**
`200.0`, `35.0`, `0.5`, `100.0`, `150.0`, `_STALE_MIN_SEC`,
`_MIN_SEGMENT_DISPLACEMENT_M`, `_MONOTONE_TOL_M`, occupancy percent map — these
live as module constants across 6 files. The user's "not flexible" complaint is
literally this. Centralize into `core/config.py` (or a `PreprocessingConfig`
dataclass) so they can be swept, A/B'd, and made route-adaptive. Several
*should* be adaptive, not global (e.g. orthogonal tolerance vs urban density;
bunch threshold vs scheduled headway).

---

## §1. Vehicle projection (`project_to_shape.py`)

The core method is independent nearest-point projection
(`shapely.line_locate_point`) per GPS sample, followed by two outlier filters.
This is the weakest link in the whole pipeline and the source of most downstream
band-aids.

### 🔺 1.1 Replace nearest-point projection with an HMM / Viterbi map-match. **[method]**
Independent nearest-point snapping has no notion of path continuity, so it snaps
to the wrong leg of self-overlapping shapes, loops, and parallel-street
segments. The teleport filter (`_implied_speed_keep_mask`) and the monotone
stop re-projection (`stop_projection.py:151`) are both *downstream patches for
this one root cause*.

Because you already have the trip's shape, you don't need full road-network
map-matching — a **1-D HMM along the shape** suffices:
- **States**: candidate along-shape positions for each GPS point (all local
  minima of distance-to-shape, not just the global nearest — that's the key:
  keep *all* legs as candidates).
- **Emission cost**: orthogonal distance (optionally Gaussian on GPS accuracy)
  **+ a bearing term** (mismatch between GPS bearing and shape tangent at the
  candidate) — this is what kills wrong-leg snaps.
- **Transition cost**: |Δalong-shape − v·Δt| — penalize physically impossible
  jumps between consecutive points (Newson & Krumm 2009 formulation).
- **Viterbi** to pick the globally consistent path.

This *replaces* both outlier filters with a principled smoother, stops
*discarding* good points (the teleport filter drops data; Viterbi re-assigns it
to the correct leg), and is robust to the anchor-poisoning failure mode in
§1.2. Off-the-shelf options if you don't want to hand-roll: `leuven.mapmatching`
(pure-Python, takes a custom graph), or `fmm`/Valhalla if you later want true
network matching.

### 1.2 The teleport filter has correctness gaps. **[improve]**
`_implied_speed_keep_mask` (`project_to_shape.py:38`) is a greedy forward pass
anchored on the last *kept* point. Issues:
- **Anchor poisoning at the head**: if point 0 is itself a wrong-leg projection,
  every subsequent point is measured against a bad anchor. There's no check that
  the initial anchor is sane.
- **It only catches *large* along-route jumps.** A wrong-leg snap that lands at a
  plausible distance (common on tight out-and-back loops where the two legs are
  <35 m·Δt apart in projected distance) passes the filter and silently corrupts
  distance/speed.
- **It deletes data** instead of correcting it — those points are lost to idle,
  bunching, and occupancy. (Viterbi, §1.1, keeps them.)
- `DEFAULT_MAX_IMPLIED_SPEED_M_S = 35` is global; a true express segment vs a
  downtown crawl have very different plausible maxima. At minimum make it
  configurable; better, derive from a robust quantile of the trip's own speeds.

**[check]** Confirm which `max_implied_speed_m_s` the production path actually
uses: `pipeline.process_trip_instance` defaults it to **`None`**
(`pipeline.py:127`) which *disables* the teleport filter unless the runner
passes a value. So the filter described at length in the module docstring may be
off in production. Verify the runner's argument.

### 1.3 `max_orthogonal_distance_m = 200` is large and global. **[check]/[improve]**
200 m is a very wide corridor — it won't reject a snap to a parallel route/branch
within 200 m, yet may be too loose to catch genuinely off-route GPS in open
areas. Profile the actual orthogonal-distance distribution (§0.1) and consider:
making it adaptive (tighter where shape variants run close together), or
replacing the hard cutoff with the emission probability in the HMM.

### 1.4 Anchor projection to the feed's own stop reports. **[method]**
When `current_stop_sequence`/`stop_id` is present with `current_status =
STOPPED_AT`/`INCOMING_AT`, the feed is telling you the bus's position to
stop-level accuracy. Use it to (a) seed/validate the map-match, and (b) directly
correct `stop_index` without trusting geometry. This is far more reliable than
projecting GPS near a self-overlap.

### 1.5 GPS accuracy is treated as uniform. **[check]**
Every point is weighted equally. If the feed carries any accuracy/HDOP hint (or
you infer it from point clustering), down-weight low-accuracy fixes rather than
hard keep/drop.

---

## §2. Resampling / upsampling (`upsample.py`)

### 🔺 2.1 No max-gap cap → fabricated motion across data holes. **[improve]**
`upsample_df` inserts a synthetic point at every 10 s boundary between *any* two
consecutive real rows, with distance from constant-velocity extrapolation
(`upsample.py:97`). There is **no maximum-gap guard**. A 6-minute GPS outage
becomes ~36 fabricated points on a smooth ramp, written into
`trip_trajectories` as if observed-ish. Contrast `labels.py:_bus_points_on_grid`
which *does* cap at `max_gap_s = 2·step`. Two different resampling policies for
the same quantity. Add a `max_gap_seconds` cap to `upsample_df` and leave true
gaps as gaps. (This directly causes false idle/bunch events downstream.)

### 🔺 2.2 Constant-velocity interpolation erases dwell — the signal you care about. **[improve]/[method]**
Linear distance interpolation between samples (`upsample.py:97`) smooths over
stop dwells: a bus that stopped 40 s at a stop and then moved is rendered as a
constant ramp, so (a) idle detection misses the dwell, and (b) bunching/headway
features see fake motion. For a *bunching* product, dwell at stops is the core
dynamic. Options:
- Snap-aware interpolation: hold distance flat near known stop locations when
  speed→0, ramp between.
- Monotone cubic (PCHIP) on cumulative distance to avoid overshoot while
  respecting curvature.
- Or **don't store a resampled table at all** — see 2.3.

### 2.3 Is the stored upsample even necessary? **[check]**
`labels.py` re-grids from the raw-ish points anyway (`_bus_points_on_grid`), and
anomaly detectors interpolate too (`anomalies.py:_interp_sorted`,
`_first_crossings`). The 10 s upsampled table may be (a) redundant work and (b) a
second place the constant-velocity assumption is baked in. Decide on **one**
canonical resampling layer and have every consumer use it. If the table is only
for the dashboard, keep it but flag synthetic-across-gap points so consumers can
exclude them.

### 2.4 The legacy speed convention is fragile. **[check]**
"speed stored on the arriving row," then `upsample_df` uses "the next row's
speed" (`upsample.py:103`, `compute_moving_speed:42`). It's parity-tested against
the legacy loop, but the convention is non-obvious and couples three files. Add
an explicit test that an inserted point's speed/distance are mutually consistent
(`dist[k] ≈ dist[k-1] + speed·Δt`).

---

## §3. Speed (`upsample.compute_moving_speed`)

### 🔺 3.1 Raw finite-difference speed is noisy and feeds everything. **[improve]**
`speed = Δdist/Δt` with no smoothing, no outlier rejection, no non-negativity
clamp. One bad projection → one speed spike → corrupted idle detection, dwell
estimate, `gap_closure`, `rel_speed_to_d1`, `target_accel_3tick`. Projection
wobble on near-straight segments also produces small **negative** along-shape
speeds that flow into features unclamped.
- Clamp to ≥ 0 (or carry sign explicitly and decide what negative means).
- Smooth with a short robust filter (median-of-3, then Savitzky-Golay) or a
  Kalman/constant-acceleration filter on (distance, speed).
- **[method]** Cross-check against the feed's `speed_mps`; large disagreement is
  a projection-quality flag.

### 3.2 Speed is undefined across the dedup/gap boundaries. **[check]**
`dist_diff/time_diff` across a large gap yields a tiny but nonzero speed that
looks like "barely moving" → spurious idle. Tie speed validity to the same
max-gap policy as resampling (§2.1).

---

## §4. Stop mapping & cross-shape comparability (`stop_projection.py`)

### 🔺 4.1 travel_distance_m and stop_index can be in different coordinate systems. **[check]/[improve]**
This is a latent correctness bug, not a style issue:
- Projection uses the **trip's own** shape: `resolve_shape_id(static, trip_id)`
  → `shape_lines[shape_id]` (`pipeline.py:180`, `:201`). So
  `travel_distance_m` is measured along *that trip's* shape.
- But `distance_to_stop_index` (`bus_grouping.py:132`) maps that distance through
  the **canonical** shape's stop-distance array (`compute_route_stops` →
  `_pick_canonical_trip`, the single most-common shape).

When a trip ran a short-turn / diversion / express variant (≠ canonical), its
`travel_distance_m` is in variant-meters but `stop_index` is read off
canonical-meters. The two don't align. Worse, the **distance-based bunching
detector and the labels compare `travel_distance_m` *across buses*** — which is
only meaningful if all buses share one shape coordinate. Buses on different
variants have **incomparable** distances, so leader/gap computation
(`labels._forward_gap_row`, `anomalies.detect_bunch_events_distance`) is
silently wrong for mixed-variant slices.

Fixes, in order of effort:
1. Measure how often it happens (§0.1 last bullet).
2. Project *all* buses in a slice onto **one common reference shape** before
   computing inter-bus gaps, OR
3. Convert each bus's position to a **shared linear reference** (e.g. fraction of
   route / common `stop_index` in real-valued form) and compute gaps in *that*
   space, not raw meters. `stop_index` is already shared-ish — gaps could be
   computed in interpolated-stop-distance space tied to the canonical shape.

### 4.2 Canonical-shape selection drops variant structure. **[check]**
`_pick_canonical_trip` picks most-common `shape_id`, ties → most stop_times.
Reasonable default, but: branches, seasonal shapes, and direction-ambiguous
loops all collapse to one. Log the set of shapes seen per (route, direction) and
their frequencies so you know what's being flattened.

### 4.3 stop_index is non-uniform in distance; anomaly logic mixes the two. **[check]**
`detect_bunch_events` (time-based) works in **integer stop crossings**
(`_first_crossings`), while the distance detector works in **meters**. Downtown
stops are ~200 m apart, suburban ~500 m+, so "same stop index gap" and "150 m
gap" mean different things in different places. Pick one space (meters, tied to a
common reference per §4.1) and express thresholds there; or make the stop-based
threshold adaptive to local stop spacing.

---

## §5. Grouping & segmentation (`bus_grouping.py`)

### 5.1 Stale-run segmentation depends on the very speed it can't trust. **[check]**
`segment_vehicle_points` splits on runs of `|speed| < 0.5` lasting ≥ 20 min and
drops segments moving < 500 m. Since speed is the noisy finite-difference (§3),
and since a long legitimate layover or a congested crawl can look like a stale
run, validate against `current_status`/`vehicle_timestamp` staleness (a frozen
`vehicle_timestamp` is a *direct* ghost signal — far more reliable than inferring
it from projected speed).

### 5.2 The grouping key assumes input ordering. **[check]**
`group_into_buses` relies on rows arriving sorted by
`(trip_id, start_date, vehicle_id, datetime)` (docstring) and flushes on key
change. If the query's ORDER BY ever drifts, two interleaved vehicles silently
merge. Add a cheap assertion or group explicitly rather than trusting sort order.

### 5.3 `vehicle_id` missing → key collapses. **[check]**
TTC may omit `vehicle_id`; the key `(trip_id, start_date, None)` then merges all
unlabeled vehicles on that trip_id. Audit null-`vehicle_id` rate (§0.1) and
decide a fallback (e.g. split on large time/distance discontinuity even within a
key).

---

## §6. Anomaly detection (`anomalies.py`)

### 6.1 Idle conflates five physically different states. **[improve]**
`is_idle = speed < 0.5` (`anomalies.py:123`) treats red lights, stop dwells,
congestion, terminal layovers, and frozen-GPS ghosts identically — and runs on
the *upsampled* speed, so it inherits both failure modes of §2 (fabricated
zero-motion across gaps → false idle; constant-velocity smoothing over a real
dwell → missed idle). Improvements:
- Run idle on observed points (or gap-capped resampling), not blind upsample.
- Distinguish "dwell at stop" (near a stop, short) from "stuck/idle" (not near a
  stop, long) using `stop_index` proximity and `current_status`.
- Exclude the terminal/layover band (you already have `edge_exclude` in labels —
  reuse it here).

### 6.2 Crowd detection may be mostly noise — verify the feed populates it. **[check]**
`OCCUPANCY_PCT` is a fixed enum→percent map with arbitrary cut points
(`STANDING_ROOM_ONLY = 75`, etc.) and the doc admits the values are advisory.
**First check how often TTC actually sets `occupancy_status`** (§0.1). If it's
sparse, crowd events are an artifact of a few labeled trips. Also: prefer the
numeric `occupancy_percentage` field when present (it's stored on the row) over
the coarse enum map.

### 6.3 Distance bunching detector interpolates across unbounded gaps. **[improve]**
`detect_bunch_events_distance` builds per-bus tracks and `_interp_sorted` clamps
at endpoints but **interpolates straight across any internal gap** — a bus with a
10-min hole gets a fabricated straight line and can falsely read as "close to"
another bus. Apply the same `max_gap_s` validity mask used in `labels.py`.
Also note the cross-shape comparability problem (§4.1) applies directly here.

### 6.4 "No leader" vs "leader not currently tracked" are conflated. **[improve]**
See §7.2 — same root issue, and it affects the live distance detector too: a
leader with a momentary telemetry gap makes the follower look isolated.

### 6.5 Time-based and distance-based bunching can double-count. **[check]**
With `bunch_method="both"`, the same physical event emits one `method="time"`
and one `method="distance"` event at slightly different stop/time. Confirm the
dashboard/aggregation dedups, or you'll over-report.

---

## §7. Bunching labels (`data_process/bunching/labels.py`)

### 🔺 7.1 The primary label is a spatial gap, but bunching is a *headway* concept. **[improve]**
`labels[h] = 1 if forward_gap < 100 m` (`labels.py:684`). A 100 m gap is ~9 s at
40 km/h but ~72 s at 5 km/h — completely different service states. You've already
built the right quantity (`labels_headway_s`) and cite the literature rule
(h ≤ 0.25 × scheduled). **Promote the headway-ratio label to primary** and keep
the spatial one only for back-compat. **[check]** Confirm which label the
deployed model is actually trained on — the docstring says the spatial label is
"kept bit-identical," which suggests the headway label may be scaffolding that
nothing trains against yet.

### 🔺 7.2 `NO_LEADER_GAP_M = 20000` injects false negatives from missingness. **[improve]**
The forward gap is computed only among buses *currently interpolated-valid in
this slice*. If the real leader has a telemetry gap at tick k, the target looks
leaderless → gap = 20 km → `label = 0` — even if it's genuinely bunched. This is
**label noise correlated with data sparsity**, and sparsity correlates with
exactly the disrupted operations where bunching happens. Distinguish "truly no
leader ahead on the route" from "leader temporarily untracked," and emit NaN (not
0) for the latter so it's excluded rather than mislabeled.

### 🔺 7.3 The "all-history-valid" gate biases the training set. **[check]/[improve]**
An example is kept only if all `seq_len` history ticks are valid and every window
value is finite (`labels.py:510`, `:660`). This systematically **excludes
trips with sparse telemetry** — i.e. the messy, irregular trips most likely to
bunch. Quantify the drop rate and whether dropped t_refs are more bunched than
kept ones (selection-bias check). Consider masked/padded history with a validity
channel instead of hard exclusion.

### 7.4 A single missing future tick truncates the whole label vector. **[improve]**
`for h in range(pred_len): ... if not valid[k_fut]: break` (`labels.py:671`). One
gap at h=5 nullifies h=6…30. Use `continue` with NaN for the missing horizon (as
terminal-masking already does two lines later) instead of `break`, so isolated
holes don't erase the long-horizon labels.

### 7.5 Scheduled headway is the terminal-departure gap, not the in-service headway. **[check]**
`schedule_headways.scheduled_headway_s` = gap between consecutive scheduled
*first departures* on the route+direction (`schedule_headways.py:118`). Caveats:
- Headways vary across the day (peak/off-peak); the terminal gap is a coarse
  proxy for the headway the bus actually had at its current location.
- **Interlined branches** sharing a corridor: consecutive trip starts may belong
  to different branches, so the "previous start" isn't the real preceding bus →
  wrong scheduled headway. Verify route 29 / your target routes aren't branched,
  or compute headway per branch.
- Assumes on-time departure. Fine as a *scheduled* reference, but don't confuse
  it with realised headway.

### 7.6 Constant-velocity grid interpolation reaches into labels too. **[check]**
`_bus_points_on_grid` linearly interpolates dist/stop/speed; the same dwell-
erasing concern as §2.2 applies to the features and to `_headway_at`'s
`_time_at_distance` inversion. The `cummax` in `_passage_track` flattens wobble
but also means a bus that genuinely backtracks (loop/short-turn) inverts wrong.

### 7.7 Feature provenance / leakage audit. **[check]**
`time_to_terminus_min` uses a forward-looking-ish median speed and a 1 m/s floor;
`dist_to_terminus` uses canonical `route_shape_length_m` (mismatched for variants
per §4.1). Confirm no label leakage (no feature is computed from future ticks)
and that terminus features degrade gracefully for short-turn trips that never
reach the canonical terminus.

---

## §8. Suggested order of work (highest leverage first)

1. **Instrument (§0.1) + ground-truth set (§0.2).** Everything else is
   un-evaluable without these. ~1–2 days, unblocks all tuning.
2. **Confirm the variant/coordinate-mismatch (§4.1) and the disabled teleport
   filter (§1.2 check).** These are potential *correctness* bugs; cheap to
   verify, possibly already biasing your model.
3. **Add max-gap caps everywhere (§2.1, §3.2, §6.3).** Small, removes a whole
   class of fabricated idle/bunch artifacts.
4. **Headway-first labels + missingness→NaN (§7.1, §7.2, §7.4).** Directly
   improves the thing you're modelling; mostly label-side, no projection rework.
5. **Use bearing + feed stop reports in projection (§0.3, §1.4).** Cheap, high
   payoff for wrong-leg snapping, and a stepping stone to:
6. **HMM/Viterbi map-match (§1.1)** — the structural fix that retires the
   teleport filter, the monotone re-projection, and the diagonal artifacts at
   once. Bigger lift; do it once the cheaper wins are banked and measurable.
7. **Speed smoothing (§3) and dwell-aware resampling (§2.2).** Improves idle,
   dwell features, and gap-closure features together.
8. **Centralize + make thresholds adaptive (§0.4).** Ongoing; do as you touch
   each module.

---

### One-line summary
The pipeline is a chain of independent per-point heuristics with hard-coded
global thresholds and no measurement layer. The two structural wins are (a) a
continuity-aware map-match that uses bearing and the feed's own stop reports
instead of blind nearest-point snapping, and (b) computing inter-bus gaps in a
single shared reference with proper missing-data handling so bunching labels stop
being corrupted by data sparsity and shape-variant mismatch. But do the
instrumentation and ground-truth work *first* — right now you can't see which of
these assumptions is actually costing you.
