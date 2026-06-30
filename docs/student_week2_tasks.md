# Summer project — Week 2: better labels and better projection

Week 1 was measurement. Week 2 is improvement — but every change must be
**evaluated against the Week 1 ground-truth set** (`out/qa/groundtruth_*.csv`).
A change isn't "done" until you've shown it helps (or at least doesn't hurt) on
that set.

**Before you start:** confirm with me which Week 1 findings change the priorities
below — if Task 1 (shape variants) showed the coordinate mismatch is common, that
becomes the top job and bumps something off this list.

**General rules:**
- Branch per task, tests for every behavior change.
- For each task, add a short before/after comparison on the ground-truth set to
  `out/qa/week2_eval.md`.
- Labels-side changes are versioned (note the `*_SCHEMA_V` pattern in
  `data_process/bunching/labels.py`): bump a version, never silently change an
  existing schema's meaning.

---

## Task 6 — Make bunching labels headway-based, not spatial-gap-based

**Why:** The primary training label is `gap < 100 m` (`labels.py:684`). But 100 m
is ~9 s at 40 km/h and ~72 s at 5 km/h — totally different service states.
Bunching is defined on *headway* (time), and you already compute
`labels_headway_s`. We just aren't using it as the label.

**Steps:**
1. First, **confirm which label the deployed model trains on.** Check
   `apps/prediction/train_bag.py` and `bagged_predictor.py` — does `y` come from
   `labels`, `labels_persist`, or `labels_headway_s`? Write the answer down.
2. Add a new labels schema version that defines the primary label as
   `headway_s ≤ HEADWAY_RATIO_BUNCHED × sched_headway_s` (the constant
   `HEADWAY_RATIO_BUNCHED = 0.25` and `sched_headway_s` already exist).
3. Keep the old spatial label available under its current name for back-compat —
   don't delete it.
4. Handle the "no scheduled headway known" case explicitly (NaN label, excluded
   from training — see `schedule_headways.scheduled_headway_s` returning `None`).

**Deliverable:** new schema version + a note in `labels.py`'s schema docs +
counts of how many examples get a valid headway label vs NaN.

**Done when:** we can build a dataset whose labels are headway-ratio-based, and
the example counts / positive rate are reported.

**Watch out for:** `sched_headway_s` is the *terminal-departure* gap and can be
wrong for interlined/branched routes. Check whether your target routes are
branched before trusting it; flag it if so.

---

## Task 7 — Stop letting missing data masquerade as "no bunching"

**Why:** Two label-noise bugs, both making bunched situations look un-bunched:
- `NO_LEADER_GAP_M = 20000`: when a leader has a momentary telemetry gap, the
  follower looks leaderless → gap 20 km → `label = 0`, even if truly bunched.
- A single missing future tick `break`s the whole horizon loop
  (`labels.py:671`), nulling all later horizons.

**Steps:**
1. In the gap/leader computation, distinguish **"genuinely no leader ahead on the
   route"** from **"leader exists but is untracked at this tick."** For the
   second case, emit **NaN** (excluded), not 0.
2. Change the `break` at `labels.py:671` to `continue` with a NaN label for the
   missing horizon (the terminal-mask code two lines down already does exactly
   this pattern — copy it).
3. Re-run dataset extraction and report how the positive/negative/NaN counts
   shift.

**Deliverable:** the edits (new schema version) + a before/after label-balance
table in `out/qa/week2_eval.md`.

**Done when:** isolated telemetry gaps no longer flip a bunched label to 0, and
a single missing tick no longer erases the rest of the horizon.

**Watch out for:** don't accidentally turn real "bus is genuinely alone at the
front of the route" cases into NaN — those *should* stay label 0. The
distinction is whether a leader trajectory exists in the slice at all vs. exists
but has a hole at this tick.

---

## Task 8 — Use signals we already store: bearing + feed stop reports

**Why:** `bearing`, `current_stop_sequence`, `stop_id`, and `current_status` are
in every row and the projection ignores them. Bearing is the cheapest fix for
wrong-leg snapping on out-and-back routes (return leg has ~opposite bearing).
(Only do the parts Week 1 Task 1 showed are actually populated — check
field-availability first.)

**Steps:**
1. **Bearing sanity term:** in `project_to_shape.py`, after projecting a point,
   compute the shape's tangent direction at the projected location and compare to
   the row's `bearing`. Add a *flag/metric* first (don't change projection yet):
   how often does a kept point's bearing disagree with its projected-leg tangent
   by > 90°? Those are likely wrong-leg snaps the current filters miss.
