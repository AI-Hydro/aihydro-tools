"""Q_mean (and its siblings) are mm/day. No digest, synopsis or workspace file may show them bare.

Regression: a study digest printed ``q_mean = 2.01686 [run:...]``; the agent wrote "Q_mean 2.017 m3/s"
(the value is a basin-depth rate in mm/day; as discharge it is about 9x off for that basin).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from binding_helpers import CHAT_A, make_study, read_resource

from ai_hydro.science import signature_units as su
from ai_hydro.session.store import HydroSession
from ai_hydro.session.chat_binding import get_binding_store
from ai_hydro.session.project_registry import get_project_registry

T0 = "2026-10-01T00:00:00+00:00"
FLOWS = ("q_mean", "q_std", "q5", "q95", "q_median")


def _study(ws, data):
    from binding_helpers import call
    res, body = call("ensure_project", {"root_dir": str(ws)})
    prj = body["project_id"]
    s = make_study("st_u", workspace=str(ws), site_id="01031500", site_name="Piscataquis River",
                   site_type="usgs_gauge")
    s.set("watershed", {"data": {"gauge_name": "Piscataquis River", "area_km2": 769.0},
                        "meta": {"computed_at": T0, "tool": "delineate_watershed"}})
    s.set("signatures", {"data": {"flow_quantile_convention": "exceedance", **data}, "meta": {"computed_at": T0, "tool": "extract_hydrological_signatures"}})
    s.save()
    get_project_registry().add_study(prj, "st_u")
    get_binding_store().bind(CHAT_A, "st_u")
    return prj


def _digest(prj):
    return read_resource(f"aihydro://project/{prj}/context?chat={CHAT_A}")["text"]


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    return d


def test_digest_prints_mm_per_day_for_an_old_result_with_no_units_map(ws):
    prj = _study(ws, {"q_mean": 2.0168551452427717, "q_std": 3.27, "q5": 0.11, "q95": 7.96,
                      "q_median": 0.866, "baseflow_index": 0.44})
    text = _digest(prj)
    for f in FLOWS:
        assert re.search(rf"- {f} = [0-9.]+ mm/day \[run:", text), (f, text)
    assert "m3/s" not in text and "m³/s" not in text
    assert re.search(r"baseflow_index = 0\.44 \[run:", text)      # dimensionless: no unit printed


def test_digest_uses_the_unit_the_result_recorded(ws):
    prj = _study(ws, {"q_mean": 2.0, "units": {"q_mean": "mm d-1"}})
    assert re.search(r"q_mean = 2 mm d-1 \[run:", _digest(prj))


def test_synopsis_for_an_old_result_carries_units(ws):
    _study(ws, {"q_mean": 2.0, "q95": 8.0, "baseflow_index": 0.44})
    from ai_hydro.session import HydroSession
    syn = HydroSession.load("st_u").synopsis_for_llm()["signatures"]
    assert syn["units"]["q_mean"] == "mm/day" and syn["units"]["q95"] == "mm/day"


def test_with_units_keeps_recorded_and_adds_missing():
    d = su.with_units({"q_mean": 1.0, "zero_q_freq": 0.0, "units": {"q_mean": "mm/day"}})
    assert d["units"] == {"q_mean": "mm/day", "zero_q_freq": "dimensionless"}
    full = {"q_mean": 1.0, "units": {"q_mean": "mm/day"}}
    assert su.with_units(full) is full


def test_table_matches_the_installed_watershed():
    sigs = pytest.importorskip("aihydro_watershed.signatures.signatures")
    theirs = getattr(sigs, "SIGNATURE_UNITS", None)
    if theirs is None:
        pytest.skip("installed aihydro-watershed predates SIGNATURE_UNITS")
    assert su.SIGNATURE_UNITS == dict(theirs)


def test_comparison_rows_carry_their_unit():
    rcmp = {"table": {"q_mean": {"value": 2.0, "reference": 1.8}, "baseflow_index": {"value": 0.4},
                      "q95": {"value": 8.0, "unit": "mm d-1"}, "not_a_signature": {"value": 1}}}
    out = su.annotate_comparison(rcmp)
    assert out is rcmp
    t = rcmp["table"]
    assert t["q_mean"]["unit"] == "mm/day" and t["baseflow_index"]["unit"] == "dimensionless"
    assert t["q95"]["unit"] == "mm d-1" and "unit" not in t["not_a_signature"]
    assert su.annotate_comparison(None) is None and su.annotate_comparison({"table": None}) == {"table": None}


def test_the_digest_helper_accepts_only_units_the_audit_recognises():
    from test_study_context import _strip_value_unit_marker_pairs as strip

    ok = "- q_mean = 2.01686 mm/day [run:a.b#key_outputs.q_mean]\n- hfd_mean = 181 day of water year [run:a.b#key_outputs.hfd_mean]"
    assert "2.01686" not in strip(ok) and "181" not in strip(ok)
    bad = "- q_mean = 2.01686 furlongs [run:a.b#key_outputs.q_mean]"
    assert "2.01686" in strip(bad)


def test_hfd_mean_prints_with_its_unit_and_still_passes_the_bare_number_check(ws):
    from test_study_context import assert_no_bare_numbers

    prj = _study(ws, {"hfd_mean": 181.0, "q_mean": 2.0, "zero_q_freq": 0.0})
    text = _digest(prj)
    assert re.search(r"hfd_mean = 181 day of water year \[run:", text), text
    assert re.search(r"q_mean = 2 mm/day \[run:", text)
    assert_no_bare_numbers(text)


def test_an_old_signatures_row_with_no_units_map_still_flags_m3_per_s(monkeypatch):
    """Without the table fallback `declared_unit` returned None for such a row and "2.017 m3/s" passed."""
    from ai_hydro.audit import resolver as R

    old = {"s1": {"slot": "signatures", "tool_name": "extract_hydrological_signatures",
                  "key_outputs": {"q_mean": 2.0169, "baseflow_index": 0.44}}}
    monkeypatch.setattr(R, "_load_run_log", lambda s: old)
    monkeypatch.setattr(R, "_load_claims", lambda s: {})
    rep = R.resolve_prose("Mean daily discharge: 2.02 m3/s [run:s1#key_outputs.q_mean].", "01031500")
    assert [v.kind for v in rep.violations] == ["unit_mismatch"] and not rep.passed
    ok = R.resolve_prose("Mean daily depth 2.02 mm/day [run:s1#key_outputs.q_mean].", "01031500")
    assert ok.passed
    # a row of another tool is not given signature units
    other = {"s1": {"slot": "streamflow", "tool_name": "summarize_series", "key_outputs": {"q_mean": 2.0169}}}
    monkeypatch.setattr(R, "_load_run_log", lambda s: other)
    assert R.resolve_prose("Mean 2.02 m3/s [run:s1#key_outputs.q_mean].", "01031500").passed


def test_units_survive_the_forty_key_cap():
    big = {f"k{i:02d}": float(i) for i in range(60)}
    s = HydroSession("capcheck")
    out = s._run_key_outputs({"data": {**big, "units": {"q_mean": "mm/day", **{f"f{i}": "x" for i in range(25)}}}})
    assert "units" in list(out)[0] and len(out["units"]) == 26         # first, and not cut at 20 either
    assert len([k for k in out if k != "units"]) == 40
