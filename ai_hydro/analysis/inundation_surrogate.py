"""
Training substrate for inundation extent surrogates (Phase 3 optional).

Exports compact HAND → physics/proxy mask pairs for offline ML experiments.
Does not train models — pairs with the existing train_hydro_model job pattern.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ai_hydro.analysis.inundation import INUNDATION_CAVEAT
from ai_hydro.analysis.inundation_physics import (
    PHYSICS_CAVEAT,
    compute_physics_validation_artifacts,
    proxy_physics_mask,
)
from ai_hydro.analysis.inundation_validation import contingency_metrics

SURROGATE_SCOPE: dict[str, Any] = {
    "dataset_kind": "inundation_extent_surrogate",
    "method_tier": "validate_physics_export",
    "target": "physics_inundated_mask",
    "input": "hand_features_and_mask",
}

SURROGATE_CAVEAT = (
    "Surrogate training samples pair HAND inputs with physics or explicit proxy extents. "
    "Proxy targets are not operational flood maps. Full SWE-GNN-style training is deferred."
)

FEATURE_SCHEMA = [
    "discharge_m3s",
    "stage_likely_m",
    "hand_area_km2",
    "cell_size_m",
    "grid_shape",
]

TARGET_SCHEMA = "physics_inundated_mask_rle"

__all__ = [
    "SURROGATE_SCOPE",
    "SURROGATE_CAVEAT",
    "FEATURE_SCHEMA",
    "TARGET_SCHEMA",
    "mask_to_rle",
    "mask_from_rle",
    "build_surrogate_sample",
    "build_surrogate_dataset",
    "write_surrogate_dataset",
    "export_surrogate_dataset",
    "export_surrogate_from_physics_job",
    "load_surrogate_dataset",
    "train_morphology_baseline",
    "train_surrogate_from_dataset",
    "apply_morphology_surrogate",
    "bench_surrogate_dataset_contract",
    "bench_surrogate_train_contract",
]


def mask_to_rle(mask: np.ndarray) -> dict[str, Any]:
    """Row-major run-length encoding for boolean inundation masks."""
    flat = np.asarray(mask, dtype=np.uint8).ravel()
    if flat.size == 0:
        return {"shape": list(mask.shape), "counts": [], "encoding": "rle_uint8"}
    counts: list[int] = []
    current = int(flat[0])
    run = 1
    for value in flat[1:]:
        v = int(value)
        if v == current:
            run += 1
        else:
            counts.extend([run, current])
            current = v
            run = 1
    counts.extend([run, current])
    return {"shape": [int(x) for x in mask.shape], "counts": counts, "encoding": "rle_uint8"}


def mask_from_rle(payload: dict[str, Any]) -> np.ndarray:
    shape = tuple(int(x) for x in payload["shape"])
    counts = payload.get("counts") or []
    flat: list[int] = []
    it = iter(counts)
    for run, value in zip(it, it, strict=False):
        flat.extend([int(value)] * int(run))
    arr = np.asarray(flat, dtype=np.uint8).reshape(shape)
    return arr.astype(bool)


def build_surrogate_sample(
    *,
    hand_mask: np.ndarray,
    physics_mask: np.ndarray,
    hand_summary: dict[str, Any],
    physics_summary: dict[str, Any],
    cell_size_m: float,
    sample_id: str | None = None,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id or uuid.uuid4().hex[:12],
        "features": {
            "discharge_m3s": hand_summary.get("discharge_m3s"),
            "stage_likely_m": hand_summary.get("stage_likely_m"),
            "hand_area_km2": hand_summary.get("area_km2_likely"),
            "cell_size_m": float(cell_size_m),
            "grid_shape": [int(x) for x in np.asarray(hand_mask).shape],
        },
        "hand_mask_rle": mask_to_rle(hand_mask),
        "target": {
            "physics_method": physics_summary.get("method"),
            "physics_status": physics_summary.get("status"),
            "physics_area_km2": physics_summary.get("area_km2"),
            "inundated_mask_rle": mask_to_rle(physics_mask),
        },
    }


def build_surrogate_dataset(
    samples: list[dict[str, Any]],
    *,
    source: str,
    physics_job_id: str | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "dataset_kind": SURROGATE_SCOPE["dataset_kind"],
        "feature_schema": FEATURE_SCHEMA,
        "target_schema": TARGET_SCHEMA,
        "scope": SURROGATE_SCOPE,
        "caveat": SURROGATE_CAVEAT,
        "inundation_caveat": INUNDATION_CAVEAT,
        "physics_caveat": PHYSICS_CAVEAT,
        "source": source,
        "physics_job_id": physics_job_id,
        "n_samples": len(samples),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "samples": samples,
    }


def write_surrogate_dataset(dataset: dict[str, Any], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    return path


def export_surrogate_dataset(
    cfg: dict[str, Any],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build dataset from synthetic cfg or inline physics-validation artifacts."""
    (
        hand_mask,
        physics_mask,
        hand_summary,
        physics_summary,
        cell_size_m,
        _backend,
    ) = compute_physics_validation_artifacts(cfg)

    sample = build_surrogate_sample(
        hand_mask=hand_mask,
        physics_mask=physics_mask,
        hand_summary=hand_summary,
        physics_summary=physics_summary,
        cell_size_m=cell_size_m,
        sample_id=cfg.get("sample_id"),
    )
    dataset = build_surrogate_dataset(
        [sample],
        source=str(cfg.get("source") or "inline_physics_artifacts"),
        physics_job_id=cfg.get("physics_job_id"),
    )

    if output_path is None:
        output_path = Path(cfg.get("workspace_dir") or ".") / "inundation_surrogate_dataset.json"
    path = write_surrogate_dataset(dataset, output_path)

    return {
        "dataset_path": str(path),
        "n_samples": dataset["n_samples"],
        "feature_schema": dataset["feature_schema"],
        "target_schema": dataset["target_schema"],
        "physics_method": physics_summary.get("method"),
        "roundtrip_ok": bool(np.array_equal(hand_mask, mask_from_rle(sample["hand_mask_rle"]))),
    }


