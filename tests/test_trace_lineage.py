"""trace_lineage: the walk over declared edges, and the tool through the real MCP handler.

Synthetic run logs for the walk; a real data_fetch -> summarize_series chain (synthetic series) for the tool.
"""
from __future__ import annotations

import pytest

from test_series_tools import SID, call, days, env, mcp_server, run, run_log, serve  # noqa: F401  (fixtures)
from ai_hydro.session import lineage as L


def row(rid, tool, parents=(), refs=(), extra=None, status="ok", sealed=True, ts="2026-10-01T00:00:00"):
    r = {"run_id": rid, "tool_name": tool, "timestamp": ts, "key_outputs": {}}
    if sealed:
        r["record"] = {"run_id": rid, "tool": tool, "status": status, "parents": list(parents),
                       "input_refs": [{"ref": x, "digest": None, "role": "served_data"} for x in refs],
                       "extra": extra or {}}
    return r


def claim(*spans, status="supported"):
    return {"status": status, "evidence_spans": [{"source_type": t, "source_id": i, "metric_ref": "x"} for t, i in spans]}


def chain():
    """fetch -> baseflow -> signatures (also reads the fetch) -> claim; plus an unrelated fetch and a
    later run whose inputs the record does not declare."""
    log = {
        "q1": row("q1", "data_fetch", ts="2026-10-01T00:00:01"),
        "b1": row("b1", "separate_baseflow", parents=["q1"], ts="2026-10-01T00:00:02"),
        "s1": row("s1", "extract_hydrological_signatures", parents=["b1"], refs=["q1"], ts="2026-10-01T00:00:03"),
        "q2": row("q2", "data_fetch", ts="2026-10-01T00:00:04"),
        "late": row("late", "compute_flow_duration_curve", ts="2026-10-01T00:00:05"),     # reads a slot, declares nothing
    }
    return log, {"c1": claim(("run", "s1"), ("paper", "doi:10.1/x"))}


def ids(nodes):
    return [n["id"] for n in nodes]


def test_up_from_a_claim_reaches_the_fetch_and_is_complete():
    log, claims = chain()
    t = L.trace(log, claims, "claim:c1", "up")
    assert ids(t["upstream"]["nodes"]) == ["s1", "doi:10.1/x", "b1", "q1"]
    assert t["upstream"]["complete"] is True and t["upstream"]["parents_unknown_at"] == []
    q1 = next(n for n in t["upstream"]["nodes"] if n["id"] == "q1")
    assert q1["edge_state"] == L.ACQUISITION_ROOT and q1["parents_known"] is True      # no parents, and that is known
    assert next(n for n in t["upstream"]["nodes"] if n["id"] == "s1")["depth"] == 1


def test_down_from_the_fetch_lists_dependents_and_says_what_it_cannot_exclude():
    log, claims = chain()
    t = L.trace(log, claims, "q1", "down")
    assert set(ids(t["downstream"]["nodes"])) == {"b1", "s1", "claim:c1"}
    assert {n["id"]: n["via"] for n in t["downstream"]["nodes"]}["claim:c1"] == "evidence_span"
    d = t["downstream"]
    assert d["complete"] is False
    assert [x["id"] for x in d["may_also_depend"]] == ["late"] and d["may_also_depend"][0]["edge_state"] == L.UNDECLARED
    # a fetch recorded after the later run, or an unrelated one, is not a "may also depend"
    assert all(x["id"] != "q2" for x in d["may_also_depend"])


def test_a_run_with_no_parent_and_no_reason_is_unknown_not_a_root():
    log, claims = chain()
    t = L.trace(log, claims, "late", "up")
    assert t["node"]["edge_state"] == L.UNDECLARED and t["node"]["parents_known"] is False
    assert t["upstream"]["nodes"] == [] and t["upstream"]["complete"] is False
    assert t["upstream"]["parents_unknown_at"] == ["late"]
    root = L.trace(log, claims, "q2", "up")
    assert root["upstream"]["nodes"] == [] and root["upstream"]["complete"] is True      # same empty list, different meaning


