from __future__ import annotations

from bench.oracle import assert_all
from bench.schema import certification_payload, load_tasks, validate_catalog


def test_bench_catalog_schema_valid():
    assert validate_catalog() == []


def test_bench_catalog_defaults_target_package_and_counts():
    tasks = load_tasks()
    assert len(tasks) == 79
    assert tasks[0]["id"] == "B-001"
    assert tasks[-1]["id"] == "B-079"
    assert all(t["target_package"] == "aihydro-tools" for t in tasks)
    assert sum(t["mark"] == "bench" for t in tasks) == 78
    assert sum(t["mark"] == "bench_live" for t in tasks) == 1


def test_certification_payload_is_release_gate_ready():
    payload = certification_payload(results={"B-001": "passed"}, git_sha="abc123")
    assert payload["suite_id"] == "hydroresearch-bench"
    assert payload["schema_version"] == 1
    assert payload["schema_valid"] is True
    assert payload["task_count"] == 79
    assert payload["fixture_task_count"] == 78
    assert payload["live_task_count"] == 1
    assert payload["default_target_package"] == "aihydro-tools"
    assert payload["data_access_policy"]
    assert payload["git_sha"] == "abc123"
    assert payload["results"]["fixture_passed"] == 1


def test_oracle_supports_less_equal_operator():
    assert_all({"value": 3}, [{"path": "value", "op": "le", "expected": 3}], "B-TEST")
