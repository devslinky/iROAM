"""Top-level forecast orchestration.

Given a materialised slice of the iROAM dataset, pick the eligible "running"
buses at ``t_ref``, build their feature windows according to the *loaded
bundle's* geometry (``seq_len`` / ``step_seconds`` / ``feature_set``), batch
them through the predictor, and emit per-bus + aggregate output.

The output payload includes a small per-bus rationale block (current forward
gap and gap-closure rate) so the dashboard can show *why* a bus is at risk
without re-running the model.

**Horizon truncation (May 2026 bug fix)** — the bagged LightGBM model was
trained on labels that only exist when a bus is still on the route at
``t_ref + h``. For buses near the terminus, the "surviving" examples at long
horizons are selection-biased toward bunched/stuck buses, so the model
extrapolates to ~1.0 for any input with high stop_index. We work around this
by computing each bus's plausible remaining trip time (from
``route_shape_length_m - travel_distance_m`` divided by recent median speed)
and truncating its per-horizon prediction to that reach. ``max_prob`` and
``first_alert_step`` are recomputed over the truncated array.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np


# ---------------------------------------------------------------------------
# Serving-side horizon cap (Option A).
#
# Two reasons this exists even though we already pick bundle pred_len at
# train time:
#   1. The same code serves multiple bundles via BUNCHING_MODEL_DIR — older
#      v1 / v2 bundles still have pred_len=30. If we want a uniform 10-min
#      product across bundles, this env var enforces it.
#   2. Defensive belt-and-braces: if a future bundle ships with pred_len > 10
#      by accident, the cap protects the UI from suddenly displaying noisy
#      long-horizon predictions.
#
# Set ``FORECAST_HORIZON_CAP_MIN=10`` (or any positive integer) to clamp.
# Unset or 0 = use the bundle's full pred_len.
# ---------------------------------------------------------------------------
HORIZON_CAP_ENV_VAR = "FORECAST_HORIZON_CAP_MIN"

from apps.analytics.anomalies import BusTrajectory, TrajectoryPoint
from apps.prediction.live_features import (
    LiveWindow,
    build_bus_window,
    merge_for_predictor,
)
from data_process.bunching.labels import NO_LEADER_GAP_M

from .bunching_predictor import PredictorUnavailable, get_predictor
from . import forecast_shadow


def _load_horizon_cap_min() -> int | None:
    raw = os.environ.get(HORIZON_CAP_ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------------------
# Distance-units compatibility (June 2026 EPSG:3857 → UTM fix).
#
# The trajectory pipeline now produces true-meter distances/speeds, but every
# bundle trained before the fix consumed EPSG:3857 "meters" — inflated by
# 1/cos(latitude) ≈ 1.382 at Toronto. Feeding such a bundle true meters would
# silently compress its whole feature distribution by that factor, so for
# bundles whose metadata lacks ``distance_units == "m"`` we rescale the input
# trajectories back into the units the model was trained on. The factor uses
# the route's mean stop latitude when the caller provides it (reproduces the
# training values to <0.1%); otherwise a Toronto-wide reference latitude.
# ---------------------------------------------------------------------------
TRUE_METER_UNITS = "m"
_TORONTO_REF_LAT_DEG = 43.7


def _model_input_scale(meta: dict[str, Any], route_mean_lat_deg: float | None) -> float:
    """Multiplier from serving units (true m) to the bundle's training units."""
    if str(meta.get("distance_units") or "") == TRUE_METER_UNITS:
        return 1.0
    lat = route_mean_lat_deg if route_mean_lat_deg is not None else _TORONTO_REF_LAT_DEG
    return 1.0 / math.cos(math.radians(lat))


