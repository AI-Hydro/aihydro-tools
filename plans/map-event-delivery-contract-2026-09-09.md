# Map event delivery contract — 2026-09-09

## Problem

Python `push_layer` and `push_set_roi` write global one-shot JSON files without a requested study/workspace identity. The extension deletes files before applying them and provides no acknowledgement. A layer or ROI can therefore be consumed in the wrong open workspace, rejected without a durable result, or reported as merely “queued” despite never reaching the intended Map.

## Contract direction

- Add a versioned envelope with command/event ID, session ID, normalized workspace ownership, issue time, payload type, and data/artifact identity where available.
- The host compares the envelope workspace to its current normalized workspace before mutation. Unknown/conflicting ownership is not applied.
- Move inputs to a processing state before apply and write an atomic receipt containing applied/rejected/failed status and a reason. Preserve enough information for crash recovery and bounded redelivery without duplicate scientific layers.
- Python callers that promise Map delivery await a bounded receipt and return the exact status. Fire-and-forget helpers must call themselves queued and expose the envelope ID; they cannot claim display success.
- Apply the same ownership rule to vector, raster, GEE and ROI paths. Keep the existing host-owned Map/layer store; do not introduce a parallel scientific store.

## Acceptance

Tests cover matching/conflicting/unknown workspace, malformed payload, host unavailable timeout, crash/restart processing recovery, duplicate envelope replay, raster artifact missing, and an applied receipt. No test reads or writes the user's real `~/.aihydro`. Existing tool callers retain compatible return shapes with additive delivery status/provenance. A real persisted-study walkthrough follows this contract slice.

## Exclusions

This does not certify the scientific layer, provider freshness, or render completeness. Those remain separate validation and Map render gates.
