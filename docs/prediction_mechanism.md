# iROAM — Bus-Bunching Prediction Mechanism

This document explains, end to end, how iROAM predicts bus *bunching* — the
condition where a bus catches up to the one ahead of it and the headway between
them collapses. The prediction is what powers the dashboard's **Forecast** panel
(`GET /iroam/forecast`).

> **What "bunching" means here.** A bus is considered bunched at a given moment
> if its *forward gap* — the along-route distance to the next bus ahead — is
> below `BUNCHING_THRESHOLD_M = 100 m`. The model's job is to predict, for each
> running bus, the probability that this will be true at each of the next
> `1 … 30` minutes.

---

## 1. The big picture

```
  GTFS-RT AVL stream                 ┌─────────────────────────────────┐
  (Postgres + PostGIS)               │   OFFLINE: training pipeline     │
        │                            │                                 │
        │  fetch_trajectories_       │  data_process/bunching/         │
        │  for_slice()               │    labels.py        (features +  │
        ▼                            │                      labels)     │
  group into BusTrajectory           │    build_dataset.py (parquet     │
        │                            │                      shards)     │
        │                            │  apps/prediction/                │
        ▼                            │    train.py / train_bag.py       │
  ┌─────────────────────┐            │    calibrate.py                  │
  │  ONLINE: serving     │           │    train_tcn/tx/xgb.py (SOTA)    │
  │                      │           │    eval_sota.py / backtest.py    │
  │  live_features.py    │           └──────────────┬──────────────────┘
  │    build_bus_window  │                          │ writes bundle
  │  forecast.py         │                          ▼
  │    run_forecast      │◀──────────  deployment/bunching_*  (model files)
  │  bunching_predictor  │             loaded lazily by BUNCHING_MODEL_DIR
  │    get_predictor     │
  └─────────┬────────────┘
            ▼
   GET /iroam/forecast  →  per-bus + aggregate risk, fed to the dashboard
```

There are two halves that must agree **column-for-column**:

- **Offline** builds labelled training examples from historical trajectories and
  fits the model.
- **Online** rebuilds the *exact same* feature window at serving time from live
  trajectories and runs the trained model.

The shared contract between them lives in
`data_process/bunching/labels.py` (channel layout, units, the extra-feature
list and order). Both the offline label builder and the online feature builder
import from it, so a change there propagates to both sides.

---

## 2. The prediction target (labels)

Defined in `data_process/bunching/labels.py`.

For a chosen *reference time* `t_ref` and a bus, the label is a vector of length
`pred_len = 30`:

```
y[h] = 1  if  forward_gap( t_ref + (h+1) · step_seconds ) < 100 m
       0  otherwise
       NaN if the bus has no data that far into the future
```

With the default geometry (`step_seconds = 60`, `pred_len = 30`) this is "will
this bus be bunched at minute +1, +2, …, +30?". `NaN` horizons (the bus's trip
ends before that horizon) are simply masked out of the loss during training —
a short trip still supervises the horizons it actually reaches.

---

## 3. Features — the input window

Every prediction is made from a fixed-size **history window** ending at `t_ref`:

- `seq_len = 20` ticks of history,
- `step_seconds = 60` → 20 minutes of look-back,
- one feature vector per tick.

### 3.1 Vendor channels (`N_CHANNELS = 9`)

Each tick carries `(speed, gap, aux)` for three buses:

| offset | channel | meaning |
|--------|---------|---------|
| 0–2 | target | the bus we're predicting: moving speed (m/s), forward gap (m), aux |
| 3–5 | upstream #1 | the closest bus behind: speed, gap-to-target, aux |
| 6–8 | upstream #2 | the next bus behind |

When there is no bus ahead, the gap is filled with the sentinel
`NO_LEADER_GAP_M = 20 000 m` (effectively "infinitely far"). This 9-channel
layout is inherited from the original *vendor* LightGBM bundle so new models
stay drop-in compatible with the shipped `BunchingPredictor`.

### 3.2 Rich extras (`N_EXTRA = 7`)

The current production model is "rich" — it appends seven engineered context
channels per tick (`EXTRA_FEATURES` in `labels.py`):

