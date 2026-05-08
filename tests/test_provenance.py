import pytest
from ai_hydro.session.store import HydroSession

def test_artifact_manifest_hashing():
    session = HydroSession("test-provenance")
    
    params = {"gauge_id": "01031500", "start": "2000-01-01"}
    data = [1.2, 3.4, 5.6]
    
    art_id = session.add_artifact(
        "obs_flow", data, params, source="USGS NWIS", units="m3/s"
    )
    
    assert art_id == "obs_flow"
    manifest = session.artifact_manifest["obs_flow"]
    assert manifest["source"] == "USGS NWIS"
    assert "parameter_hash" in manifest
    assert "content_hash" in manifest
    
    # Test reproducibility detection
    p_hash = manifest["parameter_hash"]
    c_hash = manifest["content_hash"]
    
    # Same params, different data -> different content hash
    session.add_artifact("obs_flow_v2", [1.2, 3.4, 9.9], params, source="USGS")
    assert session.artifact_manifest["obs_flow_v2"]["parameter_hash"] == p_hash
    assert session.artifact_manifest["obs_flow_v2"]["content_hash"] != c_hash

def test_uncertainty_aware_result():
    session = HydroSession("test-results")
    
    data = {"kge": 0.71}
    uncertainty = {
        "lower_ci": 0.65,
        "upper_ci": 0.76,
        "method": "bootstrap"
    }
    artifacts = ["obs_flow"]
    
    session.record_result(
        "kge_slot", 
        data, 
        uncertainty=uncertainty, 
        artifacts_used=artifacts,
        metric_ref="metric.kge",
        tool_name="eval_tool"
    )
    
    res = session.get("kge_slot")
    assert res["data"]["kge"] == 0.71
    assert res["uncertainty"]["lower_ci"] == 0.65
    assert res["artifacts_used"] == ["obs_flow"]
    assert res["meta"]["metric_ref"] == "metric.kge"
    assert "computed_at" in res["meta"]

def test_persistence_of_manifest(tmp_path):
    import ai_hydro.session.store
    # Mock sessions dir
    ai_hydro.session.store._SESSIONS_DIR = tmp_path
    
    session = HydroSession("test-persist")
    session.add_artifact("art1", [1,2,3], {"p": 1}, "src")
    session.record_result("res1", {"val": 10}, artifacts_used=["art1"])
    session.save()
    
    # Reload
    session2 = HydroSession.load("test-persist")
    assert "art1" in session2.artifact_manifest
    assert session2.get("res1")["artifacts_used"] == ["art1"]
    assert session2.artifact_manifest["art1"]["parameter_hash"] == session._hash({"p": 1})