def _scale_bus(bus: BusTrajectory, scale: float) -> BusTrajectory:
    """Copy of ``bus`` with distance-like quantities multiplied by ``scale``.

    ``stop_index`` is a ratio of distances, so it is scale-invariant and kept.
    """
    return BusTrajectory(
        bus_index=bus.bus_index,
        trip_id=bus.trip_id,
        start_date=bus.start_date,
        vehicle_id=bus.vehicle_id,
        points=[
            TrajectoryPoint(
                datetime=p.datetime,
                travel_distance_m=p.travel_distance_m * scale,
                moving_speed_m_s=(
                    p.moving_speed_m_s * scale if p.moving_speed_m_s is not None else None
                ),
                occupancy_status=p.occupancy_status,
                stop_index=p.stop_index,
            )
            for p in bus.points
        ],
    )


def _descale_gap(gap: float | None, scale: float) -> float | None:
    """Convert a model-unit gap back to true meters for the UI rationale.

    The no-leader sentinel is a fixed constant in model units, not a distance
    measurement — pass it through unchanged so the frontend's "no leader"
    handling keeps seeing the value it expects.
    """
    if gap is None or scale == 1.0 or gap >= NO_LEADER_GAP_M:
        return gap
    return gap / scale


@dataclass(frozen=True)
class ForecastResult:
    t_ref_min: float
    horizon_steps: int
    step_seconds: int
    seq_len: int
    feature_set: str
    model_label: str
    shadow_mode: bool
    # Effective per-bus cap (in minutes) applied at serving time. None means
    # "no cap, use bundle's pred_len * step_seconds". When set, the UI knows
    # to draw a forecast curve only up to this many minutes and can show a
    # "10-min horizon" chip next to the bundle label.
    horizon_cap_min: int | None
    # Serving→model unit conversion applied to the input trajectories
    # (1.0 for bundles trained on true meters). Surfaced for diagnostics.
    input_distance_scale: float
    thresholds: list[float]
    per_bus: list[dict[str, Any]]
    horizon_summary: dict[str, list[float]]
    num_buses_total: int
    num_running: int
    num_eligible: int


def _bundle_label(meta: dict[str, Any]) -> str:
    """Short human label for the loaded bundle. Surfaces in the UI chip.

    Now includes the threshold strategy, since the same model under F2 vs
    precision@0.30 vs precision@0.50 fires very different alert volumes —
    dispatchers want to see which regime they're looking at.
    """
    if not meta:
        return "predictor"
    family = str(meta.get("feature_set") or "vendor")
    # Surface the extras schema so dispatchers see what's actually feeding
    # the model. v1 = legacy 7 (with leader_speed), v2 = full 10 (with
    # terminus), v3 = trimmed 7 (dist_to_terminus_norm only).
    extras_v = meta.get("extras_schema_v")
    if extras_v == 2:
        family = f"{family}+terminus"
    elif extras_v == 3:
        family = f"{family}+terminus_norm"
    elif extras_v == 4:
        family = f"{family}+rel_kinematics"
    n_bags = int(meta.get("n_bags") or 0)
    if n_bags > 1:
        family = f"{family}·bag{n_bags}"
    if meta.get("calibrated"):
        family = f"{family}·cal"
    strat = meta.get("threshold_strategy")
    parts = [family]
    if strat:
        parts.append(str(strat))
    if meta.get("route_id"):
        parts.append(f"r{meta['route_id']}")
    if meta.get("trained_at"):
        parts.append(str(meta["trained_at"])[:10])
    return " / ".join(parts)


def _useful_horizon_steps(
    travel_distance_m: float | None,
    route_shape_length_m: float | None,
    median_speed_m_s: float | None,
    *,
    step_seconds: int,
    pred_len: int,
    floor_speed_m_s: float = 1.0,
    safety_factor: float = 1.5,
    minimum_steps: int = 3,
) -> int:
    """How many of the model's ``pred_len`` horizon steps the bus can plausibly reach.

    Rationale (the load-bearing bit): the bagged model's long-horizon labels
    only exist for buses that *survived* that long, which is a biased
    subset of buses (slow / stuck → bunched). Truncating per-bus by remaining
    trip time keeps the model from extrapolating into a region where it was
    only trained on selection-biased examples.

    Formula:
        remaining_s = (route_length - current_dist) / max(median_speed, floor)
        useful_steps = floor( safety_factor * remaining_s / step_seconds )

    The ``safety_factor`` of 1.5 gives a bit of headroom — buses do slow down
    near termini but they don't usually stop. ``minimum_steps`` keeps at least
    a few short horizons visible even for buses one stop from the end, so the
    UI doesn't go blank.

    When inputs are missing (any of distance/length/speed), we return the
    full ``pred_len`` so the fix degrades gracefully.
    """
    if travel_distance_m is None or route_shape_length_m is None or median_speed_m_s is None:
        return pred_len
    remaining_m = max(0.0, float(route_shape_length_m) - float(travel_distance_m))
    speed = max(float(median_speed_m_s), floor_speed_m_s)
    remaining_s = remaining_m / speed
    useful = int(math.floor(safety_factor * remaining_s / max(1, step_seconds)))
    return max(minimum_steps, min(pred_len, useful))


