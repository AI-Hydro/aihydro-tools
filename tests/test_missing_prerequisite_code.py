"""D4: a skipped step is a precondition failure, not UNEXPECTED_ERROR."""
import json

import pytest

from ai_hydro.mcp.helpers import (
    MissingPrerequisiteError,
    _get_session_geometry,
    _tool_error_to_dict,
)
from ai_hydro.session import store
from ai_hydro.session.store import HydroSession


def test_missing_watershed_is_a_prerequisite_error_with_next_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(store, "SESSIONS_DIR", tmp_path)
    HydroSession("no-ws").save()
    with pytest.raises(MissingPrerequisiteError) as info:
        _get_session_geometry("no-ws")
    body = _tool_error_to_dict(info.value)
    assert body["error"] is True
    assert body["code"] == "MISSING_PREREQUISITES"
    assert body["next_tools"] == ["delineate_watershed"]
    assert "No watershed cached for session 'no-ws'" in body["message"]


def test_genuine_unexpected_errors_keep_their_code():
    assert _tool_error_to_dict(ValueError("boom"))["code"] == "UNEXPECTED_ERROR"


def test_still_a_runtime_error_for_existing_callers():
    assert issubclass(MissingPrerequisiteError, RuntimeError)
