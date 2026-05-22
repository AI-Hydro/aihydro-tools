---
name: hydro-mcp-tool-builder
description: Guide the agent to create new AI-Hydro MCP tools — Python @mcp.tool() functions that extend the platform's hydrological analysis capabilities. Use when the user wants to add a new tool, wrap a hydrology library, expose a new data source, or build a custom analysis function for the AI-Hydro MCP server.
when_to_use: When the user asks to "add a tool", "create an MCP tool", "wrap this library as a tool", "expose X to the agent", or wants to extend AI-Hydro's analysis capabilities with a new Python function
domain: general
tools_used: []
tags:
  - mcp
  - tool-development
  - python
  - extensibility
---

# AI-Hydro MCP Tool Builder

Guide for creating new `@mcp.tool()` functions that extend AI-Hydro's hydrological analysis capabilities. AI-Hydro's MCP server is a Python FastMCP application — tools are decorated functions that the agent calls during conversations.

## When This Workflow Applies

- User wants to add a new analysis capability (e.g., "add an IDF curve tool")
- User wants to wrap an existing hydrology library (e.g., pysheds, hydrofunctions, neuralhydrology)
- User wants to expose a new data source (e.g., GRDC, BOM, Environment Canada)
- User wants to build a custom computation the agent can invoke

## Architecture Overview

```
python/ai_hydro/mcp/
├── app.py              — FastMCP singleton + agent instructions
├── helpers.py          — 9 shared helpers (validation, caching, session)
├── tools_analysis.py   — 8 analysis tools
├── tools_session.py    — 6 session management tools
├── tools_modelling.py  — 2 AI modelling tools
├── tools_docs.py       — documentation generation
└── __init__.py         — imports tool modules → triggers registration
```

New tools go in an existing `tools_*.py` module (if they fit) or a new `tools_<domain>.py` file.

## Step 1 — Determine the Tool Shape

### What does it connect to?

| If it needs… | Approach |
|---|---|
| USGS/NWIS data | Use `dataretrieval` (already a dependency) |
| Spatial/raster processing | Use `pysheds`, `rasterio`, `xarray` (in `[analysis]` extra) |
| Statistical computation | Use `scipy.stats`, `numpy`, `pandas` (core deps) |
| Machine learning | Use `torch`, `neuralhydrology` (in `[modelling]` extra) |
| External API | Use `httpx` or `aiohttp` for async requests |
| Pure computation | No external deps needed |

### How many actions?

- **Single focused action** → one `@mcp.tool()` function (preferred)
- **Related suite** → group 2–5 tools in one `tools_<domain>.py` module
- **Large API surface** → consider a search + execute pattern

## Step 2 — Write the Tool Function

### Template

```python
from ai_hydro.mcp.app import mcp
from ai_hydro.mcp.helpers import validate_gauge_id, get_or_create_session, cache_result
from ai_hydro.core.types import HydroResult, ToolError


@mcp.tool()
async def compute_drought_index(
    gauge_id: str,
    index_type: str = "SPI",
    window_months: int = 3,
    reference_period: str | None = None,
) -> dict:
    """Compute drought indices (SPI, SPEI, PDSI) from streamflow and climate data.

    Args:
        gauge_id: USGS gauge ID (8 digits)
        index_type: Drought index to compute — "SPI", "SPEI", or "PDSI"
        window_months: Aggregation window in months (1, 3, 6, 12, 24)
        reference_period: Baseline period as "YYYY-YYYY" (default: full record)

    Returns:
        Dictionary with monthly drought index time series, severity classification,
        and summary statistics.
    """
    # 1. Validate inputs
    validate_gauge_id(gauge_id)
    if index_type not in ("SPI", "SPEI", "PDSI"):
        raise ToolError(f"Unknown index_type: {index_type}. Use SPI, SPEI, or PDSI.")

    # 2. Fetch data (reuse existing tools where possible)
    session = get_or_create_session(gauge_id)
    # ... fetch streamflow, precipitation, temperature as needed

    # 3. Compute
    # ... core computation logic

    # 4. Store in session
    session.set_slot("drought", result_dict)

    # 5. Return structured result
    return {
        "gauge_id": gauge_id,
        "index_type": index_type,
        "window_months": window_months,
        "summary": { ... },
        "time_series": [ ... ],  # keep concise — agent context is limited
    }
```

### Key Patterns

**Use `HydroResult` for structured returns:**
```python
from ai_hydro.core.types import HydroResult, HydroMeta

result = HydroResult(
    data={"bfi": 0.65, "interpretation": "mixed geology"},
    meta=HydroMeta(gauge_id=gauge_id, source="computed", units="dimensionless"),
)
return result.to_dict()
```

