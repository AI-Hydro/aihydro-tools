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

@pytest.mark.parametrize("rr", [None, 2.3e-33, 0.0, -0.4, float("nan"), float("inf"), 1e-4, 3.5, "0.5", True])
def test_water_balance_never_passes_on_missing_or_non_physical_runoff_ratio(rr):
    # D4: a runoff ratio of 2.3e-33 (computed from a fill-valued precipitation
    # series) used to "pass" with message "Runoff Ratio: 0.00".
    session_id = "test-wb-nonphys"
    session = HydroSession(session_id)
    session.set("signatures", {"data": {"runoff_ratio": rr}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "insufficient_data", (rr, res)


def test_water_balance_insufficient_when_precipitation_record_not_used():
    session_id = "test-wb-precip-rec"
    session = HydroSession(session_id)
    session.set("signatures", {"data": {
        "runoff_ratio": 0.5,
        "_precipitation": {"status": "rejected", "reason": "max 4e32 mm/day > 2000"}}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "insufficient_data" and "rejected" in res["message"]
    session.set("signatures", {"data": {"runoff_ratio": 0.5, "_precipitation": {"status": "used"}}})
    session.save()
    assert check_water_balance_consistency(session_id)["status"] == "pass"


def test_water_balance_missing_ratio_message_names_precipitation_status():
    session_id = "test-wb-missing-msg"
    session = HydroSession(session_id)
    session.set("signatures", {"data": {
        "runoff_ratio": None,
        "_precipitation": {"status": "unavailable", "reason": "no source"}}})
    session.save()
    res = check_water_balance_consistency(session_id)
    assert res["status"] == "insufficient_data" and "unavailable" in res["message"]


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
