import pytest
import json
from pathlib import Path
from ai_hydro.session import HydroSession, merge_session_shards

def test_session_sharding_and_merging(tmp_path):
    # Mock SESSIONS_DIR for testing
    import ai_hydro.session.store as store
    import ai_hydro.session.sharding as sharding
    
    old_dir = store.SESSIONS_DIR
    test_sessions_dir = tmp_path / "sessions"
    test_sessions_dir.mkdir()
    
    store.SESSIONS_DIR = test_sessions_dir
    store._SESSIONS_DIR = test_sessions_dir
    sharding.SESSIONS_DIR = test_sessions_dir
    
    try:
        session_id = "test-session"
        
        # 1. Create main session
        main = HydroSession.load(session_id)
        main.notes.append("Initial note")
        main.set("watershed", {"data": {"area": 100}, "meta": {"tool": "delineate"}})
        main.save()
        
        # 2. Create shard A (adds a note and a new slot)
        shard_a = HydroSession.load(session_id, shard_id="worker-a")
        shard_a.notes.append("Note from A")
        shard_a.set("streamflow", {"data": {"q": [1, 2, 3]}, "meta": {"tool": "fetch"}})
        shard_a.save()
        
        # 3. Create shard B (adds a note and overwrites watershed)
        shard_b = HydroSession.load(session_id, shard_id="worker-b")
        shard_b.notes.append("Note from B")
        shard_b.set("watershed", {"data": {"area": 105}, "meta": {"tool": "delineate_v2"}})
        shard_b.save()
        
        # Verify shard files exist
        assert (test_sessions_dir / f"{session_id}.json").exists()
        assert (test_sessions_dir / f"{session_id}.worker-a.shard.json").exists()
        assert (test_sessions_dir / f"{session_id}.worker-b.shard.json").exists()
        
        # 4. Merge shards
        result = merge_session_shards(session_id)
        
        assert result["shards_merged"] == 2
        assert "streamflow" in result["slots_updated"]
        assert "watershed" in result["slots_updated"]
        
        # 5. Verify main session state
        merged = HydroSession.load(session_id)
        assert len(merged.notes) == 3
        assert "Initial note" in merged.notes
        assert "Note from A" in merged.notes
        assert "Note from B" in merged.notes
        
        assert merged.get("streamflow")["data"]["q"] == [1, 2, 3]
        assert merged.get("watershed")["data"]["area"] == 105 # Shard B wins LWW (saved later)
        
        # 6. Verify cleanup
        assert not (test_sessions_dir / f"{session_id}.worker-a.shard.json").exists()
        assert not (test_sessions_dir / f"{session_id}.worker-b.shard.json").exists()
        
    finally:
        store.SESSIONS_DIR = old_dir
        store._SESSIONS_DIR = old_dir
        sharding.SESSIONS_DIR = old_dir

def test_subagent_digest_model():
    from ai_hydro.core.digest import SubAgentDigest, UnitOutcome
    
    digest = SubAgentDigest(
        status="complete",
        summary="Processed 2 sites",
        per_unit_outcomes=[
            UnitOutcome(id="01031500", status="complete", results={"bfi": 0.5}),
            UnitOutcome(id="01031510", status="failed", message="Timeout")
        ]
    )
    
    assert digest.status == "complete"
    assert len(digest.per_unit_outcomes) == 2
    assert digest.per_unit_outcomes[1].message == "Timeout"
