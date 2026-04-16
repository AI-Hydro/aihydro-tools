# Changelog — aihydro-tools

All notable changes to the `aihydro-tools` Python package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

---

## [1.3.0] — 2026-04-15

### Added
- **Python env context in `start_session`** — response now includes:
  - `mcp_python`: path to the interpreter running the MCP server
  - `mcp_pip`: corresponding pip path
  - `available_packages`: `{name: version}` dict for all installed packages
  Agents use this to write correct Python scripts without guessing interpreter paths
  or assuming what is installed.
- **`list_available_tools` tool** — returns all registered MCP tools at runtime
  with names, descriptions, and parameter schemas. Includes community plugin tools.
  Call this instead of relying on documentation for an accurate picture of capabilities.
- **`get_library_reference` tool** — per-library reference cards for 8 core hydro
  libraries covering field-name gotchas, unit assumptions, CRS requirements, and
  copy-paste code patterns. Prevents hallucination in generated scripts.
  - `pynhd` — NLDI watershed polygons and NHD data
  - `pygeohydro` — USGS NWIS streamflow and NLCD land cover
  - `pygridmet` — GridMET daily climate (precipitation, temperature)
  - `py3dep` — 3DEP elevation (DEM) access
  - `hydrofunctions` — simple NWIS streamflow client
  - `pysheds` — DEM-based flow direction, accumulation, TWI
  - `rasterio` — raster I/O, masking, reprojection
  - `xarray` — N-dimensional labeled arrays for gridded data
- **`ai_hydro.knowledge` module** — hosts built-in library reference cards as
  structured JSON. Located at `ai_hydro/knowledge/library_refs/*.json`.
- **`aihydro.knowledge` entry point** — community plugins can contribute additional
  library reference cards by registering a `get_refs_dir` callable:
  ```toml
  [project.entry-points."aihydro.knowledge"]
  my_lib = "my_package.knowledge:get_refs_dir"
  ```
  where `get_refs_dir()` returns a `pathlib.Path` to a directory of `*.json` files.
- **Agent instructions** updated with explicit Python scripting decision tree:
  call `start_session` → call `get_library_reference` → use `mcp_python`.
  Also fixed `.clinerules/` → `.aihydrorules/` path reference.

### Changed
- Tool count: 26 → 28 (added `list_available_tools`, `get_library_reference`)

---

## [1.2.0] — 2026-04-10

### Added
- **ProjectSession** (`ai_hydro.session.project`): project-scoped research state
  that spans multiple gauges, topics, and literature — not tied to a single USGS gauge.
  Storage: `~/.aihydro/projects/<name>/project.json`.
- **ResearcherProfile** (`ai_hydro.session.persona`): persistent researcher persona
  built from agent interactions over time, analogous to memory features in Claude.ai
  and ChatGPT but domain-specific to computational hydrology.
  Storage: `~/.aihydro/researcher.json`.
- **10 new MCP tools** (26 total):
  - `start_project` — create or resume a named research project
  - `get_project_summary` — overview: gauges, journal, literature, metrics
  - `add_gauge_to_project` — associate a USGS gauge session with a project
  - `search_experiments` — full-text search across all gauge sessions in a project
  - `index_literature` — scan a folder of PDFs/txt/md → build searchable index
  - `search_literature` — query the index; returns excerpts for LLM synthesis
  - `add_journal_entry` — log a timestamped experiment note to the project journal
  - `get_researcher_profile` — return the persistent researcher persona
  - `update_researcher_profile` — update profile fields (agent or user driven)
  - `log_researcher_observation` — agent logs an observation about the researcher
- **Folder-based literature mode**: no vector database, no embeddings. Drop
  PDF/txt/md files into the project `literature/` folder, call `index_literature`,
  then `search_literature`. LLM synthesizes from plain-text excerpts.
- **Cross-session experiment search**: `search_experiments` queries stored results
  across all gauges in a project — "show me all basins where I ran LSTM",
  "which gauges have BFI > 0.6".
- **Researcher profile in research.md**: `HydroSession.write_research_context()`
  now appends the researcher profile block to `.clinerules/research.md` so the
  agent has persona context in every conversation automatically.
- **Updated agent instructions** in `app.py` documenting the full memory hierarchy:
  `ResearcherProfile → ProjectSession → HydroSession → research.md`.

