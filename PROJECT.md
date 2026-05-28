# aihydro-tools

## What it is
Python/MCP backbone for AI-Hydro: watershed delineation, hydrologic data access, signatures, terrain analysis, modelling helpers, provenance, and map/GEE integration for agent-driven hydrology research.

## Status
Global watershed delineation v1 stable; last updated 2026-05-26.

## Where to read next, by task
- Want to continue work -> `PROGRESS.md` and `CHANGELOG.md`
- Want watershed delineation details -> `local-docs/watershed-delineation.md`
- Want GEE backend details -> `local-docs/gee-backend-implementation-plan.md`
- Want MCP conventions -> `DESIGN_PRINCIPLES.md`
- Want package/dependency wiring -> `pyproject.toml`

## Current state
- Active phase: Global Watershed Delineation v1 maintenance.
- Last artifact: map quick delineation now stages non-CONUS MERIT flowdir before routing; Pfaf 45 test outlet runs through `local_merit_pyflwdir` in about 23 seconds with official MERIT UPA validation.
- Open question: broader global live benchmark expansion is future validation work, not a v1 architecture blocker.
- Next step: maintain v1 routing modes; do not add new delineation architecture unless users expose a concrete defect.

## Non-goals
- Do not turn the VS Code extension into the canonical computation backend.
- Do not require full local global MERIT-Basins hosting for the default global workflow.
- Do not replace NLDI for USGS/CONUS workflows where it is more authoritative.

## How to run / test
- Install core: `pip install -e .`
- Install delineation extras: `pip install -e ".[delineation]"`
- Run focused tests: `python -m pytest -q tests/test_delineation.py`
