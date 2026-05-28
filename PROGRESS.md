# Phase 1 Progress Log

## Initialization
- Status: Started Phase 1 execution in the correct repository path.
- Branch: `refactor/phase-1-cleanup`

## Task Status
- [x] Read mandatory docs in required order
- [x] Execute Phase 1 cleanup tasks from roadmap (T1.1, T1.2)
- [x] Update version to 1.5.2
- [x] Update CHANGELOG.md
- [x] Run tests: `pytest -m 'not live' -v`
- [x] Build package: `python -m build`
- [x] Verify artifacts: `twine check dist/*`

## Verification Log
1. Checked repository root and contents:
   - Command: `pwd && ls -la`
   - Result: repository is `/Users/mgalib/Documents/AI-Hydro/MCP/aihydro-tools`
2. Created branch:
   - Command: `git checkout -b refactor/phase-1-cleanup`
   - Result: success
3. Located mandatory architecture docs from provided external path and read in order:
   - Command: `ls -la /Users/mgalib/Library/CloudStorage/Box-Box/Obsidian/AI_Hydro2/AI-Hydro/local-docs/architecture`
   - Command: `sed -n ... ARCHITECTURE.md` (including Section 2 rubric)
   - Command: `sed -n ... REFACTOR_ROADMAP.md` (Phase 1 Cleanup)
   - Command: `sed -n ... AUDIT_FINDINGS.md` (Section 4.2 A1 and A6)
   - Result: mandatory reading complete.
4. Baseline cleanup scope check:
   - Command: `find ai_hydro -maxdepth 2 -type d -name workflows -print; find ai_hydro/workflows -maxdepth 1 -type f -print`
   - Result: `ai_hydro/workflows/` existed with 4 files.
   - Command: grep scan for `Tier 2|Tier 3|tools.hydrology|tools.watershed` under `ai_hydro/ tests/ docs/ python/`
   - Result: stale references found in live code.
5. Phase 1 edits applied:
   - Deleted `ai_hydro/workflows/` directory.
   - Removed/updated stale references in `ai_hydro/` and `tests/` via grep-driven cleanup.
   - Bumped package version in `pyproject.toml` to `1.5.2`.
6. Post-cleanup verification:
   - Command: `ls ai_hydro/workflows/`
   - Result: `No such file or directory`.
   - Command: `grep -rnI --exclude-dir='__pycache__' "Tier 2\|Tier 3\|tools\.hydrology\|tools\.watershed" ai_hydro/ tests/ docs/ python/`
   - Result: no matches in live code.
   - Command: `git branch --show-current && rg -n "^version = \"" pyproject.toml`
   - Result: branch `refactor/phase-1-cleanup`; version `1.5.2`.
7. Updated changelog:
   - File updated: `CHANGELOG.md`
   - Added section: `## [1.5.2] — 2026-04-23` with `### Removed` and `### Changed`.
8. Non-live test suite:
   - Command: `pytest -m 'not live' -v`
   - Result: `102 passed, 7 deselected`.
9. Build artifacts:
   - Command: `python -m build`
   - Result: success; built `dist/aihydro_tools-1.5.2.tar.gz` and `dist/aihydro_tools-1.5.2-py3-none-any.whl`.
10. Artifact metadata validation:
    - Command: `twine check dist/*`
    - Result: both artifacts PASSED.
11. Additional roadmap sanity check (informational):
    - Command: `python setup_mcp.py --check`
    - Result: checker reports stale expected-tool set (`extract_camels_attributes` expected; newer tools flagged as unexpected). No Phase 1 changes applied to `ai_hydro/mcp/` per restriction.

## Next Steps
- Prepare a clean commit on `refactor/phase-1-cleanup` including only Phase 1 files and excluding unrelated pre-existing working-tree changes.

---

## Phase 4 — Maturation (2.0.0)

- Branch: `refactor/phase-4-maturation`
- Commit(s): f148ab5, 23e53ff, 368eda7, 85aeb2e
- PR URL: https://github.com/AI-Hydro/aihydro-tools/pull/new/refactor/phase-4-maturation
- Date: 2026-04-24
- Executor: Claude Sonnet 4.6 (claude-sonnet-4-6) via AI-Hydro VS Code extension