| name | meaning |
|------|---------|
| `stop_index` | along-route progress (0 … num_stops−1) |
| `stop_index_norm` | `stop_index / num_stops`, scale-free |
| `gap_closure_m_per_s` | rate the forward gap is closing, over the last ~3 ticks |
| `leader_speed` | speed of the bus directly ahead (0 if none) |
| `dwell_recent_s` | seconds of the last ~5 min spent below 0.5 m/s (dwelling) |
| `tod_sin`, `tod_cos` | time-of-day encoded on a 24h circle |

A rich window is therefore `(20, 16)` — 9 vendor + 7 extra channels.

### 3.3 The critical invariant

`apps/prediction/live_features.py` reproduces the **identical** math that
`labels.py` uses offline: same channel order, same units, same lookback
constants (`gap_lookback`, `dwell_ticks`), same time-of-day encoding. The module
docstring calls this out as "load-bearing." If the two ever drift, every tree
split fires on the wrong branch at serving time and predictions silently
collapse.

### 3.4 Distance units (June 2026 CRS fix)

Until 2026-06-11 the trajectory pipeline projected GPS in EPSG:3857 (Web
Mercator), whose "meters" at Toronto's latitude are inflated by
1/cos(lat) ≈ 1.382. Every bundle trained on that data (v1–v6) therefore
expects *Mercator meters* in its distance/speed channels. The pipeline now
produces true meters (UTM 17N), and `apps/api/services/forecast.py` rescales
serving inputs by the route's 1/cos(mean stop latitude) for any bundle whose
manifest lacks `"distance_units": "m"`. Bundles trained on post-fix data are
stamped with that key by `train_bag` and bypass the shim. Retraining on
pre-fix CSV/DB extracts in Mercator units remains valid — just don't mix the
two unit systems inside one dataset.

---

## 4. Offline: building the dataset

`data_process/bunching/build_dataset.py` bulk-builds training data for one route
across many service days:

```bash
python -m data_process.bunching.build_dataset \
    --route 29 --dir 0 --dir 1 \
    --since 2026-04-22 --until 2026-05-26 \
    --out out/datasets/route29
```

For each `(date, direction)`:

1. Pull upsampled trajectories from Postgres (`fetch_trajectories_for_slice`).
2. Group rows into `BusTrajectory` objects (same grouping the API uses).
3. Interpolate every bus onto a common `step_seconds` time grid
   (`_bus_points_on_grid`), so neighbour gaps are computed at aligned instants.
4. For every valid `(bus, t_ref)` slide a window + label vector
   (`extract_labelled_examples`).

**Eligibility filters** (applied both offline and online so the distributions
match):

- The bus must have a fresh, contiguous `seq_len`-tick history ending at `t_ref`.
- It must be inside the active-stop band (drop the first/last `edge_exclude = 2`
  stops, where "gap to the bus ahead" is ill-defined).
- At least one upstream bus must be present on some tick of the window.

Output: one parquet shard per `(date, direction)` plus a `manifest.json` that
records the geometry **and a chronological train/val/test split by date**
(oldest 70% → train, next 15% → val, rest → test). Splitting by *date* avoids
same-day leakage between train and test.

---

## 5. Offline: training the model

### 5.1 Production model — bagged + calibrated LightGBM

The serving SOTA is a **per-horizon, bagged, isotonic-calibrated LightGBM**
(`deployment/bunching_local_rich_bag8`).

- **Per-horizon**: one independent LightGBM binary classifier per horizon
  (`booster_h00.txt … booster_h29.txt`). Each predicts `P(bunched at +h)`.
  (`apps/prediction/train.py:train_one_horizon`)
- **Class imbalance**: bunching is rare, so each booster uses
  `scale_pos_weight = n_neg / n_pos`.
- **Decision thresholds**: rather than a flat 0.5, each horizon gets an
  **F2-optimal** threshold swept on the val split (`_best_f2_threshold`). F2
  weights recall 2× precision — operationally we'd rather over-warn than miss a
  bunch.
- **Degenerate horizons**: if a horizon has no positives in train, the booster
  is replaced by a `CONSTANT` predictor of the empirical rate (a sentinel line
  in the booster file), matching the vendor bundle convention.
