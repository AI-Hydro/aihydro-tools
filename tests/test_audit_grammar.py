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
        txt = ("Run `0.42 in code` and see https://example.org/a/0.5/b?x=7.2 or /tmp/run_3.2/out.csv "
               "and [run:abc.2026.5#key_outputs.x] then ```\n1.5\n2.5\n``` done.")
        assert extract_numerics(txt) == []

    def test_identifier_shaped_tokens_are_masked(self):
        txt = "ids c-01031500-signatures-20261020, v2.3.7, EPSG:5070, wshed.20261020.01031500.5fcfedd8, 2.3.7"
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
        # 8-15 digit integer near USGS / gauge / COMID, no registry needed
        assert _cats("USGS 04085427 and COMID 22678241")[0][1:] == ("exempt", "identifier")
        # zero-padded 5+ digit integers are labels
        assert _cats("tile 00123")[0][1:] == ("exempt", "identifier")

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
