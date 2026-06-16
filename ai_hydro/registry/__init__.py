"""
AI-Hydro global claim registry.

Provides an append-only, file-backed store for promoted scientific claims
at ~/.aihydro/registry/claims.jsonl.  Each entry carries the evidence
version hashes captured at promotion time so that downstream staleness
checks can detect when the underlying data has changed.
"""
