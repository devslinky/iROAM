"""BaggedPredictor — averages K LightGBM bundle predictions per horizon.

Mimics the BunchingPredictor API (``predict_proba`` / ``alert`` /
``predict_scalar``) so the forecast service can swap it in via
``BUNCHING_MODEL_DIR`` without code changes elsewhere.

Layout this expects (produced by ``apps.prediction.train_bag``):

    <bundle_root>/
      bag_manifest.json        # n_bags, bag_dirs, geometry
      scaler.json              # shared across bags
      thresholds.json          # bag-averaged F2-optimal thresholds
      bag_00/model/booster_h*.txt + scaler.json + thresholds.json + metadata.json
      bag_01/model/...
      ...
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


class BaggedPredictor:
    def __init__(self, bundle_dir: str | Path) -> None:
        self.bundle_dir = Path(bundle_dir).resolve()
        man = json.loads((self.bundle_dir / "bag_manifest.json").read_text())
        if man.get("kind") != "bagged_lightgbm":
            raise ValueError(f"not a bagged_lightgbm bundle: {self.bundle_dir}")
        self._manifest = man

        self.seq_len = int(man["seq_len"])
        self.pred_len = int(man["pred_len"])
        self.n_channels = int(man["n_channels"])
        self.n_features = int(self.seq_len * self.n_channels)
        self.scaler = json.loads((self.bundle_dir / "scaler.json").read_text())

        # Calibration is optional. If ``calibration/iso_hXX.json`` files exist
        # alongside ``thresholds_calibrated.json`` we apply isotonic correction
        # after bag-averaging — same Brier scores as the eval harness reports.
        self._iso_x: list[np.ndarray] | None = None
        self._iso_y: list[np.ndarray] | None = None
        cal_dir = self.bundle_dir / "calibration"
        cal_thr_path = self.bundle_dir / "thresholds_calibrated.json"
        if cal_dir.is_dir() and cal_thr_path.exists():
            xs: list[np.ndarray] = []
            ys: list[np.ndarray] = []
            for h in range(self.pred_len):
                p = cal_dir / f"iso_h{h:02d}.json"
                if not p.exists():
                    xs.append(np.array([0.0, 1.0])); ys.append(np.array([0.0, 1.0])); continue
                d = json.loads(p.read_text())
                xs.append(np.asarray(d["x"], dtype=np.float64))
                ys.append(np.asarray(d["y"], dtype=np.float64))
            self._iso_x = xs
            self._iso_y = ys
            thr_path = cal_thr_path
        else:
            thr_path = self.bundle_dir / "thresholds.json"
        thr_raw: dict[str, dict] = json.loads(thr_path.read_text())
        self.thresholds = {int(k): v for k, v in thr_raw.items()}
        self.calibrated = self._iso_x is not None

        # Surface a metadata dict shaped like BunchingPredictor.metadata so the
        # forecast service's ``meta.get(...)`` calls work uniformly.
        self.metadata = {
            "model_type": "bagged_lightgbm" + ("_cal" if self.calibrated else ""),
            "framework": "lightgbm",
            "n_bags": int(man["n_bags"]),
            "seq_len": self.seq_len,
            "pred_len": self.pred_len,
            "n_channels": self.n_channels,
            "n_features": self.n_features,
            "feature_set": man.get("feature_set", "vendor"),
            "extra_features": man.get("extra_features", []),
            # n_extra is what the live feature builder needs to allocate
            # its extras array. Fall back to ``n_channels - 9`` for older
            # bag bundles that didn't persist it explicitly.
            "n_extra": int(man.get("n_extra", max(0, self.n_channels - 9))),
            "vendor_schema_v": int(man.get("vendor_schema_v", 1)),
            "extras_schema_v": int(
                man.get("extras_schema_v")
                or (2 if int(man.get("n_extra", 0)) == 10 else 1)
            ),
            "step_seconds": man.get("step_seconds"),
            "route_id": man.get("route_id"),
            "trained_at": man.get("trained_at"),
            "calibrated": self.calibrated,
            "threshold_strategy": man.get("threshold_strategy"),
            # Unit system of the distances/speeds the model was trained on.
            # Absent on bundles trained before the EPSG:3857→UTM fix; the
            # forecast service treats absence as "epsg3857_m" and rescales
            # serving inputs accordingly.
            "distance_units": man.get("distance_units"),
        }

        from deployment.bunching_lightgbm import BunchingPredictor

        self._bags: list[BunchingPredictor] = []
        for rel in man["bag_dirs"]:
            self._bags.append(BunchingPredictor(self.bundle_dir / rel))
        if not self._bags:
            raise ValueError("no bag members found")

    # The forecast service uses these three methods on the returned object.

    def predict_proba(self, x: np.ndarray, *, is_scaled: bool = True) -> np.ndarray:
        """Average the K bag predictions, then apply isotonic calibration if loaded."""
        acc: np.ndarray | None = None
        for b in self._bags:
            p = b.predict_proba(x, is_scaled=is_scaled).astype(np.float32)
            acc = p if acc is None else acc + p
        avg = (acc / float(len(self._bags))).astype(np.float32)
        if self._iso_x is None or self._iso_y is None:
            return avg
        # Apply per-horizon isotonic transform — vectorised via np.interp.
        out = np.empty_like(avg)
        for h in range(self.pred_len):
            out[:, h] = np.interp(avg[:, h], self._iso_x[h], self._iso_y[h])
        return out.astype(np.float32)

    def predict_scalar(self, x: np.ndarray, *, mode: str = "max",
                       is_scaled: bool = True) -> np.ndarray:
        p = self.predict_proba(x, is_scaled=is_scaled)
        if mode == "max":
            return p.max(axis=1)
        if mode == "last":
            return p[:, -1]
        if mode == "mean":
            return p.mean(axis=1)
        raise ValueError(f"unknown mode {mode!r}")

    def alert(self, x: np.ndarray, *, is_scaled: bool = True) -> list[dict]:
        probs = self.predict_proba(x, is_scaled=is_scaled)
        thrs = np.array(
            [self.thresholds[h]["threshold"] for h in range(self.pred_len)],
            dtype=np.float32,
        )
        exceed = probs >= thrs
        out: list[dict] = []
        for i in range(probs.shape[0]):
            any_hit = bool(exceed[i].any())
            first = int(np.argmax(exceed[i])) if any_hit else None
            max_idx = int(np.argmax(probs[i]))
            out.append({
                "any_alert": any_hit,
                "first_alert_step": first,
                "max_prob": float(probs[i, max_idx]),
                "max_prob_step": max_idx,
                "per_horizon": probs[i].tolist(),
            })
        return out


__all__ = ["BaggedPredictor"]
