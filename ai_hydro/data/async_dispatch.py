"""
Inline-vs-async dispatch policy for data fetches.

Some data sources are fast (USGS NWIS — a direct HTTP call) and some are slow
and queued (GloFAS via the Copernicus EWDS, which can sit in a server-side queue
for minutes regardless of request size). A blocking fetch on a slow source would
hang the MCP server, so we dispatch those to the jobs substrate and return a
handle instead.

The decision is *principled, not hard-coded per region*: we resolve which
product the router would pick for the geometry (cheaply, via
``detect_region`` + ``resolve_product_ids`` — no network), look up that
product's backend ``source``, and treat a known-slow source as async-preferred.
Today the only async-preferred source is ``cds_glofas``; the set is data-driven
so new slow backends opt in by name.

This keeps ``aihydro_data.fetch()`` a clean synchronous primitive — the
async policy is an application concern that lives in the tools layer.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# Backends whose retrieval is slow/queued enough that a blocking call is a poor
# UX → dispatch them as background jobs. Data-driven so new slow sources opt in.
ASYNC_PREFERRED_SOURCES = frozenset({"cds_glofas"})


def resolve_fetch_plan(
    variable: str,
    geometry: Any,
    product: Optional[str] = None,
) -> dict:
    """Decide how a fetch should run *without* performing it.

    Returns a plan dict:
        {
          "region":     str,            # detected CoverageTag
          "product_id": str | None,     # the product that would serve (best-effort)
          "source":     str | None,     # that product's backend id
          "async":      bool,           # True → dispatch to jobs substrate
          "reason":     str,            # human-readable rationale
        }

    Never raises for routing misses — falls back to a conservative inline plan.
    """
    plan = {"region": "", "product_id": product, "source": None,
            "async": False, "reason": ""}
    try:
        from aihydro_data.routing import detect_region, resolve_product_ids
        from aihydro_data.products import get_product

        region = detect_region(geometry)
        plan["region"] = region

        # A pinned product wins; otherwise take the router's top candidate
        # (region-specific chain, then the global chain).
        product_id = product
        if not product_id:
            candidates = (resolve_product_ids(variable, region)
                          or resolve_product_ids(variable, "global"))
            product_id = candidates[0] if candidates else None
        plan["product_id"] = product_id

        if product_id:
            try:
                spec = get_product(product_id)
                plan["source"] = spec.source
                if spec.source in ASYNC_PREFERRED_SOURCES:
                    plan["async"] = True
                    plan["reason"] = (
                        f"{product_id} is served by '{spec.source}', a slow/queued "
                        "backend → dispatched as a background job."
                    )
                else:
                    plan["reason"] = (
                        f"{product_id} ('{spec.source}') is fast → served inline."
                    )
            except Exception as exc:  # unknown product id → inline, be safe
                plan["reason"] = f"product '{product_id}' not resolvable ({exc}); inline."
        else:
            plan["reason"] = (
                f"no product registered for ('{variable}', '{region}'); inline path "
                "will surface a structured RegionUnsupported error."
            )
    except Exception as exc:  # routing import/parse failure → conservative inline
        plan["reason"] = f"routing unavailable ({exc}); inline."
    return plan


def dispatch_fetch_job(
    variable: str,
    geometry: Any,
    start: str,
    end: str,
    *,
    product: Optional[str] = None,
    aggregation: str = "basin_mean",
    session_id: Optional[str] = None,
    artifact_dir: Optional[str] = None,
) -> dict:
    """Spawn a background ``aihydro_data.fetch`` via the jobs substrate.

    Returns the job handle dict (job_id, status, artifact_dir, log_path, …) the
    agent polls with get_job_status / get_training_status.
    """
    import json
    import uuid
    from pathlib import Path
    from aihydro_core.jobs import start_job

    job_id = uuid.uuid4().hex[:12]

    # GeoJSON-serialise shapely/GeoDataFrame geometry so it survives the
    # process boundary as plain JSON in job_config.json.
    geom_payload = _geometry_to_jsonable(geometry)

    config = {
        "job_id": job_id,
        "variable": variable,
        "geometry": geom_payload,
        "start": start,
        "end": end,
        "product": product,
        "aggregation": aggregation,
        "session_id": session_id,
    }
    job = start_job(
        kind="data_fetch",
        runner_module="ai_hydro.data.fetch_runner",
        config=config,
        artifact_dir=artifact_dir,
        log_name="fetch.log",
    )
    return {
        "job_id": job["job_id"],
        "status": "pending",
        "variable": variable,
        "artifact_dir": job["artifact_dir"],
        "log_path": job["log_path"],
        "started_at": job.get("started_at"),
        "_note": (
            f"{variable} fetch dispatched as a background job (slow/queued source). "
            f"Poll with get_job_status('{job['job_id']}'); cancel with "
            f"cancel_job('{job['job_id']}'). Modelled GloFAS retrievals via EWDS "
            "typically take 1–10 min depending on queue load."
        ),
    }


def _geometry_to_jsonable(geometry: Any) -> Any:
    """Best-effort convert a geometry to a JSON-serialisable form."""
    # shapely geometry → GeoJSON mapping
    try:
        from shapely.geometry import mapping
        if hasattr(geometry, "geom_type"):
            return mapping(geometry)
    except Exception:
        pass
    # GeoDataFrame → first geometry's mapping
    try:
        if hasattr(geometry, "geometry") and hasattr(geometry, "to_json"):
            from shapely.geometry import mapping
            return mapping(geometry.geometry.iloc[0])
    except Exception:
        pass
    # tuples/lists/dicts/str already JSON-friendly
    return geometry
