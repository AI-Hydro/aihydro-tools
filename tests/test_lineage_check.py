"""Advisory ``no_observed_lineage`` check and the completion outcome (honest exit).

Run logs here are SYNTHETIC: rows with just enough sealed-record shape for the walk. The check
is advisory: it never changes ``passed`` and never changes whether a cited value is accepted.
"""
from __future__ import annotations

import pytest

from test_series_tools import SID, call, env, mcp_server  # noqa: F401  (fixtures)
from ai_hydro.audit import resolver as R
from ai_hydro.session import lineage as L


def row(rid, tool, parents=(), ko=None, extra=None, status="ok", sealed=True):
    r = {"run_id": rid, "tool_name": tool, "key_outputs": ko or {}}
    if sealed:
        r["record"] = {"run_id": rid, "tool": tool, "status": status, "parents": list(parents), "extra": extra or {}}
    return r


def audit(prose, log, monkeypatch):
    monkeypatch.setattr(R, "_load_run_log", lambda s: log)
    monkeypatch.setattr(R, "_load_claims", lambda s: {})
    return R.resolve_prose(prose, "01031500")


def kinds(rep):
    return [v.kind for v in rep.violations]


def test_value_traced_to_a_fetch_is_not_flagged(monkeypatch):
    log = {"q1": row("q1", "data_fetch", ko={"rows": 10}),
           "s1": row("s1", "separate_baseflow", parents=["q1"], ko={"bfi": 0.44})}
    rep = audit("Baseflow index is 0.44 [run:s1#key_outputs.bfi].", log, monkeypatch)
    assert rep.passed and "no_observed_lineage" not in kinds(rep)
    assert (rep.lineage_checked_count, rep.lineage_flagged_count, rep.lineage_unknown_count) == (1, 0, 0)
    lin = L.trace_to_observed(log, "s1")
    assert lin.state == L.OBSERVED and lin.observed_via == "q1" and lin.path == ["s1", "q1"]


def test_declared_parents_that_never_fetched_data_are_flagged_as_advisory(monkeypatch):
    log = {"c1": row("c1", "summarize_series"), "s1": row("s1", "summarize_series", parents=["c1"], ko={"mean": 3.5})}
    rep = audit("The mean is 3.5 [run:s1#key_outputs.mean].", log, monkeypatch)
    (v,) = [v for v in rep.violations if v.kind == "no_observed_lineage"]
    assert v.severity == "advisory" and "s1" in v.message
    assert rep.passed                                   # advisory: the value is still accepted
    assert rep.hard_count == 0 and rep.advisory_count == 1 and rep.lineage_flagged_count == 1


def test_arbitrary_code_with_no_declared_input_is_flagged(monkeypatch):
    log = {"p1": row("p1", "run_python", ko={"mean_q": 12.5})}
    rep = audit("The mean flow is 12.5 [run:p1#key_outputs.mean_q].", log, monkeypatch)
    (v,) = [v for v in rep.violations if v.kind == "no_observed_lineage"]
    assert "arbitrary code" in v.message and rep.passed


def test_a_producer_that_records_no_parent_is_unknown_not_flagged(monkeypatch):
    """Edge coverage is incomplete: most tools that read a stored series declare no parent."""
    log = {"b1": row("b1", "separate_baseflow", ko={"bfi": 0.44})}
    rep = audit("Baseflow index is 0.44 [run:b1#key_outputs.bfi].", log, monkeypatch)
    assert "no_observed_lineage" not in kinds(rep)
    assert (rep.lineage_checked_count, rep.lineage_flagged_count, rep.lineage_unknown_count) == (1, 0, 1)
    assert L.trace_to_observed(log, "b1").state == L.UNDECLARED


