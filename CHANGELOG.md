# Changelog — aihydro-tools

All notable changes to the `aihydro-tools` Python package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [2.0.0] — 2026-06-15

This major release cuts the accumulated Phase 1–5 work (defensibility core, fleet
experiments, skeptic, literature grounding, HydroResearch-Bench, headless
resources, certification) plus the agent-efficiency and context-injection fixes
below. It supersedes the never-published `[2.0.0] — 2026-04-24` doc entry further
down this file (PyPI latest was 1.7.0).

### Added

- **Token-efficient long-job waiting — `wait_for_job(job_id)`** (`tools_modelling.py`, Tier 3): blocks server-side, polling the job's `status.json` every few seconds at zero token cost, and returns only when the job reaches a terminal state (or after a ~280 s budget, just under the MCP transport timeout — then the agent calls once more). Replaces the previous pattern of calling `get_job_status` in a loop, where every poll was a full LLM turn re-reading the whole context. Returns a terse status plus a `retrieve_with` hint; `train_hydro_model` and `data_fetch_background` now point at it. Added to the `execution` discovery domain.
- **Phase 5.2 — HydroResearch-Bench artifact**: `bench/gen_scorecard.py` — self-contained HTML scorecard generator over the 60 benchmark tasks (B-001–B-060). `--run` mode executes pytest with JUnit XML and overlays per-task pass/fail; catalog mode renders the task inventory. Tasks grouped by 16 categories with color-coded status badges and a summary grid (total/passed/failed/skipped/live/pass-rate). CI (`bench.yml`) generates the scorecard after every fixture run and uploads it as the `hrb-scorecard` artifact (90-day retention). `bench/BENCHMARK.md` — standalone benchmark documentation (category table, oracle-operator reference, task format, usage).
- **Phase 4.4 — Agent-native map control**: three new Tier-2 MCP tools in `tools_map.py` — `map_fly_to` (smooth camera navigation to a lon/lat/zoom via deck.gl `FlyToInterpolator`), `map_add_layer_from_run` (reads a session's `_run_log`, auto-detects GeoJSON in `key_outputs`, pushes it as a provenance-tagged layer with `run_id`/`session_id` metadata), and `map_set_time_range` (drives the map time slider / `BrushContext`). New push helpers in `map_commands.py`: `push_fly_to`, `push_add_layer_from_run`, `push_set_time_range`.
- **Phase 3.3 — Marketplace certification + citable credit**: `Gallery/scripts/certify_manifests.py` — schema-specific certification functions for Gallery (citation/trustLevel/badges/license), Skills (description/when_to_use/tools_used/tags), and Modules (citation.text/description/tags/estimatedMinutes). Certification injected into generated `api/*.json` in all three surface `build-api.yml` CI workflows (advisory, non-blocking). `CITATION.cff` (CFF 1.2.0) created in aihydro-tools, aihydro-core, and aihydro-data. Recognition worker gains `cite` event type (fires on formal citation export, distinct from `copy_citation`). 23 tests.
- **Phase 3.2 — Headless platform**: `ai_hydro/mcp/resources.py` — 4 new session-level MCP resources: `aihydro://session/list` (enumerate all sessions), `aihydro://session/{id}` (summary), `aihydro://session/{id}/claims` (full ledger), `aihydro://session/{id}/evidence_board` (kanban by status), `aihydro://session/{id}/experiments` (experiment slot). All resources are read-only, degrade gracefully on missing session, and require only an explicit `session_id` — no VS Code or chat binding needed. `tests/test_resources.py` — 26 tests including headless-mode verification.
- **Phase 2.5 — HydroResearch-Bench**: `bench/oracle.py` — 4 new operators: `ge`, `len_gte`, `len_eq`, empty-path support for bare-list results. `tests/test_bench.py` — `run_log` setup key in `_build_session` (seeded run-log entries for provenance tests). `bench/tasks.yaml` GROUP O — 11 end-to-end tasks B-050/B-060 covering: audit pass/fail, `[lit:tag]` and `[run:id#path]` resolution, value mismatch, `write_research_interpretation` gate, claim lifecycle, `list_claims`, `export_session(defensibility_report)`, `get_session_health`.
- **Phase 2.4 — Literature grounding**: `ai_hydro/knowledge/embeddings.py` — passage-level index (SHA-256[:16] `passage_hash`, 200-word sliding chunks, TF-IDF search, no ML model). Three new MCP tools: `index_passages` (Tier 2), `search_passages_tool` (Tier 2), `resolve_passage` (Tier 3). Auditor upgraded: `[lit:<hash>]` tags resolve against passage index; `lit_unresolvable` advisory (non-blocking); `AuditReport` gains `lit_span_count`, `lit_resolved_count`, `lit_advisories`. Bench B-048/B-049.
- **Phase 2.3 — Skeptic/referee agent**: `ai_hydro/skeptic/` package — four deterministic checks (stale citations, scope overreach, unvalidated high-risk assumptions, registry conflicts). `run_skeptic` MCP tool (Tier 1). Advisory pass integrated into `write_research_interpretation` — findings are advisory, never blocking.
- **Phase 2.2 — Global claim registry + living claims**: `ai_hydro/registry/store.py` — append-only JSONL registry at `~/.aihydro/registry/claims.jsonl` with `append`, `find_by_*`, `mark_stale`, `mark_retracted`, `build_registry_id`, `snapshot_evidence_versions`, and `check_evidence_staleness`. `promote_claim_to_registry` now writes real JSONL entries with evidence version hashes. New tools: `check_registry_staleness` (detects data drift), `list_registry_claims` (query). Evidence Board shows `stale` status column.
- **Phase 2.1 — Fleet-scale experiments**: `ai_hydro/experiments/` package, `tools_experiments.py` (`define_experiment`, `run_experiment`, `get_experiment_table`). VS Code `ExperimentTable` panel reads session JSON directly. Bench tasks B-043/B-044/B-045.

- **Pour-point delineation** (`ai_hydro/analysis/delineation/`): tiered `delineate_from_point` (`auto`, `fast`, `merit_basins`), cloud DEM + pysheds fast tier, NLDI COMID for CONUS.
- **`delineate_watershed_from_point`** MCP tool and **`hydro_map_cli delineate-point`** for the VS Code map Quick delineate button.
- **`resolve_comid_for_quick`** — walks `downstreamMain` when nearest COMID is a tiny tributary reach.
- **`scripts/profile_delineation.py`** and **`tests/test_delineation.py`**.

### Fixed

- **CRITICAL: total MCP outage for the VS Code extension — `_chat_id` / `_workspace` injection (2026-06-13).** Every `aihydro-tools` tool call from the extension failed with `ValidationError: Unexpected keyword argument _chat_id` (and `_workspace`). Root cause: the interceptor that strips these injected identity params was installed by monkeypatching `mcp._call_tool_mcp` *after* `FastMCP.__init__`, but FastMCP 3.x binds that method into its low-level request handler during construction — so the patch was dead code and the injected keys reached Pydantic validation on every call. The agent could not work around it (the keys are server-injected, not agent-supplied), and would drift to unrelated servers/pipelines. Replaced the monkeypatch with a proper FastMCP `_ContextInjectionMiddleware` (registered via `add_middleware`) whose `on_call_tool` hook strips both keys before validation and stores them in the `ACTIVE_CHAT_ID` / `ACTIVE_WORKSPACE` ContextVars. Ordered before `ArgRepairMiddleware` so the injected keys are never misread as typos. Regression guard: `tests/test_context_injection.py` drives the real `CallToolRequest` handler path with injected params (the original patch shipped with no test exercising that path).
- **Agent persona — systematic-failure stop rule.** Added guidance: when several *different* tools fail with the same structural error (identical unexpected-keyword / connection / auth message), the cause is the server/config, not the arguments — stop after two such failures and report the blocker plainly rather than silently switching to a different server or an unrelated pipeline, or reimplementing a failed tool with shell heredocs. (Persona trimmed elsewhere to stay under the 700-word budget.)
- **Agent persona — discover-before-commit + scope-matching (general routing robustness).** Two general principles (naming no specific server/tool): (1) do not infer a tool, library, or another server's abilities from its name — the native toolset is the primary instrument; verify capabilities with `aihydro_describe_capability`/`list_available_tools` and use another connected server only when a task explicitly needs its specialty; (2) prefer the most direct tool that satisfies the request — do not launch a long-running or multi-stage pipeline unless the request requires it. This is the platform-side half of the native-primacy routing fix (the extension-side half reorders/frames MCP servers in the system prompt). Motivated by a traced session where the agent routed a lightweight "delineate + signatures" task to a heavyweight third-party model-building server purely by name-association.
- **B-009 benchmark re-adjudication (2026-06-13).** The B-009 bench task (`baseflow_index` for the humid synthetic series) had expected bounds [0.40, 0.75] calibrated against the old inverted filter (which returned the quick-flow fraction). The corrected Lyne-Hollick filter yields BFI ≈ 0.29 for this series. Bounds re-adjudicated to [0.20, 0.40] — calibrated against the corrected algorithm and verified across 5 seeds. The `synthetic.py` docstring also corrected (CAMELS [0.45, 0.70] uses the Eckhardt filter, not Lyne-Hollick). All 59 fixture bench tasks now pass.
- **`baseflow_index` correctness (behaviour change).** The Lyne-Hollick filter in `extract_hydrological_signatures` returned the quick-flow component instead of the baseflow residual (`q − f`), and the multi-pass loop re-fed quick-flow as input. As a result `baseflow_index` reported the *quick-flow* fraction, not the baseflow fraction (e.g. USGS 01109000 returned 0.089 instead of the correct 0.607). Replaced with the standard Nathan-McMahon multi-pass form (baseflow = `q − f` per sweep, output feeds the next alternating-direction sweep). Added known-answer regression tests (`tests/test_hydrology_tools.py::TestBaseflowFilter`). **Anyone who computed `baseflow_index` with any prior published version received the quick-flow fraction and should recompute.**
- **CONUS map quick delineate** no longer returns tiny tributary basins (~6 km²) from nearest COMID alone.
- **CONUS `auto` without `expected_area_km2`** uses fast NLDI (~1–5 s) when basin area is in range; falls back to cloud DEM only when NLDI is unavailable or out of range.
- **`delineate_watershed(gauge_id)`** — NLDI `get_basins` fallback via COMID at gauge coordinates when site-id lookup fails.

### Added

- **Map orchestration MCP tools** (`tools_map.py`): `map_get_state`, `map_list_layers`, `map_update_layer`, `map_apply_symbology`, `map_remove_layer`, `map_set_basemap`, `map_fit_layer`, `map_set_working_geometry`, `map_save_roi` — agent-governed symbology and layer control via `~/.aihydro/map_commands/` + `map_layer_catalog.json`.
- **`map_commands.py`** — host command bridge (`update_layer`, `set_basemap`, `fit_layer`, …).
- **`map_layer_catalog.py`** — graduated symbology break computation; reads host layer catalog written by the VS Code extension.
- **`_resolve_active_roi_geojson`** in `helpers.py`; **`working_geometry_path`** on `HydroSession`.

---

## [1.7.0] — 2026-05-25

### Added — Course mode

Five MCP tools that make the agent course-aware and able to author courses. Targets the HTML Preview panel's course feature (folders with a `course.json` manifest grouping multiple HTML modules into a guided learning path).

- **`course_get_state`** (tier 3) — read the active-course pointer at `~/.aihydro/active_course.json` plus the per-course progress file; return current module, completion %, locked modules, and a `next_recommended` module id.
- **`course_get_curriculum`** (tier 3) — full manifest + prerequisite graph; defaults to the active course or accepts an explicit `course.json` path.
- **`course_set_progress`** (tier 2) — `complete | uncomplete | unlock_prereqs | set_current`. Records `agentGranted: true` + caller-supplied `reason` for transparency.
- **`course_navigate`** (tier 2) — write `~/.aihydro/course_nav_intent.json`; the HTML Preview panel watches it and switches to the requested module (prerequisite gate enforced webview-side).
- **`course_scaffold`** (tier 2) — write `course.json` + AI-Hydro-styled HTML skeletons for each module. Auto-slugifies ids from titles, validates the prerequisite graph for cycles via iterative 3-colour DFS.

### Companion artifact

- `course-authoring` skill added to the AI-Hydro Skills marketplace (separate repo: `github.com/AI-Hydro/Skills`) — pairs with `course_scaffold` per the established skill+tool pattern.

### System prompt

- New `COURSE MODE` section instructing the agent to call `course_get_state` at the start of any course-related conversation and to require explicit user agreement before mutating progress.

### Disk contracts (shared with the VS Code extension)

| File | Reader | Writer |
|---|---|---|
| `~/.aihydro/active_course.json` | tools | webview |
| `~/.aihydro/course_progress/<id>.json` | tools, webview | tools, webview |
| `~/.aihydro/course_nav_intent.json` | extension host | tools |

---

## [2.0.0] — 2026-04-24

### Removed (breaking changes)

- **`sync_research_context`** MCP tool removed. Deprecated in 1.6.0. Replace with `get_session_raw_state` (Phase 1) + `write_research_interpretation` (Phase 2). See [MIGRATION.md](MIGRATION.md).
- **`_train_hydro_model_sync_alias`** private function removed. Deprecated in 1.7.0. Use `train_hydro_model` (kickoff) + `get_training_status` (poll) directly.
- **`findings` field** on `get_session_summary` and `get_project_summary` responses. Deprecated in 1.6.0. Use `get_session_raw_state` to read computed data.
- All `DeprecationWarning` emissions on normal usage are eliminated. Clean import emits zero warnings.

### Added

- **P2 library cards** under `ai_hydro/knowledge/library_refs/`: `pandas`, `numpy`, `shapely`, `matplotlib`, `folium`. Each contains ≥8 gotchas, ≥4 common patterns, and `version_compatible` range. Discoverable via `get_library_reference()`.
- **`export_session` `capsule_path` parameter** — explicit output path for the capsule folder. Signature: `export_session(session_id, capsule_path=None, format="capsule")`.
- **`model/` directory in research capsule** — copies trained model artifacts (HBV params, simulated discharge CSV, metrics summary) alongside `data/`, `figures/`, `environment.yml`, `citations.bib`.
- **`MIGRATION.md`** — migration guide covering every removed 1.x signature with before/after examples.
- **knowledge-compat.yml CI extended** with P2 card smoke tests (pandas, numpy, shapely, matplotlib, folium).

### Changed

- `get_library_reference` façade retained (M3 façade decision: MCP resource support not yet uniform across Cline/Claude Code/Claude Desktop; see §7.6). Decision documented in function docstring.
- `_sync_reminder` in `helpers.py` updated to reference `write_research_interpretation` instead of removed `sync_research_context`.
- All internal documentation and `research.md` templates updated to reference the two-phase split tools.

---

## [1.7.0] — 2026-04-24

### Added

- **`get_training_status(job_id)`** — poll a training job started by `train_hydro_model`. Reads `status.json` from the job artifact directory. Returns `{job_id, status, progress, partial_results, error, log_path, updated_at}`.
- **`ai_hydro/modelling/runner.py`** — detached subprocess runner for training jobs. Reads `job_config.json`, runs training, writes checkpoints + `status.json` at completion or failure.
- **Six built-in v1 skills** (loaded by `list_skills()`, stored under `ai_hydro/skills/`):
  - `flood-frequency-analysis` — extreme-value FFA, USGS Bulletin 17C workflow, distribution selection.
  - `baseflow-separation` — Lyne-Hollick vs UKIH method selection, BFI interpretation by climate/geology.
  - `model-selection` — HBV-light vs LSTM vs regionalization decision guide (Nearing 2021, Beck 2020).
  - `calibration-diagnostics` — NSE/KGE decomposition, common pathologies, next-step recommendations (Gupta 2009, Clark 2021).
  - `signature-interpretation` — FDC shape, BFI, runoff ratio, Q95/Q05 → basin-storyline paragraph (Addor 2018).
  - `watershed-analysis-workflow` — end-to-end pipeline orchestrating all core analysis tools.
- **P1 library cards:** `torch.json` and `geopandas.json` under `ai_hydro/knowledge/library_refs/`. Both include `version_compatible` range and hydrology-specific gotchas.
- **`.github/workflows/knowledge-compat.yml`** — weekly + release-triggered CI workflow that smoke-tests each library card against its `version_compatible` lower-bound and validates required JSON fields (B2 Tier 3 enforcement).

### Changed

- **`train_hydro_model` rewritten as kickoff tool (R2 compliance):** Now returns `{job_id, status: "pending", artifact_dir, log_path, started_at}` immediately after spawning a detached subprocess. Heavy work (HBV restarts, LSTM training) runs in the background.
- **`get_model_results`** extended to accept an optional `job_id` argument — reads results directly from the artifact directory when a complete job is available, without requiring session cache.

### Deprecated

- **`train_hydro_model` synchronous call** (`_train_hydro_model_sync_alias`): calling with session_id only kicks off then polls (backward-compat wrapper). Emits `DeprecationWarning("Calling train_hydro_model synchronously will be removed in 2.0...")`; removal in 2.0.

---

## [1.6.0] — 2026-04-23

### Added
- **`get_session_raw_state(session_id)`** — Phase 1 of the two-phase interpretation workflow (G1 compliance). Returns raw computed slot data for the LLM to read before authoring scientific prose.
- **`write_research_interpretation(session_id, site_name, interpretation)`** — Phase 2 of the two-phase workflow. Writes LLM-authored interpretation into `research.md` and session. Replaces the write path of `sync_research_context`.
- **`run_python(script, workspace_dir, timeout_seconds, allow_network)`** — first-party workspace-scoped Python execution tool. Replaces the out-of-tree `mcp_python` reference. Workspace-scoped CWD, no network by default, stdin-only (no shell interpolation), 120s default timeout.
- **`list_relevant_clis()`** — enumerate AI-Hydro-aware CLI tools installed in the environment. Discovers tools via `aihydro.clis` entry-point group; falls back to best-effort detection of `swat` and `camels-extract` binaries.
- **`list_skills(domain, workspace_dir)`** — enumerate workflow skills across built-in / plugin / workspace tiers. Returns empty list in 1.6.0 (no built-in skills yet); plugin and workspace tiers polled.
- **`load_skill(name, workspace_dir)`** — load the full content of a named workflow skill (frontmatter + body).
- **`separate_baseflow(session_id, method, alpha, n_passes)`** — baseflow separation via Lyne-Hollick (1979) recursive filter or UKIH five-day interval method (Gustard et al. 1992). Writes full daily series + BFI to `session.baseflow`. Existing BFI scalar in `extract_hydrological_signatures` unchanged.
- **`ai_hydro/mcp/resources.py`** — native MCP resource layer for knowledge cards. URI scheme: `aihydro://knowledge/library/{name}` and `aihydro://knowledge/list`. Runtime drift detection compares installed library version against card `version_compatible` range; injects `stale: true` + `stale_reason` when outside range.
- **`ai_hydro/skills/`** package — three-tier skill registry (built-in / `aihydro.skills` entry-point / workspace `.aihydrorules/skills/`). YAML frontmatter parsed; workspace tier overrides plugin overrides built-in.
- `aihydro.skills` and `aihydro.clis` entry-point groups in `pyproject.toml`.

### Changed
- **Persona rewrite (T2.1):** Replaced 126-line nominal persona with ~55-line categorical persona. Zero named tools, libraries, or CONUS assumptions. Six capability layers enumerated abstractly. Discoverable via enumeration calls.
- **`sync_research_context` deprecated** in favour of `get_session_raw_state` + `write_research_interpretation` (G1: LLM authors interpretation, Python returns raw state). Old tool aliased with `DeprecationWarning`; removal planned for 2.0.
- **`get_library_reference`** — added no-arg branch (returns catalog of all available references, fixing R6 discoverability gap). Single-arg callers unaffected. Now delegates to `resources._load_card` for consistent drift-warning behaviour.
- All 8 built-in library reference cards gain `version_compatible` semver range field.

### Deprecated
- `sync_research_context`: use `get_session_raw_state` + `write_research_interpretation`. Will be removed in 2.0.

---

## [1.5.2] — 2026-04-23

### Removed
- Deleted dead `ai_hydro/workflows/` stubs (`__init__.py`, `compute_signatures.py`, `fetch_data.py`, `modeling.py`) that referenced the pre-refactor module layout.

### Changed
- Removed remaining legacy `Tier 2` / `Tier 3` wording in live package and test surfaces.
- Removed stale `ai_hydro.tools.hydrology` and `ai_hydro.tools.watershed` references in live package and test code.

---

## [1.5.1] — 2026-04-19

### Added — Raster map support
- **`plot_raster_tile(array, bounds_wgs84, output_dir, name, colormap)`** in `analysis/plots.py` — clean, decoration-free PNG export (transparent NoData, P2–P98 colour clipping) for use as a deck.gl `BitmapLayer` image.
- **`push_raster_layer()`** in `map_events.py` — raster variant of the map event writer; stores PNG path + WGS84 bounds so the TypeScript watcher can base64-encode the image at pick-up time.
- **`_bounds_to_wgs84(bounds, crs_str)`** in `tools_analysis.py` — reprojects raster bounds to EPSG:4326 via pyproj; silent fallback for geographic CRS or missing pyproj.
- **`compute_twi` raster push** — after TWI computation, a `viridis_r` tile is pushed as layer `twi_<session_id>`.
- **`create_cn_grid` raster push** — `YlOrRd` CN tile pushed as `cn_<session_id>`.
- **5 new raster tests** in `TestRasterMapEvents`.

### Added — Vector map support
- **`ai_hydro/mcp/map_events.py`** — Python → VS Code map bridge. `push_layer()` writes a JSON event file to `~/.aihydro/map_events/` which the extension's `MapEventWatcher` picks up and renders. `push_gauge_point()` helper for single-station markers. Four style presets: `watershed`, `flowlines`, `gauge`, `default`.
- **`show_on_map` MCP tool** — new tool (10th analysis tool, 29th total) to push any GeoJSON string to the AI-Hydro map panel directly from an agent session. Validates GeoJSON, applies style presets, returns `ok`/`layer_id`/`message`.
- **`delineate_watershed` auto-push** — after every successful watershed delineation the boundary polygon and gauge point are pushed automatically; map panel opens side-by-side.
- **7 new tests** in `tests/test_mcp_integration.py` covering `push_layer`, style presets, overrides, dict input, error handling, and `show_on_map` smoke + invalid-JSON rejection.

---

## [1.5.0] — 2026-04-18

### Added
- **`ai_hydro/citations.py`** — three-tier BibTeX citation registry. Tier 1: per-tool data source citations (USGS NWIS, NHDPlus, 3DEP, GridMET, NLCD, POLARIS, CAMELS-US, HBV). Tier 2: platform citations (AI-Hydro + aihydro-tools Zenodo DOIs) always included. Tier 3: plugin citations via `register_plugin_citation(key, bibtex, tool_names)`.
- **`HydroSession.add_citations(keys)`** — accumulates citation keys per tool call (no extra save).
- **`HydroSession.export_bibtex()`** — builds a ready-to-use `.bib` string from accumulated keys; `cite_all()` is a backward-compat alias.
- **`_citations` field** persisted in session JSON and restored on `load()`.
- **`sync_research_context`** Phase 2 now writes `citations.bib` to the workspace alongside `research.md`.
- **`export_session`** includes BibTeX in all export formats.

### Fixed
- **Shadow `ai_hydro/session.py` deleted** — was unreachable (Python prefers `session/` package) but wrote to `.clinerules/research.md` if ever imported directly; removed the confusion.
- **`session/persona.py`** path corrected: `.clinerules/research.md` → `.aihydrorules/research.md`.
- **`mcp/helpers.py`** `_session_store()` now accepts `tool_name=` kwarg; auto-adds citations in the same `session.save()` call as slot data (zero extra I/O).
- **`mcp/tools_analysis.py`** — all 10 `_session_store()` calls updated with `tool_name=` for citation tracking.
- **`mcp/tools_modelling.py`** — HBV citation (`seibert2012hbv`) added after model training.
- **`mcp/tools_docs.py`** docstring: `.clinerules/tools.md` → `.aihydrorules/tools.md`.
- **`workflows/camels_extraction.py`** — `extract_camels_attributes` renamed to `fetch_camels_attributes` (stale name removed from codebase).

---

## [1.4.0] — 2026-04-17

### Removed
- **`extract_camels_attributes` tool** — the incomplete per-site CAMELS-like attribute
  extractor has been removed from the public tool set. A dedicated `camels-attrs` MCP
  server will be released as a community plugin via the `aihydro.tools` entry point.
- **`[camels]` extra** (`camels-attrs>=0.1.0`) removed from `pyproject.toml`.
- **`_get_camels_attrs_version()`** removed from `tools_docs.py`.

### Changed
- **Tool count: 28 → 27.** `list_available_tools` now returns exactly 27 built-in tools.
- **CAMELS-US benchmark data is unaffected.** The 671-gauge CAMELS-US dataset continues
  to be fetched internally by `train_hydro_model` via `fetch_camels_streamflow()` for
  HBV and LSTM training — this is a data source, not a user-facing tool.

### Note for upgraders
If you were calling `extract_camels_attributes` directly, remove those calls.
Static catchment attributes for any USGS gauge are available from `delineate_watershed`
(morphometric) and `extract_geomorphic_parameters` (DEM-derived). A full CAMELS-attribute
extractor will be available as a separate plugin package.

---

## [1.3.0] — 2026-04-16

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
- **`session_id` / `gauge_id` separation** — `session_id` is now a free-form research
  identity (any string); USGS gauge IDs are a separate `gauge_id` parameter on the two
  USGS-specific data tools only (`delineate_watershed`, `fetch_streamflow_data`). All
  analysis tools (`extract_hydrological_signatures`,
  `compute_twi`, etc.) take `session_id` only and operate on whatever data is cached.
  - Backward-compatible: sessions where `session_id` looks like an 8-digit USGS gauge
    still resolve correctly via the `_resolve_usgs_gauge()` helper.
  - Stored sessions now track `site_id` (e.g. `"01031500"`) and `site_type` (`"usgs_gauge"`)
    separately from the session identifier.
- **`add_gauge_to_project` renamed to `add_session_to_project`** — parameter renamed from
  `gauge_id` to `session_id`; old name remains as a backward-compat alias.
- **`ProjectSession.session_ids`** — replaces `gauge_ids`; backward-compat `gauge_ids`
  property kept for existing project JSON files.
- **`fetch_streamflow_data` now uses `dataretrieval`** instead of `hydrofunctions`.
  Fixes a `pd.Timedelta(Day)` incompatibility with pandas ≥ 2.2 on Python 3.13.
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