### Tasks

#### T4.1 — Remove `sync_research_context` alias
Status: Completed
Files: `ai_hydro/mcp/tools_session.py`, `ai_hydro/mcp/helpers.py`, `ai_hydro/mcp/tools_docs.py`, `ai_hydro/session/store.py`, `ai_hydro/citations.py`, `tests/test_mcp_integration.py`
Verification:
    $ python -c "
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter('always')
        import ai_hydro.mcp
    dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
    print('DeprecationWarnings on import:', len(dep_warnings))
    import ai_hydro.mcp.tools_session as ts
    print('sync_research_context exists:', hasattr(ts, 'sync_research_context'))
    "
    DeprecationWarnings on import: 0
    sync_research_context exists: False

#### T4.2 — Remove `train_hydro_model` synchronous alias
Status: Completed
Files: `ai_hydro/mcp/tools_modelling.py`
Verification:
    $ python -c "
    import ai_hydro.mcp.tools_modelling as tm
    print('_train_hydro_model_sync_alias exists:', hasattr(tm, '_train_hydro_model_sync_alias'))
    "
    _train_hydro_model_sync_alias exists: False

#### T4.3 — Remove `findings` field aliases
Status: Verified clean (removed in Phase 2 T2.3)
Files: No changes needed
Verification:
    $ python -c "
    import tempfile
    from unittest.mock import patch
    from pathlib import Path
    from ai_hydro.mcp.tools_session import get_session_summary
    with tempfile.TemporaryDirectory() as d:
        with patch('ai_hydro.session.store._SESSIONS_DIR', Path(d)), patch('ai_hydro.session.store._REPO_ROOT', Path(d)):
            r = get_session_summary('t4-test')
            print('findings in summary:', 'findings' in r)
    "
    findings in summary: False

#### T4.4 — P2 library cards (pandas, numpy, shapely, matplotlib, folium)
Status: Completed
Files: `ai_hydro/knowledge/library_refs/pandas.json`, `numpy.json`, `shapely.json`, `matplotlib.json`, `folium.json`, `.github/workflows/knowledge-compat.yml`
Verification:
    $ python -c "
    from ai_hydro.mcp.tools_analysis import get_library_reference
    cat = get_library_reference()
    print('Total cards:', len(cat['available_libraries']))
    for card in ['pandas', 'numpy', 'shapely', 'matplotlib', 'folium']:
        r = get_library_reference(card)
        print(f'{card}: gotchas={len(r[\"gotchas\"])} patterns={len(r[\"common_patterns\"])}')
    "
    Total cards: 15
    pandas: gotchas=8 patterns=4
    numpy: gotchas=8 patterns=4
    shapely: gotchas=8 patterns=4
    matplotlib: gotchas=8 patterns=4
    folium: gotchas=8 patterns=4

#### T4.5 — Re-evaluate M3 façade (`get_library_reference`)
Status: Kept (decision documented)
Files: `ai_hydro/mcp/tools_analysis.py`
Verification:
    Decision: façade retained per OPEN_QUESTIONS.md §7.6 — MCP resource support
    (list_resources / read_resource) not uniformly available across Cline,
    Claude Code, Claude Desktop. Documented in get_library_reference docstring.

#### T4.6 — `export_session` research-capsule rewrite
Status: Completed
Files: `ai_hydro/mcp/tools_session.py`
Verification:
    $ python -c "
    import tempfile
    from unittest.mock import patch
    from pathlib import Path
    from ai_hydro.mcp.tools_session import export_session
    from ai_hydro.session import HydroSession
    with tempfile.TemporaryDirectory() as d:
        with patch('ai_hydro.session.store._SESSIONS_DIR', Path(d)), patch('ai_hydro.session.store._REPO_ROOT', Path(d)):
            s = HydroSession('cap-test')
            s.site_name = 'test'; s.interpretation = 'Test.'; s.workspace_dir = d; s.save()
            custom = Path(d) / 'cap'
            r = export_session('cap-test', capsule_path=str(custom))
            cap = Path(r['capsule_dir'])
            for f in ['README.md','methods.md','citations.bib','session.json','environment.yml']:
                print(f, ':', (cap/f).exists())
            for d2 in ['data','figures','model']:
                print(d2+'/', ':', (cap/d2).is_dir())
    "
    README.md : True
    methods.md : True
    citations.bib : True
    session.json : True
    environment.yml : True
    data/ : True
    figures/ : True
    model/ : True

