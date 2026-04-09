# aihydro-tools

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.svg" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.svg" />
    <img src="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.png" alt="AI-Hydro — Intelligent Hydrological Computing" width="100%" />
  </picture>
</p>

<p align="center">
  <em>Hydrological research tools as an MCP server for AI agents.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/aihydro-tools/"><img src="https://img.shields.io/pypi/v/aihydro-tools?color=3775a9" alt="PyPI" /></a>
  <a href="https://pypi.org/project/aihydro-tools/"><img src="https://img.shields.io/pypi/pyversions/aihydro-tools" alt="Python" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License" /></a>
  <a href="https://github.com/AI-Hydro/aihydro-tools"><img src="https://img.shields.io/github/stars/AI-Hydro/aihydro-tools?style=flat" alt="Stars" /></a>
</p>

---

## What is aihydro-tools?

`aihydro-tools` is the Python backend for the [AI-Hydro](https://github.com/AI-Hydro/AI-Hydro) platform — a collection of hydrological and geospatial analysis tools exposed via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It powers the built-in tools that AI agents use for watershed delineation, streamflow analysis, terrain analysis, differentiable modelling, and more.

---

## Quick Start

```bash
# Install
pip install aihydro-tools[all]

# Verify
aihydro-mcp --diagnose

# Run the server
aihydro-mcp
```

The [AI-Hydro VS Code extension](https://github.com/AI-Hydro/AI-Hydro) auto-detects `aihydro-mcp` on startup — no manual configuration needed.

---

## Built-in Tools

| Category | Tool | Description |
|----------|------|-------------|
| **Watershed** | `delineate_watershed` | NHDPlus watershed delineation from USGS NLDI given a gauge ID |
| **Streamflow** | `fetch_streamflow_data` | Daily discharge time series from USGS NWIS |
| **Signatures** | `extract_hydrological_signatures` | 15+ flow statistics: BFI, runoff ratio, FDC percentiles, recession constants |
| **Geomorphic** | `extract_geomorphic_parameters` | 28 basin morphometry metrics (area, slope, elevation, shape factors) |
| **Terrain** | `compute_twi` | Topographic Wetness Index from 3DEP 10m DEM |
| **Curve Number** | `create_cn_grid` | NRCS Curve Number grid from NLCD land cover + Polaris soils |
| **Forcing** | `fetch_forcing_data` | Basin-averaged GridMET climate data (prcp, tmax, tmin, PET, srad, wind) |
| **CAMELS** | `extract_camels_attributes` | Full CAMELS-US attribute set (671 gauges) via pygeohydro |
| **Modelling** | `train_hydro_model` | Differentiable HBV-light (PyTorch) or NeuralHydrology LSTM |
| **Modelling** | `get_model_results` | Retrieve cached model performance (NSE, KGE, RMSE) |
| **Session** | `start_session` | Initialize or resume a research session for a gauge |
| **Session** | `get_session_summary` | Overview of computed and pending analysis slots |
| **Session** | `clear_session` | Reset cached results to force re-computation |
| **Session** | `add_note` | Attach research notes to the session |
| **Session** | `export_session` | Export a methods paragraph for manuscripts |
| **Session** | `sync_research_context` | Synchronize session state with the AI context |

Additional tools can be added by the community via the plugin system (see below).

---

## Data Sources

All data is fetched from authoritative federal sources:

- **[USGS NWIS](https://waterdata.usgs.gov/nwis)** — daily streamflow via hydrofunctions
- **[NHDPlus / NLDI](https://waterdata.usgs.gov/blog/nldi-intro/)** — watershed delineation via pynhd
- **[GridMET](https://www.climatologylab.org/gridmet.html)** — climate forcing via pygridmet
- **[3DEP](https://www.usgs.gov/3d-elevation-program)** — DEM and terrain analysis via py3dep
- **[NLCD](https://www.mrlc.gov/)** — land cover classification
- **[POLARIS](https://www.usgs.gov/publications/polaris-properties-30-meter-probabilistic-maps-soil-properties-over-contiguous-united)** — soil properties
- **[CAMELS-US](https://ral.ucar.edu/solutions/products/camels)** — catchment attributes via pygeohydro

---

## Session Persistence

Every tool result is cached in a **HydroSession** (JSON file per gauge at `~/.aihydro/sessions/`). Expensive computations — watershed delineation (~10s), multi-year streamflow downloads (~5s) — are done once and reused across conversations, days, or weeks. The session tracks provenance for reproducibility.

---

## Extending with Plugins

Community packages can register additional tools via Python entry points:

```toml
# In your package's pyproject.toml
[project.entry-points."aihydro.tools"]
my_tool = "my_package.tools:my_tool_function"
```

Install the package, restart the server, and the new tool is automatically available. Plugins get full access to HydroSession and cached data from other tools.

See the [Plugin Guide](https://github.com/AI-Hydro/AI-Hydro/blob/main/PLUGIN_GUIDE.md) for complete walkthroughs of both plugin paths (entry points and standalone MCP servers).

---

## Use as a Python Library

You can use `aihydro-tools` directly in Python scripts without the MCP server:

```python
from ai_hydro.analysis.watershed import delineate_watershed
from ai_hydro.data.streamflow import fetch_streamflow_data
from ai_hydro.analysis.signatures import extract_hydrological_signatures

# Delineate a watershed
ws = delineate_watershed("01031500")
print(f"Watershed area: {ws.data['area_km2']} km2")

# Fetch streamflow
sf = fetch_streamflow_data("01031500", start_date="2015-01-01", end_date="2024-12-31")
print(f"Records: {len(sf.data['dates'])} days")

# Extract signatures
sigs = extract_hydrological_signatures("01031500")
print(f"Baseflow index: {sigs.data['baseflow_index']}")
```

All functions return `HydroResult` objects with `.data` (dict) and `.meta` (provenance metadata).

---

## Installation Details

### Extras

Install only what you need:

| Extra | What it adds | Install command |
|-------|-------------|-----------------|
| `[data]` | Streamflow, forcing, land cover, soil, CAMELS retrieval | `pip install aihydro-tools[data]` |
| `[analysis]` | Watershed, signatures, TWI, geomorphic, curve number | `pip install aihydro-tools[analysis]` |
| `[modelling]` | PyTorch differentiable HBV-light, NeuralHydrology LSTM | `pip install aihydro-tools[modelling]` |
| `[viz]` | Matplotlib, Plotly, Folium visualisations | `pip install aihydro-tools[viz]` |
| `[all]` | Everything above | `pip install aihydro-tools[all]` |

### PATH Troubleshooting

If `aihydro-mcp` is not found after install, pip placed it outside your PATH:

| OS | Typical location |
|----|-----------------|
| **Windows (user)** | `%APPDATA%\Python\Python3XX\Scripts\aihydro-mcp.exe` |
| **Windows (system)** | `C:\Python3XX\Scripts\aihydro-mcp.exe` |
| **macOS/Linux (user)** | `~/.local/bin/aihydro-mcp` |
| **macOS/Linux (system)** | `/usr/local/bin/aihydro-mcp` |
| **Conda** | `~/miniconda3/bin/aihydro-mcp` or `~/anaconda3/bin/aihydro-mcp` |

**Universal fallback**: `python -m ai_hydro.mcp` works regardless of PATH. The AI-Hydro extension auto-detects both the console script and the module fallback.

---

## Contributing

We welcome contributions from the hydrology and geospatial sciences community. See:

- **[CONTRIBUTING.md](./CONTRIBUTING.md)** — Development setup, code style, testing
- **[Plugin Guide](https://github.com/AI-Hydro/AI-Hydro/blob/main/PLUGIN_GUIDE.md)** — Two paths for contributing tools (entry points and standalone servers)

---

## Links

- **AI-Hydro Extension**: [github.com/AI-Hydro/AI-Hydro](https://github.com/AI-Hydro/AI-Hydro)
- **PyPI**: [pypi.org/project/aihydro-tools](https://pypi.org/project/aihydro-tools/)
- **Issues**: [github.com/AI-Hydro/aihydro-tools/issues](https://github.com/AI-Hydro/aihydro-tools/issues)

---

## License

[Apache 2.0](./LICENSE) &copy; 2026 Mohammad Galib, Venkatesh Merwade
