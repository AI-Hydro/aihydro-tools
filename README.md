# aihydro-tools

**17 hydrological research tools as an MCP server for AI agents.**

`aihydro-tools` is the Python backend for [AI-Hydro](https://github.com/AI-Hydro/AI-Hydro), a VS Code extension that gives AI assistants direct access to hydrological analysis tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Install

```bash
pip install aihydro-tools[all]
```

Or install only what you need:

```bash
pip install aihydro-tools[data]       # streamflow, forcing, land cover
pip install aihydro-tools[analysis]   # watershed, signatures, TWI, CN
pip install aihydro-tools[modelling]  # differentiable HBV-light, LSTM
pip install aihydro-tools[mcp]        # MCP server only (minimal)
```

## Run the MCP Server

```bash
aihydro-mcp
```

This starts the MCP server on stdio, ready for any MCP client (AI-Hydro extension, Claude Code, Cursor, etc.).

## Register with an IDE

```bash
python setup_mcp.py --ide vscode       # AI-Hydro VS Code extension
python setup_mcp.py --ide claude-code  # Claude Code CLI
python setup_mcp.py --check            # verify 17 tools registered
```

## Tools (17)

| Category | Tools |
|----------|-------|
| **Watershed** | `delineate_watershed` |
| **Streamflow** | `fetch_streamflow_data` |
| **Signatures** | `extract_hydrological_signatures` |
| **Geomorphic** | `extract_geomorphic_parameters` |
| **Terrain** | `compute_twi` |
| **Curve Number** | `create_cn_grid` |
| **Forcing** | `fetch_forcing_data` |
| **CAMELS** | `extract_camels_attributes` |
| **Knowledge** | `query_hydro_concepts` |
| **Modelling** | `train_hydro_model`, `get_model_results` |
| **Session** | `start_session`, `get_session_summary`, `clear_session`, `add_note`, `export_session`, `sync_research_context` |

## Example

```
You: "Delineate the watershed for USGS gauge 01031500 and fetch 10 years of forcing data."

AI-Hydro: [calls delineate_watershed → fetch_forcing_data]
          Watershed: 769 km², Piscataquis River ME
          Forcing: 3,652 days of GridMET data (prcp, tmax, tmin, PET, srad, wind)
```

## Data Sources

- **USGS NWIS** — daily streamflow via hydrofunctions
- **NHDPlus / NLDI** — watershed delineation via pynhd
- **GridMET** — climate forcing via pygridmet
- **3DEP** — DEM and terrain analysis via py3dep
- **NLCD / POLARIS** — land cover and soils
- **CAMELS-US** — catchment attributes via pygeohydro

## Session Persistence

Every tool result is cached in a **HydroSession** (JSON file per gauge at `~/.aihydro/sessions/`). Expensive computations are done once and reused across conversations.

## License

[Apache 2.0](./LICENSE)

## Links

- **AI-Hydro Extension**: [github.com/AI-Hydro/AI-Hydro](https://github.com/AI-Hydro/AI-Hydro)
- **Issues**: [github.com/AI-Hydro/aihydro-tools/issues](https://github.com/AI-Hydro/aihydro-tools/issues)
- **Author**: Mohammad Galib (mgalib@purdue.edu)