def test_unsealed_row_unresolved_parent_and_missing_parent_are_unknown(monkeypatch):
    log = {"u1": row("u1", "run_python", sealed=False, ko={"x": 1.5}),
           "u2": row("u2", "run_python", extra={"parent_unresolved": "slot carries no run id"}, ko={"x": 2.5}),
           "u3": row("u3", "summarize_series", parents=["gone"], ko={"x": 3.5})}
    for rid, val in (("u1", "1.5"), ("u2", "2.5"), ("u3", "3.5")):
        rep = audit(f"The value is {val} [run:{rid}#key_outputs.x].", log, monkeypatch)
        assert "no_observed_lineage" not in kinds(rep), rid
        assert rep.lineage_unknown_count == 1
        assert L.trace_to_observed(log, rid).state == L.UNKNOWN


def test_a_failed_fetch_is_not_an_acquisition():
    log = {"q1": row("q1", "data_fetch", status="error"), "s1": row("s1", "summarize_series", parents=["q1"])}
    assert L.trace_to_observed(log, "s1").state == L.DECLARED_UNOBSERVED


def test_echoed_inputs_and_method_parameters_are_not_checked(monkeypatch):
    log = {"p1": row("p1", "run_python", ko={"mean_q": 12.5}), }
    log["p1"]["inputs"] = {"window": 30}
    rep = audit("Over a window of 30 [run:p1#inputs.window] days.", log, monkeypatch)
    assert rep.lineage_checked_count == 0


def test_a_cycle_terminates():
    log = {"a": row("a", "summarize_series", parents=["b"]), "b": row("b", "summarize_series", parents=["a"])}
    assert L.trace_to_observed(log, "a").state == L.DECLARED_UNOBSERVED


def test_the_check_does_not_change_what_is_accepted(monkeypatch):
    """Same prose, same log: the hard violations and coverage are identical with and without lineage records."""
    base = {"p1": row("p1", "run_python", ko={"mean_q": 12.5})}
    stripped = {"p1": row("p1", "run_python", ko={"mean_q": 12.5}, sealed=False)}
    a = audit("Mean 12.5 [run:p1#key_outputs.mean_q]; also 99.9 here.", base, monkeypatch)
    b = audit("Mean 12.5 [run:p1#key_outputs.mean_q]; also 99.9 here.", stripped, monkeypatch)
    assert (a.passed, a.hard_count, a.cited_numeric_count, a.numeric_coverage) == \
           (b.passed, b.hard_count, b.cited_numeric_count, b.numeric_coverage)


# ------------------------------------------------------------------ completion outcome

def test_completion_outcome_validation():
    from ai_hydro.mcp.tools_audit import record_completion_outcome as rec

    assert rec(None, None) is None
    assert rec("done", None) == {"outcome": "done"}
    assert rec("partial", ["no 2010-2015 data", " "]) == {"outcome": "partial", "unmet": ["no 2010-2015 data"]}
    for bad in (("finished", None), ("done", ["x"]), (None, ["x"])):
        r = rec(*bad)
        assert r["error"] is True and r["code"] == "INVALID_PARAMETER"
    long = rec("infeasible", ["u" * 500] * 20)
    assert len(long["unmet"]) == 10 and all(len(u) == 200 for u in long["unmet"])


def test_outcome_is_sealed_in_the_audit_calls_run_record(env, mcp_server):
    """Through the real MCP handler: the audit call's own sealed record carries the outcome."""
    from ai_hydro.session.store import HydroSession

    err, body, _ = call(mcp_server, "audit_interpretation", {
        "session_id": SID, "prose": "No streamflow record exists for this gauge.",
        "outcome": "infeasible", "unmet": ["no observed streamflow for 2010-2015"]})
    assert not err and body["completion_outcome"] == {
        "outcome": "infeasible", "unmet": ["no observed streamflow for 2010-2015"]}, body
    rows = [r for r in (HydroSession.load(SID).get("_run_log") or {}).values()
            if (r.get("record") or {}).get("tool") == "audit_interpretation"]
    assert rows and rows[-1]["record"]["extra"]["completion_outcome"] == "infeasible"
    assert rows[-1]["record"]["extra"]["completion_unmet"] == ["no observed streamflow for 2010-2015"]

    err, body, _ = call(mcp_server, "audit_interpretation", {"session_id": SID, "prose": "x", "outcome": "finished"})
    assert body.get("code") == "INVALID_PARAMETER"
