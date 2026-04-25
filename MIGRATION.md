# AI-Hydro Migration Guide: 1.x → 2.0

## Overview

Version 2.0.0 removes the deprecated aliases introduced in 1.6.0 and 1.7.0.
Every removed API has a direct replacement that has been available since the
version it was deprecated.

---

## Removed in 2.0.0

### `sync_research_context` (removed — deprecated in 1.6.0)

**Before (1.x):**
```python
# Phase 1: get raw state
result = sync_research_context(session_id)

# Phase 2: write interpretation
sync_research_context(
    session_id,
    interpretation="The basin shows...",
    site_name="piscataquis-study",
)
```

**After (2.0):**
```python
# Phase 1: get raw state
result = get_session_raw_state(session_id)

# Phase 2: write interpretation
write_research_interpretation(
    session_id,
    site_name="piscataquis-study",
    interpretation="The basin shows...",
)
```

`get_session_raw_state` returns the full computed session state for the LLM to
read. `write_research_interpretation` stores the LLM-authored prose and updates
`research.md`. This two-phase split is the G1-compliant pattern.

---

### Synchronous `train_hydro_model` polling alias (removed — deprecated in 1.7.0)

The private function `_train_hydro_model_sync_alias` was the backward-compat
synchronous wrapper. It was never a public API (no `@mcp.tool()` decorator) but
is documented here for completeness.

**Before (1.x — private API):**
```python
# Do not use — this was never stable API
result = _train_hydro_model_sync_alias(session_id, framework="hbv", ...)
```

**After (2.0 — standard kickoff+poll pattern):**
```python
# 1. Kick off training (returns immediately)
kickoff = train_hydro_model(session_id, framework="hbv", epochs=500)
job_id = kickoff["job_id"]

# 2. Poll until complete (at most once per minute)
import time
while True:
    status = get_training_status(job_id)
    if status["status"] in ("complete", "failed"):
        break
    time.sleep(60)

# 3. Read results
results = get_model_results(session_id, job_id=job_id)
```

---

### `findings` field on summary tools (removed — deprecated in 1.6.0)

`get_session_summary` and `get_project_summary` no longer return a `findings`
field. This field was an auto-generated Python interpretation (violating
principle G1: LLM authors interpretation, Python returns raw state).

**Before (1.x):**
```python
summary = get_session_summary(session_id)
# summary["findings"]  # ← no longer present in 2.0
```

**After (2.0):**
Use `get_session_raw_state` to retrieve computed data, then call
`write_research_interpretation` to have the LLM author the interpretation.

```python
raw = get_session_raw_state(session_id)
# raw["slots"]["signatures"]["bfi"] etc.
write_research_interpretation(session_id, site_name="...", interpretation="...")
```

---

## New in 2.0.0

### P2 library cards

Five new reference cards are available via `get_library_reference`:

- `pandas` — DataFrame timeseries patterns, resample, FDC construction
- `numpy` — NaN-safe statistics, NSE/FDC slope computation
- `shapely` — geometry validity, coordinate order, area-in-CRS gotchas
- `matplotlib` — hydrograph/FDC figures, log-scale, publication quality
- `folium` — interactive watershed maps, CRS requirements

```python
ref = get_library_reference("pandas")
print(ref["gotchas"])
```

### `export_session` capsule_path parameter

`export_session` now accepts an explicit `capsule_path` parameter to control
where the capsule folder is written:

```python
export_session(session_id, capsule_path="/path/to/my/output")
```

The capsule now includes a `model/` directory with trained model artifacts
(HBV parameters, simulated discharge CSV, metrics summary) when a model has
been trained.

---

## Checklist for upgrading from 1.x

- [ ] Replace all `sync_research_context(...)` calls with `get_session_raw_state` + `write_research_interpretation`
- [ ] Remove any code that reads `summary["findings"]` — use `get_session_raw_state` instead
- [ ] Replace any use of `_train_hydro_model_sync_alias` with the kickoff+poll pattern
- [ ] Run `pytest -m "not live"` — the tombstone test `test_sync_research_context_removed_in_2_0` will catch any lingering alias usage
- [ ] Verify `import ai_hydro.mcp` emits zero `DeprecationWarning`s