2. Evaluate that flag against the Week 1 projection ground-truth: do the
   bearing-disagreement points line up with the trips you marked as wrong-leg?
3. If it correlates well, propose (with me) using bearing as a tie-breaker when
   selecting the projected leg.
4. **Stop-report cross-check (separate, optional):** where `current_status =
   STOPPED_AT` and `stop_id` is set, compare the geometry-derived `stop_index`
   to the stop the feed reports. Quantify disagreement — this tells us how much
   the feed could correct/validate projection.

**Deliverable:** a bearing-disagreement metric in the profiler + a short
`out/qa/bearing_eval.md` on whether it catches the wrong-leg cases.

**Done when:** we know whether bearing reliably flags wrong-leg projections on
our routes — the evidence we need before committing to a projection rewrite.

**Watch out for:** bearing can be missing or noisy at low speed (a stationary bus
has undefined heading). Exclude near-zero-speed points from the bearing check.

---

## Task 9 — Smooth and sanitize speed

**Why:** `compute_moving_speed` is a raw `Δdist/Δt` with no smoothing, no
non-negativity clamp (`upsample.py`). One bad projection → one speed spike →
corrupted idle detection, dwell estimates, and the `gap_closure` /
`rel_speed_to_d1` / `accel` features. Projection wobble even produces small
*negative* along-shape speeds that flow into features.

**Steps:**
1. Clamp along-shape speed to ≥ 0 (or decide explicitly what negative means and
   document it).
2. Add a short robust smoother (median-of-3, optionally then a small
   Savitzky-Golay window). Keep it a *pure* function and unit-test it.
3. Tie speed validity to the max-gap policy from Week 1 Task 5: don't compute a
   speed across a capped gap (it produces a misleading "barely moving" value →
   false idle).
4. **[optional cross-check]** Compare smoothed speed to the feed's `speed_mps`
   where present; large disagreement is a useful projection-quality flag.

**Deliverable:** updated `compute_moving_speed` (behind a config flag so we can
A/B), unit tests, and a before/after look at idle-event counts on a few trips.

**Done when:** speed is non-negative, single-point spikes are damped, and the
idle-event count on the ground-truth trips moves in the right direction (fewer
false idles).

**Watch out for:** smoothing can erase real short stops. Check against the
ground-truth idle set that you're not *removing* true dwells — tune the window
small.

---

## Stretch (only if Tasks 6–9 land cleanly): start the map-match

The structural fix is to replace independent nearest-point projection with a
continuity-aware **1-D HMM / Viterbi along the shape** (states = candidate
along-shape positions including *all* legs; emission = orthogonal distance +
bearing term; transition = penalize impossible jumps). This retires the teleport
filter and the monotone re-projection at once. It's a big task — for this week,
just:
1. Write a one-page design note (`out/qa/mapmatch_design.md`) with the state /
   emission / transition definitions and which library (e.g. a hand-rolled
   Viterbi, or `leuven.mapmatching`) you'd use.
2. Prototype it on **one** hard trip (a known out-and-back wrong-leg case from
   the ground-truth set) and show before/after.

Do **not** wire it into the pipeline this week. We'll plan that as a follow-on.

---

## End-of-week handoff

Write `out/qa/week2_summary.md`:
- For each task: what changed, and the before/after numbers on the ground-truth
  set.
- Which changes are safe to enable by default vs. still need validation.
- A short list of what you'd do in a hypothetical Week 3 (the map-match is the
  obvious candidate).

Remember the theme: **measured improvement only.** If a change doesn't show up as
better on the ground-truth set, we don't ship it — we learn from it and adjust.
