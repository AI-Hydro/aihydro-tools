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
NEW_SIGNATURE_MARKER = {"signature_definitions": {"q5": {"formula": "fixture"}}}   # watershed >= 0.2.0 shape
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

def _session(tmp_path):
    s = HydroSession(SID)
    s.workspace_dir = str(tmp_path)
    return s


def _streamflow(f, **meta):
    return {"data": {"gauge_id": "1", "_data_file": str(f)},
            "meta": _stamp(retained_series={"path": str(f), "digest": _digest(f)}, **meta)}


def test_healthy_session_has_ok_slots_and_no_degraded(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps({"q_cms": [1, 2]}))
    s = _session(tmp_path)
    s.set("streamflow", _streamflow(f))
    s.set("signatures", _current_signatures())
    out = s.summary()
    assert out["slot_health"] == {"streamflow": "ok", "signatures": "ok"}
    assert "degraded" not in out and set(out["computed"]) == {"streamflow", "signatures"}


def test_missing_data_file_is_degraded_not_computed(tmp_path):
    s = _session(tmp_path)
    s.set("streamflow", {"data": {"gauge_id": "1", "_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"]["streamflow"] == "missing_artifact"
    assert "streamflow" not in out["computed"]
    d = out["degraded"][0]
    assert d["slot"] == "streamflow" and d["health"] == "missing_artifact" and "gone.json" in d["reason"] and d["fix"]


def test_digest_mismatch_is_degraded_not_computed(tmp_path):
    f = tmp_path / "q.json"
    f.write_text(json.dumps({"q_cms": [1, 2]}))
    s = _session(tmp_path)
    s.set("streamflow", _streamflow(f))
    assert s.summary()["slot_health"]["streamflow"] == "ok"
    f.write_text(json.dumps({"q_cms": [1, 3]}))                 # changed after it was recorded
    out = s.summary()
    assert out["slot_health"]["streamflow"] == "digest_mismatch"
    assert "streamflow" not in out["computed"] and out["degraded"][0]["health"] == "digest_mismatch"


@pytest.mark.parametrize("meta", [
    {},                                                                 # streamflow slot, nothing recorded
    {"retained_series": {"path": "SELF"}},                              # path but no digest
    {"retained_series": {"path": "/elsewhere/q.json", "digest": "sha256:" + "0" * 64}},   # other path
])
def test_retained_file_without_a_matching_digest_reads_digest_missing(tmp_path, meta):
    f = tmp_path / "q.json"
    f.write_text("{}")
    meta = json.loads(json.dumps(meta).replace("SELF", str(f)))
    s = _session(tmp_path)
    s.set("streamflow", {"data": {"_data_file": str(f)}, "meta": _stamp(**meta)})
    out = s.summary()
    assert out["slot_health"]["streamflow"] == "digest_missing" and "streamflow" not in out["computed"]


def test_relative_data_file_resolves_against_the_session_dirs(tmp_path):
    (tmp_path / "ws").mkdir()
    f = tmp_path / "ws" / "q.json"
    f.write_text("{}")
    s = HydroSession(SID)
    s.workspace_dir = str(tmp_path / "ws")
    s.set("forcing", {"data": {"_data_file": "q.json"}, "meta": _stamp()})
    assert s.summary()["slot_health"]["forcing"] == "ok"


def test_data_file_outside_the_session_dirs_is_refused_even_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.json"
    secret.write_text("{}")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link.json").symlink_to(secret)
    s = HydroSession(SID)
    s.workspace_dir = str(ws)
    s.set("forcing", {"data": {"_data_file": str(secret)}, "meta": _stamp()})
    s.set("evap", {"data": {"_data_file": str(ws / "link.json")}, "meta": _stamp()})
    h = s.slot_health()
    assert h["forcing"]["health"] == "missing_artifact" and "outside_session_dir" in h["forcing"]["reason"]
    assert h["evap"]["health"] == "missing_artifact"


def test_digest_is_cached_by_size_and_mtime(tmp_path, monkeypatch):
    f = tmp_path / "q.json"
    f.write_text(json.dumps({"q": [1, 2, 3]}))
    s = _session(tmp_path)
    s.set("streamflow", _streamflow(f))
    s.summary()
    calls = []
    real = store.hashlib.sha256
    monkeypatch.setattr(store.hashlib, "sha256", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert s.summary()["slot_health"]["streamflow"] == "ok"
    assert calls == []                                              # second look hashed nothing


@pytest.mark.parametrize("data", [
    {"q_mean": 1.0},                                                        # pre-q5/q95 fix: no convention
    {"q_mean": 1.0, **NEW_SIGNATURE_MARKER, "flow_quantile_convention": "camels_nonexceedance_v1"},   # 0.2.0, no report
])
def test_legacy_signatures_slot_is_degraded(data):
    s = HydroSession(SID)
    s.set("signatures", {"data": data, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"]["signatures"] == "legacy_convention"
    assert "signatures" not in out["computed"] and "signatures" in out["pending"]
    assert out["degraded"][0]["reason"]


def test_fresh_slot_from_the_released_watershed_is_ok():
    """aihydro-watershed 0.1.x: convention key, no signature_definitions, no _precipitation."""
    s = HydroSession(SID)
    s.set("signatures", {"data": {"q_mean": 1.0, "flow_quantile_convention": "camels_nonexceedance_v1"},
                         "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"]["signatures"] == "ok" and "signatures" in out["computed"]
    assert "signatures" not in out["pending"]


@pytest.mark.parametrize("prec", [
    {"status": "unavailable", "completeness": None, "reason": "no network"},
    {"status": "not_attempted", "completeness": None},
    {"status": "skip"},
    {"completeness": None},
    {"status": "used", "completeness": 0.97},
])
def test_current_slot_with_a_precipitation_report_is_ok_even_when_completeness_is_none(prec):
    s = HydroSession(SID)
    s.set("signatures", {"data": {"q_mean": 1.0, **NEW_SIGNATURE_MARKER,
                                  "flow_quantile_convention": "camels_nonexceedance_v1", "_precipitation": prec},
                         "meta": _stamp()})
    assert s.summary()["slot_health"]["signatures"] == "ok"


def test_degraded_slot_is_pending_and_not_counted_as_evidence(tmp_path):
    s = _session(tmp_path)
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": _stamp()})
    s.set("watershed", {"data": {"area_km2": 5.0}, "meta": _stamp()})
    s.save()
    assert "signatures" in s.pending() and "watershed" not in s.pending()
    assert s.computed_healthy() == ["watershed"]
    from ai_hydro.mcp.tools_session import get_session_health
    assert get_session_health(SID)["n_computed_slots"] == 1


def test_worst_health_wins_and_other_slots_keep_theirs(tmp_path):
    s = _session(tmp_path)
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": _stamp()})
    s.set("streamflow", {"data": {"_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    s.set("watershed", {"data": {"area_km2": 5.0}, "meta": _stamp()})
    out = s.summary()
    assert out["slot_health"] == {"signatures": "legacy_convention", "streamflow": "missing_artifact",
                                  "watershed": "ok"}
    assert out["computed"] == ["watershed"] and len(out["degraded"]) == 2


def test_start_session_surfaces_degraded_slots(tmp_path):
    s = _session(tmp_path)
    s.set("streamflow", {"data": {"_data_file": str(tmp_path / "gone.json")}, "meta": _stamp()})
    s.set("signatures", {"data": {"q_mean": 1.0}, "meta": _stamp()})
    s.save()
    out = start_session(session_id=SID)
    assert {d["slot"]: d["health"] for d in out["degraded"]} == {
        "streamflow": "missing_artifact", "signatures": "legacy_convention"}
    assert out["computed"] == [] and out["slot_health"]["signatures"] == "legacy_convention"


# ------------------------------------------------------------------ evidence gate

def _run(run_id="r1", outputs=None, evidence=None, **row):
    """A run-log row as a finished tool run leaves it (``outputs=None`` -> a plain numeric output)."""
    entry = {"run_id": run_id, "session_id": SID, "tool_name": "fixture", "timestamp": "2026-10-01T00:00:00+00:00",
             "key_outputs": {"baseflow_index": 0.4} if outputs is None else outputs, **row}
    if evidence is not None:
        entry["evidence"] = evidence
    return entry


def _seed(*rows):
    s = HydroSession.load(SID)
    s.set("_run_log", {r["run_id"]: r for r in rows})
    s.save()


def _claim(status="tested", claim_id="c1", **over):
    args = dict(session_id=SID, claim_id=claim_id, statement="Baseflow dominates summer low flow.",
                claim_type="empirical_result", status=status, confidence="medium",
                confidence_rationale=RAT, basins=["01013500"], period="2000-2010")
    args.update(over)
    return add_claim(**args)


def _span(run_id="r1", metric=None):
    d = {"source_type": "run", "source_id": run_id}
    if metric:
        d["metric_ref"] = metric
    return [d]


@pytest.mark.parametrize("status", ["supported", "weakly_supported"])
def test_supported_claim_with_no_span_is_refused_with_a_teaching_error(status):
    HydroSession(SID).save()
    res = _claim(status)
    assert res["error"] == "evidence_gate" and res["requested_status"] == status
    assert "_traceback" not in res
    how = res["teaching_error"]["how_to_fix"]
    assert "evidence_spans" in how and "'lit'" not in how                # EvidenceSpan has no 'lit' type
    assert "c1" not in HydroSession.load(SID).claims                    # nothing stored


@pytest.mark.parametrize("label,span,seed", [
    ("nonexistent run", _span("ghost"), None),
    ("empty source_id", [{"source_type": "run", "source_id": ""}], None),
    ("blank source_id", [{"source_type": "paper", "source_id": "  "}], None),
    ("hollow run: no outputs", _span("r1"), _run("r1", outputs={})),
    ("hollow run: error", _span("r1"), _run("r1", error=True)),
    ("hollow run: middleware row", _span("r1"), _run("r1", minimal=True)),
    ("metric missing from the run", _span("r1", "nse"), _run("r1")),
])
def test_junk_spans_are_refused(label, span, seed):
    HydroSession(SID).save()
    if seed:
        _seed(seed)
    res = _claim("weakly_supported", evidence_spans=span)
    assert res["error"] == "evidence_gate", label
    assert res["span_problems"] and "_traceback" not in res
    assert "c1" not in HydroSession.load(SID).claims


def test_one_junk_span_among_real_ones_is_still_refused():
    HydroSession(SID).save()
    _seed(_run("r1"))
    res = _claim("weakly_supported", evidence_spans=_span("r1") + _span("ghost"))
    assert res["error"] == "evidence_gate" and [p["index"] for p in res["span_problems"]] == [1]


def test_real_run_paper_and_dataset_spans_are_accepted():
    HydroSession(SID).save()
    _seed(_run("r1", {"baseflow_index": 0.4}))
    assert _claim("weakly_supported", evidence_spans=_span("r1", "baseflow_index"))["status"] == "recorded"
    assert _claim("weakly_supported", claim_id="c2",
                  evidence_spans=_span("r1", "key_outputs.baseflow_index"))["status"] == "recorded"
    assert _claim("supported", claim_id="c3", evidence_spans=_span("r1"))["status"] == "recorded"
    assert _claim("weakly_supported", claim_id="p", evidence_spans=[
        {"source_type": "paper", "source_id": "addor2017camels"}])["status"] == "recorded"
    assert _claim("weakly_supported", claim_id="d", evidence_spans=[
        {"source_type": "dataset", "source_id": "camels-us"}])["status"] == "recorded"


def test_update_to_supported_without_real_evidence_is_refused_and_leaves_the_claim():
    HydroSession(SID).save()
    assert _claim("proposed")["status"] == "recorded"
    res = update_claim_status(SID, "c1", "supported", "medium", RAT)
    assert res["error"] == "evidence_gate"
    assert HydroSession.load(SID).claims["c1"]["status"] == "proposed"


def test_other_statuses_need_no_evidence():
    HydroSession(SID).save()
    for i, st in enumerate(["proposed", "tested", "inconclusive", "contradicted"]):
        assert _claim(st, claim_id=f"o{i}")["status"] == "recorded"


def test_stored_supported_claim_without_real_evidence_is_flagged_not_rewritten_and_cannot_be_re_saved_supported():
    _seed(_run("r1"))
    s = HydroSession.load(SID)
    base = {"claim": "Stored before the gate existed.", "claim_type": "empirical_result",
            "status": "supported", "confidence": "medium", "confidence_rationale": RAT,
            "scope": {"basins": ["01013500"], "period": "2000-2010"}, "limitations": []}
    s.claims["old"] = {**base, "id": "old", "evidence_spans": []}
    s.claims["junk"] = {**base, "id": "junk", "evidence_spans": _span("ghost")}
    s.claims["fine"] = {**base, "id": "fine", "evidence_spans": _span("r1")}
    s.save()
    out = start_session(session_id=SID)
    assert sorted(f["claim_id"] for f in out["claim_flags"]) == ["junk", "old"]
    assert all(f["code"] == "evidence_gate" and f["how_to_fix"] for f in out["claim_flags"])
    assert HydroSession.load(SID).claims["old"]["status"] == "supported"        # never rewritten
    # N5: re-saving it as supported (even to change confidence) is refused until evidence is added
    assert update_claim_status(SID, "old", "supported", "high", RAT)["error"] == "evidence_gate"
    assert update_claim_status(SID, "old", "tested", "high", RAT)["status"] == "updated"


# ------------------------------------------------------------------ forcing / comparison gate

EXCLUDES_FORCING = {"varies": ["water_year_sample"], "assumes": ["exchangeable_years"],
                    "excludes": ["forcing_product", "rating_measurement", "method_definition"]}
SAMPLING_AND_FORCING = {"varies": ["water_year_sample", "forcing_product"], "assumes": [], "excludes": ["rating_measurement"]}
DIFF_CLAIM = "The runoff ratio of 0.605 is higher than the CAMELS reference value of 0.576."
PLAIN_CLAIM = "The runoff ratio is 0.605 over water years 2000-2020."
COMPARISON = {"runoff_ratio": {"can_claim_difference": True, "forcing_differs": True}}


def _rr_run(scope, run_id="r_rr", metric="runoff_ratio", comparison=COMPARISON):
    ci = {"value": 0.605, "ci_low": 0.58, "ci_high": 0.63, "ci_level": 0.9, "n": 20, "method": "fixture"}
    if scope is not None:
        ci["scope"] = scope
    outputs = {metric: 0.605}
    if comparison is not None:
        outputs["reference_comparison"] = comparison
    return _run(run_id, outputs, evidence={"uncertainty": {metric: ci}})


def _propose(statement, claim_id="c1", run_id="r_rr", metric="runoff_ratio", **over):
    r = _claim("proposed", claim_id=claim_id, statement=statement, evidence_spans=_span(run_id, metric), **over)
    assert r["status"] == "recorded", r


def _support(claim_id="c1"):
    return update_claim_status(SID, claim_id, "supported", "medium", RAT, uncertainty_verified=True)


def test_plain_claim_citing_a_run_with_a_comparison_block_and_forcing_excluded_ci_is_accepted():
    _seed(_rr_run(EXCLUDES_FORCING))            # run carries reference_comparison AND a forcing-excluded CI
    _propose(PLAIN_CLAIM)
    assert _support()["status"] == "updated"


def test_the_same_claim_with_comparison_ref_is_refused_whatever_the_wording():
    _seed(_rr_run(EXCLUDES_FORCING))
    _propose(PLAIN_CLAIM, comparison_ref="CAMELS-US v1.2")      # no "differs" in the prose: the gate is structural
    res = _support()
    assert res["error"] == "forcing_scope_gate"
    assert res["metrics"][0]["metric_ref"] == "runoff_ratio" and res["metrics"][0]["run_id"] == "r_rr"
    assert "forcing_product" in res["teaching_error"]["explanation"] and "_traceback" not in res
    assert HydroSession.load(SID).claims["c1"]["status"] == "proposed"
    assert update_claim_status(SID, "c1", "weakly_supported", "low", RAT)["error"] == "forcing_scope_gate"
    assert update_claim_status(SID, "c1", "tested", "low", RAT)["status"] == "updated"
    assert update_claim_status(SID, "c1", "inconclusive", "low", RAT)["status"] == "updated"


def test_a_claim_with_a_comparison_ref_is_a_comparison_even_if_the_run_is_not():
    _seed(_rr_run(EXCLUDES_FORCING, comparison=None))
    _propose(PLAIN_CLAIM)
    assert _support()["status"] == "updated"                    # not a comparison: sampling interval is fine
    _propose(DIFF_CLAIM, claim_id="c2", comparison_ref="CAMELS-US v1.2")
    res = _support("c2")
    assert res["error"] == "forcing_scope_gate"
    stored = HydroSession.load(SID).claims["c2"]
    assert stored["comparison_ref"] == "CAMELS-US v1.2" and HydroSession.load(SID).claims["c1"]["comparison_ref"] is None


def test_comparison_ref_is_sealed_into_the_revision_only_when_set():
    from ai_hydro.approval.records import claim_revision_fields
    base = {"id": "x", "claim": "s", "claim_type": "empirical_result", "status": "tested", "confidence": "low",
            "confidence_rationale": RAT, "scope": {"basins": [], "period": "2000"}, "limitations": []}
    assert "comparison_ref" not in claim_revision_fields(base, {})
    assert claim_revision_fields({**base, "comparison_ref": "CAMELS-US v1.2"}, {})["comparison_ref"] == "CAMELS-US v1.2"


NOT_CLAIMABLE = {"runoff_ratio": {"can_claim_difference": False, "reason": "period differs"}}


def test_can_claim_difference_false_refuses_a_comparison_claim():
    _seed(_rr_run(None, comparison=NOT_CLAIMABLE))
    _propose(DIFF_CLAIM, comparison_ref="CAMELS-US v1.2")
    res = _support()
    assert res["error"] == "comparison_gate" and "can_claim_difference=false" in res["teaching_error"]["explanation"]
    # other table shapes
    _seed(_rr_run(None, comparison={"table": [{"metric": "runoff_ratio", "can_claim_difference": False}]}))
    assert _support()["error"] == "comparison_gate"


def test_can_claim_difference_false_does_not_block_a_plain_claim_citing_the_metric():
    _seed(_rr_run(None, comparison=NOT_CLAIMABLE))
    _propose(PLAIN_CLAIM)                                       # "the value is 0.605": no comparison_ref
    assert _support()["status"] == "updated"


def test_a_ci_that_varies_the_forcing_product_does_not_trigger_the_gate():
    _seed(_rr_run(SAMPLING_AND_FORCING))                        # forcing is varied, not excluded
    _propose(DIFF_CLAIM, comparison_ref="CAMELS-US v1.2")
    assert _support()["status"] == "updated"


@pytest.mark.parametrize("scope", [
    None,                                                   # a cited CI with no scope: core says "excluded"
    "inter-annual sampling only; excludes forcing-product and rating error",   # older free text
    {"excludes": "forcing_product"},                        # malformed
    {"varies": ["water_year_sample"], "assumes": [], "excludes": "forcing_product"},
    {"varies": "forcing_product", "excludes": []},          # varies is not a list: malformed, so excluded
])
def test_gate_uses_cores_conservative_scope_excludes_absent_or_malformed_scope_excludes_forcing(scope):
    """Reconciled with aihydro-core ``scope_excludes`` (PR-4b): producers now emit scope, so a CI without a
    well-formed scope that VARIES the forcing product no longer lets a comparison claim through."""
    _seed(_rr_run(scope))
    _propose(DIFF_CLAIM, comparison_ref="CAMELS-US v1.2")
    res = _support()
    assert res["error"] == "forcing_scope_gate"


def test_scope_excludes_is_cores_function_not_a_second_implementation():
    from aihydro_core.science import scope_excludes as core
    from ai_hydro.claims.status_gate import scope_excludes as tools
    for scope in (EXCLUDES_FORCING, SAMPLING_AND_FORCING, None, "text", {"excludes": "forcing_product"},
                  {"varies": [], "excludes": []}, {"varies": ["forcing_product"], "excludes": ["forcing_product"]}):
        assert tools({"scope": scope}) == core(scope, "forcing_product"), scope
    assert tools(None) is True                                   # not a CI entry at all: conservative


def test_a_cited_metric_with_no_ci_entry_is_not_the_forcing_gates_business():
    _seed(_run("r_noci", {"runoff_ratio": 0.605}))             # no uncertainty block at all
    _propose(DIFF_CLAIM, run_id="r_noci", comparison_ref="CAMELS-US v1.2")
    assert _support()["status"] == "updated"                    # (the uncertainty gate asks for a CI separately)


def test_gate_is_keyed_on_scope_not_on_metric_or_tool_names():
    _seed(_rr_run(EXCLUDES_FORCING, run_id="r_x", metric="some_new_metric",
                  comparison={"some_new_metric": {"can_claim_difference": True}}))
    _propose("some_new_metric is larger than the published benchmark.", run_id="r_x", metric="some_new_metric",
             comparison_ref="benchmark-1")
    assert _support()["error"] == "forcing_scope_gate"


def test_prose_is_only_an_advisory_hint_never_the_gate():
    _seed(_rr_run(EXCLUDES_FORCING, comparison=None))           # not a structural comparison
    _propose(DIFF_CLAIM)
    ok = _support()
    assert ok["status"] == "updated"                           # prose alone does not block
    assert ok["warnings"][0]["code"] == "FORCING_SCOPE_PROSE" and ok["warnings"][0]["blocking"] is False


def test_reference_difference_heuristic():
    assert asserts_reference_difference(DIFF_CLAIM)
    assert asserts_reference_difference("Our BFI is lower than the benchmark value.")
    assert asserts_reference_difference("The ratio does not agree with the CAMELS value.")
    assert asserts_reference_difference("The ratio doesn't match the published value.")
    assert not asserts_reference_difference("The runoff ratio is 0.605.")
    assert not asserts_reference_difference("The value does not differ from the reference.")
    assert not asserts_reference_difference("The value is not inconsistent with the reference.")
    assert not asserts_reference_difference("Peak flow is higher in spring than in autumn.")   # no reference


def test_stored_comparison_claim_is_flagged_at_start():
    _seed(_rr_run(EXCLUDES_FORCING))
    s = HydroSession.load(SID)
    s.claims["old"] = {"id": "old", "claim": DIFF_CLAIM, "claim_type": "empirical_result", "status": "supported",
                       "confidence": "medium", "confidence_rationale": RAT, "scope": {"basins": []},
                       "evidence_spans": _span("r_rr", "runoff_ratio"), "limitations": [],
                       "comparison_ref": "CAMELS-US v1.2"}
    s.save()
    flags = stored_claim_flags(HydroSession.load(SID))
    assert [(f["claim_id"], f["code"]) for f in flags] == [("old", "forcing_scope_gate")]
    assert start_session(session_id=SID)["claim_flags"][0]["claim_id"] == "old"
    assert HydroSession.load(SID).claims["old"]["status"] == "supported"


# ------------------------------------------------------------------ metric conflicts

def _row(run_id, outputs, tool="signatures", ts="2026-10-01T00:00:00+00:00", **inputs):
    inp = {"gauge_id": "01013500", "start_date": "2000-01-01", "end_date": "2020-12-31", **inputs}
    inp = {k: v for k, v in inp.items() if v is not None}
    return {"run_id": run_id, "session_id": SID, "tool_name": tool, "timestamp": ts,
            "key_outputs": outputs, "inputs": inp}


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
    out = start_session(session_id=SID)
    assert out["metric_conflicts"][0]["metric"] == "baseflow_index"


def test_model_skill_metrics_are_not_compared_across_models():
    s = _log(_row("r1", {"nse": 0.81}, tool="train_lstm", model="lstm"),
             _row("r2", {"nse": 0.55}, tool="train_hbv", model="hbv"),
             _row("r3", {"mean": 1.0, "value": 3.0}, tool="a"), _row("r4", {"mean": 2.0, "value": 9.0}, tool="b"))
    assert find_metric_conflicts(s) == []


def test_same_signature_from_two_models_is_keyed_by_model_identity():
    s = _log(_row("r1", {"q_mean": 1.0}, tool="sim", model="lstm"),
             _row("r2", {"q_mean": 1.0}, tool="sim", model="hbv", ts="2026-10-02T00:00:00+00:00"))
    assert find_metric_conflicts(s) == []                      # same value, distinct sources: concordant
    s = _log(_row("r1", {"q_mean": 1.0}, tool="sim", model="lstm"),
             _row("r2", {"q_mean": 2.0}, tool="sim", model="hbv"))
    [c] = find_metric_conflicts(s)
    assert {o["method"] for o in c["observations"]} == {"sim/lstm", "sim/hbv"}


def test_unknown_period_runs_are_not_flagged():
    s = _log(_row("r1", {"bfi": 0.357}, tool="a", start_date=None, end_date=None),
             _row("r2", {"baseflow_index": 0.445}, tool="b", start_date=None, end_date=None))
    assert find_metric_conflicts(s) == []


def test_years_and_water_year_are_part_of_the_period():
    s = _log(_row("r1", {"bfi": 0.357}, tool="a", start_date=None, end_date=None, water_year=2001),
             _row("r2", {"baseflow_index": 0.445}, tool="b", start_date=None, end_date=None, water_year=2002))
    assert find_metric_conflicts(s) == []
    s = _log(_row("r1", {"bfi": 0.357}, tool="a", start_date=None, end_date=None, water_year=2001),
             _row("r2", {"baseflow_index": 0.445}, tool="b", start_date=None, end_date=None, water_year=2001))
    assert len(find_metric_conflicts(s)) == 1


def test_nested_obs_sim_outputs_never_collide_with_themselves():
    s = _log(_row("r1", {"obs": {"bfi": 0.357}, "sim": {"bfi": 0.445}}, tool="evaluate"),
             _row("r2", {"signatures": {"bfi": 0.30}, "bfi": 0.30}, tool="sig"))
    assert find_metric_conflicts(s) == []


def test_agreement_other_periods_other_features_and_recomputes_are_not_conflicts():
    s = _log(_row("r1", {"bfi": 0.445}, tool="a"),
             _row("r2", {"baseflow_index": 0.4452}, tool="b"),                         # agrees within tolerance
             _row("r3", {"bfi": 0.30}, tool="c", end_date="2010-12-31"),               # another period
             _row("r4", {"bfi": 0.31}, tool="d", gauge_id="01031500"),                 # another feature
             _row("r5", {"bfi": 0.10}, tool="a", ts="2026-09-01T00:00:00+00:00"))      # older run of tool a
    assert find_metric_conflicts(s) == []
    assert "metric_conflicts" not in start_session(session_id=SID)


def test_latest_run_per_tool_is_what_counts():
    s = _log(_row("r1", {"bfi": 0.357}, tool="a", ts="2026-09-01T00:00:00+00:00"),
             _row("r2", {"bfi": 0.445}, tool="a", ts="2026-10-01T00:00:00+00:00"),
             _row("r3", {"baseflow_index": 0.445}, tool="b"))
    assert find_metric_conflicts(s) == []