#### T4.7 — Migration guide for 1.x → 2.0
Status: Completed
Files: `MIGRATION.md`
Verification:
    $ ls MIGRATION.md && grep -c "Before" MIGRATION.md
    MIGRATION.md
    3

#### Tool count verification
Status: Completed (36 tools — sync_research_context removed)
Verification:
    $ python -c "
    import asyncio
    from ai_hydro.mcp import mcp
    tools = asyncio.run(mcp.list_tools())
    print('Tool count:', len(tools))
    "
    Tool count: 36

#### Test suite
Status: Completed
Verification:
    $ pytest tests/ -m "not live" -q
    130 passed, 7 deselected

#### Build + distribution check
Status: Completed
Verification:
    $ python -m build && twine check dist/aihydro_tools-2.0.0*
    Successfully built aihydro_tools-2.0.0.tar.gz and aihydro_tools-2.0.0-py3-none-any.whl
    Checking dist/aihydro_tools-2.0.0-py3-none-any.whl: PASSED
    Checking dist/aihydro_tools-2.0.0.tar.gz: PASSED

### Unrelated pre-existing changes preserved
none

### Deviations
- T4.3 required no code changes — `findings` field was already removed in Phase 2 (T2.3). Verified clean and documented.
- T4.5 façade kept (as planned in PHASE_PROMPTS.md — decision re-litigated and confirmed: keep).

---

## Patch 1.6.1 — Sub-Agent Foundation

- Branch: `feature/patch-1.6.1-subagent`
- Date: 2026-05-02
- Executor: Antigravity

### Tasks

#### T5.1 — HydroSession sharding for sub-agent safety
Status: Completed
Files: `ai_hydro/session/store.py`, `ai_hydro/session/sharding.py`, `ai_hydro/mcp/tools_session.py`, `ai_hydro/session/__init__.py`
Verification:
    Created `tests/test_sharding.py`. Verified that:
    1. Sub-agents can save to `<session_id>.<shard_id>.shard.json` without touching the main file.
    2. `merge_session_shards` correctly consolidates notes (union), citations (union), and slots (last-writer-wins).
    3. Conflict detection logs overlapping slot updates.
    4. Shard files are cleaned up after merge.

#### T5.3 — Digest contract as a typed Pydantic model
Status: Completed
Files: `ai_hydro/core/digest.py`
Verification:
    `SubAgentDigest` and `UnitOutcome` models defined and validated in `tests/test_sharding.py`.

#### T5.2 — `batch-gauge-analysis` sub-agent skill
Status: Completed
Files: `ai_hydro/skills/composition/batch-gauge-analysis/SKILL.md`
Verification:
    Skill file created with explicit sub-agent delegation playbook and digest contract.

#### T5.4 — Persona update for sub-agent delegation
Status: Completed
Files: `ai_hydro/mcp/app.py`, `ai_hydro/mcp/tools_session.py`
Verification:
    Updated `instructions` in `app.py` with guidance on `shard_id` and `merge_session_shards`. 
    Updated `start_session` tool to accept `shard_id`.

### Verification Log
1. Run sharding unit tests:
   - Command: `pytest tests/test_sharding.py`
   - Result: `2 passed in 0.12s`
2. Full test suite:
   - Command: `pytest tests/ -m "not live" -q`
   - Result: `132 passed` (baseline 130 + 2 new)

---

## Patch 1.6.2 — Session Staleness

- Branch: `feature/patch-1.6.2-staleness`
- Date: 2026-05-02
- Executor: Antigravity

### Tasks

#### T6.1 — Per-field TTL metadata in `HydroSession`
Status: Completed
Files: `ai_hydro/session/store.py`, `ai_hydro/mcp/tools_session.py`
Verification:
    Added `interpretation_at` and `archived` fields. Implemented `is_stale(field)` with:
    - 365 day TTL for computed slots.
    - 30 day TTL for notes.
    - 14 day TTL for interpretations.

