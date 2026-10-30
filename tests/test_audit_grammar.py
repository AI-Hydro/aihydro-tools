"""
Token-typed number grammar, path aliasing, hollow-evidence and unit hooks of the
Answer Auditor (ai_hydro/audit/).

The regression fixture is the real final answer of the CAMELS-US 01031500 session
study (2026-10-20) plus a trimmed copy of its run-log.  Before this grammar the
installed auditor reported, for that answer:

    29 numbers found, 8 "cited" (all via the +/-60-char year whitelist),
    21 violations: 0 of 5 [run:] markers resolved, the gauge id x4, "HUC-02 = 01",
    "60", "17" and fragments of run/claim ids flagged as uncited numbers, and the
    one real sample size (7,305) waved through because "1989" was nearby.

No session state is touched: _load_run_log / _load_claims are monkeypatched.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ai_hydro.audit import resolver as R
from ai_hydro.audit.grammar import classify_numerics, extract_numerics, segments
from ai_hydro.audit.units import normalise_unit, units_conflict

FIX = Path(__file__).parent / "fixtures" / "audit_session_01031500"
SIGS = "sigs.20261020.01031500.dd4ed1e7"
WSHED = "wshed.20261020.01031500.5fcfedd8"


@pytest.fixture
def study(monkeypatch):
    run_log = json.loads((FIX / "runlog.json").read_text(encoding="utf-8"))
    claims = json.loads((FIX / "claims.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "_load_run_log", lambda sid: run_log)
    monkeypatch.setattr(R, "_load_claims", lambda sid: claims)
    return run_log


def _audit(prose: str, run_log: dict, monkeypatch, claims: dict | None = None, sid: str = "01031500"):
    monkeypatch.setattr(R, "_load_run_log", lambda s: run_log)
    monkeypatch.setattr(R, "_load_claims", lambda s: claims or {})
    return R.resolve_prose(prose, sid)


def _cats(prose: str, ids=()):
    return [(c.span.raw, c.category, c.reason) for c in classify_numerics(prose, ids)]


# --------------------------------------------------------------------------- #
# Regression: the real session answer
# --------------------------------------------------------------------------- #

class TestSessionStudyAnswer:
    def test_after_counts(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")

        # 10 real quantities remain (was 29 "numbers"); 4 resolve cleanly
        assert rep.total_numeric_count == 10
        assert rep.cited_numeric_count == 4
        assert rep.hard_count == 5 and rep.advisory_count == 1
        assert rep.passed is False
        # hard findings first
        assert [v.severity for v in rep.violations] == ["hard"] * 5 + ["advisory"]

    def test_no_identifier_code_or_count_false_positives(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")
        flagged = {v.prose_value for v in rep.violations}
        for bogus in ("01031500", "60", "17", "02", "01", "20261020", "20261020.01031500",
                      "1989", "2009", "2017"):
            assert bogus not in flagged, bogus
        # the exempt tokens are reported for transparency
        assert rep.exempt_by_reason["identifier"] == 2     # USGS 01031500, Gauge ID provided: 01031500
        assert rep.exempt_by_reason["meta_count"] == 2      # 60 attributes, 17 signatures
        assert rep.exempt_by_reason["code_label"] == 1      # HUC-02 = 01
        assert rep.exempt_by_reason["date"] == 2            # the two ISO dates
        assert rep.exempt_by_reason["year"] == 1            # Addor et al., 2017

    def test_sample_size_flagged_not_excused_by_nearby_year(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")
        v = next(v for v in rep.violations if v.prose_value == "7,305")
        assert v.kind == "uncited_number" and v.severity == "hard" and v.category == "sample_size"
        # the n is stored on the run; the hint offers the exact marker
        # (every bootstrap block stores the same n; any of them is a valid binding)
        assert v.suggested_marker.startswith(f"[run:{SIGS}#evidence.uncertainty.")
        assert v.suggested_marker.endswith(".n]")

    def test_data_alias_markers_resolve_and_hollow_run_is_caught(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")
        kinds = {(v.kind, v.prose_value) for v in rep.violations}
        # the four signatures markers use data.<field>: no json_path_not_found any more
        assert not any(k == "json_path_not_found" for k, _ in kinds)
        assert not any(k == "value_mismatch" for k, _ in kinds)
        # the watershed run is a minimal stub -> hollow, not a path error
        hollow = next(v for v in rep.violations if v.kind == "hollow_evidence")
        assert hollow.prose_value == "774.15" and WSHED in hollow.marker_raw
        assert hollow.severity == "hard"

    def test_method_parameter_is_advisory_with_auto_binding(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")
        v = next(v for v in rep.violations if v.prose_value == "90%")
        assert v.severity == "advisory" and v.category == "method_param"
        assert v.suggested_marker.startswith(f"[run:{SIGS}#evidence.uncertainty.")
        assert v.suggested_marker.endswith(".ci_level]")

    def test_remaining_hard_findings_are_the_real_uncited_quantities(self, study):
        prose = (FIX / "answer.md").read_text(encoding="utf-8")
        rep = R.resolve_prose(prose, "01031500")
        hard_uncited = sorted(v.prose_value for v in rep.violations
                              if v.kind == "uncited_number" and v.severity == "hard")
        # table-row values the agent never bound (area, gauge lat/lon) + the sample size
        assert hard_uncited == sorted(["774.15", "45.175", "−69.3147", "7,305"])


# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #

class TestTokenisation:
    def test_digits_in_code_urls_paths_markers_are_not_numbers(self):
        txt = ("Run `step 3 of 4` and see https://example.org/a/0.5/b?x=7.2 or /tmp/run_3.2/out.csv "
               "and [run:abc.2026.5#key_outputs.x] then ```\nrow 1\nrow 2\n``` done.")
        assert extract_numerics(txt) == []

    def test_identifier_shaped_tokens_are_masked(self):
        txt = ("ids c-01031500-signatures-20261020, v2.3.7, EPSG:5070, version 2.3, "
               "wshed.20261020.01031500.5fcfedd8, watershed.ce56bc643d4c")
        assert extract_numerics(txt) == []

    def test_adjacent_separators_do_not_split_digits(self):
        # old lookbehind pulled "-10" "-01" and "20261020.01031500" out of tokens
        assert extract_numerics("window 1989-10-01 to 2009-09-30.") == []
        assert [n.raw for n in extract_numerics("range 0.35-0.50 here")] == ["0.35", "0.50"]
        assert [n.raw for n in extract_numerics("from -2 to −1.5")] == ["-2", "−1.5"]

    def test_compound_modifier_keeps_its_number(self):
        assert [n.raw for n in extract_numerics("19-day blocks")] == ["19"]
        assert [n.raw for n in extract_numerics("1.2e-3 and 5mm")] == ["1.2e-3", "5mm"[:1]]

    def test_numbers_with_attached_unit_are_found(self):
        assert [n.raw for n in extract_numerics("depth 5mm today")] == ["5"]


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

class TestClassification:
    def test_session_ids_and_id_context(self):
        assert _cats("Gauge 01031500 flows", ["01031500"])[0][1:] == ("exempt", "identifier")
        # 8-15 digit integer directly beside USGS / gauge / COMID, no registry needed
        assert _cats("USGS 04085427 and COMID 22678241")[0][1:] == ("exempt", "identifier")
        # a zero-padded integer is NOT automatically a label (model-controllable)
        assert _cats("tile 00123")[0][1:] == ("exempt", "bare_integer")

    def test_codes(self):
        assert _cats("HUC-02 = 01")[0][1:] == ("exempt", "code_label")
        assert _cats("HUC8 01020004")[0][1:] == ("exempt", "code_label")
        assert _cats("in EPSG 5070 and FIPS 23021 and NLCD class 41")[0][1:] == ("exempt", "code_label")
        assert [c[2] for c in _cats("EPSG 5070, FIPS 23021, NLCD class 41")] == ["code_label"] * 3

    def test_dates_and_years_only_as_tokens(self):
        assert _cats("in 2017")[0][1:] == ("exempt", "year")
        assert _cats("on 2009-09-30")[0][1:] == ("exempt", "date")
        assert _cats("Oct 1, 1989")[0][1:] == ("exempt", "date")
        # a year in the neighbourhood excuses nothing
        out = _cats("Over 1989 there were 7,305 daily observations")
        assert ("7,305", "sample_size", "") in out
        assert ("1989", "exempt", "year") in out
        # 2020 with a unit is a quantity, not a year
        assert _cats("rain of 2020 mm")[0][1] == "quantity"
        assert _cats("rain of 1,989")[0][2] == "bare_integer"      # comma: not a year token

    def test_structural_refs_and_list_indices(self):
        assert _cats("see Figure 3 and Table 2")[0][1:] == ("exempt", "structural_ref")
        assert _cats("1. First step")[0][1:] == ("exempt", "list_index")
        assert _cats("- 2) second")[0][1:] == ("exempt", "list_index")

    def test_meta_counts_need_integer_no_unit_and_a_closed_noun(self):
        assert _cats("17 signatures")[0][1:] == ("exempt", "meta_count")
        assert _cats("all 6 steps and 4 claims")[0][1:] == ("exempt", "meta_count")
        assert _cats("12 distinct runs")[0][1:] == ("exempt", "meta_count")
        # a decimal or a unit is a quantity even before a meta noun
        assert _cats("0.5 layers")[0][1] == "quantity"
        assert _cats("3 mm layers")[0][1] == "quantity"

    def test_sample_sizes_must_be_cited(self):
        for txt in ("7,305 daily observations", "21 years of record", "42 events", "12 basins",
                    "240 months", "10 gauges", "300 samples"):
            assert _cats(txt)[0][1] == "sample_size", txt

    def test_method_parameters_are_advisory(self):
        for txt in ("bootstrap 90% CIs", "alpha = 0.05", "1000 bootstrap resamples", "seed 42",
                    "block size 365", "three passes of 3 passes"):
            cats = [c[1] for c in _cats(txt)]
            assert "method_param" in cats, txt
        # a result value next to the word bootstrap is still a quantity
        assert _cats("runoff ratio 0.61 with bootstrap CI")[0][1] == "quantity"

    def test_quantities(self):
        assert _cats("area 774.15")[0][1] == "quantity"            # decimal
        assert _cats("42%")[0][1] == "quantity"                    # unit
        assert _cats("peak 53 m3/s")[0][1] == "quantity"
        assert _cats("NSE of 1 was reached")[0][1] == "quantity"   # next to a metric name
        assert _cats("we saw 5 of them")[0][1:] == ("exempt", "bare_integer")


# --------------------------------------------------------------------------- #
# Marker binding scope
# --------------------------------------------------------------------------- #

class TestBindingScope:
    def test_marker_in_next_table_row_does_not_bind(self, monkeypatch):
        run_log = {"r1": {"key_outputs": {"a": 0.61, "b": 0.45}}}
        prose = ("| Name | Value |\n|---|---|\n| first | 0.61 |\n"
                 "| second | 0.45 [run:r1#key_outputs.b] |\n")
        rep = _audit(prose, run_log, monkeypatch)
        assert [(v.kind, v.prose_value) for v in rep.violations] == [("uncited_number", "0.61")]

    def test_marker_in_next_bullet_or_sentence_does_not_bind(self, monkeypatch):
        run_log = {"r1": {"key_outputs": {"a": 0.61, "b": 0.45}}}
        rep = _audit("- ratio 0.61\n- other 0.45 [run:r1#key_outputs.b]", run_log, monkeypatch)
        assert [v.prose_value for v in rep.violations] == ["0.61"]
        rep = _audit("The ratio is 0.61. Another is 0.45 [run:r1#key_outputs.b].", run_log, monkeypatch)
        assert [v.prose_value for v in rep.violations] == ["0.61"]

    def test_wrapped_paragraph_and_backticked_marker_still_bind(self, monkeypatch):
        run_log = {"r1": {"key_outputs": {"a": 0.61}}}
        rep = _audit("The ratio of 0.61\nwas computed `[run:r1#key_outputs.a]`.", run_log, monkeypatch)
        assert rep.passed and rep.cited_numeric_count == 1

    def test_segments_split_table_cells(self):
        txt = "| a 1.5 | b 2.5 |"
        segs = segments(txt)
        assert len(segs) >= 2


# --------------------------------------------------------------------------- #
# Path aliasing
# --------------------------------------------------------------------------- #

class TestPathAliasing:
    @pytest.fixture
    def run_log(self):
        return {"r1": {
            "key_outputs": {"runoff_ratio": 0.605},
            "evidence": {"data": {"runoff_ratio": 0.605, "only_in_evidence": 3.5},
                         "uncertainty": {"runoff_ratio": {"ci_low": 0.55, "ci_high": 0.66, "n": 7305}}},
            "inputs": {"start_date": "1989-10-01"},
        }}

    @pytest.mark.parametrize("path", [
        "key_outputs.runoff_ratio", "data.runoff_ratio", "evidence.data.runoff_ratio", "runoff_ratio",
    ])
    def test_all_spellings_resolve(self, run_log, monkeypatch, path):
        rep = _audit(f"Ratio 0.61 [run:r1#{path}].", run_log, monkeypatch)
        assert rep.passed, rep.violations

    def test_alias_reaches_evidence_only_field(self, run_log, monkeypatch):
        rep = _audit("Value 3.5 [run:r1#key_outputs.only_in_evidence].", run_log, monkeypatch)
        assert rep.passed, rep.violations

    def test_uncertainty_alias(self, run_log, monkeypatch):
        rep = _audit("CI low 0.55 [run:r1#uncertainty.runoff_ratio.ci_low].", run_log, monkeypatch)
        assert rep.passed, rep.violations

    def test_wrong_field_still_fails_with_closest_match(self, run_log, monkeypatch):
        rep = _audit("Ratio 0.61 [run:r1#data.runof_ratio].", run_log, monkeypatch)
        v = rep.violations[0]
        assert v.kind == "json_path_not_found"
        assert "runoff_ratio" in v.fix_hint and "key_outputs.<field>" in v.fix_hint

    def test_value_still_checked_through_alias(self, run_log, monkeypatch):
        rep = _audit("Ratio 0.99 [run:r1#data.runoff_ratio].", run_log, monkeypatch)
        assert rep.violations[0].kind == "value_mismatch"


# --------------------------------------------------------------------------- #
# hollow_evidence
# --------------------------------------------------------------------------- #

class TestHollowEvidence:
    def test_minimal_run_is_hollow(self, monkeypatch):
        run_log = {"w1": {"run_id": "w1", "key_outputs": {}, "minimal": True}}
        rep = _audit("Area 774.15 km2 [run:w1#key_outputs.area_km2].", run_log, monkeypatch)
        assert [v.kind for v in rep.violations] == ["hollow_evidence"]
        assert rep.violations[0].severity == "hard" and rep.passed is False

    def test_derived_from_is_suggested(self, monkeypatch):
        run_log = {
            "w_old": {"run_id": "w_old", "key_outputs": {"area_km2": 774.15}},
            "w_new": {"run_id": "w_new", "key_outputs": {}, "minimal": True,
                      "record": {"derived_from": ["w_old"]}},
        }
        rep = _audit("Area 774.15 km2 [run:w_new#data.area_km2].", run_log, monkeypatch)
        v = rep.violations[0]
        assert v.kind == "hollow_evidence"
        assert v.suggested_marker == "[run:w_old#key_outputs.area_km2]"
        assert "w_old" in v.fix_hint

    def test_populated_run_with_missing_path_is_not_hollow(self, monkeypatch):
        run_log = {"r": {"key_outputs": {"a": 1.5}}}
        rep = _audit("B is 2.5 [run:r#key_outputs.b].", run_log, monkeypatch)
        assert rep.violations[0].kind == "json_path_not_found"


# --------------------------------------------------------------------------- #
# unit_mismatch
# --------------------------------------------------------------------------- #

class TestUnitMismatch:
    @staticmethod
    def _run(units):
        return {"s1": {"key_outputs": {"q_mean": 2.0169, "runoff_ratio": 0.605, "units": units},
                       "evidence": {"data": {"q_mean": 2.0169}}}}

    def test_session_study_unit_error_is_caught(self, monkeypatch):
        """S1: 'mean daily discharge 2.02 m3/s' when the run stores mm/day."""
        rl = self._run({"q_mean": "mm/day", "runoff_ratio": "-"})
        rep = _audit("Mean daily discharge: **2.02 m³/s** [run:s1#data.q_mean].", rl, monkeypatch)
        v = rep.violations[0]
        assert v.kind == "unit_mismatch" and v.severity == "hard"
        assert "mm/day" in v.message and v.stored_value == 2.0169

    def test_same_unit_in_other_spelling_passes(self, monkeypatch):
        rl = self._run({"q_mean": "mm d-1", "runoff_ratio": "-"})
        assert _audit("Mean 2.02 mm/day [run:s1#key_outputs.q_mean].", rl, monkeypatch).passed

    def test_no_unit_in_prose_or_in_run_is_not_a_mismatch(self, monkeypatch):
        rl = self._run({"q_mean": "mm/day"})
        assert _audit("Mean 2.02 [run:s1#key_outputs.q_mean].", rl, monkeypatch).passed
        rl_nounits = {"s1": {"key_outputs": {"q_mean": 2.0169}}}
        assert _audit("Mean 2.02 m3/s [run:s1#key_outputs.q_mean].", rl_nounits, monkeypatch).passed

    def test_percent_of_a_dimensionless_field_passes(self, monkeypatch):
        rl = self._run({"q_mean": "mm/day", "runoff_ratio": "-"})
        assert _audit("Runoff ratio 60.5% [run:s1#key_outputs.runoff_ratio].", rl, monkeypatch).passed

    def test_sibling_units_key(self, monkeypatch):
        rl = {"s": {"key_outputs": {"flow": 12.5, "flow_units": "m3/s"}}}
        rep = _audit("Flow 12.5 mm/day [run:s#key_outputs.flow].", rl, monkeypatch)
        assert rep.violations[0].kind == "unit_mismatch"

    def test_unit_helpers(self):
        assert normalise_unit("m³ s⁻¹") == normalise_unit("m3/s") == normalise_unit("cms")
        assert units_conflict("m³/s", "mm/day") is True
        assert units_conflict("km²", "km2") is False
        assert units_conflict("years", "mm/day") is False       # a time-count is not a unit claim
        assert units_conflict("m3/s", None) is False


# --------------------------------------------------------------------------- #
# Severity, report shape, backward compatibility
# --------------------------------------------------------------------------- #

class TestSeverityAndReport:
    def test_advisory_alone_does_not_block(self, monkeypatch):
        rep = _audit("We used bootstrap 90% CIs.", {}, monkeypatch)
        assert rep.passed is True
        assert rep.hard_count == 0 and rep.advisory_count == 1
        assert rep.violations[0].severity == "advisory"

    def test_hard_sorted_before_advisory(self, monkeypatch):
        rep = _audit("Bootstrap 90% CIs were used; the ratio is 0.42.", {}, monkeypatch)
        assert [v.severity for v in rep.violations] == ["hard", "advisory"]
        assert "1 hard" in rep.teaching_error() and "(advisory)" in rep.teaching_error()

    def test_every_violation_has_severity_and_old_fields(self, monkeypatch):
        rep = _audit("The ratio is 0.42.", {}, monkeypatch)
        d = rep.violations[0].model_dump()
        for k in ("kind", "text_excerpt", "marker_raw", "prose_value", "stored_value",
                  "message", "fix_hint", "severity"):
            assert k in d
        assert d["severity"] == "hard"

    def test_percent_fraction_tolerance_is_scaled(self):
        # "5%" must not match a stored fraction of 0.5 (old tolerance was 1.0 on the fraction)
        assert R._values_match("5%", 0.5) is False
        assert R._values_match("5%", 0.05) is True
        assert R._values_match("42%", 42) is True
        assert R._values_match("−0.4", -0.41) is True

    def test_exempt_never_counts_toward_coverage(self, monkeypatch):
        rep = _audit("USGS 01031500 has 3 layers, see Figure 2, HUC-02 = 01.", {}, monkeypatch)
        assert rep.passed and rep.total_numeric_count == 0 and rep.numeric_coverage == 1.0
        assert rep.exempt_count >= 4

    def test_lit_marker_binds_sample_size(self, monkeypatch):
        rep = _audit("The benchmark used 40 basins [lit:smith2020].", {}, monkeypatch)
        assert rep.passed and rep.lit_span_count == 1


# --------------------------------------------------------------------------- #
# Adversarial review of PR 4 (reviewer scripts adv.py / adv2.py / adv3.py)
# --------------------------------------------------------------------------- #

RL = {
    "r1": {"run_id": "r1", "tool_name": "sigs", "timestamp": "2026-01-01",
           "key_outputs": {"runoff_ratio": 0.61, "area_km2": 774.15, "nse": 0.61, "q95": 8.0,
                           "units": {"q95": "m3/s", "area_km2": "km2"}, "n_obs": 7305,
                           "small": 0.0032, "basin": 774},
           "evidence": {"data": {"runoff_ratio": 0.61},
                        "uncertainty": {"runoff_ratio": {"value": 0.61, "ci_low": 0.5, "ci_high": 0.7,
                                                         "n": 1000, "ci_level": 0.9}}},
           "inputs": {"gauge_id": "01031500", "n_boot": 1000}},
    "r2": {"run_id": "r2", "tool_name": "hollow", "timestamp": "2026-01-02", "key_outputs": {},
           "derived_from": ["r1"]},
}
CL = {"C-001": {"status": "supported", "scope": {"basins": ["12"]}}}


def _a(prose, monkeypatch, sid="S", site_ids=("01031500",)):
    monkeypatch.setattr(R, "_load_run_log", lambda s: copy.deepcopy(RL))
    monkeypatch.setattr(R, "_load_claims", lambda s: CL)
    monkeypatch.setattr(R, "_site_ids", lambda s: set(site_ids))
    return R.resolve_prose(prose, sid)


def _hard(rep):
    return [(v.kind, v.prose_value) for v in rep.violations if v.severity == "hard"]


class TestMarkerTextIsNotProse:
    """1. Marker brackets contributed the words "run"/"claim" to meta-count detection."""

    def test_q95_80_cited_to_a_run_is_checked(self, monkeypatch):
        rep = _a("Q95 was 80 [run:r1#q95].", monkeypatch)
        assert _hard(rep) == [("value_mismatch", "80")]
        rep = _a("Peak flow was 999 [run:r1#q95].", monkeypatch)
        assert _hard(rep) == [("value_mismatch", "999")]
        rep = _a("The area is 5000 [run:r1#area_km2].", monkeypatch)
        assert _hard(rep) == [("value_mismatch", "5000")]

    def test_meta_count_needs_no_metric_word_in_the_sentence(self):
        assert _cats("peak 412 runs")[0][1] == "quantity"
        assert _cats("area is 774 steps")[0][1] == "quantity"
        assert _cats("17 signatures show 0.61 skill")[0][1] == "quantity"
        assert _cats("17 signatures")[0][1:] == ("exempt", "meta_count")

    def test_bare_integer_in_a_sentence_with_a_marker_is_a_quantity(self, monkeypatch):
        assert _cats("we saw 412 [run:r1#nse]")[0][1] == "quantity"
        rep = _a("We saw 412 of them [run:r1#n_obs].", monkeypatch)
        assert _hard(rep) == [("value_mismatch", "412")]
        assert _cats("we saw 412 of them")[0][1:] == ("exempt", "bare_integer")

    def test_claim_marker_never_binds_a_number(self, monkeypatch):
        assert _hard(_a("Mean flow 3 [claim:C-001]", monkeypatch)) == [("uncited_number", "3")]


class TestIdExemptionCannotBeUsedToHide(object):
    """2. Exact ids / ID context never override a unit and are not model-controllable."""

    def test_unit_beats_id_context(self):
        assert _cats("12345678 cfs at USGS")[0][1] == "quantity"
        assert _cats("Flow is 12345678 cfs at USGS.")[0][1] == "quantity"
        assert _cats("Gauge 01031500 cfs", ["01031500"])[0][1] == "quantity"

    def test_own_id_needs_five_digits_and_no_unit(self):
        assert _cats("area 774 km2", ["774"])[0][1] == "quantity"
        assert _cats("area 774", ["774"])[0][1] == "quantity"
        assert _cats("flow 01031500 m3/s", ["01031500"])[0][1] == "quantity"

    def test_ids_from_claims_and_run_outputs_are_not_exempt(self, monkeypatch):
        # claim scope.basins=["12"], run inputs.gauge_id="01031500", key_outputs.basin=774:
        # none of these may exempt a number (site ids come only from the study itself)
        rep = _a("The area is 774 [run:r1#basin] and flow is 12 here.", monkeypatch, site_ids=())
        assert ("uncited_number", "12") in _hard(rep)
        rep = _a("flow 01031500 here", monkeypatch, site_ids=())
        assert _hard(rep) == [("uncited_number", "01031500")]

    def test_context_id_must_sit_beside_the_keyword(self):
        assert _cats("Area is 12345678 at USGS.")[0][1] == "quantity"
        assert _cats("Runoff 12345678 at gauge 12345678.")[1][1:] == ("exempt", "identifier")
        assert _cats("area is 01031500")[0][1] == "quantity"
        assert _cats("flow 1031500")[0][1] == "quantity"


class TestIdentifierMaskingIsNarrow:
    """3. Only real id shapes are masked; `letters + separator + number` is prose."""

    @pytest.mark.parametrize("txt,raw", [
        ("NSE:0.61 was the score.", "0.61"), ("NSE/0.61 was the score.", "0.61"),
        ("NSE_0.61", "0.61"), ("bias-12.5% overall", "12.5%"), ("KGE-0.61", "0.61"),
        ("Q95-8.2 m3/s", "8.2"), ("peak 412.5m3/s", "412.5"), ("Area ~774km2", "774"),
        ("NSE is _0.61_", "0.61"), ("NSE was v0.61.", "0.61"), ("NSE was site_0.61", "0.61"),
        ("NSE of .61", ".61"), ("NSE is *0.61*", "0.61"), ("NSE ~0.61", "0.61"),
        ("NSE gauge-0.61", "0.61"), ("NSE was run-774.15.", "774.15"), ("NSE of 0.61.5", "0.61"),
    ])
    def test_number_next_to_letters_is_extracted(self, txt, raw):
        assert raw in [n.raw for n in extract_numerics(txt)], txt
        assert any(c.span.raw == raw and c.category == "quantity" for c in classify_numerics(txt)), txt

    def test_real_id_shapes_still_masked(self):
        for tok in ("sigs.20261020.01031500.dd4ed1e7", "q.20260627.01031500.d83c",
                    "watershed.ce56bc643d4c", "c-01031500-signatures-20261020",
                    "5fcfedd8-aaaa-bbbb-cccc-0123456789ab", "claim.20261020.01031500.cf55a078"):
            assert extract_numerics(f"see {tok} now") == [], tok

    def test_unit_exponent_is_not_a_quantity(self):
        assert [n.raw for n in extract_numerics("flow 45 m3 s-1")] == ["45"]


class TestMaskingDoesNotHideNumbers:
    """4. Malformed markers, code spans with decimals, strict code labels."""

    @pytest.mark.parametrize("txt", [
        "NSE [run:r1#key_outputs.nse 0.61] fine", "NSE of [run:foo 0.61", "NSE was [lit:fake 0.61].",
        "NSE 0.99 [lit:] ", "NSE 0.99 [lit: ]", "NSE 0.99 [lit:a]", "x [claim:C 001] y",
    ])
    def test_malformed_marker_is_a_hard_violation(self, txt, monkeypatch):
        rep = _a(txt, monkeypatch)
        assert "malformed_marker" in [k for k, _ in _hard(rep)], txt
        assert rep.passed is False

    def test_lit_tag_is_a_single_token(self, monkeypatch):
        ok = _a("The benchmark NSE was 0.61 [lit:smith2020].", monkeypatch)
        assert ok.passed and ok.lit_span_count == 1
        bad = _a("The benchmark NSE was 0.61 [lit:Smith et al. 2020].", monkeypatch)
        assert "malformed_marker" in [k for k, _ in _hard(bad)] and bad.passed is False

    def test_well_formed_markers_are_not_malformed(self, monkeypatch):
        rep = _a("NSE 0.61 [run:r1#key_outputs.nse] and [lit:Smith2020] and [claim:C-001].", monkeypatch)
        assert "malformed_marker" not in [v.kind for v in rep.violations]

    def test_code_with_decimal_unit_or_percent_is_still_audited(self, monkeypatch):
        for txt in ("The result is `0.61`.", "```\nNSE = 0.61\n```", "peak `8 m3/s` here", "gain `12%`"):
            assert any(c.category == "quantity" for c in classify_numerics(txt)), txt
        assert not [c for c in classify_numerics("call `step 3 of 4` now") if c.category != "exempt"]
        assert _hard(_a("NSE is `0.61`.", monkeypatch)) == [("uncited_number", "0.61")]

    def test_code_labels_are_integer_only(self):
        assert _cats("HUC8 0.61 NSE")[0][1] == "quantity"
        assert _cats("EPSG 0.61")[0][1] == "quantity"
        assert _cats("NLCD 41%")[0][1] == "quantity"
        assert _cats("FIPS 23021.5")[0][1] == "quantity"
        assert _cats("HUC-02 = 01")[0][1:] == ("exempt", "code_label")
        assert _cats("EPSG:5070")[0][1:] == ("exempt", "code_label")


class TestHalfUlpTolerance:
    """5. A true half-ULP: the stored value must round to the printed digits."""

    def test_wide_tolerance_cases_now_fail(self):
        assert R._values_match("0.7", 0.61) is False
        assert R._values_match("9", 8.0) is False
        assert R._values_match("7306", 7305) is False
        assert R._values_match("0.62", 0.61) is False

    def test_correct_rounding_still_passes(self):
        assert R._values_match("0.61", 0.6050785) is True
        assert R._values_match("0.45", 0.4452) is True
        assert R._values_match("2.02", 2.0169) is True
        assert R._values_match("0.82", 0.819) is True
        assert R._values_match("8", 8.4) is True and R._values_match("8", 8.6) is False
        assert R._values_match("7,305", 7305) is True

    def test_through_resolve(self, monkeypatch):
        assert _hard(_a("Flow was 9 m3/s [run:r1#q95].", monkeypatch)) == [("value_mismatch", "9")]
        assert _hard(_a("Flow was 7 m3/s [run:r1#q95].", monkeypatch)) == [("value_mismatch", "7")]
        assert _hard(_a("Flow was 8.4 m3/s [run:r1#q95].", monkeypatch)) == [("value_mismatch", "8.4")]
        assert _hard(_a("Flow was 8.00 m3/s [run:r1#q95].", monkeypatch)) == []


class TestSuggestedMarkerCannotLaunder:
    """6. Exact round-half match only, and a compatible unit when one is declared."""

    def test_no_suggestion_for_a_near_miss(self, monkeypatch):
        rep = _a("The ratio is 0.72.", monkeypatch)
        assert rep.violations[0].suggested_marker is None

    def test_exact_value_is_suggested(self, monkeypatch):
        rep = _a("The area is 774.15 km2.", monkeypatch)
        assert rep.violations[0].suggested_marker == "[run:r1#key_outputs.area_km2]"

    def test_incompatible_unit_is_not_suggested(self, monkeypatch):
        rep = _a("The q95 is 8.0 mm/day.", monkeypatch)
        assert rep.violations[0].kind == "uncited_number"
        assert rep.violations[0].suggested_marker is None


class TestCoverageGaps:
    """7. Units, metric words, header units, years, scientific notation."""

    @pytest.mark.parametrize("txt", [
        "The basin drains 774 square kilometres.", "The basin drains 774 sq km.",
        "Area is 774 square miles.", "Area 774 sq. mi.", "Area 774 hectares", "Area 774 acres",
        "Flow was 45 cubic metres per second.", "Mean flow of 45 cubic feet per second.",
        "Rate 3 millimetres per day.", "Rate 3 mm per day.", "Flow 45 m\u00b3 s\u207b\u00b9.",
        "The median was 412 litres per second.", "Flow 45 L/s", "Flow 45 mgd", "Flow 45 cumecs",
        "A 3 \u00b0C warming.", "Temperature 15 deg C", "Temperature 15 degrees Celsius",
        "Runoff ratio was 61 per cent.", "Loss of 12 percentage points.",
        "Mean elevation 1450 metres", "Mean elevation 1450 meters", "Mean elevation 1450 feet",
        "Mean elevation 1450 ft", "Mean elevation 1,450 metres", "Depth 12 inches",
    ])
    def test_unit_phrases_make_a_quantity(self, txt):
        cn = [c for c in classify_numerics(txt)]
        assert cn and all(c.category == "quantity" and c.unit for c in cn), (txt, cn)

    @pytest.mark.parametrize("txt", [
        "Elevation ranges from 120 to 1450.", "The peak was 412 at USGS.", "drainage area, 774",
        "Total length 88", "Annual rainfall 1200", "Mean altitude 340", "Storage 55", "Recharge 12",
        "Sediment load 900", "Nitrate concentration 4", "Channel gradient 3", "Basin height 77",
        "Water yield 300", "Volume 5000",
    ])
    def test_metric_words_make_a_bare_integer_a_quantity(self, txt):
        assert all(c.category == "quantity" for c in classify_numerics(txt) if c.reason != "list_index"), txt

    def test_table_header_unit_and_metric_inheritance(self):
        one = classify_numerics("| Area (km\u00b2) | 774 |")
        assert [(c.span.raw, c.category, c.unit) for c in one] == [("774", "quantity", "km\u00b2")]
        tbl = "| Metric | Value |\n|---|---|\n| Area (km\u00b2) | 774 |\n| Peak flow | 412 |"
        got = [(c.span.raw, c.category) for c in classify_numerics(tbl)]
        assert got == [("774", "quantity"), ("412", "quantity")]
        hdr = "| Site | Area (km2) |\n|---|---|\n| A | 774 |"
        assert [(c.span.raw, c.category, c.unit) for c in classify_numerics(hdr)] == [("774", "quantity", "km2")]

    def test_header_unit_participates_in_unit_mismatch(self, monkeypatch):
        rep = _a("| Flow (cfs) | 8 [run:r1#q95] |", monkeypatch)
        assert _hard(rep) == [("unit_mismatch", "8")]

    def test_year_exempt_only_without_a_metric_word(self):
        assert _cats("in 2019")[0][1:] == ("exempt", "year")
        assert _cats("Peak flow in 2019 was 2019")[1][1] == "quantity"
        assert _cats("flow 1990")[0][1] == "quantity"
        assert _cats("flow was 1990")[0][1] == "quantity"
        assert _cats("as of 2024 the flow was 2024")[1][1] == "quantity"
        assert _cats("as of 2024 the flow was 2024")[0][1:] == ("exempt", "year")

    def test_scientific_notation_is_compared_with_its_exponent(self, monkeypatch):
        assert _hard(_a("Small 3.2\u00d710\u207b\u00b3 [run:r1#small].", monkeypatch)) == []
        assert _hard(_a("Small 3.2\u00d710\u207b\u00b3 [run:r1#nse].", monkeypatch)) == [
            ("value_mismatch", "3.2\u00d710\u207b\u00b3")]
        assert _hard(_a("Flow 8\u00d710\u2070 [run:r1#q95].", monkeypatch)) == []
        assert R._values_match("3.2e-3", 0.0032) is True and R._values_match("3.2e-3", 0.0042) is False
        assert R._values_match("10^3", 1000) is True

    def test_power_form_is_one_number(self):
        assert [n.raw for n in extract_numerics("Value is 10^3 m.")] == ["10^3"]
        assert _cats("Value is 10^3 m.")[0][1] == "quantity"


# --------------------------------------------------------------------------- #
# Data codes (USGS parameter / statistic, HUC, EPSG) are labels, not quantities
# --------------------------------------------------------------------------- #

_NWIS_ANSWER = (
    "Fetched hydrological data for the USGS basin in the working directory — "
    "**USGS 01031500, Piscataquis River near Dover-Foxcroft, Maine** (drainage area 774.15 km²; "
    "delineation run `watershed.0b124654840f`). ## Streamflow (observed, NWIS daily) - Product: "
    "`NWIS_STREAMFLOW`, USGS Water Data APIs, parameter 00060, statistic 00003 (daily mean) - "
    "Period: 1989-10-01 → 2009-09-30 - Records: 7,305 daily values, 0 missing, units m³/s"
)


def test_nwis_answer_exact_classification():
    got = _cats(_NWIS_ANSWER)
    assert got == [
        ("01031500", "exempt", "identifier"),
        ("774.15", "quantity", ""),            # genuinely uncited
        ("00060", "exempt", "data_code"),
        ("00003", "exempt", "data_code"),
        ("1989", "exempt", "date"),            # one mask per ISO date
        ("2009", "exempt", "date"),
        ("7,305", "exempt", "bare_integer"),   # "daily values" is not in the sample-noun list (see DECISIONS)
        ("0", "exempt", "bare_integer"),
    ]
    # the run id and the product name never surface as numerics at all
    assert not any("0b124654840f" in raw or "NWIS" in raw for raw, _, _ in got)


@pytest.mark.parametrize("txt,raw", [
    ("parameter 00060", "00060"),
    ("Parameter: 00060", "00060"),
    ("parameterCd=00060", "00060"),
    ("parameter code 00065", "00065"),
    ("param=00010", "00010"),
    ("pcode 00060", "00060"),
    ("parm_cd 00060", "00060"),
    ("statistic 00003", "00003"),
    ("statCd=00003", "00003"),
    ("stat_cd: 00001", "00001"),
    ("00060 parameter", "00060"),
])
def test_usgs_data_codes_exempt(txt, raw):
    assert (raw, "exempt", "data_code") in _cats(txt)


@pytest.mark.parametrize("txt", [
    "discharge 00060 cfs",              # malformed, carries a unit
    "discharge 00060",                  # value of a metric, no code keyword
    "streamflow of 00060",
    "parameter 00060 cfs",              # unit
    "parameter 00060.5",                # decimal
    "parameter -00060",                 # sign
    "parameter 60",                     # not 5-digit padded
    "parameter 10060",                  # not zero-padded
    "parameter 00060%",                 # percent
    "peak flow 00003 m3/s",
    "the peak statistic 01234",         # laundering edge: metric word qualifies the keyword
    "mean flow 01234 stat",
    "peak flow 01234 stat of the basin",
    "flow was 01234 parameter",
    "Peak flow 01234 statistic of the basin",
])
def test_data_code_not_exempt_when_quantity_shaped(txt):
    assert not any(c.reason == "data_code" for c in classify_numerics(txt)), txt


def test_laundering_cases_stay_quantity():
    for txt in ("the peak statistic 01234", "mean flow 01234 stat", "peak flow 01234 stat of the basin",
                "flow was 01234 parameter", "Peak flow 01234 statistic of the basin"):
        assert ("01234", "quantity", "") in _cats(txt), txt


def test_data_code_does_not_launder_neighbours():
    got = _cats("parameter 00060 peaked at 412 m³/s")
    assert ("00060", "exempt", "data_code") in got
    assert ("412", "quantity", "") in got


@pytest.mark.parametrize("txt,raw", [
    ("HUC 01020004 basin", "01020004"),
    ("HUC8 01020004", "01020004"),
    ("HUC-12: 010200040101", "12"),      # one mask covers the level and the code
    ("EPSG:5070 grid", "5070"),
    ("CRS EPSG 4326", "4326"),
    ("FIPS 23021", "23021"),
    ("state FIPS code 23", "23"),
    ("NLCD 2019", "2019"),
])
def test_huc_epsg_fips_nlcd_codes_exempt(txt, raw):
    assert (raw, "exempt", "code_label") in _cats(txt)
