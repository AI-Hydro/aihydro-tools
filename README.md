# aihydro-tools

**17 hydrological research tools as an MCP server for AI agents.**

`aihydro-tools` is the Python backend for [AI-Hydro](https://github.com/AI-Hydro/AI-Hydro), a VS Code extension that gives AI assistants direct access to hydrological analysis tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Install

```bash
pip install aihydro-tools
```

Or install with optional geo/analysis dependencies:

```bash
pip install aihydro-tools[data]       # streamflow, forcing, land cover
pip install aihydro-tools[analysis]   # watershed, signatures, TWI, CN
pip install aihydro-tools[modelling]  # differentiable HBV-light, LSTM
pip install aihydro-tools[all]        # everything above
```

### Verify installation

```bash
aihydro-mcp --help
```

If `aihydro-mcp` is not found, pip installed it outside your PATH. Check these locations:

| OS | Typical location |
|----|-----------------|
| **Windows (user)** | `%APPDATA%\Python\Python3XX\Scripts\aihydro-mcp.exe` |
| **Windows (system)** | `C:\Python3XX\Scripts\aihydro-mcp.exe` |
| **macOS/Linux (user)** | `~/.local/bin/aihydro-mcp` |
| **macOS/Linux (system)** | `/usr/local/bin/aihydro-mcp` |
| **Conda** | `~/miniconda3/bin/aihydro-mcp` or `~/anaconda3/bin/aihydro-mcp` |

> **Tip:** On Windows, replace `3XX` with your Python version (e.g., `310` for Python 3.10).

## Run the MCP Server

```bash
aihydro-mcp
```

This starts the MCP server on stdio, ready for any MCP client (AI-Hydro extension, Claude Code, Cursor, etc.).

## Configure with AI-Hydro Extension

The AI-Hydro VS Code extension **auto-detects** `aihydro-mcp` on startup — both PATH and common pip install locations. If auto-detection succeeds, no manual setup is needed.

### Manual configuration

If auto-detection fails, add the server manually to `aihydro_mcp_settings.json`:

**Windows:**
```json
{
  "mcpServers": {
    "ai-hydro": {
      "command": "C:\\Users\\<USERNAME>\\AppData\\Roaming\\Python\\Python310\\Scripts\\aihydro-mcp.exe",
      "args": []
    }
  }
}
```

**macOS/Linux:**
```json
{
  "mcpServers": {
    "ai-hydro": {
      "command": "/Users/<USERNAME>/.local/bin/aihydro-mcp",
      "args": []
    }
  }
}
```

Settings file locations:
- **Windows:** `%APPDATA%\Code\User\globalStorage\aihydro.ai-hydro\settings\aihydro_mcp_settings.json`
- **macOS:** `~/Library/Application Support/Code/User/globalStorage/aihydro.ai-hydro/settings/aihydro_mcp_settings.json`
- **Linux:** `~/.config/Code/User/globalStorage/aihydro.ai-hydro/settings/aihydro_mcp_settings.json`

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

## Troubleshooting

### "aihydro-mcp not found"

pip installed the executable outside your PATH. Either:
1. **Add the Scripts directory to PATH** (see the table above for locations)
2. **Use the full path** directly in your MCP configuration
3. **Re-install with `--user` flag removed**: `pip install aihydro-tools` (may need admin/sudo)

### "Connection closed" error

- Use the `aihydro-mcp` executable, not `python -m ai_hydro.mcp.app`
- Verify the path in your MCP settings matches the actual installed location
- Check: `pip show aihydro-tools` to confirm it's installed

### Re-install from scratch

```bash
pip uninstall -y aihydro-tools
pip install aihydro-tools
```

## License

[Apache 2.0](./LICENSE)

## Links

- **AI-Hydro Extension**: [github.com/AI-Hydro/AI-Hydro](https://github.com/AI-Hydro/AI-Hydro)
- **PyPI**: [pypi.org/project/aihydro-tools](https://pypi.org/project/aihydro-tools/)
- **Issues**: [github.com/AI-Hydro/aihydro-tools/issues](https://github.com/AI-Hydro/aihydro-tools/issues)
- **Author**: Mohammad Galib (mgalib@purdue.edu)
