"""Golden pin for the legacy ``sha256-v2`` registry evidence fingerprint.

Registry rows written before the records-v2 work store these fingerprints, and
``check_evidence_staleness`` recomputes them from the retained run. A silent
change to the algorithm would mark every legacy promotion stale (or, worse,
re-bless changed evidence). The value below was computed with the algorithm as
shipped at aihydro-tools d566b82 and is deliberately NOT regenerated from the
code under test. The canonical-JSON string is pinned too, so a failure shows
whether the encoding or the hash changed.

``sha256-v2`` is distinct from ``aihydro_core.records`` ``sha256:`` digests
(``aihydro.c14n/1``); the two must never be conflated.
"""
from __future__ import annotations

import hashlib
import json

from ai_hydro.registry.evidence import fingerprint

GOLDEN_RUN_ENTRY = {
    "run_id": "golden-run-01",
    "session_id": "golden-session",
    "tool_name": "fixture_tool",
    "key_outputs": {"nse": 0.8, "series_n": 3},
    "evidence": {
        "schema_version": 1,
        "data": {"nse": 0.8},
        "uncertainty": {"nse": {"value": 0.8, "ci_low": 0.7, "ci_high": 0.9, "ci_level": 0.95,
                                "n": 30, "method": "synthetic_fixture"}},
        "quality_flags": [{"validator": "fixture_check", "status": "pass"}],
        "error": False,
        "status": None,
    },
}

GOLDEN_PAYLOAD = (
    '{"evidence":{"data":{"nse":0.8},"error":false,"quality_flags":[{"status":"pass",'
    '"validator":"fixture_check"}],"schema_version":1,"status":null,"uncertainty":{"nse":'
    '{"ci_high":0.9,"ci_level":0.95,"ci_low":0.7,"method":"synthetic_fixture","n":30,'
    '"value":0.8}}},"key_outputs":{"nse":0.8,"series_n":3},"run_id":"golden-run-01",'
    '"session_id":"golden-session","tool_name":"fixture_tool"}'
)
GOLDEN_FINGERPRINT = "sha256-v2:8af0876c01ac6894405d3014eb52995fbe17bb23117bebb2b7810ec335fd9424"


def test_legacy_sha256_v2_fingerprint_is_unchanged():
    assert fingerprint(GOLDEN_RUN_ENTRY) == GOLDEN_FINGERPRINT


def test_golden_fingerprint_matches_independent_computation():
    """The pinned hex is the SHA-256 of the pinned payload, computed without the code under test."""
    assert "sha256-v2:" + hashlib.sha256(GOLDEN_PAYLOAD.encode()).hexdigest() == GOLDEN_FINGERPRINT
    assert json.dumps(GOLDEN_RUN_ENTRY, sort_keys=True, separators=(",", ":"), ensure_ascii=True) == GOLDEN_PAYLOAD
