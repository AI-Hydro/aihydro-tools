"""
AI-Hydro global claim registry.

Provides a file-backed store (rewritten whole under a cross-process lock) for promoted scientific claims
at $AIHYDRO_HOME/registry/claims.jsonl (default ~/.aihydro).  Each entry carries the evidence
version hashes captured at promotion time so that downstream staleness
checks can detect when the underlying data has changed.
"""
