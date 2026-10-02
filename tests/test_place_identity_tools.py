"""Slice 3 / P5: canonical place identity in the claim ledger (ADR-003).

Digest stability, auto-bind, BASIN_REF_REQUIRED, one USGS rule, alias coverage
in the skeptic, and the approval CLI display. Offline; refs built from core.
"""
from __future__ import annotations

import io

import pytest
from aihydro_core.records.place import BasinAnchor, BasinRef, PlaceAlias

from ai_hydro import identity
from ai_hydro.approval.records import claim_revision_digest
from ai_hydro.mcp import tools_analysis
from ai_hydro.mcp.tools_ledger import add_claim, promote_claim_to_registry
from ai_hydro.session import store
from ai_hydro.session.models import ClaimScope, ScientificClaim
from ai_hydro.session.store import HydroSession
from ai_hydro.skeptic.checks import check_scope_overreach


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from ai_hydro.registry import store as registry
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(registry, "REGISTRY_DIR", tmp_path / "registry")
    monkeypatch.setattr(registry, "CLAIMS_FILE", tmp_path / "registry" / "claims.jsonl")
    monkeypatch.setattr("ai_hydro.mcp.tools_ledger.push_claim_event", lambda **kw: None)


def _ref(site="01013500", comid="1234567") -> dict:
    return BasinRef(
        anchor=BasinAnchor("network_element", "nhdplusv2", "2.1", comid), method="nldi_gauge_index",
        aliases=(PlaceAlias("usgs", site, source="nldi"), PlaceAlias("comid", comid)),
    ).to_dict()


def _add(sid, **over):
    kw = dict(session_id=sid, claim_id="c1", statement="s", claim_type="empirical_result",
              status="supported", confidence="medium",
              confidence_rationale="Run-backed result with bootstrap interval reported.",
              basins=["01013500"], period="2000-2010", limitations=["one gauge"])
    kw.update(over)
    return add_claim(**kw)


def _session_with_slot(sid, ref):
    s = HydroSession(sid)
    s.set("watershed", {"data": {"gauge_id": "01013500", "area_km2": 10.0, "basin_ref": ref},
                        "meta": {}})
    s.save()


# --- (2) digest stability --------------------------------------------------

# Computed on main (3900edc) before ClaimScope.basin_refs existed.
GOLDEN_CLAIM = {
    "id": "c1", "claim": "Mean flow at 01013500 is stable.", "claim_type": "empirical_result",
    "status": "supported", "confidence": "medium",
    "confidence_rationale": "Run-backed KGE with bootstrap interval reported.",
    "scope": {"basins": ["01013500"], "period": "2000-2010", "metric": "metric.kge",
              "model_versions": {}},
    "limitations": ["l1"], "evidence_spans": [{"source_type": "run", "source_id": "run1",
                                               "metric_ref": "kge"}],
    "prereg_id": "p1", "uncertainty_verified": True,
}
GOLDEN_DIGEST = "sha256:a026676d6fc4c2611a48f0e29d8e581a01474f27376379f41e7ee8153ea96c9e"


def test_existing_claim_revision_digest_is_byte_identical():
    ev = {"run1": "sha256-v2:abc"}
    assert claim_revision_digest(GOLDEN_CLAIM, ev) == GOLDEN_DIGEST
    # a stored claim that round-tripped through the new model digests the same
    stored = ScientificClaim(**GOLDEN_CLAIM).model_dump()
    assert "basin_refs" not in stored["scope"]
    assert claim_revision_digest({**stored, "prereg_id": "p1", "uncertainty_verified": True}, ev) == GOLDEN_DIGEST


def test_binding_refs_changes_the_digest():
    ev = {"run1": "sha256-v2:abc"}
    bound = {**GOLDEN_CLAIM, "scope": {**GOLDEN_CLAIM["scope"],
             "basin_refs": [{"id": _ref()["id"], "label": "01013500"}]}}
    assert claim_revision_digest(bound, ev) != GOLDEN_DIGEST