- **Bagging** (`apps/prediction/train_bag.py`): `K = 8` LightGBM models per
  horizon, each fit on a bootstrap resample of train. Inference averages the 8
  probabilities → lower variance. Each bag is a normal single-model bundle in
  its own subdir, so the existing `BunchingPredictor` serves one bag and
  `BaggedPredictor` wraps `K` of them.
- **Calibration** (`apps/prediction/calibrate.py`): a per-horizon
  `IsotonicRegression` fit on the bag-averaged val predictions, persisted as
  tiny `{x, y}` JSON (`calibration/iso_hXX.json`). At inference,
  `np.interp(prob, x, y)` maps raw probabilities to calibrated ones, so the
  numbers shown on the dashboard mean what they say (a "70%" is right ~70% of
  the time). Thresholds are re-tuned on the calibrated probabilities
  (`thresholds_calibrated.json`).

### 5.2 The bundle layout

A trained bundle is self-contained and loadable without code changes:

```
<bundle>/model/
  booster_h00.txt … booster_h29.txt   per-horizon LightGBM (or CONSTANT)
  scaler.json                         speed/gap z-score stats + channel layout
  thresholds.json                     per-horizon F2-optimal thresholds + metrics
  metadata.json                       seq_len, pred_len, n_channels, feature_set, route, provenance
```

A bagged bundle adds `bag_manifest.json`, per-bag subdirs, and (optionally) a
`calibration/` dir + `thresholds_calibrated.json`.

**Scaling matters.** `train.py` z-scores the speed/gap channels using train-split
stats and writes those stats into `scaler.json`. Inference must apply the exact
same transform — otherwise the trees split on the wrong side. The rich extras
are passed through unscaled on both sides.

### 5.3 Alternative / research models

Several other predictors exist for ensemble diversity and as a comparison
baseline; all share the same dataset, scaler protocol, and 30-horizon head:

| module | model | role |
|--------|-------|------|
| `apps/prediction/train_xgb.py` | per-horizon XGBoost | ensemble diversity vs LightGBM |
| `apps/prediction/train_tcn.py` | dilated 1-D TCN (3 blocks, multi-task head) | cheap deep-learning variant |
| `apps/prediction/train_tx.py` | tiny Transformer encoder (2 layers, 4 heads) | deep-learning variant |
| `apps/prediction/physics_baseline.py` | deterministic gap-closure kinematics | interpretable floor, no training |
| `apps/prediction/eval_sota.py` | unified backtest + ensembles (mean/median/stacked/calibrated) | apples-to-apples comparison |
| `apps/prediction/backtest.py` | chronological held-out backtest | per-horizon P/R/F2/PR-AUC/Brier |

The physics baseline is worth highlighting: it forward-projects the current gap
using the recent closure rate and turns `(threshold − projected_gap)` into a
probability via a sigmoid. It needs no training, runs in microseconds, and gives
the ML models a floor to beat.

---

## 6. Online: serving a forecast

When the dashboard requests `GET /iroam/forecast?route_id=…&service_date=…&direction_id=…&t_ref_min=…`
(`apps/api/routers/iroam.py:iroam_forecast`), the flow is:

1. **Load the slice.** Fetch trajectories for the `(route, date, direction)` and
   group them into buses — same code path as the rest of the dashboard.

2. **Pick the predictor** (`apps/api/services/bunching_predictor.py`).
   `get_predictor()` is a lazy, cached singleton. It resolves which bundle to
   load:
   - `BUNCHING_MODEL_DIR` env var if set (explicit ops override), else
   - the first existing entry in a priority list — currently
     `bunching_local_rich_bag8` (SOTA) → rich single → vendor-schema single →
     legacy 2024 vendor bundle.

   It auto-detects bagged vs single bundles (presence of `bag_manifest.json`)
   and instantiates `BaggedPredictor` or `BunchingPredictor` accordingly. Both
   expose the same `predict_proba` / `alert` / `metadata` API, so nothing
   downstream cares which is loaded.

3. **Read the bundle's geometry.** `run_forecast` (`apps/api/services/forecast.py`)
   reads `seq_len`, `step_seconds`, and `feature_set` from the loaded model's
   metadata — the *model* dictates the feature geometry, not the server.

