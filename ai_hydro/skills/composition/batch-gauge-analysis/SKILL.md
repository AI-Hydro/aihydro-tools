---
name: batch-gauge-analysis
description: >
  Parallelizes analysis across multiple USGS gauges or sites using sub-agent delegation.
  Orchestrates data fetching, signature extraction, and summary synthesis for a list of sites.
  Use when the researcher asks to "analyze all these gauges", "run this on the whole list",
  or "batch process these sites".
when_to_use: >
  batch | parallel | multiple gauges | site list | delegation | sub-agent |
  regional analysis | large-scale extraction
domain: composition
tools_used: [start_session, merge_session_shards]
citations: [aihydro2026orchestration]
---

## Purpose

Efficiently process a large number of hydrological sites (USGS gauges, stations, etc.) by delegating
work to sub-agents. This avoids blocking the main conversation for tens of minutes and parallelizes
expensive network or compute tasks.

## When to use

- Researcher provides a list of > 10 gauges to analyze.
- Batch fetching forcing or streamflow data for a regional study.
- Running heavy analysis (e.g. TWI, CN grid) across many basins simultaneously.
- When per-gauge processing is expected to exceed 30 seconds.

## When NOT to use

- For a single site → run tools directly in the main session.
- For tasks that are already fast (< 5s per site).
- When sequential dependencies between sites exist (e.g. downstream routing).

## Workflow

1. **Prepare the batch.**
   - Identify the list of site IDs.
   - Group sites into logical batches (e.g. 5-10 sites per batch).

2. **Delegate to sub-agents.**
   - For each batch, spawn a sub-agent.
   - **Instruction for Sub-Agent**:
     - Call `start_session(session_id=SITE_ID, shard_id=BATCH_ID)`.
     - Perform the requested analysis for that site.
     - Call `save()` on the session (writes to shard).
     - Return a `SubAgentDigest` JSON return.

3. **Consolidate results.**
   - Once all sub-agents return:
   - For each site processed, call `merge_session_shards(session_id=SITE_ID, shard_ids=[BATCH_ID])`.
   - This merges the sub-agent's local results into the persistent session state.

4. **Summarize for researcher.**
   - Synthesize the sub-agent digests into a single report.
   - Report any failures or conflicts logged during merge.

## Digest Contract (SubAgentDigest)

Any sub-agent delegated by this skill must return a JSON object with this shape:

```json
{
  "status": "complete" | "partial" | "failed",
  "summary": "Short paragraph of what was done",
  "per_unit_outcomes": [
    { "id": "01031500", "status": "complete", "results": {...} },
    ...
  ],
  "recommended_next_action": "e.g. proceed to regionalization"
}
```

## Common failure modes

- **Shard merge conflict** → two sub-agents wrote to the same slot for the same session ID. 
  Check the `conflicts` list in `merge_session_shards` output.
- **Resource exhaustion** → spawning too many sub-agents at once. Keep batch count manageable.
- **Context drift** → sub-agents lack project-level context. Ensure core instructions are passed.

## Trigger examples

- "Analyze all 50 gauges in this list."
- "Fetch streamflow for these 20 sites in parallel."
- "Run the watershed analysis workflow for every gauge in the project."
