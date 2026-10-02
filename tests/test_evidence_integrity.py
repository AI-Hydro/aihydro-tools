"""Adversarial registry tests: synthetic fixtures only, all IO under tmp_path."""
from __future__ import annotations

import json
import sqlite3

import pytest

from ai_hydro.mcp.tools_ledger import check_registry_staleness, promote_claim_to_registry
from ai_hydro.registry import store as registry
from ai_hydro.registry.evidence import EvidenceError, verified_versions
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from approval_helpers import approve, basin_ref


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.knowledge.embeddings.PASSAGE_INDEX_PATH", tmp_path / "passages.jsonl")


def result():
    return {"data": {"nse": 0.8, "series": [1.0, 2.0, 3.0],
                     "_uncertainty": {"nse": {"value": 0.8, "ci_low": 0.7,
                         "ci_high": 0.9, "ci_level": 0.95, "n": 30,
                         "method": "synthetic_fixture"}}},
            "quality_flags": [{"validator": "fixture_check", "status": "pass"}]}


def run_record():
    from ai_hydro.session.evidence import capture_result_evidence
    return {"run_id": "r1", "session_id": "integrity", "tool_name": "fixture",
            "key_outputs": {"nse": 0.8, "series_n": 3},
            "evidence": capture_result_evidence(result())}


def claim():
    return {"id": "c1", "claim": "Synthetic evaluation NSE is 0.8",
            "claim_type": "empirical_result", "status": "supported",
            "confidence": "medium", "confidence_rationale": "Synthetic regression fixture only.",
            "scope": {"basins": ["synthetic"], "period": "2000-2001", "metric": "nse",
                      "basin_refs": [basin_ref("synthetic")]},
            "evidence_spans": [{"source_type": "run", "source_id": "r1", "metric_ref": "nse"}],
            "limitations": ["Synthetic regression case, no real research conclusion."],
            "uncertainty_verified": True}


@pytest.fixture
def session():
    s = HydroSession("integrity")
    s.claims["c1"] = claim()
    s.set("_run_log", {"r1": run_record()})
    s.save()
    return s


def promote():
    # A human approval for the claim's current revision is part of the
    # fixture (ADR-002a): these tests isolate the evidence gates, which run
    # before the approval check, so a blocked result is still the evidence's.
    approve("integrity", "c1")
    return promote_claim_to_registry("integrity", "c1", researcher_approved=True)


def assert_blocked(code):
    response = promote()
    assert response.get("code") == code, response
    assert response["error"] is True
    assert registry.all_entries() == []
    assert not HydroSession.load("integrity").claims["c1"].get("promoted")


def test_valid_backed_claim_and_idempotent_promotion(session):
    first = promote()
    assert first["status"] == "promoted"
    assert promote()["registry_id"] == first["registry_id"]
    entry, = registry.all_entries()
    assert entry["evidence_schema_version"] == 2
    assert entry["evidence_versions"]["r1"].startswith("sha256-v2:")
    assert entry["scope"]["period"] == "2000-2001"
    assert check_registry_staleness("integrity")["fresh_claims"] == ["c1"]


def test_nonexistent_run_does_not_promote(session):
    session.claims["c1"]["evidence_spans"][0]["source_id"] = "invented-run"
    session.save()
    assert_blocked("EVIDENCE_UNRESOLVED")


@pytest.mark.parametrize("field,value", [("session_id", "other-session"), ("run_id", "other-run")])
def test_conflicting_run_identity(session, field, value):
    rec = run_record()
    rec[field] = value
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_IDENTITY_MISMATCH")


def test_run_present_only_in_other_session_cannot_promote(session):
    other = HydroSession("other")
    other.set("_run_log", {"foreign": {"nse": 0.8}})
    other.save()
    session.claims["c1"]["evidence_spans"][0]["source_id"] = "foreign"
    session.save()
    assert_blocked("EVIDENCE_UNRESOLVED")


@pytest.mark.parametrize("value", [None, True, "0.8", [0.8], {}, float("nan"), float("inf"), -float("inf")])
def test_metric_must_be_finite_scalar(session, value):
    rec = run_record()
    rec["evidence"]["data"]["nse"] = value
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_METRIC_UNAVAILABLE")


def test_missing_metric_not_reconstructed_from_current_slot(session):
    rec = run_record()
    del rec["evidence"]["data"]["nse"]
    session.set("_run_log", {"r1": rec})
    session.set("evaluation", result())
    session.save()
    assert_blocked("EVIDENCE_METRIC_UNAVAILABLE")


def test_scope_metric_requires_a_matching_explicit_metric_ref(session):
    session.claims["c1"]["evidence_spans"][0]["metric_ref"] = None
    session.save()
    assert_blocked("EVIDENCE_METRIC_UNAVAILABLE")
    session.claims["c1"]["evidence_spans"][0]["metric_ref"] = "kge"
    session.save()
    assert_blocked("EVIDENCE_METRIC_MISMATCH")