**Use `ToolError` for user-facing errors:**
```python
from ai_hydro.core.types import ToolError

if len(daily_q) < 365 * 5:
    raise ToolError(
        f"Record too short ({len(daily_q)} days). "
        "Need at least 5 years for reliable drought index computation."
    )
```

**Use session slots for state:**
```python
session = get_or_create_session(gauge_id)
session.set_slot("drought", {"spi_3": series, "computed_at": timestamp})
# Later tools can access: session.get_slot("drought")
```

**Use caching for expensive operations:**
```python
@cache_result(ttl=3600)
async def _fetch_gridded_precip(lat, lon, start, end):
    # expensive API call
    ...
```

## Step 3 — Register the Tool

### In an existing module:

Just add the function to the appropriate `tools_*.py` file. The `@mcp.tool()` decorator handles registration automatically since `__init__.py` imports all tool modules.

### In a new module:

1. Create `python/ai_hydro/mcp/tools_<domain>.py`
2. Add the import in `python/ai_hydro/mcp/__init__.py`:

```python
from . import tools_drought  # triggers @mcp.tool() registration
```

## Step 4 — Write the Docstring Right

The docstring is what the agent sees when deciding whether and how to call the tool. It matters more than the code.

**Good docstring:**
```python
"""Compute drought indices (SPI, SPEI, PDSI) from streamflow and climate data.

Use this tool when the user asks about drought severity, dry spells, water
scarcity, or low-flow conditions. Returns monthly time series with severity
classification (exceptional/extreme/severe/moderate/near-normal/wet).

Args:
    gauge_id: USGS gauge ID (8 digits). Get from the user or session.
    index_type: "SPI" (precipitation only), "SPEI" (precip + temp),
                or "PDSI" (full water balance). Default SPI.
    window_months: Aggregation window. 3 for seasonal, 12 for annual trends.
"""
```

**Bad docstring:**
```python
"""Calculate drought index."""  # Too vague — agent won't know when to use it
```

### Docstring Checklist

- [ ] First line: what it does (one sentence)
- [ ] Second paragraph: when to use it (trigger phrases)
- [ ] Args: every parameter with type, meaning, and default rationale
- [ ] Returns: what the agent gets back and how to interpret it
- [ ] Mention related tools if the user might need to call them first

## Step 5 — Test

### Quick smoke test:

```bash
cd python
/opt/miniconda3/bin/python -c "
from ai_hydro.mcp.app import mcp
print([t.name for t in mcp.tools])
# Should include your new tool
"
```

### Integration test (add to `python/tests/test_mcp_integration.py`):

```python
@pytest.mark.skipif(not HAS_DEPS, reason="optional deps missing")
def test_compute_drought_index_registered():
    tool_names = [t.name for t in mcp.tools]
    assert "compute_drought_index" in tool_names

@pytest.mark.live
async def test_compute_drought_index_runs():
    result = await compute_drought_index("01031500", "SPI", 3)
    assert "summary" in result
    assert result["gauge_id"] == "01031500"
```

### Full server test:

```bash
/opt/miniconda3/bin/python -m pytest tests/test_mcp_integration.py -v
```

## Step 6 — Add to pyproject.toml (if new dependencies)

```toml
[project.optional-dependencies]
drought = ["standard-precip>=0.1"]  # new extra
all = ["aihydro-tools[geo,data,analysis,modelling,camels,mcp,viz,drought]"]
```

## Common Hydrology Libraries to Wrap

| Library | What it does | Tool idea |
|---|---|---|
| `hydrofunctions` | NWIS data access | Already covered by `fetch_streamflow_data` |
| `pysheds` | Flow direction, accumulation | Extend `delineate_watershed` |
| `standard-precip` | SPI computation | `compute_drought_index` |
| `baseflow` | Digital filter separation | Extend `extract_hydrological_signatures` |
| `neuralhydrology` | LSTM/Transformer rainfall-runoff | Already covered by `train_hydro_model` |
| `pyet` | PET estimation (20+ methods) | `estimate_pet` |
| `xclim` | Climate indices, IDF curves | `compute_climate_indices` |
| `flopy` | MODFLOW groundwater modelling | `setup_groundwater_model` |

## Anti-Patterns

- **Don't return raw DataFrames.** Convert to dicts/lists — MCP tools return JSON.
- **Don't swallow exceptions.** Use `ToolError` with helpful messages.
- **Don't require file paths.** Tools should work with gauge IDs and coordinates, not local files.
- **Don't duplicate existing tools.** Check `get_session_summary()` to see what's already computed.
- **Don't return huge arrays.** Summarize time series (min/max/mean/percentiles) and let the user request subsets.
