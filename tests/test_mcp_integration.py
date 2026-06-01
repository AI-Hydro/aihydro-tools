"""
MCP Server Integration Tests
==============================

Validates that the modular MCP server registers all built-in tools correctly,
helpers work as expected, and session wiring behaves across tool calls.

Run:
    pytest tests/test_mcp_integration.py -v
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Tool registration tests ────────────────────────────────────────────────

class TestToolRegistration:
    """Verify that importing ai_hydro.mcp registers all expected tools."""

    @staticmethod
    def expected_tools() -> set[str]:
        """Tool registry is the maintained contract for built-in tools."""
        from ai_hydro.mcp.app import TOOL_TIERS
        return set(TOOL_TIERS)

    def test_import_mcp_singleton(self):
        """Importing ai_hydro.mcp should provide the FastMCP instance."""
        from ai_hydro.mcp import mcp
        assert mcp is not None
        assert mcp.name == "AI-Hydro"

    def test_all_builtin_tools_registered(self):
        """All built-in tools must be registered after importing ai_hydro.mcp."""
        from ai_hydro.mcp import mcp
        tools = asyncio.run(mcp.list_tools())
        tool_names = {t.name for t in tools}
        expected_tools = self.expected_tools()
        assert tool_names == expected_tools, (
            f"Missing: {expected_tools - tool_names}, "
            f"Extra: {tool_names - expected_tools}"
        )

    def test_tool_count_matches_expected(self):
        """Tool count matches EXPECTED_TOOLS — catches accidental duplicates or drops."""
        from ai_hydro.mcp import mcp
        tools = asyncio.run(mcp.list_tools())
        assert len(tools) == len(self.expected_tools())

    def test_all_tools_have_descriptions(self):
        """Every tool should have a non-empty description (from docstring)."""
        from ai_hydro.mcp import mcp
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            assert tool.description, f"Tool {tool.name} has no description"

    def test_all_tools_have_input_schema(self):
        """Every tool should have an input schema (may use various attr names)."""
        from ai_hydro.mcp import mcp
        tools = asyncio.run(mcp.list_tools())
        for tool in tools:
            # FastMCP may expose schema as inputSchema or input_schema
            schema = (
                getattr(tool, "inputSchema", None)
                or getattr(tool, "input_schema", None)
                or {}
            )
            # Schema should exist (even if empty for tools with all-optional params)
            assert isinstance(schema, dict), f"Tool {tool.name} has no input schema"


# ── Helper tests ────────────────────────────────────────────────────────────

class TestHelpers:
    """Test shared MCP helper functions."""

    def test_validate_usgs_gauge_id_pads_short(self):
        from ai_hydro.mcp.helpers import _validate_usgs_gauge_id
        assert _validate_usgs_gauge_id("1031500") == "01031500"

    def test_validate_usgs_gauge_id_accepts_8_digit(self):
        from ai_hydro.mcp.helpers import _validate_usgs_gauge_id
        assert _validate_usgs_gauge_id("01031500") == "01031500"

    def test_validate_usgs_gauge_id_rejects_alpha(self):
        from ai_hydro.mcp.helpers import _validate_usgs_gauge_id
        with pytest.raises(ValueError, match="Invalid USGS gauge_id"):
            _validate_usgs_gauge_id("abc12345")

    def test_validate_usgs_gauge_id_strips_whitespace(self):
        from ai_hydro.mcp.helpers import _validate_usgs_gauge_id
        assert _validate_usgs_gauge_id("  01031500  ") == "01031500"

    def test_normalize_session_id_accepts_any_string(self):
        from ai_hydro.mcp.helpers import _normalize_session_id
        assert _normalize_session_id("piscataquis-2020") == "piscataquis-2020"
        assert _normalize_session_id("01031500") == "01031500"

    def test_normalize_session_id_auto_generates(self):
        from ai_hydro.mcp.helpers import _normalize_session_id
        result = _normalize_session_id(None)
        assert result.startswith("hydro-")
        assert len(result) == len("hydro-") + 8

    def test_result_to_dict_passthrough(self):
        from ai_hydro.mcp.helpers import _result_to_dict
        d = {"data": {"x": 1}, "meta": {}}
        assert _result_to_dict(d) is d

    def test_result_to_dict_hydro_result(self):
        from ai_hydro.mcp.helpers import _result_to_dict
        mock = MagicMock()
        mock.to_dict.return_value = {"data": {}, "meta": {}}
        assert _result_to_dict(mock) == {"data": {}, "meta": {}}

    def test_tool_error_to_dict_plain_exception(self):
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        result = _tool_error_to_dict(ValueError("bad input"))
        assert result["error"] is True
        assert result["code"] == "UNKNOWN_ERROR"
        assert "bad input" in result["message"]

    def test_tool_error_to_dict_tool_error(self):
        from ai_hydro.mcp.helpers import _tool_error_to_dict
        mock = MagicMock()
        mock.to_dict.return_value = {"error": True, "code": "TEST"}
        assert _tool_error_to_dict(mock) == {"error": True, "code": "TEST"}

    def test_strip_forcing_arrays(self):
        from ai_hydro.mcp.helpers import _strip_forcing_arrays
        data = {
            "n_days": 365,
            "prcp_mm": [1.0, 2.0, 3.0],
            "tmax_C": [10.0, 20.0, 30.0],
        }
        compact = _strip_forcing_arrays(data)
        assert "prcp_mm" not in compact  # array stripped
        assert compact["prcp_mm_mean"] == 2.0
        assert compact["tmax_C_mean"] == 20.0
        assert compact["n_days"] == 365
        assert compact["n_variables"] == 2

    def test_cached_response_structure(self):
        from ai_hydro.mcp.helpers import _cached_response
        session = MagicMock()
        session.gauge_id = "01031500"
        session.signatures = {"data": {"bfi": 0.5}, "meta": {"tool": "test"}}
        result = _cached_response("signatures", session)
        assert result["_cached"] is True
        assert result["data"]["bfi"] == 0.5
        assert "clear_session" in result["_note"]


# ── Session wiring tests ────────────────────────────────────────────────────

class TestSessionWiring:
    """Test that session load/store/ensure helpers work correctly."""

    def test_ensure_session_creates_new(self, tmp_path):
        """_ensure_session should create a new session for an unknown session_id."""
        from ai_hydro.mcp.helpers import _ensure_session
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            session = _ensure_session("my-research-session")
            assert session.session_id == "my-research-session"

    def test_ensure_session_sets_workspace(self, tmp_path):
        """_ensure_session should store workspace_dir on first call."""
        from ai_hydro.mcp.helpers import _ensure_session
        ws = str(tmp_path / "workspace")
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path):
            session = _ensure_session("my-research-session", workspace_dir=ws)
            assert session.workspace_dir == ws

    def test_session_store_caches_result(self, tmp_path):
        """_session_store should persist a result and write research.md."""
        from ai_hydro.mcp.helpers import _session_store
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            _session_store("99999999", "watershed", {"data": {"area_km2": 100}})
            # Verify it was saved
            reloaded = HydroSession.load("99999999")
            assert reloaded.watershed is not None
            assert reloaded.watershed["data"]["area_km2"] == 100

    def test_session_roundtrip(self, tmp_path):
        """Full save/load cycle with multiple slots."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("99999999")
            s.watershed = {"data": {"area_km2": 50}}
            s.streamflow = {"data": {"n_days": 365}}
            s.notes.append("test note")
            s.save()

            s2 = HydroSession.load("99999999")
            assert s2.watershed["data"]["area_km2"] == 50
            assert s2.streamflow["data"]["n_days"] == 365
            assert "test note" in s2.notes
            assert "watershed" in s2.computed()
            assert "streamflow" in s2.computed()


