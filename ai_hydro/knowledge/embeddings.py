"""Former name of ``ai_hydro.knowledge.passages`` (it never used embeddings). Same module object, so
``monkeypatch`` and attribute access through either name see the same state."""
import sys

from ai_hydro.knowledge import passages as _passages

sys.modules[__name__] = _passages
