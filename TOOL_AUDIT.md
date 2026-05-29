# AI-Hydro MCP Tool Audit (WS-3)

Generated from live `mcp.list_tools()` introspection. **113 tools total.**

Verdict legend: **Keep-hot** (full schema inline) · **Keep-summary** (name+1-liner, schema fetched via describe_tool) · **Deprecate** (shim now, remove next minor) · **Delete** (confirmed dead/duplicate).

## Candidates for removal — USER SIGN-OFF DECISION (2026-05-28)

User reviewed all 3 candidates. **Approved: the 2 fetch shims only.** `get_citation_by_doi` is KEPT live by user request. No hard deletes — approved items remain working shims, scheduled for removal next minor.

| Tool | Tier | Domain | Decision | Rationale |
|---|---|---|---|---|
| `fetch_streamflow_data` | 3 | streamflow | ✅ Deprecate (approved) | Superseded by data_fetch. Docstring now `[DEPRECATED — prefer data_fetch]`; emits `_deprecated` marker. Remove next minor. |
| `fetch_forcing_data` | 3 | forcing | ✅ Deprecate (approved) | Superseded by data_fetch. Docstring now `[DEPRECATED — prefer data_fetch]`; emits `_deprecated` marker. Remove next minor. |
| `get_citation_by_doi` | 2 | general | ❌ Kept (declined) | Thin wrapper over lookup_citation, but user opted to keep it live. |

## Full inventory