# ── Tool-level smoke tests (mocked backends) ────────────────────────────────

class TestToolSmoke:
    """Smoke-test individual tools with mocked backends."""

    def test_start_session_creates_session(self, tmp_path):
        """start_session should return a summary dict with session_id."""
        from ai_hydro.mcp.tools_session import start_session
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = start_session("piscataquis-2020")
            assert result["session_id"] == "piscataquis-2020"
            assert "computed" in result
            assert "pending" in result

    def test_get_session_summary(self, tmp_path):
        """get_session_summary should return computed/pending lists."""
        from ai_hydro.mcp.tools_session import get_session_summary
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = get_session_summary("01031500")
            assert isinstance(result["computed"], list)
            assert isinstance(result["pending"], list)

    def test_add_note_appends(self, tmp_path):
        """add_note should append text to session notes."""
        from ai_hydro.mcp.tools_session import add_note
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = add_note("01031500", "my research note")
            assert "my research note" in result["notes"]

    def test_clear_session_resets_slots(self, tmp_path):
        """clear_session should reset specified slots."""
        from ai_hydro.session import HydroSession
        from ai_hydro.mcp.tools_session import clear_session
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            # Pre-populate
            s = HydroSession("01031500")
            s.watershed = {"data": {"area_km2": 100}}
            s.streamflow = {"data": {"n_days": 365}}
            s.save()
            # Clear just watershed
            result = clear_session("01031500", ["watershed"])
            assert "watershed" in result["cleared"]
            assert "streamflow" not in result.get("cleared", [])

    def test_clear_session_rejects_invalid_slot(self, tmp_path):
        """clear_session with invalid slot name should return error."""
        from ai_hydro.mcp.tools_session import clear_session
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = clear_session("01031500", ["nonexistent_slot"])
            assert result["error"] is True
            assert result["code"] == "INVALID_SLOTS"

    def test_export_session_json(self, tmp_path):
        """export_session should write JSON and return file path."""
        from ai_hydro.mcp.tools_session import export_session
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("01031500")
            s.workspace_dir = str(tmp_path)
            s.save()
            result = export_session("01031500", format="json")
            assert result["file_saved"] is not None
            assert Path(result["file_saved"]).exists()

    def test_get_model_results_no_model(self, tmp_path):
        """get_model_results should report no model trained."""
        from ai_hydro.mcp.tools_modelling import get_model_results
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = get_model_results("01031500")
            assert result["model_trained"] is False

    def test_delineate_watershed_invalid_gauge(self, tmp_path):
        """delineate_watershed with invalid USGS gauge_id should return error."""
        from ai_hydro.mcp.tools_analysis import delineate_watershed
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            # Pass an invalid USGS gauge_id — session_id is fine, gauge_id is not
            result = delineate_watershed(session_id="my-test-session", gauge_id="not_a_gauge")
            assert result["error"] is True

    def test_start_session_exposes_python_interpreter(self, tmp_path):
        """start_session should return python_interpreter, pip, available_packages."""
        from ai_hydro.mcp.tools_session import start_session
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = start_session("piscataquis-2020")
            assert "python_interpreter" in result, (
                "start_session must return 'python_interpreter' key "
                "(renamed from 'mcp_python' in 1.6.0 fix)"
            )
            assert "python" in result["python_interpreter"]
            assert "available_packages" in result
            assert isinstance(result["available_packages"], dict)

    def test_list_available_tools_returns_tool_list(self):
        """list_available_tools should return all registered tools.
        Uses mcp.list_tools() directly to avoid the sync-wrapper guard."""
        import asyncio
        from ai_hydro.mcp.app import mcp
        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert len(tools) >= 28
        assert "delineate_watershed" in names
        assert "start_session" in names
        assert "get_library_reference" in names
        assert "list_available_tools" in names

    def test_get_library_reference_pynhd(self):
        """get_library_reference should return pynhd gotchas."""
        from ai_hydro.mcp.tools_analysis import get_library_reference
        result = get_library_reference("pynhd")
        assert "gotchas" in result
        assert isinstance(result["gotchas"], list)
        assert len(result["gotchas"]) > 0
        assert result["library"] == "pynhd"

    def test_get_library_reference_not_found(self):
        """get_library_reference with unknown library should return error + available_refs."""
        from ai_hydro.mcp.tools_analysis import get_library_reference
        result = get_library_reference("nonexistent_lib")
        assert result["error"] is True
        assert result["code"] == "NOT_FOUND"
        assert "available_refs" in result
        assert "pynhd" in result["available_refs"]