### Changed
- `ai_hydro/session/__init__.py`: exports `ProjectSession` and `ResearcherProfile`
  alongside `HydroSession`.
- `ai_hydro/mcp/__init__.py`: imports `tools_project` module on startup.
- Agent system prompt rewritten to reflect v1.2 architecture and memory layers.

---

## [1.1.0] — 2026-03-31

### Added
- Published to PyPI as `aihydro-tools` (`pip install aihydro-tools`).
- Console script `aihydro-mcp` → starts MCP server on stdio.
- Plugin entry-point system: `[project.entry-points."aihydro.tools"]` for
  community tool registration via pip packages.
- `--version` / `--diagnose` CLI flags.
- `python -m ai_hydro.mcp` fallback entry point.

### Removed
- **RAG system** (`rag/`, `registry/`, `knowledge/` directories): removed due to
  heavy dependencies (chromadb, sentence-transformers) and immaturity.
  Archived at `github.com/AI-Hydro/aihydro-rag`.
- `query_hydro_concepts` MCP tool (was part of RAG system).
- `[rag]` extras in `pyproject.toml`.

### Changed
- All hardcoded "17 tools" references replaced with generic language throughout
  docs, tests, and setup scripts — tool count is now dynamic.
- Version bumped from 1.0.5 → 1.1.0.
- `setup_mcp.py`: updated expected tool set, docstrings, print output.
- `tests/test_mcp_integration.py`: tool count assertion now uses
  `len(EXPECTED_TOOLS)` instead of hardcoded 17.

---

## [1.0.5] — 2026-03-28

### Added
- `--diagnose` / `--check` flag to `aihydro-mcp` CLI for verifying server health.
- `python -m ai_hydro.mcp` module entry point as fallback for environments where
  the console script is not on PATH.

### Fixed
- Box Drive read-only filesystem workaround: `os.chdir(~/.aihydro/cache/)` at
  server startup prevents write errors on macOS with Box Drive sync.

---

## [1.0.4] — 2026-03-27

### Fixed
- MCP server registry and session import path corrections after Phase 2
  modularisation.
- `setup_mcp.py` dual-mode detection: prefers `which aihydro-mcp` (pip install),
  falls back to `python mcp_server.py` (monorepo dev).

---

## [1.0.3] — 2026-03-26

### Added
- `export_session` tool: exports session as JSON, BibTeX, or plain-text methods
  paragraph. Saved to disk (not returned inline) to preserve context window.
- `sync_research_context` tool: refreshes `.clinerules/research.md` and
  `.clinerules/tools.md` from live server state.

---

## [1.0.2] — 2026-03-25

### Added
- Phase 2 MCP server modularisation: monolithic `mcp_server.py` split into
  `tools_analysis.py`, `tools_session.py`, `tools_modelling.py`, `tools_docs.py`,
  `app.py`, `helpers.py`, `registry.py`.
- Plugin discovery via `importlib.metadata.entry_points(group="aihydro.tools")`.

---

## [1.0.1] — 2026-03-24

### Added
- Phase 1 package restructure: `ai_hydro/` now organized into `core/`, `data/`,
  `analysis/`, `modelling/`, `session/` subpackages.
- `HydroSession` dynamic slot system: plugins can add custom result slots without
  modifying core code.
- `HydroSession.write_research_context()`: auto-writes `.clinerules/research.md`
  on every session save.
- `cite_all()`: generates combined BibTeX for all computed session results.

---

## [1.0.0] — 2026-03-20

### Added
- Initial release: 16 MCP tools across analysis, session, and modelling.
- Data tools: `delineate_watershed`, `fetch_streamflow_data`, `fetch_forcing_data`,
  `extract_camels_attributes`, `create_cn_grid`.
- Analysis tools: `extract_hydrological_signatures`, `extract_geomorphic_parameters`,
  `compute_twi`.
- Session tools: `start_session`, `get_session_summary`, `clear_session`,
  `add_note`, `export_session`, `sync_research_context`.
- Modelling tools: `train_hydro_model` (HBV-light + LSTM), `get_model_results`.
- `HydroSession`: per-gauge persistent research state at
  `~/.aihydro/sessions/<gauge_id>.json`.

---

[Unreleased]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.5...v1.1.0
[1.0.5]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/AI-Hydro/aihydro-tools/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/AI-Hydro/aihydro-tools/releases/tag/v1.0.0