#### T6.2 — `archive_session` tool
Status: Completed
Files: `ai_hydro/mcp/tools_session.py`, `ai_hydro/session/store.py`
Verification:
    New tool `archive_session(session_id)` marks session as archived, forcing all content into the historical section.

#### T6.3 — "Archive" section in `research.md`
Status: Completed
Files: `ai_hydro/session/store.py`
Verification:
    `write_research_context` updated to separate active vs historical context. Stale or archived information is moved below a "Historical / Stale Context" header with a warning.

### Verification Log
1. Run staleness unit tests:
   - Command: `pytest tests/test_staleness.py`
   - Result: `2 passed in 0.10s`
2. Full test suite:
   - Command: `pytest tests/ -m "not live" -q`
   - Result: `134 passed` (baseline 132 + 2 new)

---

## Patch 1.6.3 — Skill Quality & Workspace Governance

- Branch: `feature/patch-1.6.3-governance`
- Date: 2026-05-02
- Executor: Antigravity

### Tasks

#### T7.1 & T7.2 — Skill Linter & Filtering
Status: Completed
Files: `ai_hydro/skills/registry.py`
Verification:
    Implemented `_lint_skill` to enforce name, description, and `when_to_use` presence. Skills failing these rules are skipped during discovery with a warning. Verified via `tests/test_skills_linter.py`.

#### T7.3 — New Built-in Skills
Status: Completed
Files: 
    - `ai_hydro/skills/interpretation/snow-hydrology-trends/SKILL.md`
    - `ai_hydro/skills/composition/ungauged-basin-transcription/SKILL.md`
    - `ai_hydro/skills/frequency-analysis/drought-indices-calculation/SKILL.md`
Verification:
    Skills added and confirmed discoverable via `list_skills()`. Total skill count raised to 9.

### Verification Log
1. Run skill linter tests:
   - Command: `pytest tests/test_skills_linter.py`
   - Result: `2 passed in 0.15s`
2. Full test suite:
   - Command: `pytest tests/ -m "not live" -q`
   - Result: `136 passed`

---

## Feature — GEE Backend Contracts

- Branch: `feature/gee-backend-contracts`
- Date: 2026-05-20
- Executor: Codex

### Tasks

#### G1 — Backend audit and ecosystem review
Status: Completed
Files:
    - `local-docs/gee-backend-audit.md`
    - `local-docs/geetools-inspection.md`
Verification:
    Audited MCP registration, GEE modules, session ROI/provenance helpers, dependency extras, tests, and duplication risk with the VS Code extension. Reviewed `geetools` conceptually and deferred dependency adoption.

#### G2 — GEE contracts and preset registry
Status: Completed
Files:
    - `ai_hydro/gee/contracts.py`
    - `ai_hydro/gee/presets.py`
    - `local-docs/gee-contracts-design.md`
    - `local-docs/gee-capability-registry.md`
Verification:
    Added JSON-serializable contracts for ROI, dataset presets, live layers, analysis artifacts, report bundles, provenance records, export task records, and workflow runs. Added hydrology-aware presets for CHIRPS, SRTM, MODIS NDVI, NLCD, ESA WorldCover, and ERA5-Land.

#### G3 — MCP GEE contract alignment
Status: Completed
Files:
    - `ai_hydro/mcp/tools_gee.py`
    - `tests/test_mcp_gee_tools.py`
    - `tests/test_gee_contracts.py`
Verification:
    Updated `gee.status` to scrub secret-adjacent fields. Updated `gee.preview_layer` to return a `LiveLayer` and provenance record. Updated `gee.extract_timeseries` to return an `AnalysisArtifact`, summary JSON, and provenance record. Enforced explicit ROI resolution and preset reducer/aggregation validation.

#### G4 — Live marker correction
Status: Completed
Files:
    - `tests/test_bench.py`
Verification:
    Marked `bench_live` tasks with `@pytest.mark.live` so the documented non-live command excludes live network tests.

#### G5 — Dataset preset-only input support
Status: Completed
Files:
    - `ai_hydro/mcp/tools_gee.py`
    - `tests/test_mcp_gee_tools.py`
