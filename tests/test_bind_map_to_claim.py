"""Tests for bind_map_to_claim MCP tool — Phase 3.4.2."""
from pathlib import Path
from unittest.mock import patch

from ai_hydro.session import HydroSession


def _make_session(session_id: str, tmp_dir: Path, *, claims=None):
    """Create a HydroSession with optional claims (keyed by claim_id in the claims dict)."""
    with patch("ai_hydro.session.store._SESSIONS_DIR", tmp_dir):
        s = HydroSession(session_id)
        for c in (claims or []):
            claim_id = c["claim_id"]
            s.claims[claim_id] = {
                "id": claim_id,
                "claim": c.get("statement", ""),
                "confidence": c.get("confidence", 0.5),
                "status": "proposed",
                "limitations": c.get("limitations", []),
            }
        s.save()


class TestBindMapToClaim:
    def test_bind_succeeds_when_claim_exists(self, tmp_path):
        _make_session(
            "s1",
            tmp_path,
            claims=[
                {
                    "claim_id": "CLM-001",
                    "statement": "Runoff ratio is 0.45",
                    "confidence": 0.8,
                    "evidence": [],
                    "limitations": "test",
                    "methods": [],
                }
            ],
        )
        with (
            patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
            patch("ai_hydro.mcp.tools_session._resolve_session", return_value="s1"),
            patch("ai_hydro.mcp.ledger_commands.write_ledger_event"),
        ):
            from ai_hydro.mcp.tools_session import bind_map_to_claim
            result = bind_map_to_claim("CLM-001", "s1")
        assert result.get("claim_id") == "CLM-001"
        assert result.get("session_id") == "s1"
        assert "bound" in result.get("message", "").lower()

    def test_bind_persists_to_session_metadata(self, tmp_path):
        _make_session(
            "s2",
            tmp_path,
            claims=[
                {
                    "claim_id": "CLM-002",
                    "statement": "Baseflow index is 0.6",
                    "confidence": 0.75,
                    "evidence": [],
                    "limitations": "test",
                    "methods": [],
                }
            ],
        )
        with (
            patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
            patch("ai_hydro.mcp.tools_session._resolve_session", return_value="s2"),
            patch("ai_hydro.mcp.ledger_commands.write_ledger_event"),
        ):
            from ai_hydro.mcp.tools_session import bind_map_to_claim
            bind_map_to_claim("CLM-002", "s2")
            # Reload and verify metadata was persisted
            session = HydroSession.load("s2")
        assert session.extra.get("map_bound_claim_id") == "CLM-002"

    def test_bind_fails_when_claim_not_found(self, tmp_path):
        _make_session("s3", tmp_path)
        with (
            patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
            patch("ai_hydro.mcp.tools_session._resolve_session", return_value="s3"),
        ):
            from ai_hydro.mcp.tools_session import bind_map_to_claim
            result = bind_map_to_claim("CLM-MISSING", "s3")
        assert result.get("code") == "CLAIM_NOT_FOUND"
        assert "error" in result

    def test_bind_error_message_lists_available_claims(self, tmp_path):
        _make_session(
            "s4",
            tmp_path,
            claims=[
                {
                    "claim_id": "CLM-OK",
                    "statement": "BFI is 0.5",
                    "confidence": 0.7,
                    "evidence": [],
                    "limitations": "x",
                    "methods": [],
                }
            ],
        )
        with (
            patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
            patch("ai_hydro.mcp.tools_session._resolve_session", return_value="s4"),
        ):
            from ai_hydro.mcp.tools_session import bind_map_to_claim
            result = bind_map_to_claim("CLM-WRONG", "s4")
        assert "CLM-OK" in result.get("error", "")

    def test_bind_registers_in_tool_tiers(self):
        from ai_hydro.mcp.app import TOOL_TIERS
        assert "bind_map_to_claim" in TOOL_TIERS
        assert TOOL_TIERS["bind_map_to_claim"] == 2