@pytest.mark.parametrize("metric_ref", ["nse", "key_outputs.nse", "data.nse"])
def test_supported_metric_paths(session, metric_ref):
    session.claims["c1"]["evidence_spans"][0]["metric_ref"] = metric_ref
    session.save()
    assert promote()["status"] == "promoted"


@pytest.mark.parametrize("status", ["fail", "failed", "error", "invalid"])
def test_failed_checks_prevent_promotion(session, status):
    rec = run_record()
    rec["evidence"]["quality_flags"][0]["status"] = status
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_CHECK_FAILED")


def test_failed_result_prevents_promotion(session):
    rec = run_record()
    rec["evidence"]["error"] = True
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_CHECK_FAILED")


def test_agent_uncertainty_assertion_is_insufficient(session):
    rec = run_record()
    rec["evidence"]["uncertainty"] = None
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_UNCERTAINTY_UNAVAILABLE")


@pytest.mark.parametrize("field,value", [("value", None), ("ci_low", float("nan")),
    ("ci_high", float("inf")), ("ci_low", 0.95), ("ci_level", 1),
    ("ci_level", 0), ("n", 0), ("n", True), ("n", 2.5), ("method", ""),
    ("method", "none"), ("status", "failed")])
def test_invalid_uncertainty(session, field, value):
    rec = run_record()
    rec["evidence"]["uncertainty"]["nse"][field] = value
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_UNCERTAINTY_INVALID")


def test_uncertainty_for_different_metric_cannot_substitute(session):
    rec = run_record()
    unc = rec["evidence"]["uncertainty"]
    unc["kge"] = unc.pop("nse")
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_UNCERTAINTY_UNAVAILABLE")


def test_uncertainty_point_estimate_must_match(session):
    rec = run_record()
    rec["evidence"]["uncertainty"]["nse"]["value"] = 0.5
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_UNCERTAINTY_MISMATCH")


def test_interval_need_not_contain_point_estimate(session):
    rec = run_record()
    rec["evidence"]["uncertainty"]["nse"]["ci_low"] = 0.81
    session.set("_run_log", {"r1": rec})
    assert promote()["status"] == "promoted"


def test_core_bootstrap_output_round_trips_through_production_writer(session):
    import numpy as np
    from aihydro_core.science.uncertainty import bootstrap_ci

    from ai_hydro.mcp.enforcement import post_run
    data = np.linspace(0.2, 1.4, 40)
    estimate = bootstrap_ci(np.mean, data, n=40, ci=0.9, random_state=7)
    output = {"data": {"sample_mean": float(np.mean(data)),
                       "_uncertainty": {"sample_mean": estimate}}}
    rid = post_run("fixture", session.session_id, output)["_run_id"]
    session.claims["c1"]["claim"] = "Synthetic sample mean is 0.8"
    session.claims["c1"]["scope"]["metric"] = "sample_mean"
    session.claims["c1"]["evidence_spans"] = [
        {"source_type": "run", "source_id": rid, "metric_ref": "sample_mean"}]
    session.save()
    assert promote()["status"] == "promoted"


def test_negative_result_has_same_numerical_evidence_gate(session):
    session.claims["c1"]["claim_type"] = "negative_result"
    session.save()
    rec = run_record()
    rec["evidence"]["uncertainty"] = None
    session.set("_run_log", {"r1": rec})
    assert_blocked("EVIDENCE_UNCERTAINTY_UNAVAILABLE")


@pytest.mark.parametrize("change", ["metric", "uncertainty", "delete"])
def test_changed_or_deleted_run_is_stale(session, change):
    assert promote()["status"] == "promoted"
    rec = run_record()
    if change == "delete":
        with sqlite3.connect(store._run_log_db_path("integrity")) as conn:
            conn.execute("DELETE FROM runs WHERE run_id = ?", ("r1",))
    else:
        if change == "metric":
            rec["evidence"]["data"]["nse"] = 0.1
        else:
            rec["evidence"]["uncertainty"]["nse"]["ci_low"] = 0.2
        session.set("_run_log", {"r1": rec})
    check = check_registry_staleness("integrity")
    assert check["n_stale"] == 1
    assert check["stale_claims"][0]["stale_sources"] == ["r1"]
    assert HydroSession.load("integrity").claims["c1"]["status"] == "stale"


@pytest.mark.parametrize("version", ["r1", "", "old-metadata-hash"])
def test_legacy_registry_versions_require_review_without_rehash_upgrade(session, version):
    registry.append({"registry_id": "legacy", "claim_id": "c1", "session_id": "integrity",
        "status": "promoted", "evidence_spans": claim()["evidence_spans"],
        "evidence_versions": {"r1": version}})
    check = check_registry_staleness("integrity")
    assert check["n_stale"] == 1
    assert check["stale_claims"][0]["reason"] == "legacy_evidence_unverifiable"
    assert registry.all_entries()[0]["evidence_versions"] == {"r1": version}


