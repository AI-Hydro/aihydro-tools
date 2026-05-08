import pytest
from ai_hydro.session.store import HydroSession
from ai_hydro.mcp.tools_validators import (
    check_water_balance_consistency,
    check_temporal_alignment,
    check_unit_consistency
)

def test_water_balance_validator():
    session_id = "test-wb-phys"
    session = HydroSession(session_id)
    
    # Pass case
    session.set("signatures", {"data": {"runoff_ratio": 0.5}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "pass"
    
    # Warning case
    session.set("signatures", {"data": {"runoff_ratio": 1.05}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "warning"
    assert res["severity"] == "medium"
    
    # High severity case
    session.set("signatures", {"data": {"runoff_ratio": 1.5}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "warning"
    assert res["severity"] == "high"

def test_temporal_alignment_validator():
    session_id = "test-temporal-phys"
    session = HydroSession(session_id)
    
    session.set("slot1", {"meta": {"params": {"start_date": "2000-01-01", "end_date": "2010-12-31"}}})
    session.set("slot2", {"meta": {"params": {"start_date": "2000-01-01", "end_date": "2010-12-31"}}})
    session.save()
    
    res = check_temporal_alignment(session_id, "slot1", "slot2")
    assert res["status"] == "pass"
    
    session.set("slot2", {"meta": {"params": {"start_date": "2001-01-01", "end_date": "2010-12-31"}}})
    session.save()
    res = check_temporal_alignment(session_id, "slot1", "slot2")
    assert res["status"] == "fail"

def test_unit_consistency_validator():
    session_id = "test-units-phys"
    session = HydroSession(session_id)
    
    session.set("q", {"data": {"units": "m3/s"}})
    session.save()
    
    res = check_unit_consistency(session_id, "q", "m3/s")
    assert res["status"] == "pass"
    
    res = check_unit_consistency(session_id, "q", "ft3/s")
    assert res["status"] == "fail"
    assert res["severity"] == "high"