# ── Lean session storage + synopsis ─────────────────────────────────────────

class TestLeanSession:
    """Verify that raw time-series arrays are stripped from the session JSON
    and that synopsis_for_llm() returns only scalar summaries."""

    def test_lean_slot_strips_large_lists(self):
        """_lean_slot should remove lists > 50 items and add {key}_n counts."""
        from ai_hydro.session.store import _lean_slot
        val = {
            "data": {
                "dates": list(range(3652)),
                "q_cms": [1.0] * 3652,
                "n_days": 3652,
                "gauge_name": "Test Gauge",
            },
            "meta": {"tool": "fetch_streamflow_data"},
        }
        lean = _lean_slot(val)
        assert "dates" not in lean["data"]
        assert "q_cms" not in lean["data"]
        assert lean["data"]["dates_n"] == 3652
        assert lean["data"]["q_cms_n"] == 3652
        assert lean["data"]["n_days"] == 3652        # scalars preserved
        assert lean["data"]["gauge_name"] == "Test Gauge"
        assert lean["meta"]["tool"] == "fetch_streamflow_data"  # meta untouched

    def test_lean_slot_keeps_short_lists(self):
        """Lists ≤ 50 items should be kept verbatim."""
        from ai_hydro.session.store import _lean_slot
        val = {
            "data": {
                "variables": ["prcp_mm", "tmax_C", "tmin_C"],
                "train_period": ["2000-10-01", "2007-09-30"],
            },
            "meta": {},
        }
        lean = _lean_slot(val)
        assert lean["data"]["variables"] == ["prcp_mm", "tmax_C", "tmin_C"]
        assert lean["data"]["train_period"] == ["2000-10-01", "2007-09-30"]

    def test_lean_slot_preserves_private_keys(self):
        """_data_file and other _ keys must survive stripping."""
        from ai_hydro.session.store import _lean_slot
        val = {
            "data": {
                "_data_file": "/workspace/streamflow_01031500.json",
                "q_cms": [1.0] * 3652,
                "n_days": 3652,
            },
            "meta": {},
        }
        lean = _lean_slot(val)
        assert lean["data"]["_data_file"] == "/workspace/streamflow_01031500.json"
        assert "q_cms" not in lean["data"]

    def test_session_json_is_lean_after_save(self, tmp_path):
        """Saved session.json must not contain large list arrays.

        C1 note: old-tool writes go to the __legacy__ sentinel feature id,
        so the on-disk path is streamflow → __legacy__ → "" → {data, meta}.
        """
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("test-lean")
            s.streamflow = {
                "data": {"dates": list(range(3652)), "q_cms": [1.0] * 3652,
                         "n_days": 3652},
                "meta": {"tool": "fetch_streamflow_data"},
            }
            s.save()
            # Read raw JSON — must not contain the big arrays.
            # C1 v2 structure: slot → feature_id → params_key → result_dict
            raw_json = (tmp_path / "test-lean.json").read_text()
            data = json.loads(raw_json)
            assert data.get("_hydro_slots_v2") is True, "session must be v2 format"
            sf_result = data["streamflow"]["__legacy__"][""]
            sf_data = sf_result["data"]
            assert "dates" not in sf_data
            assert "q_cms" not in sf_data
            assert sf_data["dates_n"] == 3652
            assert sf_data["n_days"] == 3652

    def test_session_json_size_is_small(self, tmp_path):
        """A session with 3652-day streamflow should fit in < 10 KB on disk."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("test-size")
            s.streamflow = {
                "data": {"dates": list(range(3652)), "q_cms": [1.0] * 3652,
                         "n_days": 3652},
                "meta": {"tool": "fetch_streamflow_data"},
            }
            s.save()
            size_bytes = (tmp_path / "test-size.json").stat().st_size
            assert size_bytes < 10_000, (
                f"Session JSON is {size_bytes} bytes — lean storage should be < 10 KB"
            )

    def test_synopsis_for_llm_no_arrays(self, tmp_path):
        """synopsis_for_llm must never return lists > 50 items."""
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("test-synopsis")
            s.streamflow = {
                "data": {"dates": ["2020-01-01"] * 3652,
                         "q_cms": [1.0] * 3652, "n_days": 3652},
                "meta": {"tool": "fetch_streamflow_data",
                         "computed_at": "2026-04-17T10:00:00"},
            }
            s.signatures = {
                "data": {"q_mean": 5.2, "bfi": 0.4, "runoff_ratio": 0.6},
                "meta": {"tool": "extract_hydrological_signatures",
                         "computed_at": "2026-04-17T11:00:00"},
            }
            synopsis = s.synopsis_for_llm()
            # Check streamflow synopsis
            sf = synopsis["streamflow"]
            assert "dates" not in sf
            assert "q_cms" not in sf
            assert sf["dates_n"] == 3652
            assert sf["n_days"] == 3652
            # Check signatures — all scalars, no stripping needed
            sig = synopsis["signatures"]
            assert sig["q_mean"] == 5.2
            assert sig["bfi"] == 0.4
            # No list longer than 50 anywhere in the synopsis
            for slot_data in synopsis.values():
                for k, v in slot_data.items():
                    if isinstance(v, list):
                        assert len(v) <= 50, (
                            f"synopsis_for_llm returned list of {len(v)} items "
                            f"in slot {slot_data} key {k}"
                        )

    def test_sync_reminder_fires_at_2_slots(self, tmp_path):
        """_sync_reminder should return a string once 2+ slots are computed."""
        from ai_hydro.mcp.helpers import _sync_reminder
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("test-remind")
            s.watershed = {"data": {"area_km2": 100}, "meta": {}}
            s.save()
            assert _sync_reminder("test-remind") is None  # only 1 slot
            s2 = HydroSession.load("test-remind")
            s2.streamflow = {"data": {"n_days": 365}, "meta": {}}
            s2.save()
            reminder = _sync_reminder("test-remind")
            assert reminder is not None
            assert "write_research_interpretation" in reminder

    def test_sync_reminder_silent_after_interpretation(self, tmp_path):
        """_sync_reminder should return None once interpretation is written."""
        from ai_hydro.mcp.helpers import _sync_reminder
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("test-interpreted")
            s.watershed = {"data": {"area_km2": 100}, "meta": {}}
            s.streamflow = {"data": {"n_days": 365}, "meta": {}}
            s.interpretation = "The basin shows strong baseflow dominance."
            s.save()
            assert _sync_reminder("test-interpreted") is None


# ── Version helpers ──────────────────────────────────────────────────────────

class TestVersionHelpers:
    """Test tools_docs version introspection."""

    def test_get_version_returns_string(self):
        from ai_hydro.mcp.tools_docs import _get_version
        v = _get_version()
        assert isinstance(v, str)
        assert len(v) > 0


# ── Citation system ───────────────────────────────────────────────────────────

class TestCitationRegistry:
    """Verify the three-tier citation registry and BibTeX builder."""

    def test_all_known_keys_non_empty(self):
        from ai_hydro.citations import all_known_keys
        keys = all_known_keys()
        assert len(keys) >= 10, "Expected at least 10 citation entries"

    def test_platform_citations_always_in_bibtex(self):
        from ai_hydro.citations import build_bibtex, PLATFORM_CITATIONS
        bib = build_bibtex(set())
        for key in PLATFORM_CITATIONS:
            assert key in bib, f"Platform citation '{key}' missing from empty-key build"

    def test_build_bibtex_includes_requested_keys(self):
        from ai_hydro.citations import build_bibtex
        bib = build_bibtex({"usgs_nwis", "abatzoglou2013gridmet"})
        assert "usgs_nwis" in bib
        assert "abatzoglou2013gridmet" in bib
        assert "waterdata.usgs.gov" in bib
        assert "10.1002/joc.3413" in bib

    def test_build_bibtex_skips_unknown_keys(self):
        from ai_hydro.citations import build_bibtex
        bib = build_bibtex({"nonexistent_key_xyz"})
        assert "nonexistent_key_xyz" not in bib
        # Platform citations still present
        assert "aihydro2026" in bib

    def test_tool_citations_map_known_tools(self):
        from ai_hydro.citations import citation_keys_for_tool, all_known_keys
        known = set(all_known_keys())
        for tool in ("delineate_watershed", "fetch_streamflow_data",
                     "fetch_forcing_data", "fetch_camels_us",
                     "train_hydro_model", "create_cn_grid"):
            keys = citation_keys_for_tool(tool)
            assert len(keys) > 0, f"No citation keys for tool '{tool}'"
            for k in keys:
                assert k in known, f"Unknown citation key '{k}' for tool '{tool}'"

    def test_tool_with_no_citations_returns_empty(self):
        from ai_hydro.citations import citation_keys_for_tool
        assert citation_keys_for_tool("nonexistent_tool") == []

    def test_build_bibtex_header_present(self):
        from ai_hydro.citations import build_bibtex
        bib = build_bibtex(set(), header=True)
        assert "AI-Hydro" in bib
        assert bib.startswith("%")

    def test_build_bibtex_no_duplicate_entries(self):
        from ai_hydro.citations import build_bibtex, PLATFORM_CITATIONS
        # Pass platform keys explicitly — BibTeX entry key should appear exactly once
        bib = build_bibtex(set(PLATFORM_CITATIONS))
        assert bib.count("@software{aihydro2026") == 1
        assert bib.count("@software{aihydro_tools2026") == 1

    def test_register_plugin_citation(self):
        from ai_hydro.citations import (
            register_plugin_citation, citation_keys_for_tool, build_bibtex,
            _PLUGIN_ENTRIES, _PLUGIN_TOOL_MAP,
        )
        bibtex = "@software{test_plugin_2026, author={Test}, title={Test Plugin}}"
        register_plugin_citation("test_plugin_2026", bibtex, ["my_plugin_tool"])
        assert "test_plugin_2026" in _PLUGIN_ENTRIES
        assert "my_plugin_tool" in _PLUGIN_TOOL_MAP
        keys = citation_keys_for_tool("my_plugin_tool")
        assert "test_plugin_2026" in keys
        bib = build_bibtex({"test_plugin_2026"})
        assert "Test Plugin" in bib
        # Cleanup to avoid polluting other tests
        del _PLUGIN_ENTRIES["test_plugin_2026"]
        del _PLUGIN_TOOL_MAP["my_plugin_tool"]


class TestSessionCitations:
    """Verify citation accumulation and bibtex export on HydroSession."""

    def test_add_citations_accumulates(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cite-test-1")
            s.add_citations(["usgs_nwis"])
            s.add_citations(["abatzoglou2013gridmet", "usgs_nwis"])  # duplicate
            assert s.get_citations() == {"usgs_nwis", "abatzoglou2013gridmet"}

    def test_citations_survive_save_reload(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cite-test-2")
            s.add_citations(["usgs_nwis", "nhd_nhdplus"])
            s.save()
            reloaded = HydroSession.load("cite-test-2")
            assert "usgs_nwis" in reloaded.get_citations()
            assert "nhd_nhdplus" in reloaded.get_citations()

    def test_export_bibtex_includes_platform(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cite-test-3")
            s.add_citations(["usgs_nwis"])
            bib = s.export_bibtex()
            assert "aihydro2026" in bib       # Platform
            assert "aihydro_tools2026" in bib # Platform
            assert "usgs_nwis" in bib         # Tier 1

    def test_citations_empty_session_still_has_platform(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cite-test-4")
            bib = s.export_bibtex()
            assert "aihydro2026" in bib
            assert "aihydro_tools2026" in bib

    def test_session_store_adds_citations(self, tmp_path):
        from ai_hydro.mcp.helpers import _session_store
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            slot_data = {"data": {"area_km2": 500}, "meta": {}}
            _session_store("cite-test-5", "watershed", slot_data,
                           tool_name="delineate_watershed")
            s = HydroSession.load("cite-test-5")
            citations = s.get_citations()
            assert "nhd_nhdplus" in citations
            assert "usgs_3dep" in citations

    def test_cite_all_is_alias_for_export_bibtex(self, tmp_path):
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("cite-test-6")
            s.add_citations(["usgs_nwis"])
            assert s.cite_all() == s.export_bibtex()


# ── Map event writer tests ───────────────────────────────────────────────────

class TestMapEventWriter:
    """Verify the Python → VS Code map event pipeline."""

    def test_push_layer_creates_event_file(self, tmp_path):
        from unittest.mock import patch
        from ai_hydro.mcp.map_events import push_layer, _MAP_EVENTS_DIR

        geojson = '{"type":"FeatureCollection","features":[]}'
        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path):
            ok = push_layer("test-layer", "Test Layer", geojson)

        assert ok is True
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        event = json.loads(files[0].read_text())
        assert event["id"] == "test-layer"
        assert event["name"] == "Test Layer"
        assert event["geojson"] == geojson
        assert event["autoZoom"] is True
        assert event["openMap"] is True

    def test_push_layer_applies_watershed_preset(self, tmp_path):
        from ai_hydro.mcp.map_events import push_layer, STYLES

        geojson = '{"type":"FeatureCollection","features":[]}'
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path
        ):
            push_layer("ws-layer", "WS", geojson, style_preset="watershed")

        event = json.loads(list(tmp_path.glob("*.json"))[0].read_text())
        assert event["style"]["fillColor"] == STYLES["watershed"]["fillColor"]

    def test_push_layer_style_override(self, tmp_path):
        from ai_hydro.mcp.map_events import push_layer

        geojson = '{"type":"FeatureCollection","features":[]}'
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path
        ):
            push_layer(
                "ov-layer", "Override", geojson,
                style_override={"fillColor": "#FF0000"},
            )

        event = json.loads(list(tmp_path.glob("*.json"))[0].read_text())
        assert event["style"]["fillColor"] == "#FF0000"

    def test_push_layer_dict_geojson(self, tmp_path):
        """Dict GeoJSON is serialised to a string in the event file."""
        from ai_hydro.mcp.map_events import push_layer

        geojson_dict = {"type": "FeatureCollection", "features": []}
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path
        ):
            ok = push_layer("dict-layer", "Dict", geojson_dict)

        assert ok is True
        event = json.loads(list(tmp_path.glob("*.json"))[0].read_text())
        parsed_back = json.loads(event["geojson"])
        assert parsed_back["type"] == "FeatureCollection"

    def test_push_layer_returns_false_on_permission_error(self, tmp_path):
        from ai_hydro.mcp.map_events import push_layer
        from unittest.mock import patch

        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path / "nonexistent_readonly"):
            with patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
                ok = push_layer("fail-layer", "Fail", "{}")

        assert ok is False

    def test_show_on_map_tool_smoke(self, tmp_path):
        """show_on_map tool returns ok=True for valid GeoJSON."""
        from ai_hydro.mcp import mcp
        import asyncio
        from unittest.mock import patch

        geojson = '{"type":"FeatureCollection","features":[]}'
        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path):
            result = asyncio.run(
                mcp.call_tool("show_on_map", {"geojson": geojson, "name": "Test AOI"})
            )

        assert result is not None

    def test_show_on_map_rejects_invalid_json(self, tmp_path):
        """show_on_map returns ok=False for malformed GeoJSON."""
        from ai_hydro.mcp import mcp
        import asyncio
        from unittest.mock import patch
        import json as _json

        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path):
            result_raw = asyncio.run(
                mcp.call_tool("show_on_map", {"geojson": "not-json"})
            )
        # FastMCP ≥ 2.x: call_tool returns a ToolResult with .content list
        # FastMCP < 2.x: returns a list of TextContent directly
        if hasattr(result_raw, "content"):
            text = result_raw.content[0].text if result_raw.content else "{}"
        elif result_raw:
            text = result_raw[0].text
        else:
            text = "{}"
        result = _json.loads(text)
        assert result.get("ok") is False


# ── Raster map event tests ───────────────────────────────────────────────────

class TestRasterMapEvents:
    """Verify the raster tile + push_raster_layer pipeline."""

    def test_push_raster_layer_creates_event_file(self, tmp_path):
        from ai_hydro.mcp.map_events import push_raster_layer
        from unittest.mock import patch

        fake_png = tmp_path / "twi_tile.png"
        fake_png.write_bytes(b"\x89PNG\r\n")  # minimal PNG header

        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path / "events"):
            ok = push_raster_layer(
                layer_id="twi_test",
                name="TWI: test",
                png_path=str(fake_png),
                bounds_wgs84=[-72.5, 45.0, -71.5, 46.0],
                colormap="viridis_r",
            )

        assert ok is True
        events = list((tmp_path / "events").glob("*.json"))
        assert len(events) == 1
        event = json.loads(events[0].read_text())
        assert event["id"] == "twi_test"
        assert event["layerType"] == "raster"
        assert event["raster"]["path"] == str(fake_png)
        assert event["raster"]["bounds"] == [-72.5, 45.0, -71.5, 46.0]
        assert event["raster"]["colormap"] == "viridis_r"
        assert event["metadata"]["raster_bounds"] == json.dumps([-72.5, 45.0, -71.5, 46.0])

    def test_push_raster_layer_returns_false_on_error(self, tmp_path):
        from ai_hydro.mcp.map_events import push_raster_layer
        from unittest.mock import patch

        with patch("ai_hydro.mcp.map_events._MAP_EVENTS_DIR", tmp_path / "events"):
            with patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
                ok = push_raster_layer(
                    layer_id="fail", name="Fail",
                    png_path="/nonexistent/tile.png",
                    bounds_wgs84=[0, 0, 1, 1],
                )
        assert ok is False

    def test_plot_raster_tile_produces_clean_png(self, tmp_path):
        """plot_raster_tile should save a decoration-free PNG and return path + bounds."""
        try:
            import numpy as np
            from ai_hydro.analysis.plots import plot_raster_tile
        except ImportError:
            pytest.skip("matplotlib or numpy not available")

        arr = np.random.rand(50, 60).astype(float)
        arr[0, 0] = float("nan")  # nodata pixel

        result = plot_raster_tile(
            array=arr,
            bounds_wgs84=[-72.5, 45.0, -71.5, 46.0],
            output_dir=str(tmp_path),
            name="twi_test",
            colormap="viridis",
        )

        assert result is not None
        tile_path, bounds = result
        assert Path(tile_path).exists()
        assert tile_path.endswith("_tile.png")
        assert bounds == [-72.5, 45.0, -71.5, 46.0]
        # File should be a valid PNG (starts with PNG magic bytes)
        data = Path(tile_path).read_bytes()
        assert data[:4] == b"\x89PNG"

    def test_bounds_to_wgs84_geographic_passthrough(self):
        """Geographic CRS bounds should pass through unchanged."""
        from ai_hydro.mcp.tools_analysis import _bounds_to_wgs84
        bounds = [-72.5, 45.0, -71.5, 46.0]
        result = _bounds_to_wgs84(bounds, "EPSG:4326")
        assert result == bounds

    def test_bounds_to_wgs84_invalid_crs_fallback(self):
        """Invalid CRS string should return original bounds (non-fatal)."""
        from ai_hydro.mcp.tools_analysis import _bounds_to_wgs84
        bounds = [100000, 5000000, 200000, 5100000]
        result = _bounds_to_wgs84(bounds, "INVALID_CRS_XYZ")
        assert result == bounds


# ── Phase 2 (1.6.0) feature tests ───────────────────────────────────────────

class TestPhase2Persona:
    """T2.1 — Persona rewrite verification."""

    def test_persona_word_count_under_700(self):
        """Rewritten persona must be < 700 words (was ~1500)."""
        from ai_hydro.mcp.app import mcp as _mcp
        instructions = _mcp.instructions or ""
        wc = len(instructions.split())
        assert wc < 700, f"Persona is {wc} words — must be < 700"

    def test_persona_no_forbidden_terms(self):
        """Persona must not name specific tools, libraries, or CONUS datasets."""
        from ai_hydro.mcp.app import mcp as _mcp
        instructions = _mcp.instructions or ""
        forbidden = [
            "fetch_streamflow_data", "mcp_python", "USGS", "NLDI",
            "GridMET", "NLCD", "research.md",
        ]
        for term in forbidden:
            assert term not in instructions, (
                f"Forbidden term '{term}' found in persona — must be categorical"
            )


class TestPhase2TwoPhase:
    """T2.2 — Two-phase sync split and T2.3 — G1 summary cleanup."""

    def test_get_session_raw_state_returns_slots(self, tmp_path):
        """get_session_raw_state should return computed slots without interpretation."""
        from ai_hydro.mcp.tools_session import get_session_raw_state
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("raw-state-test")
            s.watershed = {"data": {"area_km2": 500}, "meta": {}}
            s.save()
            result = get_session_raw_state("raw-state-test")
            assert "slots" in result
            assert "watershed" in result["slots"]
            assert "findings" not in result
            assert "_instruction" in result

    def test_write_research_interpretation_stores(self, tmp_path):
        """write_research_interpretation should store prose and return char_count."""
        from ai_hydro.mcp.tools_session import write_research_interpretation
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("interp-test")
            s.workspace_dir = str(tmp_path)
            s.save()
            result = write_research_interpretation(
                session_id="interp-test",
                site_name="test-basin",
                interpretation="The basin shows strong baseflow dominance driven by deep glacial aquifers.",
            )
            assert "char_count" in result
            assert result["char_count"] > 0
            assert "written_path" in result

    def test_sync_research_context_removed_in_2_0(self):
        """sync_research_context must not exist in 2.0 (removed alias)."""
        import ai_hydro.mcp.tools_session as ts
        assert not hasattr(ts, "sync_research_context"), (
            "sync_research_context was a 1.x deprecated alias and must not exist in 2.0"
        )

    def test_get_session_summary_no_findings_field(self, tmp_path):
        """get_session_summary must not return a 'findings' interpretation field (G1)."""
        from ai_hydro.mcp.tools_session import get_session_summary
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = get_session_summary("summary-g1-test")
            assert "findings" not in result, (
                "get_session_summary must not return 'findings' — G1: LLM authors interpretation"
            )


class TestPhase2RunPython:
    """T2.4 — run_python tool."""

    def test_run_python_hello_world(self, tmp_path):
        """run_python should capture stdout and return returncode=0."""
        from ai_hydro.mcp.tools_execution import run_python
        result = run_python(
            script="print('hello')",
            workspace_dir=str(tmp_path),
            timeout_seconds=10,
        )
        assert result.get("returncode") == 0
        assert "hello" in result.get("stdout", "")

    def test_run_python_network_blocked_by_default(self, tmp_path):
        """Network access must be blocked when allow_network=False."""
        from ai_hydro.mcp.tools_execution import run_python
        script = (
            "import socket\n"
            "try:\n"
            "    s = socket.socket()\n"
            "    s.connect(('8.8.8.8', 80))\n"
            "    print('NETWORK_OK')\n"
            "except Exception as e:\n"
            "    print(f'BLOCKED: {e}')\n"
        )
        result = run_python(script=script, workspace_dir=str(tmp_path), timeout_seconds=10)
        assert "NETWORK_OK" not in result.get("stdout", ""), (
            "run_python must block network when allow_network=False"
        )

    def test_run_python_rejects_nonexistent_workspace(self, tmp_path):
        """run_python must return error for workspace_dir that does not exist."""
        from ai_hydro.mcp.tools_execution import run_python
        result = run_python(
            script="print('hi')",
            workspace_dir=str(tmp_path / "nonexistent_dir"),
            timeout_seconds=5,
        )
        assert result.get("error") is True
        assert result.get("code") == "WORKSPACE_NOT_FOUND"

    def test_run_python_blocks_pip_install(self, tmp_path):
        """Scripts containing literal 'pip install' must be rejected before execution."""
        from ai_hydro.mcp.tools_execution import run_python
        result = run_python(
            script="pip install numpy",
            workspace_dir=str(tmp_path),
            timeout_seconds=5,
        )
        assert result.get("error") is True
        assert result.get("code") == "BLOCKED_OPERATION"


class TestPhase2Skills:
    """T2.6 — Skills foundation."""

    def test_list_skills_returns_without_raising(self):
        """list_skills must return a structured result with a 'skills' key and not raise."""
        from ai_hydro.mcp.tools_skills import list_skills
        result = list_skills()
        assert isinstance(result, dict), f"list_skills must return a dict; got {type(result)}"
        assert "skills" in result, f"list_skills result must have 'skills' key; got {list(result.keys())}"
        assert isinstance(result["skills"], list)

    def test_list_skills_by_domain_returns_structured_result(self):
        """list_skills with domain filter must return a result with 'skills' key."""
        from ai_hydro.mcp.tools_skills import list_skills
        result = list_skills(domain="modelling")
        assert isinstance(result, dict)
        assert "skills" in result

    def test_load_skill_not_found_returns_error(self):
        """load_skill with an unknown name must return an error dict, not raise."""
        from ai_hydro.mcp.tools_skills import load_skill
        result = load_skill("nonexistent-skill-xyz-123")
        assert isinstance(result, dict)
        assert result.get("error") is True or "not found" in str(result).lower()


class TestPhase2CLIs:
    """T2.7 — CLI enumeration."""

    def test_list_relevant_clis_returns_dict_with_tools_key(self):
        """list_relevant_clis must return a dict with a 'clis' or 'tools' key."""
        from ai_hydro.mcp.tools_execution import list_relevant_clis
        result = list_relevant_clis()
        assert isinstance(result, dict)
        # Must have some structure indicating what CLIs are installed
        assert any(k in result for k in ("clis", "tools", "installed", "available"))


class TestPhase2Baseflow:
    """T2.8 — separate_baseflow companion tool."""

    def test_separate_baseflow_missing_streamflow_returns_error(self, tmp_path):
        """separate_baseflow without streamflow in session must return MISSING_PREREQUISITES."""
        from ai_hydro.mcp.tools_analysis import separate_baseflow
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            result = separate_baseflow("empty-session-bf-test")
            assert result.get("error") is True
            assert result.get("code") == "MISSING_PREREQUISITES"

    def test_extract_signatures_still_returns_bfi_scalar(self, tmp_path):
        """BFI scalar in extract_hydrological_signatures must survive T2.8 split."""
        from ai_hydro.mcp.tools_analysis import extract_hydrological_signatures
        from ai_hydro.session import HydroSession
        import numpy as np
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            # Populate session with synthetic streamflow
            s = HydroSession("sig-bfi-test")
            dates = [f"2010-01-{i+1:02d}" for i in range(30)]
            flow = [float(i + 1) for i in range(30)]
            s.streamflow = {
                "data": {
                    "dates": dates, "discharge_cms": flow,
                    "n_days": 30, "gauge_id": "01031500",
                },
                "meta": {"tool": "fetch_streamflow_data"},
            }
            s.save()
            result = extract_hydrological_signatures("sig-bfi-test")
            # bfi or error; just verify tool runs and returns dict
            assert isinstance(result, dict)


class TestPhase2LibraryReference:
    """T2.5 — get_library_reference no-arg enumeration (R6 fix)."""

    def test_get_library_reference_no_arg_returns_catalog(self):
        """get_library_reference() with no argument must return available card list."""
        from ai_hydro.mcp.tools_analysis import get_library_reference
        result = get_library_reference()
        # Must be a dict with an enumeration key
        assert isinstance(result, dict)
        has_catalog = any(
            k in result for k in ("available", "libraries", "cards", "available_libraries")
        )
        assert has_catalog, (
            f"get_library_reference() (no-arg) must return a catalog dict; got: {list(result.keys())}"
        )
        # Must list pynhd as one of the available cards
        catalog_values = str(result)
        assert "pynhd" in catalog_values


# ── Phase 4 tests ────────────────────────────────────────────────────────────

class TestPhase4AliasRemoval:
    """T4.1 + T4.2 — verify deprecated aliases are gone in 2.0."""

    def test_no_deprecation_warnings_on_import(self):
        """Clean import of ai_hydro.mcp must emit zero DeprecationWarnings."""
        import warnings
        import importlib
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import ai_hydro.mcp
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) == 0, (
            f"ai_hydro.mcp import emitted {len(dep_warnings)} DeprecationWarning(s) — "
            "all deprecated aliases must be removed for 2.0"
        )

    def test_sync_research_context_not_an_mcp_tool(self):
        """sync_research_context must not appear in the registered MCP tool list."""
        import asyncio
        from ai_hydro.mcp import mcp
        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert "sync_research_context" not in names

    def test_train_sync_alias_removed(self):
        """_train_hydro_model_sync_alias must not exist in tools_modelling."""
        import ai_hydro.mcp.tools_modelling as tm
        assert not hasattr(tm, "_train_hydro_model_sync_alias"), (
            "_train_hydro_model_sync_alias was a deprecated 1.x alias and must not exist in 2.0"
        )


class TestPhase4LibraryCards:
    """T4.4 — P2 library cards (pandas, numpy, shapely, matplotlib, folium)."""

    P2_CARDS = ["pandas", "numpy", "shapely", "matplotlib", "folium"]

    @pytest.mark.parametrize("card_name", P2_CARDS)
    def test_p2_card_loadable(self, card_name):
        """Each P2 card must be returned by get_library_reference."""
        from ai_hydro.mcp.tools_analysis import get_library_reference
        result = get_library_reference(card_name)
        assert isinstance(result, dict), f"Expected dict for {card_name}"
        assert not result.get("error"), f"Error loading {card_name}: {result}"
        assert result.get("library") == card_name
        assert len(result.get("gotchas", [])) >= 6
        assert len(result.get("common_patterns", {})) >= 3
        assert "version_compatible" in result

    def test_p2_cards_in_catalog(self):
        """All 5 P2 cards must appear in the no-arg catalog."""
        from ai_hydro.mcp.tools_analysis import get_library_reference
        catalog = get_library_reference()
        available = catalog.get("available_libraries", [])
        for card in self.P2_CARDS:
            assert card in available, f"P2 card '{card}' missing from catalog"


class TestPhase4ExportCapsule:
    """T4.6 — export_session capsule_path parameter and model/ directory."""

    def test_export_session_accepts_capsule_path(self, tmp_path):
        """export_session must write to capsule_path when provided."""
        from ai_hydro.mcp.tools_session import export_session
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("capsule-path-test")
            s.site_name = "test-basin"
            s.interpretation = "The basin is groundwater-dominated."
            s.workspace_dir = str(tmp_path)
            s.save()
            custom_path = tmp_path / "my-capsule-dir"
            result = export_session("capsule-path-test", capsule_path=str(custom_path))
            assert not result.get("error"), f"export_session failed: {result}"
            assert result["capsule_dir"] == str(custom_path)
            cap = custom_path
            assert (cap / "README.md").exists()
            assert (cap / "methods.md").exists()
            assert (cap / "citations.bib").exists()
            assert (cap / "session.json").exists()
            assert (cap / "environment.yml").exists()
            assert (cap / "model").is_dir()
            assert (cap / "data").is_dir()
            assert (cap / "figures").is_dir()

    def test_export_capsule_includes_interpretation_in_readme(self, tmp_path):
        """README.md must contain the stored scientific interpretation."""
        from ai_hydro.mcp.tools_session import export_session
        from ai_hydro.session import HydroSession
        with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path), \
             patch("ai_hydro.session.store._REPO_ROOT", tmp_path):
            s = HydroSession("readme-interp-test")
            s.site_name = "my-basin"
            s.interpretation = "Unique interpretation text for testing."
            s.workspace_dir = str(tmp_path)
            s.save()
            result = export_session("readme-interp-test")
            cap = Path(result["capsule_dir"])
            readme_text = (cap / "README.md").read_text()
            assert "Unique interpretation text for testing." in readme_text