| Tool | Tier | Domain | Hot | Verdict | Summary |
|---|---|---|---|---|---|
| `get_citation_by_doi` | 2 | general |  | Deprecate | Resolve a citation by exact DOI (uses cache when fresh). Same schema |
| `fetch_forcing_data` | 3 | forcing |  | Deprecate | Basin-averaged daily forcing data. Globally aware: |
| `fetch_streamflow_data` | 3 | streamflow |  | Deprecate | Fetch USGS streamflow time series (NWIS). |
| `add_assumption` | 1 | claims | Y | Keep-hot | Record a scientific assumption or caveat in the session ledger. |
| `add_claim` | 1 | claims | Y | Keep-hot | Add a scoped scientific claim to the session ledger. |
| `draft_claim_from_run` | 1 | claims | Y | Keep-hot | Draft a claim pre-bound to evidence from a Tier 1 run. Reads |
| `promote_claim_to_registry` | 1 | claims | Y | Keep-hot | Promote a session claim to the global knowledge registry. |
| `get_model_results` | 1 | modelling | Y | Keep-hot | Return cached training results: NSE, KGE, RMSE, model_dir. job_id reads |
| `train_hydro_model` | 1 | modelling | Y | Keep-hot | Kick off a model training job (detached subprocess). Returns {job_id} |
| `extract_hydrological_signatures` | 1 | streamflow | Y | Keep-hot | Extract 17 CAMELS-style hydrological signatures (flow stats, BFI, |
| `separate_baseflow` | 1 | streamflow | Y | Keep-hot | Separate baseflow from streamflow via digital filter. Writes daily series |
| `check_temporal_alignment` | 1 | validators | Y | Keep-hot | Check if two time-series slots share the same temporal range. |
| `check_unit_consistency` | 1 | validators | Y | Keep-hot | Check if a session slot uses the expected scientific units. |
| `check_water_balance_consistency` | 1 | validators | Y | Keep-hot | Check if annual runoff is less than or equal to annual precipitation. |
| `compute_twi` | 1 | watershed | Y | Keep-hot | Topographic Wetness Index TWI = ln(a / tan(beta)). Used for soil |
| `create_cn_grid` | 1 | watershed | Y | Keep-hot | NRCS Curve Number grid: NLCD land cover × Polaris soil → distributed CN. |
| `delineate_watershed` | 1 | watershed | Y | Keep-hot | Delineate USGS gauge watershed via NLDI + NWIS metadata. |
| `delineate_watershed_from_point` | 1 | watershed | Y | Keep-hot | Delineate a watershed from a pour point (lat/lon, EPSG:4326). Tiered: |
| `extract_geomorphic_parameters` | 1 | watershed | Y | Keep-hot | Extract 28 geomorphic parameters (morphometry, relief, drainage network, |
| `compute_spectral_index` | 2 | analysis | Y | Keep-hot | Compute a spectral index (NDWI, NDVI, NDBI, NBR, MNDWI, …) for the |
| `fetch_camels_us` | 2 | camels | Y | Keep-hot | CAMELS-US static catchment attributes (671 minimally-disturbed CONUS |
| `index_literature` | 2 | citations |  | Keep-summary | Scan a folder of papers (.txt, .md, .pdf via pypdf) and build a |
| `lookup_citation` | 2 | citations |  | Keep-summary | Look up a citation (free text or DOI). Cascade: CrossRef → Semantic |
| `search_literature` | 2 | citations |  | Keep-summary | Query the literature index. Text match on filenames + excerpts, no vector |
| `update_claim_status` | 2 | claims |  | Keep-summary | Update the status and confidence of an existing claim. |
| `course_navigate` | 2 | course |  | Keep-summary | Push the HTML Preview panel to open a specific course module. |
| `course_scaffold` | 2 | course |  | Keep-summary | Scaffold a course on disk: course.json + one styled module.html per |
| `course_set_progress` | 2 | course |  | Keep-summary | Mutate progress for the active course. Requires explicit user agreement. |
| `data_batch_fetch` | 2 | data_fetch |  | Keep-summary | Parallel fetch over N geometries (e.g. a set of watersheds or gauges). |
| `data_describe_product` | 2 | data_fetch |  | Keep-summary | Return the full ProductSpec for a single product, including citation, |
| `data_fetch` | 2 | data_fetch | Y | Keep-hot | Fetch a single hydrology variable for one geometry / time window. |
| `data_list_products` | 2 | data_fetch |  | Keep-summary | Discover available data products, optionally filtered. |
| `data_validate_request` | 2 | data_fetch |  | Keep-summary | Pre-flight dry-run — validate a request without hitting any backend. |
| `run_python` | 2 | execution | Y | Keep-hot | Execute a Python snippet in a sandboxed subprocess inside workspace_dir. |
| `write_research_interpretation` | 2 | general |  | Keep-summary | Phase 2 of interpretation: persist LLM-authored scientific synthesis to |
| `add_journal_entry` | 2 | ledger |  | Keep-summary | Add a timestamped entry to the project's experiment journal (persistent |
| `search_experiments` | 2 | ledger |  | Keep-summary | Full-text search across all sessions in a project (case-insensitive over |
| `gee.extract_timeseries` | 2 | maps |  | Keep-summary | Extract basin time series from a GEE ImageCollection and save CSV. |
| `gee.preview_layer` | 2 | maps |  | Keep-summary | Create a GEE tile layer and add it to the AI-Hydro map panel. |
| `map_apply_symbology` | 2 | maps |  | Keep-summary | Apply graduated (choropleth) symbology to an existing vector layer on the map. |
| `map_save_roi` | 2 | maps |  | Keep-summary | Save the host map session active ROI to workspace roi/<slug>.geojson |
| `map_set_roi` | 2 | maps |  | Keep-summary | Set the active map ROI (study basin polygon). The map panel updates via |
| `map_set_working_geometry` | 2 | maps |  | Keep-summary | Set which workspace GeoJSON file is the active study geometry for this session |
| `map_update_layer` | 2 | maps |  | Keep-summary | Update layer style/visibility in-place (don't rewrite GeoJSON for styling). |
| `log_researcher_observation` | 2 | persona |  | Keep-summary | Log a meaningful observation about the researcher (memory-style; for |
| `preview_address_comment` | 2 | preview |  | Keep-summary | Address a user comment. new_text (optional) proposes replacement text |
| `preview_revise_section` | 2 | preview |  | Keep-summary | Propose a revised HTML section (well-formed snippet, no <html>/<body>). |
| `add_note` | 2 | session |  | Keep-summary | Append a researcher annotation (hypothesis, anomaly, decision) to the session. |
| `export_session` | 2 | session |  | Keep-summary | Export the session. format='capsule' (default) writes a reproducible |
| `merit_add_map_layers` | 2 | watershed |  | Keep-summary | Build MERIT vector layers for the map viewport and optionally push to the map panel. |
| `merit_ensure_basin` | 2 | watershed |  | Keep-summary | Ensure MERIT-Hydro vectors exist for the Pfaf basin at (lat, lon) WGS84. |
| `merit_ensure_basins_region` | 2 | watershed |  | Keep-summary | Check or stage regional MERIT-Basins vector/topology assets for hybrid routing. |
| `merit_ensure_region` | 2 | watershed |  | Keep-summary | Ensure MERIT river vectors for all Pfaf basins in a named region preset. |
| `merit_ensure_routing_region` | 2 | watershed |  | Keep-summary | Check or stage flowdir-first regional MERIT routing assets. |
| `list_spectral_indices` | 3 | analysis |  | Keep-summary | List all available spectral indices, their required bands, colormaps, |
| `list_assumptions` | 3 | claims |  | Keep-summary | List all assumptions in the session. |
| `list_claims` | 3 | claims |  | Keep-summary | List all scientific claims in the session. |
| `course_get_curriculum` | 3 | course |  | Keep-summary | Full manifest + prerequisite_graph for a course. Use when you need the |
| `course_get_state` | 3 | course |  | Keep-summary | Snapshot of the active course: current module, completion %, locked |
| `data_doctor` | 3 | data_fetch |  | Keep-summary | Environment health check — probes each backend, auth state, cache size, |
| `data_get_cache_status` | 3 | data_fetch |  | Keep-summary | Return a summary of the disk cache at ~/.aihydro/cache/data/. |
| `data_help` | 3 | data_fetch |  | Keep-summary | Guided onboarding and topic reference for aihydro-data. |
| `data_invalidate_cache` | 3 | data_fetch |  | Keep-summary | Remove a specific entry from the disk cache. |
| `aihydro_describe_capability` | 3 | discovery | Y | Keep-hot | Return a focused 1-line-per-tool summary of tools relevant to a domain. |
| `describe_tool` | 3 | discovery | Y | Keep-hot | Fetch the FULL parameter schema for a single tool, plus a worked example. |
| `describe_tools` | 3 | discovery | Y | Keep-hot | Fetch full parameter schemas for several tools at once (batch describe_tool). |
| `list_available_tools` | 3 | discovery | Y | Keep-hot | List all registered MCP tools (built-in + community plugins via the |
| `get_library_reference` | 3 | execution |  | Keep-summary | Look up field-name gotchas, API quirks, and copy-paste patterns for a |
| `list_relevant_clis` | 3 | execution |  | Keep-summary | List installed AI-Hydro-aware CLIs (registered via aihydro.clis entry-point, |
| `aihydro_chat_status` | 3 | general |  | Keep-summary | Show what study (if any) is currently bound to this chat. |
| `aihydro_rebind_chat` | 3 | general |  | Keep-summary | Rebind the current chat to a specific existing study. |
| `list_cached_citations` | 3 | general |  | Keep-summary | List cached DOIs in ~/.aihydro/citations/. No API calls. |
| `get_dataset_info` | 3 | knowledge |  | Keep-summary | Return metadata and limitations for a hydrological dataset (e.g. USGS NWIS, gridMET). |
| `get_equation_definition` | 3 | knowledge |  | Keep-summary | Return the definition, formula, and assumptions for a scientific equation. |
| `get_metric_definition` | 3 | knowledge |  | Keep-summary | Return the structured definition of a model evaluation metric (e.g. KGE, NSE). |
| `get_variable_definition` | 3 | knowledge |  | Keep-summary | Return the canonical definition of a hydrological variable. |
| `list_known_datasets` | 3 | knowledge |  | Keep-summary | Return all known datasets, optionally filtered by domain. |
| `list_known_metrics` | 3 | knowledge |  | Keep-summary | Return all known metrics, optionally filtered by domain. |
| `list_known_variables` | 3 | knowledge |  | Keep-summary | Return a summary of all known hydrological variables. |
| `gee.status` | 3 | maps |  | Keep-summary | Check Earth Engine availability/authentication status. |
| `map_fit_extent` | 3 | maps |  | Keep-summary | Ask the map to fit the viewport to the active ROI or visible layers. |
| `map_fit_layer` | 3 | maps |  | Keep-summary | Zoom the map viewport to a layer's extent. |
| `map_get_state` | 3 | maps |  | Keep-summary | Map session snapshot from ~/.aihydro/map_session.json: basemap, view, |
| `map_list_layers` | 3 | maps |  | Keep-summary | List map layer ids and catalog entries from the host (~/.aihydro/map_layer_catalog.json). |
| `map_remove_layer` | 3 | maps |  | Keep-summary | Remove a layer from the map by id. |
| `map_set_basemap` | 3 | maps |  | Keep-summary | Set the map basemap (e.g. esri-imagery, usgs-topo). |
| `map_show` | 3 | maps |  | Keep-summary | Open the AI-Hydro map panel if it is not already visible. |
| `show_on_map` | 3 | maps |  | Keep-summary | Push any GeoJSON geometry directly onto the AI-Hydro map panel. |
| `get_training_status` | 3 | modelling |  | Keep-summary | Poll the status of a training job started by train_hydro_model. |
| `get_researcher_profile` | 3 | persona |  | Keep-summary | Persistent researcher profile (domain, tools, preferences, focus). Built |
| `update_researcher_profile` | 3 | persona |  | Keep-summary | Update the researcher profile. Strings replace; list fields (expertise, |
| `preview_focus_cell` | 3 | preview |  | Keep-summary | Scroll the HTML Preview to a specific cell and highlight it. |
| `preview_get_pending_changes` | 3 | preview |  | Keep-summary | Return the batched queue of pending user comments + text edits for a |
| `preview_get_state` | 3 | preview |  | Keep-summary | HTML Preview session snapshot: manifest, cell registry (IDs + last-run |
| `preview_list_modules` | 3 | preview |  | Keep-summary | List the module IDs that currently have an open preview session. |
| `preview_recent_events` | 3 | preview |  | Keep-summary | Recent PreviewEvent records (cell runs/errors, user comments, iframe |
| `show_html_preview` | 3 | preview |  | Keep-summary | Open an HTML file in the AI-Hydro HTML Preview panel (executes any |
| `add_session_to_project` | 3 | project |  | Keep-summary | Link a session to a project. Session need not exist yet (pre-registration |
| `get_project_summary` | 3 | project |  | Keep-summary | Overview of a project: metadata, session summaries (with computed/pending |
| `start_project` | 3 | project |  | Keep-summary | Create or resume a named research project — top-level unit spanning |
| `archive_session` | 3 | session |  | Keep-summary | Freeze the session: move current interpretations + notes to a 'Historical' |
| `clear_session` | 3 | session |  | Keep-summary | Clear cached results to force recompute. Notes/workspace_dir/site_name/ |
| `get_session_health` | 3 | session |  | Keep-summary | Audit a session for identity drift, workspace problems, and consistency |
| `get_session_raw_state` | 3 | session |  | Keep-summary | Phase 1 of interpretation: return raw computed state for the LLM to read |
| `get_session_summary` | 3 | session | Y | Keep-hot | Return computed/pending slots for the session + notes + interpretation. |
| `merge_session_shards` | 3 | session |  | Keep-summary | Consolidate sub-agent shard files into the main session (notes, citations, |
| `start_session` | 3 | session | Y | Keep-hot | Start or resume a research session (persistent memory for a study). |
| `list_skills` | 3 | skills |  | Keep-summary | List installed workflow skills (playbooks). See SKILL DISCOVERY in the |
| `load_skill` | 3 | skills |  | Keep-summary | Load a skill's full SKILL.md (frontmatter + body). The skill's steps and |
| `save_skill` | 3 | skills |  | Keep-summary | Persist a new SKILL.md (Agent Skills open format) to |
| `delineation_doctor` | 3 | watershed |  | Keep-summary | Check runtime readiness for global MERIT Hydro watershed delineation. |
| `get_workflow_manifest` | 3 | workflows |  | Keep-summary | Return the detailed steps, tool dependencies, and recommended  |
| `list_available_workflows` | 3 | workflows |  | Keep-summary | List all available scientific workflows (e.g. rainfall-runoff benchmarking). |