def test_unsealed_unresolved_and_missing_parents_each_make_the_answer_incomplete():
    log = {"old": row("old", "run_python", sealed=False),
           "unr": row("unr", "extract_hydrological_signatures", extra={"parent_unresolved": "slot carries no run id"}),
           "kid": row("kid", "summarize_series", parents=["ghost"])}
    t = L.trace(log, {}, "old", "up")
    assert t["node"]["edge_state"] == L.NO_RECORD and t["upstream"]["complete"] is False
    t = L.trace(log, {}, "unr", "up")
    assert t["node"]["edge_state"] == L.DECLARED_UNRESOLVED and "no run id" in t["node"]["reason"]
    t = L.trace(log, {}, "kid", "up")
    (g,) = t["upstream"]["nodes"]
    assert g["id"] == "ghost" and g["edge_state"] == L.NOT_IN_LOG and t["upstream"]["complete"] is False


def test_run_valued_input_refs_are_edges_and_file_refs_are_not():
    log = {"q1": row("q1", "data_fetch"),
           "s1": row("s1", "extract_hydrological_signatures", parents=[], refs=["q1", "session-data:streamflow_x.json"])}
    t = L.trace(log, {}, "s1", "up")
    assert [(n["id"], n["via"]) for n in t["upstream"]["nodes"]] == [("q1", "input_refs")]


def test_unknown_and_ambiguous_nodes():
    log, claims = chain()
    assert L.trace(log, claims, "nope")["found"] is False
    both = L.trace({"x": row("x", "data_fetch")}, {"x": claim()}, "x")
    assert both["found"] is False and both["kind"] == "ambiguous"
    assert L.trace({"x": row("x", "data_fetch")}, {"x": claim()}, "run:x")["node"]["kind"] == "run"
    assert L.trace({"x": row("x", "data_fetch")}, {"x": claim()}, "claim:x")["node"]["kind"] == "claim"


def test_cycles_and_depth_and_node_limits_terminate():
    log = {"a": row("a", "summarize_series", parents=["b"]), "b": row("b", "summarize_series", parents=["a"])}
    t = L.trace(log, {}, "a", "both")
    assert ids(t["upstream"]["nodes"]) == ["b"] and ids(t["downstream"]["nodes"]) == ["b"]
    long = {f"r{i}": row(f"r{i}", "summarize_series", parents=[f"r{i+1}"]) for i in range(30)}
    long["r30"] = row("r30", "data_fetch")
    assert len(L.trace(long, {}, "r0", "up", max_depth=5)["upstream"]["nodes"]) == 5
    capped = L.trace(long, {}, "r0", "up", max_depth=50, max_nodes=7)
    assert len(capped["upstream"]["nodes"]) == 7 and capped["truncated"] is True and capped["upstream"]["complete"] is False


def test_edge_coverage_counts_the_studys_runs():
    log, claims = chain()
    cov = L.trace(log, claims, "q1")["edge_coverage"]
    assert cov == {"runs": 5, "acquisition_root": 2, "declared": 2, "undeclared": 1}


# ---------------------------------------------------------------- the tool, through the real handler

def test_tool_over_a_real_fetch_then_summary_chain(serve, mcp_server):
    fetch = serve(days("2020-01-01", 6), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])["_run_id"]
    err, summ = run(mcp_server, "summarize_series", series=fetch)
    assert not err
    sid = summ["_run_id"]

    err, up = run(mcp_server, "trace_lineage", node=sid, direction="up")
    assert not err and up["found"] and up["upstream"]["complete"] is True
    (q,) = up["upstream"]["nodes"]
    assert q["id"] == fetch and q["tool"] == "data_fetch" and q["edge_state"] == "acquisition_root"

    err, down = run(mcp_server, "trace_lineage", node=fetch, direction="down")
    assert ids(down["downstream"]["nodes"]) == [sid]
    assert down["edge_coverage"]["runs"] >= 2

    err, bad = run(mcp_server, "trace_lineage", node=sid, direction="sideways")
    assert bad["code"] == "INVALID_PARAMETER"
    err, miss = run(mcp_server, "trace_lineage", node="no-such-run")
    assert miss["found"] is False


