"""
Tests for the argument-repair + self-correcting-error middleware (WS-4).

Covers:
  - repair_arguments: alias rename, fuzzy match, type coercion (pure fn)
  - repair_arguments: no-op on already-valid args + empty props
  - ArgRepairMiddleware.on_call_tool: repairs args before execution
  - ArgRepairMiddleware.on_call_tool: returns structured self-help on failure
  - retry-loop breaker escalates after a repeated failing call
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_hydro.mcp.arg_repair import (
    ArgRepairMiddleware,
    repair_arguments,
    _coerce_type,
)


def _run(coro):
    return asyncio.run(coro)


class TestRepairArguments:
    def test_alias_rename(self):
        props = {"index_name", "session_id"}
        types = {"index_name": "string", "session_id": "string"}
        repaired, notes = repair_arguments({"index": "NDWI"}, props, types)
        assert repaired == {"index_name": "NDWI"}
        assert any("renamed" in n for n in notes)

    def test_fuzzy_match_close_key(self):
        props = {"latitude", "longitude"}
        types = {"latitude": "number", "longitude": "number"}
        # 'lattitude' (typo) is close enough to 'latitude' (>0.82)
        repaired, notes = repair_arguments({"lattitude": 28.2}, props, types)
        assert "latitude" in repaired
        assert "lattitude" not in repaired

    def test_no_rename_when_target_present(self):
        props = {"index_name"}
        types = {"index_name": "string"}
        # Both alias and canonical supplied → don't clobber canonical
        repaired, _ = repair_arguments(
            {"index": "NDWI", "index_name": "NDVI"}, props, types
        )
        assert repaired["index_name"] == "NDVI"

    def test_type_coercion_string_to_number(self):
        props = {"latitude"}
        types = {"latitude": "number"}
        repaired, notes = repair_arguments({"latitude": "28.2"}, props, types)
        assert repaired["latitude"] == 28.2
        assert any("coerced" in n for n in notes)

    def test_type_coercion_scalar_to_array(self):
        props = {"bands"}
        types = {"bands": "array"}
        repaired, _ = repair_arguments({"bands": "B4"}, props, types)
        assert repaired["bands"] == ["B4"]

    def test_valid_args_unchanged(self):
        props = {"index_name", "session_id"}
        types = {"index_name": "string", "session_id": "string"}
        repaired, notes = repair_arguments(
            {"index_name": "NDWI", "session_id": "abc"}, props, types
        )
        assert repaired == {"index_name": "NDWI", "session_id": "abc"}
        assert notes == []

    def test_empty_props_noop(self):
        repaired, notes = repair_arguments({"foo": 1}, set(), {})
        assert repaired == {"foo": 1}
        assert notes == []


class TestCoerceType:
    def test_bool_from_string(self):
        assert _coerce_type("true", "boolean") is True
        assert _coerce_type("no", "boolean") is False

    def test_int_from_string(self):
        assert _coerce_type("42", "integer") == 42
        assert _coerce_type("-7", "integer") == -7

    def test_number_from_string(self):
        assert _coerce_type("3.14", "number") == 3.14

    def test_unparseable_left_alone(self):
        assert _coerce_type("not-a-number", "number") == "not-a-number"


class _FakeMCP:
    """Minimal stand-in so the middleware can build its schema cache."""

    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        return self._tools


def _fake_tool(name, schema):
    mcp_tool = SimpleNamespace(name=name, inputSchema=schema)
    return SimpleNamespace(to_mcp_tool=lambda: mcp_tool)


_SCHEMA = {
    "type": "object",
    "properties": {
        "index_name": {"type": "string"},
        "session_id": {"type": "string"},
    },
    "required": ["index_name"],
}


def _make_mw():
    fake = _FakeMCP([_fake_tool("compute_spectral_index", _SCHEMA)])
    return ArgRepairMiddleware(fake)


def _ctx(name, arguments):
    msg = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(message=msg)


class TestMiddleware:
    def test_repairs_args_before_execution(self):
        mw = _make_mw()
        ctx = _ctx("compute_spectral_index", {"index": "NDWI"})

        captured = {}

        async def call_next(c):
            captured["args"] = c.message.arguments
            return "ok"

        result = _run(mw.on_call_tool(ctx, call_next))
        assert result == "ok"
        # alias 'index' was renamed to 'index_name' before the body ran
        assert captured["args"] == {"index_name": "NDWI"}

    def test_self_help_on_failure(self):
        mw = _make_mw()
        ctx = _ctx("compute_spectral_index", {"bogus": 1})

        async def call_next(c):
            raise ValueError("missing required argument index_name")

        from ai_hydro.mcp.errors import StructuredToolError
        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(ctx, call_next))
        payload = ei.value.envelope
        assert json.loads(str(ei.value)) == payload   # the error text IS the help payload
        assert payload["error"] is True
        assert payload["tool"] == "compute_spectral_index"
        assert "index_name" in payload["required"]
        assert "input_schema" in payload
        assert payload["example_call"]["tool"] == "compute_spectral_index"

    def test_retry_breaker_escalates(self):
        mw = _make_mw()

        async def call_next(c):
            raise ValueError("boom")

        # First failure: normal teaching message
        from ai_hydro.mcp.errors import StructuredToolError
        with pytest.raises(StructuredToolError) as e1:
            _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert "already tried" not in e1.value.envelope["message"].lower()
        # Second identical failure: escalated guidance
        with pytest.raises(StructuredToolError) as e2:
            _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert "already tried" in e2.value.envelope["message"].lower()

    def test_unknown_tool_passes_through(self):
        mw = _make_mw()
        ctx = _ctx("not_a_registered_tool", {"x": 1})

        async def call_next(c):
            raise ValueError("upstream error")

        # No schema info → middleware must not swallow; re-raises
        with pytest.raises(ValueError):
            _run(mw.on_call_tool(ctx, call_next))


class TestStructuredRefusalPassthrough:
    def test_structured_tool_error_passes_through_untaught(self):
        from ai_hydro.mcp.errors import StructuredToolError
        mw = _make_mw()
        err = StructuredToolError({"error": True, "code": "X", "message": "m"})

        async def call_next(c):
            raise err

        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert ei.value is err and ei.value.envelope["code"] == "X"

    def test_envelope_is_the_json_text(self):
        import json
        from ai_hydro.mcp.errors import StructuredToolError
        env = {"error": True, "code": "X", "message": "m", "next_tools": []}
        assert json.loads(str(StructuredToolError(env))) == env

    def test_legacy_json_text_toolerror_still_passes_through(self):
        import json
        from fastmcp.exceptions import ToolError
        mw = _make_mw()

        async def call_next(c):
            raise ToolError(json.dumps({"error": True, "code": "Y"}))

        with pytest.raises(ToolError):
            _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))

    def test_plain_toolerror_is_still_taught(self):
        from fastmcp.exceptions import ToolError
        mw = _make_mw()

        async def call_next(c):
            raise ToolError("Error calling tool: bad")

        from ai_hydro.mcp.errors import StructuredToolError
        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_spectral_index", {"bogus": 1}), call_next))
        assert ei.value.envelope["error"] is True and "example_call" in ei.value.envelope


# --------------------------------------------------------------------------- #
# T1-A: a repair never changes meaning, is disclosed, and is sealed
# --------------------------------------------------------------------------- #

from ai_hydro.mcp.arg_repair import (  # noqa: E402
    _inject_arg_repairs,
    judge_rename,
    repair_arguments_detailed,
    unit_tokens,
)
from ai_hydro.mcp.errors import StructuredToolError  # noqa: E402

_HYDROGRAPH = {
    "type": "object",
    "properties": {
        "area_km2": {"type": "number", "description": "Basin area in km2"},
        "rainfall_mm": {"type": "number"},
        "slope_length_m": {"type": "number"},
        "expected_area_km2": {"type": "number"},
        "discharge_m3s": {"type": "number"},
        "tc_hr": {"type": "number"},
        "model": {"type": "string"},
        "min_lat": {"type": "number"},
        "max_lat": {"type": "number"},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "series": {"type": "string"},
        "metric": {"type": "string"},
        "gauge_id": {"type": "string"},
        "session_id": {"type": "string"},
        "latitude": {"type": "number"},
        "longitude": {"type": "number"},
    },
    "required": ["area_km2"],
}


def _hmw():
    return ArgRepairMiddleware(_FakeMCP([_fake_tool("compute_design_hydrograph", _HYDROGRAPH)]))


def _detail(args):
    props = set(_HYDROGRAPH["properties"])
    types = {k: v.get("type") for k, v in _HYDROGRAPH["properties"].items()}
    return repair_arguments_detailed(args, props, types)


class TestUnitCrossingRefused:
    """The D06 / F20 audit probes."""

    @pytest.mark.parametrize("supplied,expected", [
        ("area_m2", "area_km2"),
        ("expected_area_m2", "expected_area_km2"),
        ("rainfall_m", "rainfall_mm"),
        ("slope_length_mm", "slope_length_m"),
        ("tc_s", "tc_hr"),
        ("discharge_cfs", "discharge_m3s"),
        ("discharge", "discharge_m3s"),       # a bare name never picks a unit
        ("area", "area_km2"),
    ])
    def test_no_rename_across_units(self, supplied, expected):
        out = _detail({supplied: 2.5e9})
        assert supplied in out.arguments and out.arguments[supplied] == 2.5e9
        assert expected not in out.arguments
        assert not any(r["kind"] == "rename" for r in out.repairs)
        assert out.unit_conflicts and out.unit_conflicts[0]["to"] == expected

    def test_middleware_returns_structured_error_and_never_runs_the_tool(self):
        mw = _hmw()
        ran = []

        async def call_next(c):
            ran.append(c.message.arguments)
            return "ran"

        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"area_m2": 2.5e9}), call_next))
        env = ei.value.envelope
        assert ran == []
        assert env["error"] is True and env["code"] == "ARG_UNIT_MISMATCH"
        assert env["supplied_parameter"] == "area_m2"
        assert env["expected_parameter"] == "area_km2"
        assert env["expected_unit"] == "km2" and env["supplied_unit"] == "m2"
        assert env["rescaled"] is False
        assert env["expected_parameter_description"] == "Basin area in km2"

    def test_values_are_never_rescaled(self):
        out = _detail({"area_m2": 2500000.0})
        assert out.arguments == {"area_m2": 2500000.0}

    def test_correct_unit_passes_untouched(self):
        out = _detail({"area_km2": 2500.0, "rainfall_mm": 50})
        assert out.arguments == {"area_km2": 2500.0, "rainfall_mm": 50} and not out.repairs


class TestNoOtherMeaningChange:
    @pytest.mark.parametrize("supplied", [
        "mode",          # mode -> model (F20)
        "metrics",       # plural list -> singular
        "series_a",      # sibling of series
        "series_b",
        "gauge_ids",     # list -> scalar id
        "feature_id",
        "min_lon", "max_lon",   # not a parameter here; must not flip to min_lat/max_lat
    ])
    def test_sibling_or_shape_change_is_not_renamed(self, supplied):
        out = _detail({supplied: "x"})
        assert not any(r["kind"] == "rename" for r in out.repairs), out.repairs
        assert supplied in out.arguments

    def test_min_max_swap_refused(self):
        assert not judge_rename("max_lat", "min_lat").ok
        assert not judge_rename("end", "start").ok
        assert not judge_rename("longitude", "latitude").ok
        assert not judge_rename("train_start", "val_start").ok
        assert not judge_rename("series_a", "series_b").ok

    def test_list_is_not_pushed_into_scalar_target(self):
        out = _detail({"gauge": ["01", "02"]})   # alias, but the target is a string
        assert "gauge" in out.arguments and "gauge_id" not in out.arguments

    def test_site_is_not_a_gauge_alias(self):
        for key in ("site", "site_id"):
            assert "gauge_id" not in _detail({key: "01646500"}).arguments

    def test_axis_aliases_still_apply(self):
        out = _detail({"lat": 40.0, "lon": -86.0})
        assert out.arguments == {"latitude": 40.0, "longitude": -86.0}

    def test_pure_spelling_repairs_still_apply(self):
        assert _detail({"lattitude": 40.0}).arguments == {"latitude": 40.0}
        assert _detail({"areaKm2": 5.0}).arguments == {"area_km2": 5.0}
        assert _detail({"Area_Km2": 5.0}).arguments == {"area_km2": 5.0}
        assert _detail({"start_date": "2020-01-01"}).arguments == {"start": "2020-01-01"}
        assert _detail({"sid": "s"}).arguments == {"session_id": "s"}

    def test_non_finite_strings_are_not_coerced(self):
        for bad in ("nan", "NaN", "inf", "-inf", "Infinity"):
            assert _coerce_type(bad, "number") == bad
        assert _coerce_type("1e3", "number") == 1000.0


class TestUnitTokens:
    def test_parses_units(self):
        assert unit_tokens("area_km2") == ("km2",)
        assert unit_tokens("discharge_m3s") == ("m3s",)
        assert unit_tokens("tc_hr") == ("hr",)
        assert unit_tokens("max_wait_seconds") == ("s",)
        assert unit_tokens("rainfall_mm") == ("mm",)
        assert unit_tokens("flowLengthKm") == ("km",)
        assert unit_tokens("session_id") == ()


class TestDisclosure:
    def _result(self, payload):
        text = SimpleNamespace(text=json.dumps(payload))
        return SimpleNamespace(structured_content=dict(payload), content=[text])

    def test_repair_is_in_the_tool_result(self):
        mw = _hmw()

        async def call_next(c):
            return self._result({"peak": 1.0})

        res = _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"sid": "s", "area_km2": "2500"}), call_next))
        entries = res.structured_content["_arg_repairs"]
        assert {"kind": "rename", "from": "sid", "to": "session_id", "rule": "alias"} in entries
        assert any(e["kind"] == "coerce" and e["param"] == "area_km2" for e in entries)
        assert json.loads(res.content[0].text)["_arg_repairs"] == entries

    def test_no_repair_no_field(self):
        mw = _hmw()

        async def call_next(c):
            return self._result({"peak": 1.0})

        res = _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"area_km2": 2500.0}), call_next))
        assert "_arg_repairs" not in res.structured_content

    def test_error_result_is_disclosed_too(self):
        res = self._result({"error": True, "code": "X", "message": "m"})
        _inject_arg_repairs(res, [{"kind": "rename", "from": "a", "to": "b", "rule": "alias", "note": "n"}])
        assert res.structured_content["_arg_repairs"][0]["to"] == "b"

    def test_non_dict_result_gets_an_extra_block(self):
        res = SimpleNamespace(structured_content={"result": [1, 2]}, content=[SimpleNamespace(text="[1, 2]")])
        _inject_arg_repairs(res, [{"kind": "rename", "from": "a", "to": "b", "rule": "alias", "note": "n"}])
        assert res.content[0].text == "[1, 2]"
        assert json.loads(res.content[-1].text)["_arg_repairs"][0]["from"] == "a"

    def test_teach_payload_lists_applied_repairs(self):
        mw = _hmw()

        async def call_next(c):
            raise ValueError("boom")

        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"sid": "s"}), call_next))
        assert ei.value.envelope["_arg_repairs"][0]["to"] == "session_id"


class TestSealedRunRecord:
    def test_seal_describes_the_arguments_that_ran(self):
        from ai_hydro.session import run_records

        mw = _hmw()
        supplied = {"sid": "s1", "area_km2": 10.0}
        capture, token = run_records.begin_capture(
            tool="compute_design_hydrograph", arguments=dict(supplied), chat_id=None, study_id=None)
        try:
            async def call_next(c):
                return SimpleNamespace(structured_content={"ok": 1}, content=[])

            _run(mw.on_call_tool(_ctx("compute_design_hydrograph", dict(supplied)), call_next))
        finally:
            run_records.end_capture(token)
        assert capture.call["effective_arguments"] == {"session_id": "s1", "area_km2": 10.0}
        assert capture.call["arguments"] == capture.call["effective_arguments"]
        assert capture.notes["arg_repairs"][0]["to"] == "session_id"
        assert capture.notes["supplied_input_digest"].startswith("sha256:")

    def test_record_call_digests_effective_arguments(self, monkeypatch):
        from ai_hydro.session import run_records

        seen = {}
        monkeypatch.setattr(
            run_records, "_record_call",
            lambda **kw: seen.update(arguments=kw["arguments"]) or run_records.RecordOutcome("no_session"))
        capture, token = run_records.begin_capture(tool="t", arguments={"sid": "s"})
        try:
            run_records.note_effective_arguments({"sid": "s"}, {"session_id": "s"}, [{"kind": "rename"}])
        finally:
            run_records.end_capture(token)
        run_records.record_call(tool="t", arguments={"sid": "s"}, result=None, failure=None, capture=capture,
                                chat_id=None, id_factory=lambda *a: "r")
        assert seen["arguments"] == {"session_id": "s"}

    def test_arg_repairs_is_a_transport_key(self):
        from ai_hydro.session.run_records import output_payload

        assert output_payload({"a": 1, "_arg_repairs": [{"x": 1}]}) == {"a": 1}

    def test_repair_runs_inside_the_run_record(self):
        """RunRecordMiddleware must wrap ArgRepairMiddleware so repair can seal into its capture."""
        from ai_hydro.mcp import mcp
        from ai_hydro.mcp.app import RunRecordMiddleware

        order = [type(m).__name__ for m in mcp.middleware]
        assert order.index(RunRecordMiddleware.__name__) < order.index("ArgRepairMiddleware")


class TestEverySchema:
    """Property-style guard over the real tool surface: no repair crosses a semantic token."""

    @pytest.fixture(scope="class")
    def schemas(self):
        from ai_hydro.mcp import mcp

        out = {}
        for t in _run(mcp.list_tools()):
            s = t.to_mcp_tool().inputSchema or {}
            props = s.get("properties") or {}
            out[t.name] = (set(props), {k: (v or {}).get("type") for k, v in props.items()})
        assert len(out) > 50
        return out

    def test_no_rename_changes_units_or_qualifiers(self, schemas):
        from ai_hydro.mcp.arg_repair import _GLOBAL_ALIASES, _split

        probes = set(_GLOBAL_ALIASES)
        for props, _ in schemas.values():
            probes |= props
        checked = 0
        for tool, (props, types) in schemas.items():
            for probe in sorted(probes):
                if probe in props:
                    continue
                out = repair_arguments_detailed({probe: 1}, props, types)
                for r in out.repairs:
                    if r["kind"] != "rename":
                        continue
                    checked += 1
                    assert unit_tokens(r["from"]) == unit_tokens(r["to"]), (tool, r)
                    assert _split(r["from"])[0] == _split(r["to"])[0], (tool, r)
        assert checked > 100

    def test_every_unit_variant_of_every_parameter_is_refused(self, schemas):
        from ai_hydro.mcp.arg_repair import _UNIT_TOKENS, _canon

        swaps = ["m", "mm", "km", "m2", "km2", "ha", "hr", "s", "day", "pct", "frac", "m3s", "cfs", "deg"]
        n = 0
        for tool, (props, types) in schemas.items():
            for p in sorted(props):
                toks = p.split("_")
                idx = [i for i, t in enumerate(toks) if _canon(t) in _UNIT_TOKENS and not t.isalpha() or t in swaps]
                for i in idx:
                    for sw in swaps:
                        if _canon(sw) == _canon(toks[i]):
                            continue
                        variant = "_".join(toks[:i] + [sw] + toks[i + 1:])
                        if variant in props:
                            continue
                        out = repair_arguments_detailed({variant: 1.0}, props, types)
                        n += 1
                        assert not [r for r in out.repairs if r["kind"] == "rename" and r["to"] == p], (
                            tool, variant, p)
        assert n > 50


class TestStructuredRefusalCarriesRepairs:
    def test_repairs_merged_into_envelope(self):
        mw = _hmw()

        async def call_next(c):
            raise StructuredToolError({"error": True, "code": "SESSION_RESOLUTION_FAILED", "message": "m"})

        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"gauge": "xyz"}), call_next))
        env = ei.value.envelope
        assert env["code"] == "SESSION_RESOLUTION_FAILED"
        assert env["_arg_repairs"] == [{"kind": "rename", "from": "gauge", "to": "gauge_id", "rule": "alias"}]
        assert json.loads(str(ei.value)) == env

    def test_no_repair_re_raises_same_error(self):
        mw = _hmw()
        err = StructuredToolError({"error": True, "code": "X"})

        async def call_next(c):
            raise err

        with pytest.raises(StructuredToolError) as ei:
            _run(mw.on_call_tool(_ctx("compute_design_hydrograph", {"gauge_id": "x"}), call_next))
        assert ei.value is err

    def test_through_the_real_fastmcp_client(self):
        from fastmcp import Client, FastMCP
        from fastmcp.exceptions import ToolError

        app = FastMCP("t")

        @app.tool()
        def fetch_streamflow(gauge_id: str, start_date: str = "") -> dict:
            """Refuses with a structured envelope."""
            raise StructuredToolError({"error": True, "code": "SESSION_RESOLUTION_FAILED", "message": "no"})

        async def _list_tools():          # the real app installs this shim on its FastMCP too
            return list((await app.get_tools()).values())

        app.list_tools = _list_tools
        app.add_middleware(ArgRepairMiddleware(app))

        async def go():
            async with Client(app) as c:
                with pytest.raises(ToolError) as ei:
                    await c.call_tool("fetch_streamflow", {"gauge": "xyz"})
                return str(ei.value)

        text = _run(go())
        env = json.loads(text[text.index("{"):])
        assert env["code"] == "SESSION_RESOLUTION_FAILED"
        assert env["_arg_repairs"][0]["from"] == "gauge" and env["_arg_repairs"][0]["to"] == "gauge_id"


class TestGeometryPrAliasesStillApply:
    """tools#21 adds start->start_date and end->end_date; they must pass the rule in alias mode."""

    def test_judge_allows_them(self):
        assert judge_rename("start", "start_date", alias=True).ok
        assert judge_rename("end", "end_date", alias=True).ok
        assert not judge_rename("start", "end_date", alias=True).ok

    def test_rename_applies_when_tool_has_only_date_params(self, monkeypatch):
        from ai_hydro.mcp import arg_repair

        monkeypatch.setitem(arg_repair._GLOBAL_ALIASES, "start", "start_date")
        monkeypatch.setitem(arg_repair._GLOBAL_ALIASES, "end", "end_date")
        props = {"gauge_id", "start_date", "end_date"}
        types = {"gauge_id": "string", "start_date": "string", "end_date": "string"}
        out = repair_arguments_detailed({"start": "2020-01-01", "end": "2020-12-31"}, props, types)
        assert out.arguments == {"start_date": "2020-01-01", "end_date": "2020-12-31"}
        assert not out.unit_conflicts