4. **Build a window per bus** (`live_features.build_bus_window`). For each bus it
   reconstructs the `(seq_len, n_channels)` window ending at `t_ref`, applying the
   same freshness / edge-exclude / contiguity / has-upstream eligibility rules as
   offline. Ineligible buses get a human-readable `reason` instead of a window.

5. **Scale + batch.** For rich bundles, `merge_for_predictor` z-scores the 9
   vendor channels (matching `preprocess.scale_window`), concatenates the 7
   passthrough extras, and flags the result as already-scaled so the predictor
   doesn't double-scale.

6. **Predict.** `predict_proba(batch)` returns a `(num_eligible, 30)` matrix of
   probabilities; for a bagged+calibrated bundle this is the isotonic-corrected
   average of the 8 bags. `alert(batch)` compares each horizon against its tuned
   threshold to produce `any_alert`, `first_alert_step`, `max_prob`, etc.

7. **Assemble the payload.** Per bus: eligibility, current stop index, the 30-step
   probability vector, and a small **rationale** block (current forward gap and
   gap-closure rate) so the UI can explain *why* a bus is flagged without
   re-running the model. Plus two aggregate series across all eligible buses:
   `any_alert_rate[h]` (fraction over threshold) and `mean_prob[h]`.

A short `model_label` (e.g. `rich·bag8·cal / r29 / 2026-05-31`) is surfaced in
the payload so the UI chip shows exactly which bundle produced the numbers.

---

## 7. How to swap models

Because every bundle is self-describing, switching the served model is a config
change, not a code change:

```bash
# Point the API at a different bundle and restart.
export BUNCHING_MODEL_DIR=deployment/bunching_local_rich_v1/model
```

`run_forecast` re-reads the new bundle's geometry and `live_features` adapts the
window shape automatically. Use `apps/prediction/backtest.py` /
`eval_sota.py` first to confirm a candidate bundle actually beats the incumbent
on the held-out test split.

---

## 8. File map (quick reference)

| Concern | File |
|---------|------|
| Feature/label contract (channels, units, extras) | `data_process/bunching/labels.py` |
| Offline label extraction | `data_process/bunching/labels.py:extract_labelled_examples` |
| Offline dataset build (parquet + split) | `data_process/bunching/build_dataset.py` |
| LightGBM trainer (per-horizon, F2 thresholds) | `apps/prediction/train.py` |
| Bagging trainer | `apps/prediction/train_bag.py` |
| Isotonic calibration | `apps/prediction/calibrate.py` |
| SOTA / baseline variants | `apps/prediction/train_xgb.py`, `train_tcn.py`, `train_tx.py`, `physics_baseline.py` |
| Evaluation | `apps/prediction/backtest.py`, `eval_sota.py` |
| Live feature builder | `apps/prediction/live_features.py` |
| Forecast orchestration | `apps/api/services/forecast.py` |
| Predictor loader (bundle resolution) | `apps/api/services/bunching_predictor.py` |
| Bagged inference wrapper | `apps/prediction/bagged_predictor.py` |
| Single-bundle inference | `deployment/bunching_lightgbm/src/predict.py` |
| HTTP endpoint | `apps/api/routers/iroam.py:iroam_forecast` |
| Ops evaluation harness | `apps/prediction/eval_ops.py` |
| Shadow-mode logging + drift summary | `apps/api/services/forecast_shadow.py` |

---

## 9. V2 — operational-grade upgrade (route 29)

The original (v1) pipeline optimises F2, which weights recall 2× over
precision. On an ops floor, that yields ~50 alerts per bus-hour at ~14%
precision — a non-starter for dispatcher trust. V2 changes three things in
isolation so each contribution is measurable.

### 9.1 New extras (terminus awareness — fixes selection bias)

`labels.py:EXTRA_FEATURES` now ends with three v2 channels (`N_EXTRA = 10`):

| ch | name | unit | why |
|----|------|------|-----|
| 7  | `dist_to_terminus_m`    | m   | how much route is left |
| 8  | `dist_to_terminus_norm` | [0,1] | scale-free version |
| 9  | `time_to_terminus_min`  | min   | `dist / max(median_speed, 1 m/s)` |