Verification:
    `gee.preview_layer` and `gee.extract_timeseries` now resolve `dataset_id` and `band` from `dataset_preset` when callers provide a preset only. Added regression coverage for preset-only preview and extraction calls.

### Verification Log
1. Run focused GEE tests:
   - Command: `pytest tests/test_gee_contracts.py tests/test_mcp_gee_tools.py tests/test_gee_cli.py -q`
   - Initial result: `16 passed in 155.85s`
   - Final result after preset-only regression tests: `18 passed in 2.36s`
2. Run documented non-live suite:
   - Command: `pytest -m 'not live' -v`
   - First result: failed because `bench_live` was not also marked `live`; this ran a live watershed benchmark and failed against current external data.
   - Intermediate result after marker correction: `209 passed, 8 deselected, 1 warning in 31.33s`
   - Final result after preset-only regression tests: `211 passed, 8 deselected, 1 warning in 12.67s`

## 2026-05-25 — MERIT Hydro GEE + pyflwdir global delineation tier

- Added `merit_gee_pyflwdir` as the global default path behind `delineate_watershed_from_point(method="auto")` after CONUS NLDI attempts.
- Added `local_merit_pyflwdir`, `nldi`, `merit_gee`, and `dem_raw_fallback` router methods; kept `fast` as a backward-compatible alias for raw DEM fallback.
- Added GEE MERIT Hydro raster fetch/cache, MERIT outlet snapping using `upa`/`wth`, local `pyflwdir` basin delineation, area validation, quality flags, citation, and license fields.
- Added `delineation_doctor()` MCP tool for GEE/project/dependency readiness.
- Updated watershed delineation docs and added missing `PROJECT.md` cold-start entry point.

Verification:
- `python -m pytest -q tests/test_delineation.py -m 'not live'`: `19 passed, 1 deselected`.
- `python -m pytest -q tests/test_delineation.py::test_delineate_watershed_from_point_invalid_method`: `1 passed`.
- `python -m compileall -q ai_hydro/analysis/delineation ai_hydro/mcp`: passed.
- `delineation_doctor()` smoke check returned `default_global_method="merit_gee_pyflwdir"` with `pyflwdir`, `earthengine_api`, `rasterio`, and `geopandas` available.
- Broader registry tests remain blocked by stale pre-existing tier/expected-tool coverage for map, preview, citation, course, and discovery tools; not introduced by this delineation change.

Follow-up live NLDI comparison:
- `nebraska_test` at `(40.71829, -96.41265)`: NLDI area `114.332 km²`; MERIT GEE + `pyflwdir` area `114.047 km²`; area error `0.25%`; polygon IoU `0.9756`; no quality flags; first uncached MERIT run `39.43 s`.
- Larger CONUS comparison attempts at `(40.4, -86.1)` and `(35.03, -120.48)` reached Earth Engine synchronous `getPixels` limits during adaptive window expansion; the fetcher now reports the GEE error body with async-export guidance instead of a generic HTTP error.
- Adjusted MERIT GEE sync defaults to `20 km` initial half-window and required bands `dir`, `upa`, `wth` to stay under GEE sync limits for small/medium basins.
- Added `workflow_steps` to delineation outputs so agents/users can read the exact method, data, and step sequence without inferring it from metadata.
- Added `scripts/benchmark_merit_gee_vs_nldi.py` for repeatable live NLDI-vs-MERIT accuracy checks.
- Script smoke result for `nebraska_test`: cached MERIT run `1.69 s`, total comparison `4.29 s`, area error `0.249%`, IoU `0.9756`.
- Ran 18 live CONUS NLDI comparison attempts across Plains/Midwest/Southeast/Texas candidates. Current state after snap hardening:
  - `12` completed MERIT comparisons, `6` failed due GEE sync memory/window limits or invalid far-snap basin.
  - `11` completed comparisons were clean/unflagged: median area error `0.35%`, max clean area error `4.51%`, median IoU `0.9255`, min clean IoU `0.7454`.
  - `1` completed comparison was flagged (`OUTLET_SNAP_FAR`); it had good area agreement but zero overlap against NLDI, confirming far area-target snaps must not be treated as high-confidence.
  - Main production gap: larger/adaptive windows need async GEE export or tiled MERIT fetch; synchronous `getPixels` is not enough for many 200-300 km² basins.

