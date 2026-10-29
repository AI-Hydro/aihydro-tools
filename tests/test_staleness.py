import pytest
from datetime import datetime, timezone, timedelta
from ai_hydro.session import HydroSession

def test_session_staleness_logic(tmp_path):
    import ai_hydro.session.store as store
    old_dir = store.SESSIONS_DIR
    test_sessions_dir = tmp_path / "sessions"
    test_sessions_dir.mkdir()
    store.SESSIONS_DIR = test_sessions_dir
    store._SESSIONS_DIR = test_sessions_dir
    
    try:
        session_id = "stale-test"
        session = HydroSession.load(session_id)
        
        # 1. Fresh interpretation
        session.interpretation = "Fresh research"
        session.interpretation_at = datetime.now(timezone.utc).isoformat()
        assert not session.is_stale("interpretation")
        
        # 2. Stale interpretation (15 days ago)
        stale_date = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        session.interpretation_at = stale_date
        assert session.is_stale("interpretation")
        
        # 3. Fresh computed slot
        session.set("watershed", {
            "data": {}, 
            "meta": {"computed_at": datetime.now(timezone.utc).isoformat()}
        })
        assert not session.is_stale("watershed")
        
        # 4. Stale computed slot (2 years ago)
        ancient_date = (datetime.now(timezone.utc) - timedelta(days=730)).isoformat()
        session.set("watershed", {
            "data": {}, 
            "meta": {"computed_at": ancient_date}
        })
        assert session.is_stale("watershed")
        
        # 5. Archived session overrides everything
        session.interpretation_at = datetime.now(timezone.utc).isoformat()
        assert not session.is_stale("interpretation")
        session.archive()
        assert session.archived
        assert session.is_stale("interpretation")
        assert session.is_stale("watershed")
        
    finally:
        store.SESSIONS_DIR = old_dir
        store._SESSIONS_DIR = old_dir

def test_research_md_is_a_stub_not_a_digest(tmp_path):
    import ai_hydro.session.store as store
    old_dir = store.SESSIONS_DIR
    test_sessions_dir = tmp_path / "sessions"
    test_sessions_dir.mkdir()
    store.SESSIONS_DIR = test_sessions_dir
    store._SESSIONS_DIR = test_sessions_dir
    
    try:
        session_id = "format-test"
        session = HydroSession.load(session_id)
        session.workspace_dir = str(tmp_path)
        
        # Mix of active and stale
        session.interpretation = "My interpretation"
        session.interpretation_at = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        
        session.notes = ["Recent note"]
        # Force updated_at to be fresh
        session.updated_at = datetime.now(timezone.utc).isoformat()
        
        session.save(sync_context=True)
        
        research_md = tmp_path / ".aihydrorules" / "research.md"
        content = research_md.read_text()

        # T-R1: research.md is the retirement stub; staleness is rendered by the context resource
        # (tests/test_study_context.py::test_stale_interpretation_is_marked).
        assert "no longer updated" in content
        assert "Recent note" not in content and "Historical" not in content

    finally:
        store.SESSIONS_DIR = old_dir
        store._SESSIONS_DIR = old_dir
