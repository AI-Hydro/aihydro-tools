"""
Capsule: reproducibility artefact generation and verification.

Public API:
    build_manifest(capsule_dir)  → dict
    verify_manifest(capsule_dir) → (bool, list[dict])

Both are used by export_session and by replay.py.
"""
from ai_hydro.capsule.manifest import (
    MANIFEST_FILE,
    TOLERANCE_DEFAULT,
    build_manifest,
    verify_manifest,
)

__all__ = ["MANIFEST_FILE", "TOLERANCE_DEFAULT", "build_manifest", "verify_manifest"]