## 2026-05-26 — MERIT-Basins hybrid overflow routing scaffold

- Added provisional adaptive-window safe-envelope policy:
  - `MERIT_INTERACTIVE_MAX_WINDOW_CELLS = 60_000_000`
  - `MERIT_INTERACTIVE_MAX_RSS_DELTA_MB = 600`
  - `MERIT_SCIENTIFIC_MAX_WINDOW_CELLS = 120_000_000`
  - `MERIT_SCIENTIFIC_MAX_RSS_DELTA_MB = 1_500`
  - `safe_envelope_version = "benchmark_2026-05-26_v1"`
- Added MERIT-Basins regional cache/status helpers and MCP tool `merit_ensure_basins_region`.
- Added `merit_basins_hybrid_delineate`: GEE tiny `upa/wth` snap reference, staged MERIT-Basins catchment topology traversal, upstream vector assembly, and terminal local MERIT flowdir refinement when safe.
- Router now promotes adaptive local overflow / GEE memory recovery cases to `method_used="merit_basins_hybrid"` when staged topology is available, or returns/records `HYBRID_ROUTING_REQUIRED` rather than unbounded raster expansion.
- Offline snap-cache preparation now defaults to optional `published_accum`; full-Pfaf local upstream-area derivation remains explicit via `offline_snap_asset="local_upstream_area"`.

Verification:
- `python -m pytest -q tests/test_delineation.py -m 'not live'`: `33 passed, 1 deselected`.
- `python -m compileall -q ai_hydro/analysis/delineation ai_hydro/data ai_hydro/mcp`: passed.
- `python -m pytest -q tests/test_tool_tiers.py`: initially exposed pre-existing un-tiered tools (`data_fetch`, citation tools, map tools, preview tools); resolved in the follow-up hygiene patch below.

## 2026-05-26 — Global Watershed Delineation v1 freeze

- Staged real MERIT-Basins Level-2 vectors for Pfaf 74 and 77 from the ReachHydro Google Drive MERIT_Hydro_v07_Basins_v01 source.
- Wrote vector/raster provenance manifests:
  - `/Users/mgalib/.aihydro/merit/metadata/basins_74.json`
  - `/Users/mgalib/.aihydro/merit/metadata/basins_77.json`
- Fixed hybrid topology loading so unit-catchment polygons merge river-flowline topology (`NextDownID`, `uparea`, `up1..up4`) by `COMID`; real catchment polygons only carry `COMID` and `unitarea`.
- Changed terminal raster refinement to read only the terminal catchment flowdir window and intersect with the terminal catchment, avoiding full adaptive-basin routing inside the hybrid method.
- Validated real topology:
  - Pfaf 74 Nebraska: terminal `COMID=74033128`, upstream catchments `1`, vector-only area `126.600 km²`, official MERIT UPA `113.873 km²`.
  - Pfaf 77 Sacramento: terminal `COMID=77013205`, upstream catchments `1311`, vector area `58154.251 km²`, official MERIT UPA error `0.131%`.
- Live gate benchmarks:
  - Nebraska/Pfaf 74 hybrid: area `114.038 km²`, NLDI area error `0.257%`, IoU vs NLDI `0.9779`, IoU vs adaptive local `~1.0`, MERIT UPA error `0.145%`, terminal refinement succeeded.
  - Sacramento/Pfaf 77 hybrid: area `58154.251 km²`, NLDI area error `2.299%`, IoU vs NLDI `0.8322`, MERIT UPA error `0.131%`, terminal refinement succeeded, upstream catchments `1311`.
  - Sacramento adaptive local interactive correctly refused the 116,238,493-cell window with `HYBRID_ROUTING_REQUIRED`.
  - Sacramento adaptive local scientific completed in `66.5 s`; hybrid completed in `9.6 s` with IoU vs scientific adaptive `0.99988`.

Verification:
- `python -m pytest -q tests/test_delineation.py -m 'not live'`: `34 passed, 1 deselected`.
- `python -m compileall -q ai_hydro/analysis/delineation ai_hydro/data ai_hydro/mcp`: passed.
- `git diff --check ...`: passed.
- `python -m pytest -q tests/test_tool_tiers.py`: initially failed on pre-existing un-tiered tools outside delineation (`data_fetch`, citation, map, preview); resolved in the follow-up hygiene patch below.

