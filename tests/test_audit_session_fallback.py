"""The audit never resolves a study by guessing ("most recent study on disk").

``_resolve_session`` keeps its WS-4 recent-study fallback for analysis tools by default; a tool
(or a deployment, via AIHYDRO_RECENT_SESSION_FALLBACK=off) can forbid it. The audit forbids it.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def one_study_on_disk(tmp_path, monkeypatch):
    """A single study file on disk, no chat binding: the 'most recent study' candidate."""
    from ai_hydro.session import chat_binding
    from ai_hydro.session import store as _store

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "study_on_disk.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_store, "_SESSIONS_DIR", sessions)
    monkeypatch.setattr(chat_binding, "get_binding_store",
                        lambda: chat_binding.ChatBindingStore(tmp_path / "bindings.json"))
    monkeypatch.delenv("AIHYDRO_RECENT_SESSION_FALLBACK", raising=False)
    return "study_on_disk"


def test_resolver_still_falls_back_to_the_recent_study_by_default(one_study_on_disk):
    from ai_hydro.mcp.helpers import _resolve_session_with_rule

    assert _resolve_session_with_rule(None, None) == (one_study_on_disk, "recent_fallback")


def test_resolver_can_forbid_the_recent_fallback_per_call_and_globally(one_study_on_disk, monkeypatch):
    from ai_hydro.mcp.helpers import SessionResolutionError, _resolve_session

    with pytest.raises(SessionResolutionError):
        _resolve_session(None, None, allow_recent_fallback=False)
    monkeypatch.setenv("AIHYDRO_RECENT_SESSION_FALLBACK", "off")
    with pytest.raises(SessionResolutionError):
        _resolve_session(None, None)
    # an explicit session is never affected
    assert _resolve_session("explicit_study", None, allow_recent_fallback=False) == "explicit_study"


def test_audit_interpretation_refuses_to_pick_the_most_recent_study(one_study_on_disk):
    from ai_hydro.mcp.tools_audit import audit_interpretation

    out = audit_interpretation(session_id=None, prose="Runoff ratio was 0.42.")
    assert out["error"] is True and out["code"] == "SESSION_RESOLUTION_FAILED"
    assert "session_id" in out.get("recovery", "")


def test_audit_interpretation_resolves_an_explicit_session(one_study_on_disk):
    from ai_hydro.mcp.tools_audit import audit_interpretation

    out = audit_interpretation(session_id=one_study_on_disk, prose="No numbers here.")
    assert out.get("error") is not True
    assert out["session_id"] == one_study_on_disk
