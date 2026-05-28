#!/usr/bin/env python3
"""
Batch delineation validation on CAMELS-US gauges (pygeohydro).

Compares reference area (CAMELS ``area_gages2``) vs:
  - ``delineate_watershed(gauge_id)`` (NLDI by NWIS site)
  - ``delineate_from_point`` auto / fast at gauge coordinates

Usage:
  python scripts/validate_delineation_camels.py --n 25
  python scripts/validate_delineation_camels.py --n 50 --skip-fast
  python scripts/validate_delineation_camels.py --gauges 01031500,12045500
  python scripts/validate_delineation_camels.py --experiment-worst 3  # deep dive on top errors
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np


def load_camels_basins() -> list[dict]:
    import pygeohydro as gh

    attr, _ = gh.get_camels()
    basins = []
    for gid, row in attr.iterrows():
        gid_s = str(gid).zfill(8) if str(gid).isdigit() else str(gid)
        area = row.get("area_gages2")
        if area is None or not np.isfinite(float(area)) or float(area) <= 0:
            continue
        basins.append(
            {
                "id": gid_s,
                "name": str(row.get("gauge_name", gid_s))[:40],
                "lat": float(row["gauge_lat"]),
                "lon": float(row["gauge_lon"]),
                "pub": float(area),
                "huc_02": str(row.get("huc_02", "")),
            }
        )
    return basins


def stratified_sample(basins: list[dict], n: int, seed: int = 42) -> list[dict]:
    if n >= len(basins):
        return basins
    rng = np.random.default_rng(seed)
    areas = np.array([b["pub"] for b in basins])
    log_a = np.log10(np.maximum(areas, 1.0))
    # Equal-count bins on log-area
    quantiles = np.linspace(0, 1, min(n, 10) + 1)
    bins = np.quantile(log_a, quantiles)
    chosen: list[dict] = []
    per_bin = max(1, n // len(quantiles))
    for i in range(len(quantiles) - 1):
        mask = (log_a >= bins[i]) & (log_a <= bins[i + 1] if i == len(quantiles) - 2 else log_a < bins[i + 1])
        pool = [b for b, m in zip(basins, mask) if m]
        if not pool:
            continue
        k = min(per_bin, len(pool))
        idx = rng.choice(len(pool), size=k, replace=False)
        chosen.extend(pool[j] for j in idx)
    # Top up to n
    remaining = [b for b in basins if b not in chosen]
    if len(chosen) < n and remaining:
        extra = min(n - len(chosen), len(remaining))
        idx = rng.choice(len(remaining), size=extra, replace=False)
        chosen.extend(remaining[j] for j in idx)
    return chosen[:n]


def pct_error(actual: float, expected: float) -> float:
    if expected <= 0:
        return float("nan")
    return 100.0 * abs(actual - expected) / expected


def summarize(rows: list[dict], key: str) -> None:
    errs = [r[key] for r in rows if r.get(key) is not None and r[key] == r[key]]
    if not errs:
        return
    errs_sorted = sorted(errs)
    mid = errs_sorted[len(errs_sorted) // 2]
    print(
        f"  {key:28s}  n={len(errs):3d}  "
        f"median={mid:5.0f}%  p90={errs_sorted[int(0.9 * (len(errs_sorted)-1))]:5.0f}%  "
        f"max={max(errs):5.0f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate delineation on CAMELS-US gauges")
    parser.add_argument("--n", type=int, default=25, help="Number of stratified gauges (default 25)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--gauges", type=str, default="", help="Comma-separated gauge IDs (overrides --n)")
    parser.add_argument("--skip-fast", action="store_true", help="Skip slow fast-tier DEM runs")
    parser.add_argument("--skip-auto", action="store_true")
    parser.add_argument(
        "--out",
        type=str,
        default="/tmp/camels_delineation_validation.json",
    )
    parser.add_argument(
        "--experiment-worst",
        type=int,
        default=0,
        metavar="K",
        help="Run experiment_delineation_snap on K worst fast-tier errors",
    )
    args = parser.parse_args()

    print("Loading CAMELS-US attributes…")
    all_basins = load_camels_basins()
    print(f"  {len(all_basins)} gauges with valid area_gages2")

    if args.gauges.strip():
        ids = {g.strip().zfill(8) for g in args.gauges.split(",") if g.strip()}
        basins = [b for b in all_basins if b["id"] in ids]
    else:
        basins = stratified_sample(all_basins, args.n, seed=args.seed)

    print(f"Validating {len(basins)} gauges…\n")
    print(
        f"{'Gauge':<10} {'Area':>8} {'NLDI_g':>8} {'%e':>5} "
        f"{'Auto':>8} {'%e':>5} {'Fast':>8} {'%e':>5}  note"
    )
    print("-" * 88)

    from ai_hydro.analysis.watershed import delineate_watershed

    rows: list[dict] = []
    t0 = time.time()

    for i, b in enumerate(basins):
        gid = b["id"]
        pub = b["pub"]
        note = ""

        nldi_area = nldi_err = None
        auto_area = auto_err = auto_method = None
        fast_area = fast_err = fast_method = None

        try:
            r = delineate_watershed(gid)
            nldi_area = r.data.get("area_km2")
            nldi_err = pct_error(nldi_area, pub) if nldi_area else None
        except Exception as e:
            note = f"gauge:{e.__class__.__name__}"

        if not args.skip_auto:
            try:
                from ai_hydro.analysis.delineation.router import delineate_from_point

                ar = delineate_from_point(
                    b["lat"], b["lon"], method="auto", expected_area_km2=pub
                )
                auto_area = ar.data.get("area_km2")
                auto_method = ar.data.get("method_used")
                auto_err = pct_error(auto_area, pub) if auto_area else None
            except Exception as e:
                note = (note + f" auto:{e.__class__.__name__}").strip()

        if not args.skip_fast:
            try:
                from ai_hydro.analysis.delineation.router import delineate_from_point

                fr = delineate_from_point(
                    b["lat"], b["lon"], method="fast", expected_area_km2=pub
                )
                fast_area = fr.data.get("area_km2")
                fast_method = fr.data.get("method_used")
                fast_err = pct_error(fast_area, pub) if fast_area else None
            except Exception as e:
                note = (note + f" fast:{e.__class__.__name__}").strip()

        def fmt(v, err):
            if v is None:
                return "—", "—"
            return f"{v:.0f}", f"{err:.0f}" if err is not None and err == err else "—"

        na, ne = fmt(nldi_area, nldi_err)
        aa, ae = fmt(auto_area, auto_err)
        fa, fe = fmt(fast_area, fast_err)
        print(
            f"{gid:<10} {pub:>8.0f} {na:>8} {ne:>5} {aa:>8} {ae:>5} {fa:>8} {fe:>5}  "
            f"{(auto_method or fast_method or note)[:14]}"
        )

        rows.append(
            {
                "gauge_id": gid,
                "name": b["name"],
                "huc_02": b.get("huc_02"),
                "lat": b["lat"],
                "lon": b["lon"],
                "camels_area_km2": pub,
                "gauge_nldi_km2": nldi_area,
                "gauge_nldi_pct_error": nldi_err,
                "auto_km2": auto_area,
                "auto_pct_error": auto_err,
                "auto_method": auto_method,
                "fast_km2": fast_area,
                "fast_pct_error": fast_err,
                "fast_method": fast_method,
                "notes": note,
            }
        )

    elapsed = time.time() - t0
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(
            {
                "n_gauges": len(rows),
                "elapsed_s": round(elapsed, 1),
                "rows": rows,
            },
            f,
            indent=2,
        )

    print(f"\nWrote {out_path}  ({elapsed/60:.1f} min)")
    print("\nError summary (|%| vs CAMELS area_gages2):")
    summarize(rows, "gauge_nldi_pct_error")
    summarize(rows, "auto_pct_error")
    summarize(rows, "fast_pct_error")

    within_20 = sum(
        1 for r in rows if r.get("fast_pct_error") is not None and r["fast_pct_error"] <= 20
    )
    fast_ok = [r for r in rows if r.get("fast_pct_error") is not None]
    if fast_ok:
        print(f"\nFast tier within 20% error: {within_20}/{len(fast_ok)}")

    if args.experiment_worst > 0 and not args.skip_fast:
        worst = sorted(
            [r for r in rows if r.get("fast_pct_error") is not None],
            key=lambda r: r["fast_pct_error"],
            reverse=True,
        )[: args.experiment_worst]
        print(f"\nDeep experiment on {len(worst)} worst fast-tier gauges…")
        import importlib.util

        exp_path = Path(__file__).resolve().parent / "experiment_delineation_snap.py"
        spec = importlib.util.spec_from_file_location("experiment_delineation_snap", exp_path)
        exp_mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(exp_mod)

        for r in worst:
            exp_mod.diagnose_basin(
                {
                    "id": r["gauge_id"],
                    "name": r["name"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "pub": r["camels_area_km2"],
                }
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