def test_scope_rejects_malformed_basin_ref_id():
    with pytest.raises(ValueError):
        ClaimScope(basins=["b"], period="p", basin_refs=[{"id": "01013500"}])


# --- (3) slot keeps basin_ref ----------------------------------------------

def test_watershed_slot_retains_basin_ref(tmp_path):
    ref = _ref()
    d = {"data": {"geometry_geojson": {"type": "Polygon", "coordinates": []}, "gauge_id": "01013500",
                  "area_km2": 1.0, "basin_ref": ref, "delineation_path": "gauge_index"},
         "meta": {}}
    HydroSession("slot").save()
    tools_analysis._store_point_watershed("slot", d, d["data"]["geometry_geojson"], "x", None)
    slot = HydroSession.load("slot").watershed["data"]
    assert slot["basin_ref"] == ref and "geometry_geojson" not in slot
    assert identity.session_basin_ref(HydroSession.load("slot")) == ref


# --- (4) add_claim binding and promotion refusal ---------------------------

def test_auto_bind_by_usgs_alias_and_label():
    ref = _ref()
    _session_with_slot("ab", ref)
    res = _add("ab")
    assert res["status"] == "recorded" and res["auto_bound"] is True
    assert HydroSession.load("ab").claims["c1"]["scope"]["basin_refs"] == [
        {"id": ref["id"], "label": "01013500"}]
    # no match -> unbound, flagged false/absent
    res2 = _add("ab", claim_id="c2", basins=["99999999"])
    assert "auto_bound" not in res2
    assert "basin_refs" not in HydroSession.load("ab").claims["c2"]["scope"]


def test_explicit_full_ref_is_verified():
    HydroSession("ex").save()
    ref = _ref()
    ok = _add("ex", basin_refs=[ref])
    assert ok["auto_bound"] is False and ok["basin_refs"][0]["id"] == ref["id"]
    forged = {**ref, "id": "aihydro:basin:sha256:" + "0" * 64}
    bad = _add("ex", claim_id="c2", basin_refs=[forged])
    assert bad["error"] and "failed verification" in bad["message"]
    bad2 = _add("ex", claim_id="c3", basin_refs=[{"id": "not-an-id"}])
    assert bad2["error"]


def test_promotion_refused_without_basin_refs():
    HydroSession("pr").save()
    _add("pr")
    res = promote_claim_to_registry("pr", "c1", researcher_approved=True)
    assert res["code"] == "BASIN_REF_REQUIRED" and "delineate" in res["message"]


def test_promotion_passes_basin_gate_when_bound():
    _session_with_slot("pb", _ref())
    _add("pb")
    res = promote_claim_to_registry("pb", "c1", researcher_approved=True)
    assert res.get("code") != "BASIN_REF_REQUIRED"   # next gate (evidence) now speaks
    assert "evidence" in res["message"]


# --- (5) one USGS rule and alias equivalence -------------------------------

def test_usgs_site_id_rule():
    assert identity.is_usgs_site_id("01013500") and identity.is_usgs_site_id("0" * 15)
    assert not identity.is_usgs_site_id("1013500") and not identity.is_usgs_site_id("0" * 16)
    assert not identity.is_usgs_site_id("0101350a") and not identity.is_usgs_site_id(1013500)


def test_gauge_shape_counts_usgs_alias():
    ref = _ref()
    refs = {ref["id"]: ref}
    assert identity.gauge_shaped(["01013500"])
    assert not identity.gauge_shaped(["my-basin"])
    assert identity.gauge_shaped(["01013500"], [{"id": ref["id"], "label": "x"}], refs)
    assert identity.gauge_shaped(["01013500", "my-basin"], None, refs) is False
    cp = identity.gauge_shaped(["01013500"], None, None)
    assert cp


class _Sess:
    def __init__(self, claims, watershed=None):
        self.claims, self.assumptions, self.watershed = claims, {}, watershed