The diagnostic earlier (see commit history / `scripts/diag_forecast_anomaly.py`)
showed v1's predictions were perfectly rank-correlated with `stop_idx` at
long horizons — caused by selection bias in the labels (only buses that
*survived* a long horizon contributed labels there, and survivors near the
end of route were disproportionately bunched). Giving the model an
explicit "minutes left in trip" feature lets it condition on the right
geometry instead of memorising stop position. Both the offline extractor
(`labels.extract_labelled_examples`) and the live builder
(`live_features.build_bus_window`) now produce identical layouts when the
caller passes `route_shape_length_m`.

### 9.2 Precision-targeted thresholds (fixes the operating point)

`train._pick_threshold(probs, y, strategy=...)` replaces the old
F2-only `_best_f2_threshold`. Supported strategies:

* `f2` / `f1` / `f0.5` — F-beta maximisation (legacy `f2` is the default
  for back-compat).
* `precision@<X>` — pick the threshold that maximises **recall subject to
  precision ≥ X** on the val split. Returns (0.99, 0.0) — i.e. "never
  alert" — if no threshold meets the floor, so a horizon that can't reach
  the precision target is honestly silent rather than chronically false-
  alarming.

Both `train.py` and `train_bag.py` now expose `--threshold-strategy`. The
chosen strategy is persisted in `metadata.json` / `bag_manifest.json` so
the ops eval harness knows what it's measuring.

### 9.3 Ops evaluation harness

`apps/prediction/eval_ops.py` reports the metrics a dispatcher actually
asks about:

* precision + recall at the **bundle's chosen threshold** (not at an
  oracular F2-optimum),
* **alerts per bus-hour** — proxy for dispatcher cognitive load,
* **recall achievable at precision floors** (0.20 / 0.30 / 0.40 / 0.50 / 0.60),
* **per-period breakdown** — am-peak / midday / pm-peak / evening,
* **reliability bins** — when the model says 30%, do 30% of those buses
  actually bunch? (Calibration trust.)

Run: `python -m apps.prediction.eval_ops --bundle <bundle> --dataset out/datasets/route29_v2 --out out/eval/ops`

### 9.4 Shadow-mode logging + drift hook

Setting `FORECAST_SHADOW_MODE=1` makes every `/iroam/forecast` call also
append to:

* `out/shadow/<service_date>.jsonl` — one line per scored bus with raw
  (pre-truncation) probabilities, post-truncation summary, current gap +
  closure, and a feature-window hash. Enough to back-compute precision/
  recall once the bus's later trajectory is ingested, without storing
  feature values.
* `out/shadow/<today>_drift.csv` — per-channel mean/std/p01/p99 snapshot
  of every batch. Watching these for sudden drift is the cheapest
  early-warning signal that the training distribution has moved.

The forecast payload also returns `shadow_mode: true|false` so the
dashboard can surface a "SHADOW" banner. Shadow mode is the recommended
way to run any new bundle for ≥1 week before flipping the default.

### 9.5 Production deployment checklist

Before pointing `BUNCHING_MODEL_DIR` at a new bundle:

1. **Train + calibrate**: `train_bag.py --threshold-strategy precision@0.30`
   then `calibrate.py` on the bag.
2. **Evaluate on the held-out test split**:
   `eval_ops.py --bundle <new> --dataset out/datasets/route29_v2`
   Required: alerts/bus-hour ≤ 5, precision-at-threshold ≥ 0.30 in
   the 5–30 min band, mean Brier ≤ 0.07.
3. **Shadow run for ≥1 week**:
   `FORECAST_SHADOW_MODE=1 docker compose up -d api`
   Watch `out/shadow/*.jsonl` line counts vs `*_drift.csv` stability.
4. **Back-compute realised precision** from the shadow log against
   actual bunching events in the trajectory store (see
   `scripts/eval_shadow.py` once implemented).
5. **Flip default**: update `_DEFAULT_CANDIDATES` in
   `apps/api/services/bunching_predictor.py`, rebuild API container,
   keep the old bundle in `_DEFAULT_CANDIDATES` as the next fallback for
   rollback.
6. **Keep shadow on for another week post-flip** to confirm the live
   distribution matches the shadowed one.
</content>
</invoke>