def export_surrogate_from_physics_job(
    artifact_dir: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Export one sample from a completed physics validation job directory."""
    ad = Path(artifact_dir)
    masks_path = ad / "validation_masks.npz"
    status_path = ad / "status.json"
    if not masks_path.exists():
        raise FileNotFoundError(
            f"validation_masks.npz not found in {ad}. "
            "Re-run run_inundation_physics_validation on aihydro-tools >= iteration 14."
        )
    if not status_path.exists():
        raise FileNotFoundError(f"status.json not found in {ad}")

    status = json.loads(status_path.read_text())
    report = status.get("partial_results") or {}
    hand_summary = report.get("hand") or {}
    physics_summary = report.get("physics") or {}

    npz = np.load(masks_path)
    hand_mask = np.asarray(npz["hand_mask"], dtype=bool)
    physics_mask = np.asarray(npz["physics_mask"], dtype=bool)
    cell_size_m = float(np.asarray(npz["cell_size_m"]).ravel()[0])

    sample = build_surrogate_sample(
        hand_mask=hand_mask,
        physics_mask=physics_mask,
        hand_summary=hand_summary,
        physics_summary=physics_summary,
        cell_size_m=cell_size_m,
    )
    dataset = build_surrogate_dataset(
        [sample],
        source="physics_validation_job",
        physics_job_id=str(status.get("job_id") or ad.name),
    )

    if output_path is None:
        output_path = ad / "surrogate_dataset.json"
    path = write_surrogate_dataset(dataset, output_path)

    return {
        "dataset_path": str(path),
        "n_samples": dataset["n_samples"],
        "feature_schema": dataset["feature_schema"],
        "target_schema": dataset["target_schema"],
        "physics_method": physics_summary.get("method"),
        "physics_job_id": status.get("job_id"),
        "roundtrip_ok": bool(np.array_equal(physics_mask, mask_from_rle(sample["target"]["inundated_mask_rle"]))),
    }


def load_surrogate_dataset(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not payload.get("samples"):
        raise ValueError(f"Dataset at {path} has no samples")
    return payload


def apply_morphology_surrogate(hand_mask: np.ndarray, *, iterations: int) -> np.ndarray:
    """Apply tuned morphological dilation to a HAND extent mask."""
    if int(iterations) <= 0:
        return np.asarray(hand_mask, dtype=bool)
    return proxy_physics_mask(hand_mask, iterations=int(iterations))


def _tune_morphology_iterations(
    hand_mask: np.ndarray,
    target_mask: np.ndarray,
    *,
    max_iterations: int = 8,
) -> dict[str, Any]:
    hand = np.asarray(hand_mask, dtype=bool)
    target = np.asarray(target_mask, dtype=bool)
    baseline = contingency_metrics(hand, target)
    best = {
        "iterations": 0,
        "csi": float(baseline["csi"]),
        "pod": float(baseline["pod"]),
        "far": float(baseline["far"]),
    }
    for it in range(1, max(int(max_iterations), 1) + 1):
        pred = apply_morphology_surrogate(hand, iterations=it)
        metrics = contingency_metrics(pred, target)
        if float(metrics["csi"]) >= best["csi"]:
            best = {
                "iterations": it,
                "csi": float(metrics["csi"]),
                "pod": float(metrics["pod"]),
                "far": float(metrics["far"]),
            }
    return best


def train_morphology_baseline(
    dataset: dict[str, Any],
    *,
    max_iterations: int = 8,
) -> dict[str, Any]:
    """Tune dilation iterations to maximize CSI vs physics/proxy targets."""
    per_sample: list[dict[str, Any]] = []
    for sample in dataset.get("samples") or []:
        hand = mask_from_rle(sample["hand_mask_rle"])
        target = mask_from_rle(sample["target"]["inundated_mask_rle"])
        tuned = _tune_morphology_iterations(hand, target, max_iterations=max_iterations)
        baseline_csi = float(contingency_metrics(hand, target)["csi"])
        tuned["hand_csi_baseline"] = baseline_csi
        tuned["csi_gain"] = float(tuned["csi"]) - baseline_csi
        per_sample.append(tuned)

    iterations = int(np.median([row["iterations"] for row in per_sample]))
    train_csi = float(np.mean([row["csi"] for row in per_sample]))
    baseline_csi = float(np.mean([row["hand_csi_baseline"] for row in per_sample]))

    return {
        "framework": "morphology_baseline",
        "model_kind": "morphological_dilation",
        "dilation_iterations": iterations,
        "max_iterations_searched": int(max_iterations),
        "n_samples": len(per_sample),
        "train_csi": train_csi,
        "hand_csi_baseline": baseline_csi,
        "csi_gain": train_csi - baseline_csi,
        "per_sample": per_sample,
        "caveat": (
            "Morphology baseline calibrates HAND dilation against physics/proxy masks. "
            "Not a SWE-GNN surrogate; operational extent remains HAND+SRC."
        ),
    }


def train_surrogate_from_dataset(
    dataset_path: str | Path,
    *,
    output_dir: str | Path,
    framework: str = "morphology",
    max_iterations: int = 8,
) -> dict[str, Any]:
    """Train a lightweight surrogate and write ``surrogate_model.json``."""
    dataset = load_surrogate_dataset(dataset_path)
    fw = (framework or "morphology").lower().replace("-", "_")
    if fw not in ("morphology", "morphology_baseline", "morph"):
        raise ValueError(f"Unsupported surrogate framework: {framework!r}")

    model = train_morphology_baseline(dataset, max_iterations=max_iterations)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "surrogate_model.json"
    payload = {
        "version": 1,
        "dataset_path": str(Path(dataset_path).resolve()),
        "dataset_kind": dataset.get("dataset_kind"),
        "physics_job_id": dataset.get("physics_job_id"),
        "scope": SURROGATE_SCOPE,
        "caveat": SURROGATE_CAVEAT,
        **model,
        "trained_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    model_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "model_path": str(model_path),
        "framework": model["framework"],
        "dilation_iterations": model["dilation_iterations"],
        "n_samples": model["n_samples"],
        "train_csi": model["train_csi"],
        "hand_csi_baseline": model["hand_csi_baseline"],
        "csi_improved": model["train_csi"] >= model["hand_csi_baseline"],
    }


def bench_surrogate_dataset_contract() -> dict[str, Any]:
    """Synthetic export yields RLE round-trip dataset (B-078)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = export_surrogate_dataset(
            {
                "synthetic_mode": True,
                "discharge_m3s": 800.0,
                "engine": "sfincs",
                "source": "bench_synthetic",
            },
            output_path=Path(tmp) / "surrogate_dataset.json",
        )
    return out


def bench_surrogate_train_contract() -> dict[str, Any]:
    """Synthetic dataset → morphology baseline with CSI gain (B-079)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "surrogate_dataset.json"
        export_surrogate_dataset(
            {
                "synthetic_mode": True,
                "discharge_m3s": 800.0,
                "engine": "sfincs",
                "source": "bench_train",
            },
            output_path=dataset_path,
        )
        out = train_surrogate_from_dataset(
            dataset_path,
            output_dir=tmp_path / "model",
            framework="morphology",
        )
    return out