def test_skeptic_counts_alias_ids_as_covered():
    ref = _ref(site="01013500")
    claim = {"id": "c", "scope": {"basins": ["my-basin"], "period": "p",
                                  "basin_refs": [{"id": ref["id"], "label": "my-basin"}]}}
    text = "Flow at 01013500 is stable."
    unbound = check_scope_overreach(_Sess({"c": {"scope": {"basins": ["my-basin"]}}}), text)
    assert [i.issue_type for i in unbound] == ["scope_overreach"]
    bound = check_scope_overreach(_Sess({"c": claim}, {"data": {"basin_ref": ref}}), text)
    assert bound == []


def test_skeptic_regex_matches_15_digit_ids():
    claim = {"scope": {"basins": ["01013500"]}}
    out = check_scope_overreach(_Sess({"c": claim}), "See gauge 123456789012.")
    assert len(out) == 1


# --- (6) approval CLI -------------------------------------------------------

def test_approval_render_shows_basin_refs():
    from ai_hydro.approval.cli import _render
    from ai_hydro.approval.records import claim_revision_fields
    ref = _ref()
    claim = {**GOLDEN_CLAIM, "scope": {**GOLDEN_CLAIM["scope"],
             "basin_refs": [{"id": ref["id"], "label": "01013500"}]}}
    f = claim_revision_fields(claim, {"run1": "sha256-v2:abc"})
    assert ref["id"] in _render("s", "c1", f, "sha256:x")
    f0 = claim_revision_fields(GOLDEN_CLAIM, {"run1": "x"})
    assert "unbound labels" in _render("s", "c1", f0, "sha256:x")


# --- INVALID_ID (lead requirement: approval commands are built from ids) ---

@pytest.mark.parametrize("bad", ["", "-x", ".x", "a b", "a;rm -rf", "a$(x)", "a/b", "x" * 129, "é"])
def test_add_claim_refuses_unsafe_ids(bad):
    HydroSession("ok-session").save()
    r1 = _add("ok-session", claim_id=bad)
    assert r1["code"] == "INVALID_ID" and r1["kind"] == "claim_id"
    r2 = _add(bad)
    assert r2["code"] == "INVALID_ID" and r2["kind"] == "session_id"


def test_safe_ids_accepted():
    HydroSession("s.1_a-b").save()
    assert _add("s.1_a-b", claim_id="claim.kge_high-2")["status"] == "recorded"
    assert identity.is_safe_id("x" * 128)


def test_stored_unsafe_claim_id_readable_not_promotable():
    s = HydroSession("legacy")
    s.claims["bad id;x"] = {**GOLDEN_CLAIM, "id": "bad id;x"}
    s.save()
    assert "bad id;x" in HydroSession.load("legacy").claims
    res = promote_claim_to_registry("legacy", "bad id;x", researcher_approved=True)
    assert res["code"] == "INVALID_ID" and "cannot be promoted" in res["message"]


# --- refusal envelopes carry session_id and claim_id at top level -----------

def _assert_envelope(res, code, sid, cid):
    assert res["code"] == code and res["session_id"] == sid and res["claim_id"] == cid


def test_refusal_envelopes_have_session_and_claim_id():
    HydroSession("env").save()
    _add("env")
    _assert_envelope(promote_claim_to_registry("env", "c1", researcher_approved=True),
                     "BASIN_REF_REQUIRED", "env", "c1")
    _assert_envelope(_add("env", claim_id="bad id"), "INVALID_ID", "env", "bad id")
    _assert_envelope(promote_claim_to_registry("env", "no way", researcher_approved=True),
                     "INVALID_ID", "env", "no way")
    # APPROVAL_REQUIRED: a bound claim with no approval record
    _session_with_slot("env2", _ref())
    _add("env2")
    s = HydroSession.load("env2")
    s.set("_run_log", {"r1": {"run_id": "r1", "session_id": "env2", "key_outputs": {"kge": 0.8}}})
    s.save()
    _add("env2", evidence_spans=[{"source_type": "run", "source_id": "r1", "metric_ref": "kge"}])
    res = promote_claim_to_registry("env2", "c1", researcher_approved=False)
    _assert_envelope(res, "APPROVAL_REQUIRED", "env2", "c1")
    assert res["approval_command"].startswith("aihydro-approve ")
