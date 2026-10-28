"""
Tests for Wave 3 chat ↔ study binding infrastructure.

Covers:
- ChatBindingStore: bind / lookup / unbind
- _resolve_session priority chain
- Auto-create from hint (delineation genesis)
- Same chat_id always resolves same study
- Different chat_ids never collide
- Explicit session_id overrides chat binding
- SessionResolutionError when nothing to resolve
- aihydro_chat_status and aihydro_rebind_chat tools
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# ChatBindingStore tests
# ---------------------------------------------------------------------------

class TestChatBindingStore:

    def test_bind_and_lookup_study(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_123")
        assert store.lookup_study("01HCHAT0010000000000000000") == "basin_123"

    def test_bind_and_lookup_chat(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_123")
        assert store.lookup_chat("basin_123") == "01HCHAT0010000000000000000"

    def test_lookup_missing_returns_none(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        assert store.lookup_study("no-such-chat") is None
        assert store.lookup_chat("no-such-study") is None

    def test_rebind_replaces_old_forward_pointer(self, tmp_path):
        """A chat rebound to a new study: old study's reverse pointer removed."""
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_old")
        store.bind("01HCHAT0010000000000000000", "basin_new")
        assert store.lookup_study("01HCHAT0010000000000000000") == "basin_new"
        assert store.lookup_chat("basin_old") is None
        assert store.lookup_chat("basin_new") == "01HCHAT0010000000000000000"

    def test_second_chat_on_a_study_does_not_steal_the_first(self, tmp_path):
        """v2: many chats may share a study (v1 removed the first chat's binding)."""
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHATA000000000000000000", "basin_shared")
        store.bind("01HCHATB000000000000000000", "basin_shared")
        assert store.lookup_study("01HCHATA000000000000000000") == "basin_shared"
        assert store.lookup_study("01HCHATB000000000000000000") == "basin_shared"

    def test_unbind_chat(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_123")
        removed = store.unbind_chat("01HCHAT0010000000000000000")
        assert removed is True
        assert store.lookup_study("01HCHAT0010000000000000000") is None
        assert store.lookup_chat("basin_123") is None

    def test_unbind_study(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_123")
        removed = store.unbind_study("basin_123")
        assert removed is True
        assert store.lookup_study("01HCHAT0010000000000000000") is None

    def test_unbind_missing_returns_false(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        assert store.unbind_chat("ghost") is False
        assert store.unbind_study("ghost") is False

    def test_all_bindings(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHATA000000000000000000", "study-1")
        store.bind("01HCHATB000000000000000000", "study-2")
        pairs = dict(store.all_bindings())
        assert pairs["01HCHATA000000000000000000"] == "study-1"
        assert pairs["01HCHATB000000000000000000"] == "study-2"

    def test_atomic_write_creates_valid_json(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        store.bind("01HCHAT0010000000000000000", "basin_xyz")
        data = json.loads((tmp_path / "bindings.json").read_text())
        assert data["schema"] == 2
        assert data["bindings"]["01HCHAT0010000000000000000"]["study_id"] == "basin_xyz"
        assert "study_to_chat" not in data              # v2 stores no inverse

    def test_is_bound(self, tmp_path):
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "bindings.json")
        assert store.is_bound("01HCHATX000000000000000000") is False
        store.bind("01HCHATX000000000000000000", "study-A")
        assert store.is_bound("01HCHATX000000000000000000") is True

    def test_corrupt_file_is_set_aside_not_overwritten(self, tmp_path):
        """Corrupt JSON must not wipe the bindings: the file is kept as .corrupt-<ts> and the store starts empty."""
        from ai_hydro.session.chat_binding import ChatBindingStore
        p = tmp_path / "bindings.json"
        p.write_text("{not valid json!!!")
        store = ChatBindingStore(p)
        assert store.lookup_study("any") is None          # a read never touches the file
        assert p.read_text() == "{not valid json!!!"
        store.bind("01HCHAT0010000000000000000", "s")     # the next write sets it aside first
        kept = list(tmp_path.glob("bindings.json.corrupt-*"))
        assert len(kept) == 1 and kept[0].read_text() == "{not valid json!!!"
        assert store.lookup_study("01HCHAT0010000000000000000") == "s"

# ---------------------------------------------------------------------------
# _resolve_session priority chain tests
# ---------------------------------------------------------------------------

class TestResolveSession:
    """
    Priority:
    1. Explicit session_id → returned (and chat rebound if chat_id given)
    2. Chat binding lookup
    3. Auto-create from hint
    4. SessionResolutionError
    """

    def test_explicit_session_id_wins(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHAT1000000000000000000", "old-study")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = _resolve_session("explicit-session", "01HCHAT1000000000000000000")
        assert result == "explicit-session"

    def test_explicit_session_rebinds_chat(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHAT1000000000000000000", "old-study")
        with _patch_store(store), _patch_sessions(tmp_path):
            from ai_hydro.session import HydroSession
            HydroSession.load("new-study").save()      # an explicit id binds only an existing study
            _resolve_session("new-study", "01HCHAT1000000000000000000")
        # Chat should now point to new-study
        assert store.lookup_study("01HCHAT1000000000000000000") == "new-study"

    def test_explicit_unknown_session_does_not_rebind_chat(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHAT1000000000000000000", "old-study")
        with _patch_store(store), _patch_sessions(tmp_path):
            assert _resolve_session("Bogus", "01HCHAT1000000000000000000") == "Bogus"
        assert store.lookup_study("01HCHAT1000000000000000000") == "old-study"

    def test_chat_binding_used_when_session_id_omitted(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHAT1000000000000000000", "bound-study")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = _resolve_session(None, "01HCHAT1000000000000000000")
        assert result == "bound-study"

    def test_auto_create_when_no_session_or_binding(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = _resolve_session(None, None, auto_create_hint="basin-new")
        assert result == "basin-new"

    def test_auto_create_binds_chat(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            _resolve_session(None, "01HCHATX000000000000000000", auto_create_hint="basin-new")
        assert store.lookup_study("01HCHATX000000000000000000") == "basin-new"

    def test_no_auto_create_when_disabled(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session, SessionResolutionError
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path), pytest.raises(SessionResolutionError):
            _resolve_session(None, None, auto_create_hint="basin-x", allow_auto_create=False)

    def test_raises_when_nothing_to_resolve(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session, SessionResolutionError
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path), pytest.raises(SessionResolutionError):
            _resolve_session(None, None)

    def test_error_has_recovery_hint(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session, SessionResolutionError
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            try:
                _resolve_session(None, None)
            except SessionResolutionError as exc:
                assert exc.recovery
                assert exc.next_tools

    def test_two_tools_same_chat_same_study(self, tmp_path):
        """Simulates two sequential tool calls from the same chat."""
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            # First call: auto-create
            s1 = _resolve_session(None, "01HCHATA000000000000000000", auto_create_hint="basin-abc")
            # Second call: should find binding without hint
            s2 = _resolve_session(None, "01HCHATA000000000000000000")
        assert s1 == s2 == "basin-abc"

    def test_different_chats_never_collide(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            _resolve_session(None, "01HCHAT1000000000000000000", auto_create_hint="basin-1")
            _resolve_session(None, "01HCHAT2000000000000000000", auto_create_hint="basin-2")
            r1 = _resolve_session(None, "01HCHAT1000000000000000000")
            r2 = _resolve_session(None, "01HCHAT2000000000000000000")
        assert r1 == "basin-1"
        assert r2 == "basin-2"
        assert r1 != r2

    def test_explicit_session_overrides_bound_chat(self, tmp_path):
        from ai_hydro.mcp.helpers import _resolve_session
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHAT1000000000000000000", "study-A")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = _resolve_session("study-B", "01HCHAT1000000000000000000")
        assert result == "study-B"


# ---------------------------------------------------------------------------
# SessionResolutionError.to_dict
# ---------------------------------------------------------------------------

def test_session_resolution_error_to_dict():
    from ai_hydro.mcp.helpers import SessionResolutionError
    err = SessionResolutionError(
        "test msg",
        recovery="do this",
        next_tools=["delineate_watershed"],
    )
    d = err.to_dict()
    assert d["error"] is True
    assert d["code"] == "SESSION_RESOLUTION_FAILED"
    assert d["recovery"] == "do this"
    assert "delineate_watershed" in d["next_tools"]


# ---------------------------------------------------------------------------
# aihydro_chat_status tool
# ---------------------------------------------------------------------------

class TestChatStatusTool:
    def test_unbound_chat(self, tmp_path):
        from ai_hydro.mcp.tools_session import aihydro_chat_status
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = aihydro_chat_status(chat_id="01HVNB0VNDCHAT000000000000")
        assert result["bound"] is False
        assert result["study_id"] is None

    def test_bound_chat(self, tmp_path):
        from ai_hydro.mcp.tools_session import aihydro_chat_status
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        store.bind("01HCHATB0VND00000000000000", "study-xyz")
        with _patch_store(store), _patch_sessions(tmp_path):
            result = aihydro_chat_status(chat_id="01HCHATB0VND00000000000000")
        assert result["bound"] is True
        assert result["study_id"] == "study-xyz"


# ---------------------------------------------------------------------------
# aihydro_rebind_chat tool
# ---------------------------------------------------------------------------

class TestRebindChatTool:
    def test_rebind_creates_binding(self, tmp_path):
        from ai_hydro.mcp.tools_session import aihydro_rebind_chat
        from ai_hydro.session.chat_binding import ChatBindingStore
        store = ChatBindingStore(tmp_path / "b.json")
        with _patch_store(store), _patch_sessions(tmp_path):
            from ai_hydro.session import HydroSession
            HydroSession.load("my-study").save()      # rebinding needs an existing study
            result = aihydro_rebind_chat(
                study_id="my-study", chat_id="01HCHATXYZ0000000000000000"
            )
        assert result.get("error") is None or "error" not in result
        assert result["study_id"] == "my-study"
        assert store.lookup_study("01HCHATXYZ0000000000000000") == "my-study"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from contextlib import contextmanager
from unittest.mock import patch as _patch


@contextmanager
def _patch_store(store):
    """Redirect the module-level singleton to our test store."""
    import ai_hydro.mcp.helpers as _helpers
    import ai_hydro.session.chat_binding as _cb
    with (
        _patch.object(_cb, "_store", store),
        _patch.object(_helpers, "_resolve_session.__globals__", {"get_binding_store": lambda: store}, create=True),
    ):
        # Simpler: just set the module-level _store directly
        orig = _cb._store
        _cb._store = store
        try:
            yield
        finally:
            _cb._store = orig


@contextmanager
def _patch_sessions(tmp_path):
    """Redirect HydroSession storage to tmp_path."""
    with (
        _patch("ai_hydro.session.store._SESSIONS_DIR", tmp_path),
        _patch("ai_hydro.session.store._REPO_ROOT", tmp_path),
    ):
        yield
