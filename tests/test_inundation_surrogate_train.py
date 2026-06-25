"""Surrogate training tests."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from ai_hydro.analysis.inundation_surrogate import (
    apply_morphology_surrogate,
    bench_surrogate_train_contract,
    export_surrogate_dataset,
    load_surrogate_dataset,
    mask_from_rle,
    mask_to_rle,
    train_morphology_baseline,
    train_surrogate_from_dataset,
)


def test_train_morphology_improves_csi():
    hand = np.zeros((16, 16), dtype=bool)
    hand[4:12, 4:12] = True
    target = apply_morphology_surrogate(hand, iterations=2)
    dataset = {
        "samples": [
            {
                "hand_mask_rle": mask_to_rle(hand),
                "target": {"inundated_mask_rle": mask_to_rle(target)},
            }
        ]
    }
    model = train_morphology_baseline(dataset)
    assert model["dilation_iterations"] == 2
    assert model["train_csi"] >= model["hand_csi_baseline"]


def test_train_surrogate_writes_model_json():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dataset_path = tmp_path / "dataset.json"
        export_surrogate_dataset(
            {"synthetic_mode": True, "discharge_m3s": 500.0, "engine": "sfincs"},
            output_path=dataset_path,
        )
        out = train_surrogate_from_dataset(
            dataset_path,
            output_dir=tmp_path / "run",
            framework="morphology",
        )
        model = json.loads(Path(out["model_path"]).read_text())
        assert model["framework"] == "morphology_baseline"
        assert load_surrogate_dataset(dataset_path)["n_samples"] == 1


def test_bench_surrogate_train_contract():
    out = bench_surrogate_train_contract()
    assert out["csi_improved"] is True
    assert out["dilation_iterations"] == 2
