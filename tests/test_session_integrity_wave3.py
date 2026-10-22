"""Science Wave 3, PR-4a: artifact health, one status gate, forcing-scoped comparison claims,
metric conflicts. Offline; fixture sessions live in a temporary session directory."""
from __future__ import annotations

import json

import pytest

import ai_hydro.mcp  # noqa: F401  (registers every tool)
from ai_hydro.claims.status_gate import asserts_reference_difference, stored_claim_flags
from ai_hydro.mcp.tools_ledger import add_claim, update_claim_status
from ai_hydro.mcp.tools_session import start_session
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession
from ai_hydro.skeptic.checks import canonical_metric, find_metric_conflicts, run_all_checks

SID = "w3-integrity"
RAT = "Synthetic fixture rationale, long enough to pass."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)


def _digest(path):
    from aihydro_core.records import digest_bytes
    return digest_bytes(path.read_bytes())


def _stamp(**extra):
    return {"computed_at": "2026-10-01T00:00:00+00:00", **extra}


def _current_signatures(**over):
    data = {"q_mean": 1.0, "flow_quantile_convention": "camels_nonexceedance_v1",
            "_precipitation": {"status": "used", "completeness": 1.0}}
    data.update(over)
    return {"data": data, "meta": _stamp()}


# ------------------------------------------------------------------ slot health

def test_healthy_session_has_ok_slots_and_no_degraded(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps({"q_cms": [1, 2]}))
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"gauge_id": "1", "_data_file": str(f)},
                         "meta": _stamp(retained_series={"path": str(f), "digest": _digest(f)})})
    s.set("signatures", _current_signatures())
    out = s.summary()
    assert out["slot_health"] == {"streamflow": "ok", "signatures": "ok"}
    assert "degraded" not in out and set(out["computed"]) == {"streamflow", "signatures"}


def test_missing_data_file_is_degraded_not_computed(tmp_path):
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"gauge_id": "1", "_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"]["streamflow"] == "missing_artifact"
    assert "streamflow" not in out["computed"]
    d = out["degraded"][0]
    assert d["slot"] == "streamflow" and d["health"] == "missing_artifact" and "gone.json" in d["reason"] and d["fix"]


def test_digest_mismatch_is_degraded_not_computed(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps({"q_cms": [1, 2]}))
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"_data_file": str(f)},
                         "meta": _stamp(retained_series={"path": str(f), "digest": _digest(f)})})
    assert s.summary()["slot_health"]["streamflow"] == "ok"
    f.write_text(json.dumps({"q_cms": [1, 3]}))                 # changed after it was recorded
    out = s.summary()
    assert out["slot_health"]["streamflow"] == "digest_mismatch"
    assert "streamflow" not in out["computed"] and out["degraded"][0]["health"] == "digest_mismatch"


def test_file_without_a_recorded_digest_cannot_be_checked_and_is_ok(tmp_path):
    f = tmp_path / "q.json"
    f.write_text("{}")
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"_data_file": str(f)}, "meta": _stamp()})
    assert s.summary()["slot_health"]["streamflow"] == "ok"


