"""Bagged-LightGBM trainer.

K LightGBM models per horizon, each fit on a bootstrap resample of train.
Inference averages the K probabilities. The bundle uses one subdirectory per
bag member so the existing BunchingPredictor (which only knows the vendor
booster_hXX.txt layout) keeps working for one bag — a separate
BaggedPredictor wraps K of them.

The training cost scales linearly with K. K=8 with our 200k examples and rich
features takes ~10 minutes on CPU.

CLI:
    python -m apps.prediction.train_bag \
        --dataset out/datasets/route29_v1 \
        --out deployment/bunching_local_rich_bag8 \
        --bags 8 --feature-set rich
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from apps.prediction.data import (
    apply_scaler_to_vendor_block,
    build_feature_matrix,
    compute_scaler,
    load_dataset,
)
from apps.prediction.train import (
    HorizonResult,
    train_one_horizon,
    write_bundle,
)
from data_process.bunching.labels import N_CHANNELS, N_EXTRA


def fit_one_bag(
    bag_idx: int,
    Xtr: np.ndarray, Ytr: np.ndarray,
    Xva: np.ndarray, Yva: np.ndarray,
    *, pred_len: int, rng: np.random.Generator,
    n_estimators: int, num_leaves: int, learning_rate: float,
    min_child_samples: int, early_stopping_rounds: int,
    threshold_strategy: str = "f2",
) -> list[HorizonResult]:
    """Bootstrap-resample then fit per-horizon."""
    n = Xtr.shape[0]
    # With-replacement bootstrap of training rows. Validation is not resampled
    # so early-stopping is stable across bag members.
    idx = rng.integers(0, n, size=n)
    Xtr_b = Xtr[idx]
    Ytr_b = Ytr[idx]

    params = {
        "objective": "binary",
        "metric": "average_precision",
        "num_leaves": num_leaves,
        "learning_rate": learning_rate,
        "min_child_samples": min_child_samples,
        "feature_pre_filter": False,
    }

    results: list[HorizonResult] = []
    for h in range(pred_len):
        r = train_one_horizon(
            h, Xtr_b, Ytr_b[:, h], Xva, Yva[:, h],
            params=params,
            n_estimators=n_estimators,
            early_stopping_rounds=early_stopping_rounds,
            threshold_strategy=threshold_strategy,
        )
        results.append(r)
    return results


def fit_and_write_bag(
    dataset_dir: Path,
    out_dir: Path,
    *,
    feature_set: str,
    bags: int,
    seed: int,
    n_estimators: int,
    num_leaves: int,
    learning_rate: float,
    min_child_samples: int,
    early_stopping_rounds: int,
    threshold_strategy: str = "f2",
    # When set, train + persist only the first ``pred_len_cap`` horizons of
    # the dataset's labels. Use this when the model is being deployed for a
    # *shorter* operational horizon than the dataset was built for — the per-
    # horizon PR-AUC decays steeply past ~10 min, so a 10-step bundle is a
    # smaller artefact (3× fewer boosters per bag) with no accuracy loss on
    # the horizons it actually serves.
    pred_len_cap: int | None = None,
) -> None:
    ds = load_dataset(str(dataset_dir))

    # Effective pred_len after capping. The dataset's labels stay (N, pred_len)
    # in memory; we just iterate fewer horizons and persist the capped value
    # everywhere so downstream tooling sees a consistent contract.
    effective_pred_len = ds.pred_len
    if pred_len_cap is not None and pred_len_cap > 0:
        effective_pred_len = min(ds.pred_len, int(pred_len_cap))
    print(f"Loaded train={ds.train.n}  val={ds.val.n}  test={ds.test.n}  "
          f"pred_len={effective_pred_len}"
          + (f" (capped from {ds.pred_len})" if effective_pred_len != ds.pred_len else ""),
          flush=True)

    # Single scaler computed once on the full train split (not bag-by-bag) so
    # every bag operates in the same feature space. This is standard practice;
    # bag variance should come from row resampling, not scale jitter.
    scaler = compute_scaler(ds.train.X_vendor)
    Xtr_v = apply_scaler_to_vendor_block(ds.train.X_vendor, scaler)
    Xva_v = apply_scaler_to_vendor_block(ds.val.X_vendor, scaler)
    Xtr = build_feature_matrix(Xtr_v, ds.train.X_extras, feature_set)
    Xva = build_feature_matrix(Xva_v, ds.val.X_extras, feature_set)
    n_chans_per_tick = N_CHANNELS + (ds.n_extra if feature_set == "rich" else 0)

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    bag_results: list[list[HorizonResult]] = []
    t0 = time.time()
    for k in range(bags):
        print(f"--- bag {k+1}/{bags} ---", flush=True)
        results = fit_one_bag(
            k, Xtr, ds.train.Y, Xva, ds.val.Y,
            pred_len=effective_pred_len, rng=rng,
            n_estimators=n_estimators, num_leaves=num_leaves,
            learning_rate=learning_rate, min_child_samples=min_child_samples,
            early_stopping_rounds=early_stopping_rounds,
            threshold_strategy=threshold_strategy,
        )
        bag_results.append(results)
        bag_dir = out_dir / f"bag_{k:02d}" / "model"
        write_bundle(
            bag_dir,
            manifest={
                **ds.manifest,
                "data_root": str(dataset_dir),
                "bag_index": k,
                "n_bags": bags,
                # Override the dataset's pred_len so the per-bag metadata
                # advertises the capped horizon. The bag uses the override
                # via this manifest.
                "pred_len": effective_pred_len,
            },
            train_split=ds.manifest["split"],
            results=results,
            feature_set=feature_set,
            n_features_per_tick=n_chans_per_tick,
            extra_features=list(ds.manifest.get("extra_features", [])),
            speed_mean=scaler["speed_mean"], speed_std=scaler["speed_std"],
            gap_mean=scaler["gap_mean"], gap_std=scaler["gap_std"],
            threshold_strategy=threshold_strategy,
            n_extra=int(ds.n_extra) if feature_set == "rich" else 0,
        )
        avg_f2 = float(np.mean([r.f2_val for r in results]))
        avg_pr = float(np.mean([r.pr_auc_val for r in results]))
        print(f"  bag {k}: mean F2={avg_f2:.3f}  mean PR-AUC={avg_pr:.3f}  "
              f"({time.time()-t0:.0f}s elapsed)", flush=True)

    # Average-probs thresholds: re-tune per-horizon F2 thresholds on the val
    # split using AVERAGED bag predictions. Single-bag thresholds are usually
    # optimistic because they were fit on the same data the booster early-
    # stopped on; we want thresholds that work for the ensemble.
    print("Tuning bag-averaged thresholds on val...", flush=True)
    bag_probs_val = np.zeros((bags, Xva.shape[0], effective_pred_len), dtype=np.float32)
    for k in range(bags):
        for h, r in enumerate(bag_results[k]):
            if r.booster is None:
                bag_probs_val[k, :, h] = r.constant or 0.0
            else:
                bag_probs_val[k, :, h] = r.booster.predict(
                    Xva, num_iteration=r.best_iter
                ).astype(np.float32)
    avg_probs_val = bag_probs_val.mean(axis=0)

    from sklearn.metrics import average_precision_score, brier_score_loss
    from apps.prediction.train import _pick_threshold

    bag_thresholds: dict[str, dict] = {}
    for h in range(effective_pred_len):
        y = ds.val.Y[:, h]
        m = np.isfinite(y)
        if m.sum() == 0:
            bag_thresholds[str(h)] = {
                "threshold": 0.5, "f2_val": 0.0, "pr_auc_val": 0.0,
                "brier_val": None, "best_iter": 0, "positive_rate_train": 0.0,
                "method": f"bag_avg/{threshold_strategy}",
            }
            continue
        p = avg_probs_val[m, h]
        yy = y[m]
        thr, _ = _pick_threshold(p, yy, strategy=threshold_strategy)
        # Always report F2 too so cross-strategy bundles remain comparable.
        _, f2 = _pick_threshold(p, yy, strategy="f2")
        pr = float(average_precision_score(yy, p)) if len(np.unique(yy)) > 1 else 0.0
        br = float(brier_score_loss(yy, p)) if len(np.unique(yy)) > 1 else None
        bag_thresholds[str(h)] = {
            "threshold": float(thr),
            "f2_val": float(f2),
            "pr_auc_val": pr,
            "brier_val": br,
            "best_iter": 0,
            "positive_rate_train": float(np.mean([
                bag_results[k][h].positive_rate_train for k in range(bags)
            ])),
            "method": f"bag_avg/{threshold_strategy}",
        }

    # Top-level manifest: tells BaggedPredictor where each bag lives + the
    # bag-averaged thresholds it should use for alerting.
    top_manifest = {
        "kind": "bagged_lightgbm",
        "n_bags": bags,
        "seed": seed,
        "feature_set": feature_set,
        "seq_len": ds.seq_len,
        "pred_len": effective_pred_len,
        "n_channels": n_chans_per_tick,
        "vendor_schema_v": int(ds.manifest.get("vendor_schema_v", 1)),
        "extras_schema_v": int(
            ds.manifest.get("extras_schema_v")
            or (2 if int(ds.manifest.get("n_extra", 0)) == 10 else 1)
        ),
        "n_extra": ds.n_extra,
        "extra_features": list(ds.manifest.get("extra_features", [])),
        "step_seconds": ds.manifest.get("step_seconds"),
        "route_id": ds.manifest.get("route_id"),
        "trained_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "bag_dirs": [f"bag_{k:02d}/model" for k in range(bags)],
        "threshold_strategy": threshold_strategy,
        # Distances/speeds in the training data are true meters (UTM 17N)
        # since the EPSG:3857 fix; the serving shim is a no-op for "m".
        "distance_units": "m",
        # Also store a flat copy of scaler + thresholds at the top level so
        # BunchingPredictor would partially recognise the bundle if a user
        # accidentally points BUNCHING_MODEL_DIR at it.
        "scaler_inline": True,
    }
    (out_dir / "bag_manifest.json").write_text(json.dumps(top_manifest, indent=2))
    (out_dir / "scaler.json").write_text(json.dumps(scaler, indent=2))
    (out_dir / "thresholds.json").write_text(json.dumps(bag_thresholds, indent=2))
    print(f"Wrote bag bundle to {out_dir}  ({time.time()-t0:.0f}s total)", flush=True)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--feature-set", choices=("vendor", "rich"), default="rich")
    p.add_argument("--bags", type=int, default=8)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--n-estimators", type=int, default=300)
    p.add_argument("--num-leaves", type=int, default=63)
    p.add_argument("--learning-rate", type=float, default=0.05)
    p.add_argument("--min-child-samples", type=int, default=50)
    p.add_argument("--early-stopping-rounds", type=int, default=20)
    p.add_argument(
        "--threshold-strategy", default="f2",
        help="Per-horizon threshold rule: f2|f1|f0.5|precision@<X>  (e.g. precision@0.30)",
    )
    p.add_argument(
        "--pred-len-cap", type=int, default=None,
        help="Train + persist only the first N horizons of the dataset's labels "
             "(useful when deploying a shorter-horizon model from a longer-horizon dataset).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    fit_and_write_bag(
        dataset_dir=args.dataset,
        out_dir=args.out,
        feature_set=args.feature_set,
        bags=args.bags,
        seed=args.seed,
        n_estimators=args.n_estimators,
        num_leaves=args.num_leaves,
        learning_rate=args.learning_rate,
        min_child_samples=args.min_child_samples,
        early_stopping_rounds=args.early_stopping_rounds,
        threshold_strategy=args.threshold_strategy,
        pred_len_cap=args.pred_len_cap,
    )


if __name__ == "__main__":
    main()