def test_empty_legacy_registry_entry_is_not_fresh(session):
    registry.append({"registry_id": "legacy", "claim_id": "c1", "session_id": "integrity",
                     "status": "promoted", "evidence_versions": {}})
    assert check_registry_staleness("integrity")["n_stale"] == 1


def test_changed_evidence_gets_new_promotion_identity(session):
    first = promote()["registry_id"]
    rec = run_record()
    rec["evidence"]["uncertainty"]["nse"]["ci_low"] = 0.6
    session.set("_run_log", {"r1": rec})
    second = promote()["registry_id"]
    assert first != second
    assert len(registry.all_entries()) == 2
    check = check_registry_staleness("integrity")
    assert check["n_stale"] == 1
    assert check["fresh_claims"] == ["c1"]
    assert HydroSession.load("integrity").claims["c1"]["status"] == "supported"


def test_restored_evidence_needs_new_entry_after_staleness(session):
    first = promote()["registry_id"]
    rec = run_record()
    rec["evidence"]["data"]["nse"] = 0.1
    session.set("_run_log", {"r1": rec})
    assert check_registry_staleness("integrity")["n_stale"] == 1
    session.set("_run_log", {"r1": run_record()})
    # Explicitly restore supported status after review, not during staleness.
    current = HydroSession.load("integrity")
    current.claims["c1"]["status"] = "supported"
    current.save()
    second = promote()["registry_id"]
    assert first != second
    entries = registry.all_entries()
    assert [e["status"] for e in entries] == ["stale", "promoted"]


def test_dataset_requires_exact_payload_not_manifest_or_fuzzy_slot(session):
    session.set("flow", result())
    session.artifact_manifest["source"] = {"source": "usgs_flow", "content_hash": "unverifiable"}
    spans = [{"source_type": "dataset", "source_id": "usgs_flow"}]
    with pytest.raises(EvidenceError, match="no retained"):
        verified_versions(session, spans)


def test_deleted_dataset_detected(session):
    session.set("flow", result())
    spans = [{"source_type": "dataset", "source_id": "flow", "metric_ref": "nse"}]
    versions = verified_versions(session, spans, require_uncertainty=True)
    session._slots.pop("flow")
    assert registry.check_evidence_staleness(session, versions, spans) == ["flow"]


def test_paper_requires_intact_indexed_passage(session):
    from ai_hydro.knowledge.embeddings import PASSAGE_INDEX_PATH, _passage_hash
    text = "Synthetic methodological passage for a registry regression test."
    pid = _passage_hash(text)
    record = {"passage_hash": pid, "text": text, "doc_name": "fixture.md", "chunk_idx": 0}
    PASSAGE_INDEX_PATH.write_text(json.dumps(record) + "\n")
    spans = [{"source_type": "paper", "source_id": pid}]
    versions = verified_versions(session, spans)
    assert registry.check_evidence_staleness(session, versions, spans) == []
    record["text"] = "Changed text with the old passage hash."
    PASSAGE_INDEX_PATH.write_text(json.dumps(record) + "\n")
    assert registry.check_evidence_staleness(session, versions, spans) == [pid]
    with pytest.raises(EvidenceError):
        verified_versions(session, spans)


def test_old_json_run_log_still_loads_but_missing_ci_cannot_promote(tmp_path):
    path = store._SESSIONS_DIR / "integrity.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"session_id": "integrity", "claims": {"c1": claim()},
                               "_run_log": {"r1": {"nse": 0.8}}}))
    loaded = HydroSession.load("integrity")
    assert loaded.get("_run_log")["r1"] == {"nse": 0.8}
    assert_blocked("EVIDENCE_UNCERTAINTY_UNAVAILABLE")


@pytest.mark.parametrize("writer", ["put_result", "post_run", "legacy_helper"])
def test_writers_retain_numerical_evidence_without_array_counts(session, writer):
    original = result()
    if writer == "put_result":
        original["run_id"] = "captured"
        session.put_result("evaluation", "basin", "params", original)
        rid = "captured"
    elif writer == "legacy_helper":
        from ai_hydro.mcp.helpers import _record_run_log_entry
        original["run_id"] = "captured"
        _record_run_log_entry(session, "evaluation", original)
        rid = "captured"
    else:
        from ai_hydro.mcp.enforcement import post_run
        rid = post_run("fixture", session.session_id, original)["_run_id"]
    retained = session.get("_run_log")[rid]
    assert retained["evidence"]["uncertainty"]["nse"]["value"] == 0.8
    assert retained["evidence"]["quality_flags"][0]["status"] == "pass"
    assert "series_n" not in retained["evidence"]["data"]
    assert "series" not in retained["evidence"]["data"]
    session.claims["c1"]["evidence_spans"][0]["source_id"] = rid
    session.save()
    assert promote()["status"] == "promoted"


def test_summary_array_count_cannot_be_used_as_original_metric(session):
    session.claims["c1"]["scope"]["metric"] = "series_n"
    session.claims["c1"]["evidence_spans"][0]["metric_ref"] = "series_n"
    session.save()
    assert_blocked("EVIDENCE_METRIC_UNAVAILABLE")
