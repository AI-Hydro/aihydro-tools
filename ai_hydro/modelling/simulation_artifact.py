"""The simulated series a model job produced, referenced by path and sha256 in its result.

Until now neither training path stored the series the model simulated, so a reviewer could
re-run nothing and re-score nothing (DEMO-1 rehearsal, finding 4a). What is persisted depends on
the installed ``aihydro-modelling``:

* **0.7.0 and later** write ``simulation.csv`` (the whole continuous run, five states, scored and
  imputation flags) and return ``simulation_csv`` + ``simulation_sha256``. Tools references that
  file and re-digests it; it does not rewrite it.
* **0.6.x** returns no series on the spec path (the evaluation arrays are private to the backend and
  dropped) and, on the legacy path, the scored test-period observed/simulated pairs without dates.
  Where those pairs exist they are written to ``simulation_scored_test.csv`` and said to be exactly
  that (no dates, test period, scored days only). Where nothing exists the record says
  ``not_available`` and why. Nothing is reconstructed or re-simulated.

Free of any ``aihydro_modelling`` import so it runs in the public test suite.
"""
from __future__ import annotations

import csv
import hashlib
import math
from pathlib import Path
from typing import Any, Optional, Sequence

#: Result key that carries the reference.
RESULT_KEY = "simulation_series"

SCORED_TEST_FILE = "simulation_scored_test.csv"
SCORED_TEST_COLUMNS = ("k", "obs_mm_day", "sim_mm_day")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _f(x: Any) -> str:
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else repr(float(x))


def _write_scored_test(path: Path, obs: Sequence[float], sim: Sequence[float]) -> tuple[str, int]:
    """Write the paired scored test days (LF line endings, round-trip exact floats); ``(sha256, rows)``."""
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SCORED_TEST_COLUMNS)
        for k, (o, s) in enumerate(zip(obs, sim)):
            w.writerow([k, _f(o), _f(s)])
    return _sha256_file(path), min(len(obs), len(sim))


def simulation_record(
    result: dict,
    artifact_dir: Any,
    *,
    eval_obs: Optional[Sequence[float]] = None,
    eval_pred: Optional[Sequence[float]] = None,
) -> dict:
    """The ``simulation_series`` description for a finished model result. Never raises.

    ``result`` is the model's result dict. ``eval_obs`` / ``eval_pred`` are the scored test-period
    pairs a 0.6.x backend exposes (legacy path only); they are used only when the installed
    release returned no ``simulation_csv`` of its own.
    """
    version = result.get("aihydro_modelling_version")
    try:
        csv_path, recorded = result.get("simulation_csv"), result.get("simulation_sha256")
        if csv_path and recorded:
            p = Path(str(csv_path))
            if not p.is_file():
                return {"status": "file_missing", "path": str(p), "sha256": str(recorded),
                        "sha256_verified": False, "producer": "aihydro_modelling",
                        "producer_version": version}
            on_disk = _sha256_file(p)
            rec: dict = {
                "status": "stored", "path": str(p), "sha256": str(recorded),
                "sha256_verified": on_disk == str(recorded),
                "rows": result.get("simulation_rows"), "columns": result.get("simulation_columns"),
                "scope": "full_run", "producer": "aihydro_modelling", "producer_version": version,
            }
            if on_disk != str(recorded):
                rec["sha256_on_disk"] = on_disk
            return rec

        if eval_obs is not None and eval_pred is not None and len(eval_obs) and len(eval_obs) == len(eval_pred):
            base = Path(str(result.get("model_dir") or artifact_dir))
            base.mkdir(parents=True, exist_ok=True)
            target = base / SCORED_TEST_FILE
            sha, rows = _write_scored_test(target, eval_obs, eval_pred)
            return {
                "status": "stored", "path": str(target), "sha256": sha, "sha256_verified": True,
                "rows": rows, "columns": list(SCORED_TEST_COLUMNS),
                "scope": "scored_test_days_only",
                "dates": "not recorded: this aihydro-modelling release returns no dates for the scored days "
                         "(scored_period gives the first and last day only)",
                "scored_period": result.get("scored_period"),
                "producer": "aihydro_tools", "producer_version": version,
            }

        return {
            "status": "not_available",
            "reason": ("the installed aihydro-modelling returned no simulated series for this run "
                       "(series files come with aihydro-modelling >= 0.7.0)"),
            "producer_version": version,
        }
    except Exception as exc:       # the reference is additive: never cost a finished model
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:300],
                "producer_version": version}