@pytest.mark.parametrize("data,missing", [
    ({"q_mean": 1.0}, "flow_quantile_convention"),                                     # pre-q5/q95 fix
    ({"q_mean": 1.0, "flow_quantile_convention": "camels_nonexceedance_v1"}, "_precipitation.completeness"),
    ({"q_mean": 1.0, "flow_quantile_convention": "camels_nonexceedance_v1",
      "_precipitation": {"status": "used"}}, "_precipitation.completeness"),            # before completeness
])
def test_legacy_signatures_slot_is_degraded(data, missing):
    s = HydroSession(SID)
    s.set("signatures", {"data": data, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"]["signatures"] == "legacy_convention"
    assert "signatures" not in out["computed"]
    assert missing in out["degraded"][0]["reason"]


def test_worst_health_wins_and_other_slots_keep_theirs(tmp_path):
    s = HydroSession(SID)
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": _stamp()})
    s.set("streamflow", {"data": {"_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    s.set("watershed", {"data": {"area_km2": 5.0}, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"] == {"signatures": "legacy_convention", "streamflow": "missing_artifact",
                                  "watershed": "ok"}
    assert out["computed"] == ["watershed"] and len(out["degraded"]) == 2


def test_start_session_surfaces_degraded_slots(tmp_path):
    s = HydroSession(SID)
    s.set("streamflow", {"data": {"_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": _stamp()})
    s.save()
    out = start_session(session_id=SID)
    assert {d["slot"]: d["health"] for d in out["degraded"]} == {
        "streamflow": "missing_artifact", "signatures": "legacy_convention"}
    assert out["computed"] == [] and out["slot_health"]["signatures"] == "legacy_convention"


# ------------------------------------------------------------------ evidence gate

def _claim(status="tested", claim_id="c1", **over):
    args = dict(session_id=SID, claim_id=claim_id, statement="Baseflow dominates summer low flow.",
                claim_type="empirical_result", status=status, confidence="medium",
                confidence_rationale=RAT, basins=["01013500"], period="2000-2010")
    args.update(over)
    return add_claim(**args)


@pytest.mark.parametrize("status", ["supported", "weakly_supported"])
def test_supported_claim_with_no_span_is_refused_with_a_teaching_error(status):
    HydroSession(SID).save()
    res = _claim(status)
    assert res["error"] == "evidence_gate" and res["requested_status"] == status
    assert "_traceback" not in res
    assert "evidence_spans" in res["teaching_error"]["how_to_fix"]
    assert "c1" not in HydroSession.load(SID).claims                  # nothing stored


def test_update_to_supported_without_span_is_refused_and_leaves_the_claim():
    HydroSession(SID).save()
    assert _claim("proposed")["status"] == "recorded"
    res = update_claim_status(SID, "c1", "supported", "medium", RAT)
    assert res["error"] == "evidence_gate"
    assert HydroSession.load(SID).claims["c1"]["status"] == "proposed"


def test_supported_claim_with_a_span_and_other_statuses_pass():
    HydroSession(SID).save()
    span = [{"source_type": "run", "source_id": "r1"}]
    assert _claim("supported", evidence_spans=span)["status"] == "recorded"
    assert _claim("weakly_supported", claim_id="c2", evidence_spans=span)["status"] == "recorded"
    for i, st in enumerate(["proposed", "tested", "inconclusive", "contradicted"]):
        assert _claim(st, claim_id=f"o{i}")["status"] == "recorded"


def test_stored_supported_claim_without_span_is_flagged_at_start_not_rewritten():
    s = HydroSession(SID)
    s.claims["old"] = {"id": "old", "claim": "Stored before the gate existed.", "claim_type": "empirical_result",
                       "status": "supported", "confidence": "medium", "confidence_rationale": RAT,
                       "scope": {"basins": []}, "evidence_spans": [], "limitations": []}
    s.claims["fine"] = {**s.claims["old"], "id": "fine", "evidence_spans": [{"source_type": "run", "source_id": "r1"}]}
    s.save()
    out = start_session(session_id=SID)
    assert [f["claim_id"] for f in out["claim_flags"]] == ["old"]
    assert out["claim_flags"][0]["code"] == "evidence_gate" and out["claim_flags"][0]["how_to_fix"]
    assert HydroSession.load(SID).claims["old"]["status"] == "supported"        # never rewritten


# ------------------------------------------------------------------ forcing-scope gate

EXCLUDES_FORCING = {"varies": ["water_year_sample"], "assumes": ["exchangeable_years"],
                    "excludes": ["forcing_product", "rating_measurement", "method_definition"]}
SAMPLING_AND_FORCING = {"varies": ["water_year_sample", "forcing_product"], "assumes": [], "excludes": ["rating_measurement"]}
DIFF_CLAIM = "The runoff ratio of 0.605 is higher than the CAMELS reference value of 0.576."


def _seed_run(scope, run_id="r_rr", metric="runoff_ratio"):
    s = HydroSession(SID)
    ci = {"value": 0.605, "ci_low": 0.58, "ci_high": 0.63, "ci_level": 0.9, "n": 20, "method": "fixture"}
    if scope is not None:
        ci["scope"] = scope
    s.set("_run_log", {run_id: {"run_id": run_id, "session_id": SID, "tool_name": "fixture",
                                "timestamp": "2026-10-01T00:00:00+00:00",
                                "key_outputs": {metric: 0.605}, "evidence": {"uncertainty": {metric: ci}}}})
    s.save()


def _propose(statement, run_id="r_rr", metric="runoff_ratio"):
    r = _claim("proposed", statement=statement,
               evidence_spans=[{"source_type": "run", "source_id": run_id, "metric_ref": metric}])
    assert r["status"] == "recorded", r


def _support(**kw):
    return update_claim_status(SID, "c1", "supported", "medium", RAT, uncertainty_verified=True, **kw)


def test_difference_from_reference_on_a_forcing_excluded_ci_is_refused():
    _seed_run(EXCLUDES_FORCING)
    _propose(DIFF_CLAIM)
    res = _support()
    assert res["error"] == "forcing_scope_gate" and res["metrics"] == [{"metric_ref": "runoff_ratio", "run_id": "r_rr"}]
    assert "forcing_product" in res["teaching_error"]["explanation"] and "_traceback" not in res
    assert HydroSession.load(SID).claims["c1"]["status"] == "proposed"
    weak = update_claim_status(SID, "c1", "weakly_supported", "low", RAT)
    assert weak["error"] == "forcing_scope_gate"


def test_downgraded_statuses_and_wording_that_does_not_assert_a_difference_pass():
    _seed_run(EXCLUDES_FORCING)
    _propose(DIFF_CLAIM)
    assert update_claim_status(SID, "c1", "tested", "low", RAT)["status"] == "updated"
    assert update_claim_status(SID, "c1", "inconclusive", "low", RAT)["status"] == "updated"
    r = _claim("proposed", claim_id="c2", statement="The runoff ratio is 0.605 over water years 2000-2020.",
               evidence_spans=[{"source_type": "run", "source_id": "r_rr", "metric_ref": "runoff_ratio"}])
    assert r["status"] == "recorded"
    ok = update_claim_status(SID, "c2", "supported", "medium", RAT, uncertainty_verified=True)
    assert ok["status"] == "updated"
    _claim("proposed", claim_id="c3", statement="The runoff ratio does not differ from the CAMELS reference.",
           evidence_spans=[{"source_type": "run", "source_id": "r_rr", "metric_ref": "runoff_ratio"}])
    # (a statement of no difference is not the overclaim this gate targets)
    assert update_claim_status(SID, "c3", "supported", "medium", RAT, uncertainty_verified=True)["status"] == "updated"


@pytest.mark.parametrize("scope", [
    SAMPLING_AND_FORCING,                                   # forcing is varied, not excluded
    None,                                                   # no scope metadata: nothing known, nothing claimed
    "inter-annual sampling only; excludes forcing-product and rating error",   # older free text
    {"excludes": "forcing_product"},                        # malformed
])
def test_gate_needs_the_documented_scope_shape_to_fire(scope):
    _seed_run(scope)
    _propose(DIFF_CLAIM)
    assert _support()["status"] == "updated"


def test_gate_is_keyed_on_scope_not_on_metric_or_tool_names():
    _seed_run(EXCLUDES_FORCING, run_id="r_x", metric="some_new_metric")
    _propose("some_new_metric is larger than the published benchmark.", run_id="r_x", metric="some_new_metric")
    assert _support()["error"] == "forcing_scope_gate"


def test_reference_difference_heuristic():
    assert asserts_reference_difference(DIFF_CLAIM)
    assert asserts_reference_difference("Our BFI is lower than the benchmark value.")
    assert not asserts_reference_difference("The runoff ratio is 0.605.")
    assert not asserts_reference_difference("The value does not differ from the reference.")
    assert not asserts_reference_difference("Peak flow is higher in spring than in autumn.")   # no reference


def test_stored_forcing_claim_is_flagged_at_start():
    _seed_run(EXCLUDES_FORCING)
    s = HydroSession.load(SID)
    s.claims["old"] = {"id": "old", "claim": DIFF_CLAIM, "claim_type": "empirical_result", "status": "supported",
                       "confidence": "medium", "confidence_rationale": RAT, "scope": {"basins": []},
                       "evidence_spans": [{"source_type": "run", "source_id": "r_rr", "metric_ref": "runoff_ratio"}],
                       "limitations": []}
    s.save()
    flags = stored_claim_flags(HydroSession.load(SID))
    assert [(f["claim_id"], f["code"]) for f in flags] == [("old", "forcing_scope_gate")]
    assert start_session(session_id=SID)["claim_flags"][0]["claim_id"] == "old"
    assert HydroSession.load(SID).claims["old"]["status"] == "supported"


# ------------------------------------------------------------------ metric conflicts

def _row(run_id, outputs, tool="signatures", ts="2026-10-01T00:00:00+00:00", **inputs):
    return {"run_id": run_id, "session_id": SID, "tool_name": tool, "timestamp": ts,
            "key_outputs": outputs, "inputs": {"gauge_id": "01013500", "start_date": "2000-01-01",
                                               "end_date": "2020-12-31", **inputs}}


def _log(*rows):
    s = HydroSession(SID)
    s.set("_run_log", {r["run_id"]: r for r in rows})
    s.save()
    return HydroSession.load(SID)


def test_aliases_resolve():
    assert canonical_metric("BFI") == canonical_metric("baseflow-index") == canonical_metric("baseflow_index")
    assert canonical_metric("q_mean") == "q_mean"


def test_bfi_under_two_methods_is_flagged_across_aliases():
    s = _log(_row("r1", {"bfi": 0.357, "baseflow_method": "legacy_lyne_hollick"}, tool="extract_hydrological_signatures"),
             _row("r2", {"baseflow_index": 0.445}, tool="separate_baseflow", method="lyne_hollick_ladson2013"))
    [c] = find_metric_conflicts(s)
    assert c["metric"] == "baseflow_index" and c["feature"] == "01013500" and c["methods_differ"]
    assert {o["value"] for o in c["observations"]} == {0.357, 0.445}
    assert {o["method"] for o in c["observations"]} == {"legacy_lyne_hollick", "lyne_hollick_ladson2013"}
    report = run_all_checks(s, "")
    assert any(i.issue_type == "metric_conflict" and "baseflow_index" in i.description for i in report.issues)
    assert report.verdict == "advisory"
    HydroSession.load(SID).save()
    out = start_session(session_id=SID)
    assert out["metric_conflicts"][0]["metric"] == "baseflow_index"


def test_agreement_other_periods_other_features_and_recomputes_are_not_conflicts():
    s = _log(_row("r1", {"bfi": 0.445}, tool="a"),
             _row("r2", {"baseflow_index": 0.4452}, tool="b"),                         # agrees within tolerance
             _row("r3", {"bfi": 0.30}, tool="c", end_date="2010-12-31"),               # another period
             _row("r4", {"bfi": 0.31}, tool="d", gauge_id="01031500"),                 # another feature
             _row("r5", {"bfi": 0.10}, tool="a", ts="2026-09-01T00:00:00+00:00"))      # older run of tool a
    assert find_metric_conflicts(s) == []
    out = start_session(session_id=SID)
    assert "metric_conflicts" not in out


def test_latest_run_per_tool_is_what_counts():
    s = _log(_row("r1", {"bfi": 0.357}, tool="a", ts="2026-09-01T00:00:00+00:00"),
             _row("r2", {"bfi": 0.445}, tool="a", ts="2026-10-01T00:00:00+00:00"),
             _row("r3", {"baseflow_index": 0.445}, tool="b"))
    assert find_metric_conflicts(s) == []
