"""
Physics-based scientific validators.

Validators reduce invalid scientific composition at the workflow level.
They are agent-callable tools that produce diagnostic outputs (ValidatorResult).
"""
from __future__ import annotations

import logging
from typing import Literal
from ai_hydro.mcp.app import mcp
from ai_hydro.session import HydroSession
from ai_hydro.validators.models import ValidatorResult
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def check_water_balance_consistency(session_id: str) -> dict:
    """
    Check if annual runoff is less than or equal to annual precipitation.
    
    Runoff ratio > 1.0 indicates a potential error or significant 
    non-precipitation water source (snowmelt, groundwater).
    """
    try:
        session = HydroSession.load(session_id)
        sigs = session.get("signatures")
        if not sigs:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Hydrological signatures (runoff_ratio) not found in session.",
                affected_slots=["signatures"],
                recommendation="Run extract_hydrological_signatures first."
            ).model_dump()

        rr = sigs.get("data", {}).get("runoff_ratio")
        if rr is None:
            return ValidatorResult(
                validator="water_balance_consistency",
                status="insufficient_data",
                message="Runoff ratio missing from signatures data.",
                affected_slots=["signatures"]
            ).model_dump()

        if rr > 1.0:
            severity = "medium" if rr < 1.1 else "high"
            return ValidatorResult(
                validator="water_balance_consistency",
                status="warning",
                severity=severity,
                message=f"Annual runoff ratio is {rr:.2f} (> 1.0).",
                recommendation="Investigate if snowmelt, groundwater release, or gauging errors are present.",
                affected_slots=["signatures"]
            ).model_dump()

        return ValidatorResult(
            validator="water_balance_consistency",
            status="pass",
            message=f"Water balance is consistent (Runoff Ratio: {rr:.2f}).",
            affected_slots=["signatures"]
        ).model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_temporal_alignment(session_id: str, slot_a: str, slot_b: str) -> dict:
    """
    Check if two time-series slots share the same temporal range.
    """
    try:
        session = HydroSession.load(session_id)
        a = session.get(slot_a)
        b = session.get(slot_b)

        if not a or not b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="insufficient_data",
                message=f"One or both slots ({slot_a}, {slot_b}) are missing.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        ma = a.get("meta", {}).get("params", {})
        mb = b.get("meta", {}).get("params", {})
        
        start_a, end_a = ma.get("start_date"), ma.get("end_date")
        start_b, end_b = mb.get("start_date"), mb.get("end_date")

        if not all([start_a, end_a, start_b, end_b]):
            return ValidatorResult(
                validator="temporal_alignment",
                status="warning",
                severity="low",
                message="Missing temporal metadata (start/end dates). Cannot verify alignment.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        if start_a == start_b and end_a == end_b:
            return ValidatorResult(
                validator="temporal_alignment",
                status="pass",
                message=f"Slots aligned: {start_a} to {end_a}.",
                affected_slots=[slot_a, slot_b]
            ).model_dump()

        return ValidatorResult(
            validator="temporal_alignment",
            status="fail",
            severity="medium",
            message=f"Temporal mismatch: {slot_a} ({start_a} to {end_a}) vs {slot_b} ({start_b} to {end_b}).",
            recommendation="Re-fetch or truncate data to ensure consistent temporal overlap.",
            affected_slots=[slot_a, slot_b]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_unit_consistency(session_id: str, slot: str, expected_units: str) -> dict:
    """
    Check if a session slot uses the expected scientific units.
    """
    try:
        session = HydroSession.load(session_id)
        s = session.get(slot)
        if not s:
            return ValidatorResult(
                validator="unit_consistency",
                status="insufficient_data",
                message=f"Slot '{slot}' not found.",
                affected_slots=[slot]
            ).model_dump()

        # Check 'data.units' or 'meta.units'
        actual_units = s.get("data", {}).get("units") or s.get("meta", {}).get("units")
        
        if not actual_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="warning",
                severity="low",
                message=f"Units not explicitly declared for slot '{slot}'.",
                affected_slots=[slot]
            ).model_dump()

        if actual_units == expected_units:
            return ValidatorResult(
                validator="unit_consistency",
                status="pass",
                message=f"Units consistent: {actual_units}.",
                affected_slots=[slot]
            ).model_dump()

        return ValidatorResult(
            validator="unit_consistency",
            status="fail",
            severity="high",
            message=f"Unit mismatch for '{slot}': Found '{actual_units}', expected '{expected_units}'.",
            recommendation=f"Apply unit conversion to {expected_units} before proceeding.",
            affected_slots=[slot]
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_record_length(session_id: str) -> dict:
    """
    Guard against analyses on too-short records.

    Reads n_days from the streamflow slot and applies tiered thresholds
    from hydrology literature (Vogel & Fennessey 1994; Burn & Elnur 2002):
      - FAIL    : n < 3650 days (< 10 years) — flood frequency unreliable
      - WARNING : n < 7300 days (< 20 years) — adequate but at low end
      - PASS    : n ≥ 7300 days (20+ years)

    Fires automatically after fetch_streamflow_data and
    extract_hydrological_signatures.
    """
    try:
        session = HydroSession.load(session_id)
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="record_length",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        n_days: int | None = data.get("n_days")

        # Fall back: use q_cms_n (stripped count) or count short q_cms array
        if n_days is None:
            n_days = data.get("q_cms_n")  # stored when array was stripped on save
            if n_days is None:
                q_vals = data.get("q_cms", [])
                if q_vals:
                    n_days = sum(1 for v in q_vals if v is not None)

        if n_days is None:
            return ValidatorResult(
                validator="record_length",
                status="insufficient_data",
                message="Record length could not be determined from the streamflow slot.",
                affected_slots=["streamflow"],
            ).model_dump()

        years_approx = n_days / 365.25

        if n_days < 3650:
            return ValidatorResult(
                validator="record_length",
                status="fail",
                severity="high",
                message=(
                    f"Record is only {n_days} days ({years_approx:.1f} years). "
                    "Flood-frequency analysis requires ≥ 10 years of data."
                ),
                recommendation=(
                    "Extend the record length or restrict the analysis to "
                    "low-return-period statistics (< 10-year events). "
                    "Do not use this record for design-flood estimation."
                ),
                affected_slots=["streamflow"],
                metadata={"n_days": n_days, "quality_flag": "record_too_short"},
            ).model_dump()

        if n_days < 7300:
            return ValidatorResult(
                validator="record_length",
                status="warning",
                severity="medium",
                message=(
                    f"Record is {n_days} days ({years_approx:.1f} years) — "
                    "adequate but below the 20-year recommended minimum for "
                    "robust flood-frequency and trend analyses."
                ),
                recommendation=(
                    "Use 90% CIs on all frequency estimates and note the "
                    "limited record length as a limitation in any claim. "
                    "If a longer record is available, extend the fetch range."
                ),
                affected_slots=["streamflow"],
                metadata={"n_days": n_days, "quality_flag": "record_length_low"},
            ).model_dump()

        return ValidatorResult(
            validator="record_length",
            status="pass",
            message=(
                f"Record length is {n_days} days ({years_approx:.1f} years) — "
                "meets the ≥ 20-year threshold for reliable flood-frequency analysis."
            ),
            affected_slots=["streamflow"],
            metadata={"n_days": n_days},
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_usgs_qualification_codes(session_id: str) -> dict:
    """
    Flag provisional or estimated USGS streamflow data.

    Reads qualification_codes from the streamflow slot metadata. USGS
    NWIS uses 'P' for provisional (not yet reviewed) and 'e' for estimated
    records. Provisional data may be revised; using it in published claims
    without noting this is a scientific integrity risk.

    Returns 'insufficient_data' when no qualification codes are stored —
    the fetch pipeline does not yet propagate these codes for all sources.
    In that case, treat the data as unreviewed and note it as a limitation.
    """
    try:
        session = HydroSession.load(session_id)
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        meta = sf.get("meta", {})

        # Check meta first, then data-level keys
        qual_codes: list | None = (
            meta.get("qualification_codes")
            or data.get("qualification_codes")
        )
        provisional_flag: bool | None = (
            meta.get("provisional")
            or data.get("provisional")
        )

        if qual_codes is None and provisional_flag is None:
            # Not propagated by fetch pipeline yet
            source = meta.get("source", "")
            note = (
                " NWIS streams may include provisional records not flagged here."
                if "nwis" in source.lower() or "usgs" in source.lower()
                else ""
            )
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="insufficient_data",
                message=(
                    "Qualification codes are not present in the streamflow metadata."
                    + note
                ),
                affected_slots=["streamflow"],
                recommendation=(
                    "Verify the data review status directly from the NWIS data portal "
                    "(waterdata.usgs.gov) for the gauge. Note in any claim whether the "
                    "data has been reviewed/approved by USGS."
                ),
                metadata={"quality_flag": "qualification_codes_absent"},
            ).model_dump()

        has_provisional = bool(provisional_flag) or (
            isinstance(qual_codes, list) and "P" in qual_codes
        )
        has_estimated = isinstance(qual_codes, list) and "e" in qual_codes

        if has_provisional:
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="warning",
                severity="medium",
                message=(
                    "Streamflow record contains provisional ('P') data that has not "
                    "yet been reviewed and approved by USGS. Values may be revised."
                ),
                recommendation=(
                    "Add a limitations entry to any claim using this data: "
                    "'Data is provisional and subject to revision by USGS.' "
                    "Re-check the NWIS portal before publication."
                ),
                affected_slots=["streamflow"],
                metadata={"quality_flag": "provisional_data", "codes": qual_codes},
            ).model_dump()

        if has_estimated:
            return ValidatorResult(
                validator="usgs_qualification_codes",
                status="warning",
                severity="low",
                message=(
                    "Streamflow record contains estimated ('e') values — "
                    "gaps were filled using regression or nearby gauges."
                ),
                recommendation=(
                    "Note in any claim that some values are estimated. "
                    "Estimated records are particularly common during flood peaks."
                ),
                affected_slots=["streamflow"],
                metadata={"quality_flag": "estimated_data", "codes": qual_codes},
            ).model_dump()

        return ValidatorResult(
            validator="usgs_qualification_codes",
            status="pass",
            message="Streamflow data is approved / reviewed (no provisional or estimated codes).",
            affected_slots=["streamflow"],
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_regulated_basin(session_id: str) -> dict:
    """
    Flag basins with known upstream regulation (dams, diversions).

    First checks the session delineation slot for an explicit 'regulated'
    metadata flag (populated by delineate_watershed when NID data is
    available). Falls back to checking the NID GeoPackage at
    ~/.aihydro/cache/full_nid_inventory.gpkg — if present, performs a
    spatial query on the watershed boundary.

    Returns 'insufficient_data' when neither the metadata flag nor the NID
    cache is available. In that case, the regulation status is unknown and
    should be listed as a limitation.
    """
    try:
        from pathlib import Path as _Path
        session = HydroSession.load(session_id)

        # --- Step 1: check explicit metadata flag in delineation or signatures slot
        for slot_name in ("delineation", "watershed", "signatures"):
            slot = session.get(slot_name)
            if not slot:
                continue
            data = slot.get("data", {})
            meta = slot.get("meta", {})
            regulated = data["regulated"] if "regulated" in data else meta.get("regulated")
            if regulated is not None:
                if regulated:
                    return ValidatorResult(
                        validator="regulated_basin",
                        status="warning",
                        severity="high",
                        message=(
                            f"Basin is flagged as regulated (upstream dams or diversions "
                            f"detected via '{slot_name}' metadata)."
                        ),
                        recommendation=(
                            "Natural-flow statistics (BFI, flood frequency, recession constants) "
                            "may not reflect catchment hydrology. Add a limitations entry: "
                            "'Basin contains upstream regulation; natural-flow statistics "
                            "are approximate.' Consider using regulated-flow modelling."
                        ),
                        affected_slots=[slot_name],
                        metadata={"quality_flag": "regulated_basin"},
                    ).model_dump()
                else:
                    return ValidatorResult(
                        validator="regulated_basin",
                        status="pass",
                        message=f"Basin flagged as unregulated in '{slot_name}' metadata.",
                        affected_slots=[slot_name],
                    ).model_dump()

        # --- Step 2: NID GeoPackage spatial check
        nid_path = _Path.home() / ".aihydro" / "cache" / "full_nid_inventory.gpkg"
        if nid_path.exists():
            try:
                import geopandas as _gpd  # optional dep

                # Try to get watershed geometry from delineation slot
                geom_wkt: str | None = None
                for slot_name in ("delineation", "watershed"):
                    slot = session.get(slot_name)
                    if slot:
                        geom_wkt = slot.get("data", {}).get("geometry_wkt")
                        if geom_wkt:
                            break

                if geom_wkt:
                    from shapely import wkt as _wkt
                    basin_geom = _wkt.loads(geom_wkt)
                    nid = _gpd.read_file(nid_path, mask=basin_geom)
                    n_dams = len(nid)
                    if n_dams > 0:
                        return ValidatorResult(
                            validator="regulated_basin",
                            status="warning",
                            severity="high",
                            message=(
                                f"NID inventory found {n_dams} dam(s) within the watershed boundary."
                            ),
                            recommendation=(
                                "Natural-flow statistics may be affected by upstream regulation. "
                                "Add a limitations entry and consider regulated-flow corrections."
                            ),
                            affected_slots=["delineation"],
                            metadata={
                                "quality_flag": "regulated_basin",
                                "n_dams_in_watershed": n_dams,
                            },
                        ).model_dump()
                    return ValidatorResult(
                        validator="regulated_basin",
                        status="pass",
                        message=f"NID inventory found no dams within the watershed boundary.",
                        affected_slots=["delineation"],
                        metadata={"n_dams_in_watershed": 0},
                    ).model_dump()
            except ImportError:
                pass  # geopandas/shapely not installed — fall through to insufficient_data

        return ValidatorResult(
            validator="regulated_basin",
            status="insufficient_data",
            message=(
                "Regulation status could not be determined: no 'regulated' metadata "
                "flag in session and NID GeoPackage not found at "
                "~/.aihydro/cache/full_nid_inventory.gpkg."
            ),
            affected_slots=["delineation"],
            recommendation=(
                "Add 'regulated: false/true' to the watershed metadata, or download "
                "the NID GeoPackage to ~/.aihydro/cache/full_nid_inventory.gpkg. "
                "As a precaution, list regulation status as a limitation in any claim."
            ),
            metadata={"quality_flag": "regulation_unknown"},
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def check_stationarity(session_id: str) -> dict:
    """
    Test for a statistically significant trend in annual streamflow totals.

    Applies the Mann-Kendall rank-correlation trend test (Kendall 1975;
    Mann 1945) to annual streamflow totals derived from the streamflow slot.
    A significant positive or negative trend (p < 0.05) indicates
    non-stationarity, which invalidates the classical Flood Frequency Analysis
    assumption of identically distributed annual maxima.

    Requires at least 5 years of data for a meaningful test.
    Degrades gracefully when scipy is unavailable.
    """
    try:
        session = HydroSession.load(session_id)
        sf = session.get("streamflow")
        if not sf:
            return ValidatorResult(
                validator="stationarity",
                status="insufficient_data",
                message="No streamflow slot found. Fetch streamflow data first.",
                affected_slots=["streamflow"],
                recommendation="Call fetch_streamflow_data or data_fetch(variable='streamflow', ...).",
            ).model_dump()

        data = sf.get("data", {})
        dates: list = data.get("dates", [])
        q_vals: list = data.get("q_cms", [])

        # Large arrays are stripped to counts when saved (session store threshold = 50).
        # Load them back from the _data_file path if available.
        if (not dates or not q_vals) and data.get("_data_file"):
            try:
                import json as _json
                from pathlib import Path as _PF
                raw = _json.loads(_PF(data["_data_file"]).read_text())
                dates = raw.get("dates", dates)
                q_vals = raw.get("q_cms", q_vals)
            except Exception:
                pass

        if not dates or not q_vals or len(dates) != len(q_vals):
            return ValidatorResult(
                validator="stationarity",
                status="insufficient_data",
                message=(
                    "Streamflow time-series arrays (dates, q_cms) not found or "
                    "mismatched in the streamflow slot."
                ),
                affected_slots=["streamflow"],
                recommendation=(
                    "The streamflow slot may have been stored in a compact form "
                    "(arrays stripped to save context). Re-fetch streamflow data to "
                    "populate the full arrays, or ensure _data_file is set."
                ),
            ).model_dump()

        # Build annual totals (water year starting October 1)
        from collections import defaultdict as _dd
        year_totals: dict[int, list[float]] = _dd(list)
        for date_str, q in zip(dates, q_vals):
            if q is None or not isinstance(q, (int, float)):
                continue
            try:
                year = int(date_str[:4])
                month = int(date_str[5:7])
                wy = year if month >= 10 else year - 1
                year_totals[wy].append(float(q))
            except (ValueError, IndexError):
                continue

        # Only keep water years with ≥ 330 days of data (avoid incomplete years)
        annual: list[tuple[int, float]] = sorted(
            (wy, sum(vals)) for wy, vals in year_totals.items() if len(vals) >= 330
        )

        if len(annual) < 5:
            return ValidatorResult(
                validator="stationarity",
                status="insufficient_data",
                message=(
                    f"Only {len(annual)} complete water year(s) found "
                    f"(need ≥ 5 for a meaningful trend test)."
                ),
                affected_slots=["streamflow"],
                recommendation="Extend the record to at least 5 complete water years.",
            ).model_dump()

        years = [a[0] for a in annual]
        totals = [a[1] for a in annual]

        # Mann-Kendall via scipy.stats.kendalltau (available as a scipy dep)
        try:
            from scipy.stats import kendalltau as _kt
            tau, p_value = _kt(years, totals)
        except ImportError:
            return ValidatorResult(
                validator="stationarity",
                status="insufficient_data",
                message=(
                    "scipy is not installed — Mann-Kendall trend test could not run."
                ),
                affected_slots=["streamflow"],
                recommendation="Install scipy (pip install scipy) to enable stationarity testing.",
            ).model_dump()

        direction = "increasing" if tau > 0 else "decreasing"
        n_years = len(annual)

        if p_value < 0.05:
            return ValidatorResult(
                validator="stationarity",
                status="warning",
                severity="high",
                message=(
                    f"Significant {direction} trend in annual streamflow totals "
                    f"over {n_years} water years (Mann-Kendall τ = {tau:.3f}, "
                    f"p = {p_value:.4f} < 0.05). Flood-frequency analysis assumes "
                    "stationarity — this assumption may be violated."
                ),
                recommendation=(
                    "Add a limitations entry: 'Statistically significant "
                    f"{direction} streamflow trend detected (p = {p_value:.4f}); "
                    "classical FFA stationarity assumption may not hold.' "
                    "Consider non-stationary FFA (e.g. GEV with time covariate) "
                    "or climate-corrected analysis."
                ),
                affected_slots=["streamflow"],
                metadata={
                    "quality_flag": "non_stationary",
                    "mann_kendall_tau": round(tau, 4),
                    "p_value": round(p_value, 4),
                    "n_water_years": n_years,
                    "trend_direction": direction,
                },
            ).model_dump()

        return ValidatorResult(
            validator="stationarity",
            status="pass",
            message=(
                f"No significant trend in annual streamflow totals over {n_years} "
                f"water years (Mann-Kendall τ = {tau:.3f}, p = {p_value:.4f} ≥ 0.05). "
                "Stationarity assumption is not violated."
            ),
            affected_slots=["streamflow"],
            metadata={
                "mann_kendall_tau": round(tau, 4),
                "p_value": round(p_value, 4),
                "n_water_years": n_years,
            },
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)


def check_uncertainty_present(session_id: str, slot: str = "signatures") -> dict:
    """
    Check that a session result slot contains uncertainty (bootstrap CI) data.

    Fires as a post-run validator for analysis tools that should produce
    uncertainty estimates. Returns a warning quality_flag when '_uncertainty'
    is absent, so the agent knows to interpret results as point estimates only.

    This is NOT an @mcp.tool() — it is registered via register_post_validator
    and fires automatically after Tier 1 analysis tools complete.
    """
    try:
        from ai_hydro.session import HydroSession
        session = HydroSession.load(session_id)
        result = session.get(slot)

        if result is None:
            return ValidatorResult(
                validator="uncertainty_present",
                status="insufficient_data",
                message=f"Slot '{slot}' not found in session.",
                affected_slots=[slot],
                recommendation=f"Run the analysis tool that populates '{slot}' first.",
            ).model_dump()

        data = result.get("data", {}) if isinstance(result, dict) else {}
        # C3 results nest data under feature_id → params_key → {data, meta, uncertainty}.
        # Walk one level: check direct 'data' and also first feature's first result.
        has_uncertainty = "_uncertainty" in data
        if not has_uncertainty:
            for fid_val in data.values():
                if isinstance(fid_val, dict):
                    for pk_val in fid_val.values():
                        if isinstance(pk_val, dict) and "_uncertainty" in pk_val.get("data", {}):
                            has_uncertainty = True
                            break
                if has_uncertainty:
                    break

        if not has_uncertainty:
            return ValidatorResult(
                validator="uncertainty_present",
                status="warning",
                severity="medium",
                message=(
                    f"Slot '{slot}' result has no '_uncertainty' key — "
                    "all values are point estimates without confidence intervals."
                ),
                recommendation=(
                    "The analysis ran without bootstrap CI computation (possibly "
                    "insufficient data length, n<30). Treat numeric values as "
                    "point estimates. For defensible claims, ensure ≥30 data "
                    "points and re-run the analysis."
                ),
                affected_slots=[slot],
                metadata={"quality_flag": "uncertainty_missing"},
            ).model_dump()

        return ValidatorResult(
            validator="uncertainty_present",
            status="pass",
            message=f"Slot '{slot}' contains bootstrap CI uncertainty estimates.",
            affected_slots=[slot],
        ).model_dump()

    except Exception as exc:
        return _tool_error_to_dict(exc)