Status:
- Global Watershed Delineation v1 is frozen/stable. Supported routing modes and limitations are documented in `local-docs/watershed-delineation.md`.
- Stop adding delineation architecture unless future users expose a concrete defect or a clearly measured production gap.

## 2026-05-26 — Tool tier registry hygiene

- Added missing tier entries for pre-existing citation, data-fetch, map, and preview tools in `ai_hydro/mcp/app.py`.
- Assigned citation/data-fetch and user-facing map/preview edit commands to Tier 2; assigned map/preview state/navigation/listing commands and cached-citation listing to Tier 3.

Verification:
- `python -m pytest -q tests/test_tool_tiers.py`: `6 passed`.
- `python -m pytest -q tests/test_delineation.py -m 'not live'`: `34 passed, 1 deselected`.
- `python -m compileall -q ai_hydro/mcp ai_hydro/analysis/delineation ai_hydro/data`: passed.

Status:
- Global Watershed Delineation v1: stable.
- Feature-level tests: passing.
- Previously unrelated tool-tier registry issue: resolved.

## 2026-05-26 — Repository hygiene after delineation v1 freeze

- Replaced the stale hard-coded MCP expected-tool list in `tests/test_mcp_integration.py` with the maintained `TOOL_TIERS` registry as the registration contract.
- Shortened the MCP server persona/instructions block to stay under the Phase 2 budget while preserving skill discovery, provenance, recovery, session-context, long-running-work, transparency, and course-mode guidance.
- Aligned `HydroSession.is_stale()` with archived-session behavior: archived sessions are treated as historical/stale regardless of per-field timestamps.

Verification:
- `python -m pytest -q tests/test_mcp_integration.py::TestToolRegistration tests/test_mcp_integration.py::TestPhase2Persona tests/test_staleness.py::test_session_staleness_logic`: `8 passed`.
- `python -m pytest -q -m 'not live'`: `251 passed, 9 deselected, 9 warnings`.
- `python -m compileall -q ai_hydro tests/test_mcp_integration.py tests/test_staleness.py`: passed.

Status:
- Global Watershed Delineation v1: stable.
- Feature-level and broad non-live tests: passing.
- Known unrelated tool-tier registry issue: resolved.

## 2026-05-26 — Map quick delineation uses staged MERIT workflow

- Fixed `hydro_map_cli delineate-point --method auto` so non-CONUS map clicks first stage/check the Pfaf regional MERIT flowdir with `acquisition_policy="download_if_missing"`.
- When the regional flowdir is available, the map bridge now calls `method="local_merit"`; when staging is unavailable, it calls `method="merit_gee"` instead of silently allowing raw DEM fallback.
- CONUS map clicks still preserve the existing `method="auto"` path so NLDI/NHDPlus remains first.
- Staged Pfaf 45 flowdir only for the live map test:
  - `/Users/mgalib/.aihydro/merit/raster/flowdir_basins/flowdir45.tif`
  - size `276,428,799` bytes
  - no accumulation raster downloaded
- Re-ran the exact map outlet `(25.744005258240417, 79.38185026023648)`:
  - method `local_merit_pyflwdir`
  - wall time `22.59 s`
  - local routing runtime `16.99 s`
  - area `10,781.518 km²`
  - official MERIT UPA `10,775.046 km²`
  - relative area error vs MERIT UPA `0.0006`
  - `window_complete=true`
  - quality flags: `ADAPTIVE_WINDOW_EXPANDED`

Verification:
- `python -m pytest -q tests/test_hydro_map_cli.py tests/test_delineation.py -m 'not live'`: `37 passed, 1 deselected`.
- `python -m pytest -q -m 'not live'`: `254 passed, 9 deselected, 9 warnings`.
- `python -m compileall -q ai_hydro/hydro_map_cli.py tests/test_hydro_map_cli.py`: passed.
- `git diff --check -- ai_hydro/hydro_map_cli.py tests/test_hydro_map_cli.py tests/test_delineation.py`: passed.