def _recompute_alert_after_truncation(
    per_horizon_full: np.ndarray,
    thresholds: np.ndarray,
    useful_steps: int,
) -> dict[str, Any]:
    """Restate ``any_alert`` / ``max_prob`` / ``first_alert_step`` over the
    horizons the bus can plausibly reach. ``per_horizon`` itself is returned
    truncated so the UI plots only those bars."""
    n = max(0, min(int(useful_steps), per_horizon_full.shape[0]))
    if n == 0:
        return {
            "any_alert": False,
            "first_alert_step": None,
            "max_prob": 0.0,
            "max_prob_step": 0,
            "per_horizon": [],
        }
    probs = per_horizon_full[:n]
    thrs = thresholds[:n]
    exceed = probs >= thrs
    any_hit = bool(exceed.any())
    first = int(np.argmax(exceed)) if any_hit else None
    max_idx = int(np.argmax(probs))
    return {
        "any_alert": any_hit,
        "first_alert_step": first,
        "max_prob": float(probs[max_idx]),
        "max_prob_step": max_idx,
        "per_horizon": [float(x) for x in probs.tolist()],
    }


def run_forecast(
    buses: list[BusTrajectory],
    *,
    num_stops: int,
    t_ref_min: float,
    freshness_s: float = 90.0,
    edge_exclude: int = 2,
    predictor: Any | None = None,
    route_shape_length_m: float | None = None,
    # Mean stop latitude of the route — sharpens the legacy-bundle unit
    # conversion (see _model_input_scale). Optional; tests may omit it.
    route_mean_lat_deg: float | None = None,
    # Identity passthrough for shadow logging — when shadow mode is off
    # these are ignored. The router fills them; tests don't have to.
    route_id: str = "",
    direction_id: int = 0,
    service_date_iso: str = "",
) -> ForecastResult:
    """Run the bunching predictor for every "running" bus in ``buses``.

    ``route_shape_length_m`` is the total along-route distance from
    ``RouteStops.shape_length_m``. When provided, per-bus predictions are
    truncated to the bus's plausible remaining trip time (see
    ``_useful_horizon_steps``); when omitted, no truncation is applied (old
    behaviour preserved for tests).
    """
    pred = predictor if predictor is not None else get_predictor()

    meta = getattr(pred, "metadata", {}) or {}
    seq_len = int(meta.get("seq_len", 20))
    step_seconds = int(meta.get("step_seconds", 60))
    feature_set = str(meta.get("feature_set", "vendor"))
    scaler = getattr(pred, "scaler", {}) or {}

    # ``n_extra`` from the bundle's metadata drives whether the terminus-
    # aware extras (v2) are filled or left at 0. The shape length is the
    # second prerequisite — older bundles never see it because their
    # ``n_extra`` is 7 and the v2 channels live at indices 7-9.
    n_extra = int(meta.get("n_extra", 7))
    vendor_schema_v = int(meta.get("vendor_schema_v", 1))  # default: legacy
    # extras_schema_v default depends on n_extra so older bundles without
    # the explicit field still get a sensible default (v1 = 7-channel
    # legacy, v2 = 10-channel, v3 = 7-channel trimmed, v4 = 6-channel
    # post-diag). When the bundle does advertise it, we trust the explicit
    # value — n_extra alone can't distinguish v1 (with leader_speed) from
    # v3 (with dist_to_terminus_norm); both are 7.
    extras_schema_v = int(meta.get("extras_schema_v") or (
        4 if n_extra == 6 else (2 if n_extra == 10 else 1)
    ))

    # Legacy-bundle unit shim: present the model with inputs in the units it
    # was trained on. All downstream feature math (gaps, closure rates,
    # terminus distances) inherits the scale from the points themselves.
    input_scale = _model_input_scale(meta, route_mean_lat_deg)
    if input_scale != 1.0:
        model_buses = [_scale_bus(b, input_scale) for b in buses]
        model_shape_length_m = (
            route_shape_length_m * input_scale if route_shape_length_m is not None else None
        )
    else:
        model_buses = list(buses)
        model_shape_length_m = route_shape_length_m

    results: list[LiveWindow] = []
    for bus in model_buses:
        results.append(
            build_bus_window(
                bus, model_buses,
                t_ref_min=t_ref_min,
                num_stops=num_stops,
                seq_len=seq_len,
                step_seconds=step_seconds,
                feature_set=feature_set,
                n_extra=n_extra,
                freshness_s=freshness_s,
                edge_exclude=edge_exclude,
                route_shape_length_m=route_shape_length_m,
                vendor_schema_v=vendor_schema_v,
                extras_schema_v=extras_schema_v,
            )
        )

    eligible = [r for r in results if r.window is not None]

    def _is_running(r: LiveWindow) -> bool:
        if r.window is not None:
            return True
        reason = r.reason or ""
        # Edge-excluded or no-neighbour-tick buses still count as physically
        # operating (just unscoreable). Match both v1 and v2 reason prefixes.
        return reason.startswith("stop_idx=") or reason.startswith(
            ("no upstream bus on any tick", "no leader on any tick")
        )

    num_running = sum(1 for r in results if _is_running(r))

    per_bus_out: dict[int, dict[str, Any]] = {}
    thresholds = [float(pred.thresholds[h]["threshold"]) for h in range(pred.pred_len)]
    thresholds_arr = np.asarray(thresholds, dtype=np.float32)

    if eligible:
        merged: list[np.ndarray] = []
        is_scaled_flag = False
        for r in eligible:
            arr, scaled = merge_for_predictor(r.window, r.extras, scaler)
            merged.append(arr)
            is_scaled_flag = scaled or is_scaled_flag
        batch = np.stack(merged, axis=0).astype(np.float32)
        probs = pred.predict_proba(batch, is_scaled=is_scaled_flag)

        # Option-A horizon cap: bundle pred_len may be longer than what we
        # actually want to surface. Compute the per-bus useful horizon as
        # min(remaining-trip-time-truncation, env-var-cap).
        env_cap_min = _load_horizon_cap_min()
        env_cap_steps = (
            max(1, int(env_cap_min * 60 / max(1, step_seconds)))
            if env_cap_min is not None else pred.pred_len
        )

        for i, r in enumerate(eligible):
            # travel distance / shape length / speed are all in model units
            # here; _useful_horizon_steps only consumes their ratio, so the
            # scale cancels.
            useful = _useful_horizon_steps(
                r.travel_distance_m_at_ref,
                model_shape_length_m,
                r.median_speed_m_s_recent,
                step_seconds=step_seconds,
                pred_len=pred.pred_len,
            )
            useful = min(useful, env_cap_steps)
            row = _recompute_alert_after_truncation(probs[i], thresholds_arr, useful)
            row.update({
                "eligible": True,
                "ineligible_reason": None,
                "stop_idx": _round_or_none(r.stop_idx_at_ref),
                "forward_gap_m": _round_or_none(
                    _descale_gap(r.forward_gap_at_ref, input_scale), 1
                ),
                "gap_closure_m_s": _round_or_none(
                    r.gap_closure_m_per_s_at_ref / input_scale
                    if r.gap_closure_m_per_s_at_ref is not None
                    else None,
                    2,
                ),
                "useful_horizon_steps": int(useful),
            })
            per_bus_out[r.bus_index] = row

        # For the aggregate curves we use ALL horizons across ALL eligibles
        # (untruncated) — the aggregate is for the dashboard's "what's the
        # fleet-wide risk look like?" view and benefits from a full curve.
        any_alert_rate = (probs >= thresholds_arr).mean(axis=0)
        mean_prob = probs.mean(axis=0)
    else:
        any_alert_rate = np.zeros(pred.pred_len, dtype=np.float32)
        mean_prob = np.zeros(pred.pred_len, dtype=np.float32)

    out_rows: list[dict[str, Any]] = []
    for bus, r in zip(buses, results, strict=True):
        row: dict[str, Any] = {
            "bus_id": bus.bus_index,
            "trip_id": bus.trip_id,
            "vehicle_id": bus.vehicle_id,
        }
        row.update(
            per_bus_out.get(
                r.bus_index,
                {
                    "eligible": False,
                    "ineligible_reason": r.reason,
                    "stop_idx": _round_or_none(r.stop_idx_at_ref),
                    "any_alert": None,
                    "first_alert_step": None,
                    "max_prob": None,
                    "max_prob_step": None,
                    "per_horizon": None,
                    "useful_horizon_steps": None,
                    "forward_gap_m": _round_or_none(
                        _descale_gap(r.forward_gap_at_ref, input_scale), 1
                    ),
                    "gap_closure_m_s": _round_or_none(
                        r.gap_closure_m_per_s_at_ref / input_scale
                        if r.gap_closure_m_per_s_at_ref is not None
                        else None,
                        2,
                    ),
                },
            )
        )
        out_rows.append(row)

    # Shadow logging: only side-effecting when FORECAST_SHADOW_MODE is set,
    # so the hot path stays free of disk I/O in production-without-shadow.
    shadow_cfg = forecast_shadow.load_config()
    if shadow_cfg.enabled and eligible:
        elig_rows = [per_bus_out[r.bus_index] for r in eligible]
        # Re-stamp with bus identity so the shadow line carries vehicle_id +
        # trip_id (per_bus_out doesn't have them; out_rows does).
        bus_meta_by_idx = {b.bus_index: b for b in buses}
        for r, row in zip(eligible, elig_rows):
            b = bus_meta_by_idx.get(r.bus_index)
            if b is not None:
                row.setdefault("vehicle_id", b.vehicle_id)
                row.setdefault("trip_id", b.trip_id)
                row.setdefault("bus_id", b.bus_index)
        forecast_shadow.log_predictions(
            shadow_cfg,
            bundle_label=_bundle_label(meta),
            route_id=route_id,
            direction_id=direction_id,
            service_date=service_date_iso,
            t_ref_min=float(t_ref_min),
            eligible_rows=elig_rows,
            raw_probs=probs if eligible else None,
            feature_batch=batch if eligible else None,
        )
        forecast_shadow.log_drift_summary(
            shadow_cfg,
            bundle_label=_bundle_label(meta),
            route_id=route_id,
            direction_id=direction_id,
            feature_batch=batch,
        )

    horizon_cap_min_env = _load_horizon_cap_min()
    return ForecastResult(
        t_ref_min=float(t_ref_min),
        horizon_steps=int(pred.pred_len),
        step_seconds=int(step_seconds),
        seq_len=int(seq_len),
        feature_set=feature_set,
        model_label=_bundle_label(meta),
        shadow_mode=shadow_cfg.enabled,
        horizon_cap_min=horizon_cap_min_env,
        input_distance_scale=float(input_scale),
        thresholds=thresholds,
        per_bus=out_rows,
        horizon_summary={
            "any_alert_rate": [float(x) for x in any_alert_rate.tolist()],
            "mean_prob": [float(x) for x in mean_prob.tolist()],
        },
        num_buses_total=len(buses),
        num_running=int(num_running),
        num_eligible=len(eligible),
    )


def _round_or_none(x: float | None, ndigits: int = 3) -> float | None:
    return None if x is None else round(float(x), ndigits)


__all__ = ["run_forecast", "ForecastResult", "PredictorUnavailable"]