def test_the_tool_is_read_only_and_looking_does_not_change_what_is_seen(serve, mcp_server):
    fetch = serve(days("2020-01-01", 3), [1.0, 2.0, 3.0])["_run_id"]
    before = set(run_log())
    err, first = run(mcp_server, "trace_lineage", node=fetch, direction="down")
    err, second = run(mcp_server, "trace_lineage", node=fetch, direction="down")
    assert not err and first["found"]
    # the only new rows are the tool's own, and they are not reported as dependents or counted as runs
    assert {r["record"]["tool"] for rid, r in run_log().items() if rid not in before} == {"trace_lineage"}
    assert second["downstream"] == first["downstream"] and second["edge_coverage"] == first["edge_coverage"]
    from ai_hydro.mcp.tool_surface import load_annotation_rows

    assert load_annotation_rows()["trace_lineage"]["readOnlyHint"] is True


def test_view_rows_are_left_out_of_dependents_and_coverage():
    log, claims = chain()
    log["v1"] = row("v1", "trace_lineage", ts="2026-10-01T00:00:09")
    t = L.trace(log, claims, "q1", "down")
    assert [x["id"] for x in t["downstream"]["may_also_depend"]] == ["late"] and t["edge_coverage"]["runs"] == 5


def test_a_walk_cut_by_depth_is_not_complete():
    log = {f"r{i}": row(f"r{i}", "summarize_series", parents=[f"r{i+1}"]) for i in range(6)}
    log["r6"] = row("r6", "data_fetch")
    short = L.trace(log, {}, "r0", "up", max_depth=2)["upstream"]
    assert short["complete"] is False and short["depth_or_size_limited"] is True and len(short["nodes"]) == 2
    full = L.trace(log, {}, "r0", "up", max_depth=10)["upstream"]
    assert full["complete"] is True and full["depth_or_size_limited"] is False
    # exactly at the depth where nothing further exists is not a cut
    assert L.trace(log, {}, "r0", "up", max_depth=6)["upstream"]["complete"] is True
    d = L.trace(log, {}, "r6", "down", max_depth=2)["downstream"]
    assert d["complete"] is False and d["depth_or_size_limited"] is True


def test_edge_state_sees_run_valued_input_refs():
    log = {"q1": row("q1", "data_fetch"), "s1": row("s1", "summarize_series", refs=["q1"])}
    assert L.edge_state(log["s1"], set(log)) == L.DECLARED and L.edge_state(log["s1"]) == L.DECLARED
    f = row("f1", "summarize_series", refs=["session-data:x.json"])
    f["record"]["input_refs"][0]["role"] = "other"
    assert L.edge_state(f, set(log)) == L.UNDECLARED
    assert L.trace(log, {}, "s1", "up")["node"]["parents_known"] is True


def test_dependents_compare_real_times_and_keep_a_run_when_a_time_cannot_be_read():
    log = {"q1": row("q1", "data_fetch", ts="2026-10-01T10:00:00+00:00"),
           "early": row("early", "compute_flow_duration_curve", ts="2026-10-01T09:00:00Z"),
           "late": row("late", "compute_flow_duration_curve", ts="2026-10-01T05:00:00-07:00"),    # 12:00 UTC
           "naive": row("naive", "compute_flow_duration_curve", ts="2026-10-01T11:00:00"),
           "junk": row("junk", "compute_flow_duration_curve", ts="not a time")}
    ids_ = {x["id"] for x in L.trace(log, {}, "q1", "down")["downstream"]["may_also_depend"]}
    assert ids_ == {"late", "naive", "junk"}


def test_max_nodes_is_exposed_and_validated(serve, mcp_server):
    fetch = serve(days("2020-01-01", 3), [1.0, 2.0, 3.0])["_run_id"]
    err, out = run(mcp_server, "trace_lineage", node=fetch, max_nodes=5)
    assert not err and out["found"]
    err, bad = run(mcp_server, "trace_lineage", node=fetch, max_nodes=0)
    assert bad["code"] == "INVALID_PARAMETER"
