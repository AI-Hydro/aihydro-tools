"""Surrogate dataset export tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from ai_hydro.analysis.inundation_physics import compute_physics_validation_artifacts
from ai_hydro.analysis.inundation_surrogate import (
    bench_surrogate_dataset_contract,
    export_surrogate_dataset,
    export_surrogate_from_physics_job,
    mask_from_rle,
    mask_to_rle,
)


def test_mask_rle_roundtrip():
    mask = np.array([[1, 1, 0], [0, 1, 1], [0, 0, 0]], dtype=bool)
    restored = mask_from_rle(mask_to_rle(mask))
    assert np.array_equal(mask, restored)


def test_export_synthetic_dataset():
    with tempfile.TemporaryDirectory() as tmp:
        out = export_surrogate_dataset(
            {"synthetic_mode": True, "discharge_m3s": 500.0},
            output_path=Path(tmp) / "dataset.json",
        )
        assert out["n_samples"] == 1
        assert out["roundtrip_ok"] is True
        data = json.loads(Path(out["dataset_path"]).read_text())
        assert data["feature_schema"]
        assert data["samples"][0]["target"]["inundated_mask_rle"]["counts"]


def test_export_from_physics_job_artifacts():
    with tempfile.TemporaryDirectory() as tmp:
        ad = Path(tmp)
        hand, physics, hand_summary, physics_summary, cell_size_m, _ = (
            compute_physics_validation_artifacts({"synthetic_mode": True})
        )
        np.savez_compressed(
            ad / "validation_masks.npz",
            hand_mask=hand,
            physics_mask=physics,
            cell_size_m=np.array([cell_size_m]),
        )
        (ad / "status.json").write_text(
            json.dumps(
                {
                    "job_id": "testjob",
                    "status": "complete",
                    "partial_results": {
                        "hand": hand_summary,
                        "physics": physics_summary,
                    },
                }
            ),
            encoding="utf-8",
        )
        out = export_surrogate_from_physics_job(ad)
        assert out["physics_job_id"] == "testjob"
        assert out["roundtrip_ok"] is True


def test_bench_surrogate_contract():
    out = bench_surrogate_dataset_contract()
    assert out["target_schema"] == "physics_inundated_mask_rle"
    assert out["roundtrip_ok"] is True
