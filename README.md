# aihydro-tools

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.svg" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.svg" />
    <img src="https://raw.githubusercontent.com/AI-Hydro/AI-Hydro/main/assets/docs/aihydro-hero-static.png" alt="AI-Hydro — Intelligent Hydrological Computing" width="100%" />
  </picture>
</p>

<p align="center">
  <em>Stop writing plumbing. Give AI agents real hydrological superpowers.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/aihydro-tools/"><img src="https://img.shields.io/pypi/v/aihydro-tools?color=3775a9" alt="PyPI" /></a>
  <a href="https://pypi.org/project/aihydro-tools/"><img src="https://img.shields.io/pypi/pyversions/aihydro-tools" alt="Python" /></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green" alt="License" /></a>
  <a href="https://github.com/AI-Hydro/aihydro-tools"><img src="https://img.shields.io/github/stars/AI-Hydro/aihydro-tools?style=flat" alt="Stars" /></a>
  <a href="https://ai-hydro.github.io/AI-Hydro/"><img src="https://img.shields.io/badge/docs-ai--hydro.github.io-00A3FF" alt="Docs" /></a>
</p>

---

## What is aihydro-tools?

`aihydro-tools` is the Python backbone of the [AI-Hydro](https://github.com/AI-Hydro/AI-Hydro) platform. It turns a conversation with an AI agent into real hydrological computation — watershed delineation, streamflow retrieval, signature extraction, terrain analysis, and model calibration — with full structured provenance recorded automatically at every step.

Tools are exposed via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), the open standard for agent-tool communication. Any AI model that supports MCP — Claude, GPT, Gemini — can call these tools directly, without writing a single line of processing code. And because `aihydro-tools` is built as a community platform, any researcher can register domain-specific tools (flood frequency, sediment transport, groundwater, remote sensing) via Python entry points, extending the ecosystem without touching the core.

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

### Analysis Tools

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
| **Session** | `start_session` | Initialize or resume a per-gauge research session |
| **Session** | `get_session_summary` | Overview of computed and pending analysis slots |
| **Session** | `clear_session` | Reset cached results to force re-computation |
| **Session** | `add_note` | Attach research notes to the session |
| **Session** | `export_session` | Export a citable methods paragraph for manuscripts |
| **Session** | `sync_research_context` | Refresh AI context with current session state |

### Project, Literature & Researcher Memory

| Category | Tool | Description |
|----------|------|-------------|
| **Project** | `start_project` | Create or resume a named research project spanning multiple gauges or topics |
| **Project** | `get_project_summary` | Overview of all gauges, journal entries, literature, and metrics in a project |
| **Project** | `add_gauge_to_project` | Associate a gauge session with the active project |
| **Project** | `search_experiments` | Full-text search across all gauge sessions in a project |
| **Literature** | `index_literature` | Scan a folder of PDF, txt, or md files and build a searchable text index |
| **Literature** | `search_literature` | Query the index and return excerpts for the agent to synthesise |
| **Literature** | `add_journal_entry` | Log a timestamped experiment note to the project journal |
| **Persona** | `get_researcher_profile` | Retrieve the persistent researcher profile (expertise, focus, preferences) |
| **Persona** | `update_researcher_profile` | Update profile fields — agent or researcher driven |
| **Persona** | `log_researcher_observation` | Record an observation about the researcher's evolving interests and methods |

Community plugins can add further tools via the entry-point system (see below).

---

## Data Sources

All data is fetched from authoritative federal sources:

- **[USGS NWIS](https://waterdata.usgs.gov/nwis)** — daily streamflow via dataretrieval (official USGS Python client)
- **[NHDPlus / NLDI](https://waterdata.usgs.gov/blog/nldi-intro/)** — watershed delineation via pynhd
- **[GridMET](https://www.climatologylab.org/gridmet.html)** — climate forcing via pygridmet
- **[3DEP](https://www.usgs.gov/3d-elevation-program)** — DEM and terrain analysis via py3dep
- **[NLCD](https://www.mrlc.gov/)** — land cover classification
- **[POLARIS](https://www.usgs.gov/publications/polaris-properties-30-meter-probabilistic-maps-soil-properties-over-contiguous-united)** — soil properties
- **[CAMELS-US](https://ral.ucar.edu/solutions/products/camels)** — catchment attributes via pygeohydro

---

## Memory & Provenance

AI-Hydro maintains a three-tier memory hierarchy so research context survives between conversations, sessions, and projects.

**HydroSession** — per-gauge state at `~/.aihydro/sessions/<gauge_id>.json`. Expensive computations (watershed delineation, multi-year streamflow downloads) are done once and reused across days or weeks. Every result carries structured provenance metadata — data source, parameters, timestamp — making reproducibility a natural byproduct rather than a documentation chore.

**ProjectSession** — project-scoped state at `~/.aihydro/projects/<name>/project.json`. Organises research across multiple gauges, topics, or datasets. Supports cross-session experiment search, a timestamped journal, and literature indexing.

**ResearcherProfile** — a persistent persona at `~/.aihydro/researcher.json`. Built up from agent-researcher interactions over time: expertise areas, preferred models, active projects, and accumulated observations. Injected into every conversation automatically so the agent knows who it is working with.

---

## Extending with Plugins

`aihydro-tools` is a platform, not a closed product. Any researcher can package domain knowledge as a plugin and make it immediately available to every AI agent that uses AI-Hydro — flood frequency analysis, sediment transport, groundwater modelling, remote sensing workflows, or anything else the core doesn't yet cover.

**Entry-point plugins** load into the same process with full access to HydroSession and cached data:

```toml
# In your package's pyproject.toml
[project.entry-points."aihydro.tools"]
my_tool = "my_package.tools:my_tool_function"
```

Install the package, restart the server, and the tool is automatically discovered — no changes to the core required.

**Standalone MCP servers** let you build fully independent toolkits with their own dependencies, registered alongside the core `ai-hydro` server.

See the [Plugin Guide](https://ai-hydro.github.io/AI-Hydro/plugins/overview/) for complete walkthroughs of both paths, the data contract, and session integration.

---

## Use as a Python Library

You don't need an AI agent to benefit from `aihydro-tools`. Every tool is a regular Python function — import and call directly in scripts, notebooks, or pipelines:

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

The most impactful contributions to AI-Hydro are new domain tools — knowledge that currently lives in papers and custom scripts, packaged so any AI agent can use it. High-priority areas include flood frequency analysis, sediment transport, groundwater modelling, remote sensing workflows (MODIS, Landsat, SAR), snow hydrology, and water quality.

You don't need to fork the core. Write a Python package, register an entry point, publish to PyPI. That's it.

- **[Contributing Guide](https://ai-hydro.github.io/AI-Hydro/contributing/)** — Development setup, code style, testing
- **[Plugin Guide](https://ai-hydro.github.io/AI-Hydro/plugins/overview/)** — Step-by-step walkthroughs for both contribution paths

---

## Links

- **Documentation**: [ai-hydro.github.io/AI-Hydro](https://ai-hydro.github.io/AI-Hydro/)
- **AI-Hydro Extension**: [github.com/AI-Hydro/AI-Hydro](https://github.com/AI-Hydro/AI-Hydro)
- **PyPI**: [pypi.org/project/aihydro-tools](https://pypi.org/project/aihydro-tools/)
- **YouTube**: [AI-Hydro Channel](https://www.youtube.com/channel/UC8RWDhJm61i2tlV9mt982cw)
- **Issues**: [github.com/AI-Hydro/aihydro-tools/issues](https://github.com/AI-Hydro/aihydro-tools/issues)

---

## License

[Apache 2.0](./LICENSE) &copy; 2026 Mohammad Galib, Venkatesh Merwade
