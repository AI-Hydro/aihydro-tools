"""Schema and certification helpers for HydroResearch-Bench.

This module intentionally stays lightweight: it validates the task catalog and
emits release/CI certification metadata without importing AI-Hydro runtime code.
The benchmark is a governance layer, not another hydrology compute package.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

BENCH_DIR = Path(__file__).parent
TASKS_YAML = BENCH_DIR / "tasks.yaml"

SUITE_ID = "hydroresearch-bench"
SCHEMA_VERSION = 1
DEFAULT_TARGET_PACKAGE = "aihydro-tools"

ALLOWED_MARKS = {"bench", "bench_live"}
ALLOWED_TIERS = {1, 2, 3}
ALLOWED_CALL_STYLES = {"session_op", "mcp_tool", "compute_fn", "enforcement_fn"}
ALLOWED_OPS = {
    "eq",
    "ne",
    "between",
    "gt",
    "ge",
    "lt",
    "le",
    "approx",
    "approx_pct",
    "present",
    "absent",
    "contains",
    "startswith",
    "len_gte",
    "len_eq",
}


@dataclass(frozen=True)
class BenchValidationIssue:
    """One catalog/schema validation issue."""

    task_id: str
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"task_id": self.task_id, "field": self.field, "message": self.message}


def load_catalog(path: Path = TASKS_YAML) -> dict[str, Any]:
    """Load the raw benchmark catalog."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{path} must contain a mapping at top level")
    raw.setdefault("schema_version", SCHEMA_VERSION)
    raw.setdefault("suite_id", SUITE_ID)
    raw.setdefault("default_target_package", DEFAULT_TARGET_PACKAGE)
    return raw


def load_tasks(path: Path = TASKS_YAML) -> list[dict[str, Any]]:
    """Load task list with catalog defaults applied."""

    catalog = load_catalog(path)
    tasks = catalog.get("tasks")
    if not isinstance(tasks, list):
        raise TypeError(f"{path} must define tasks: list")
    default_target = catalog.get("default_target_package", DEFAULT_TARGET_PACKAGE)
    out: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            out.append({"id": "<invalid>", "_invalid_task": task})
            continue
        t = dict(task)
        t.setdefault("target_package", default_target)
        out.append(t)
    return out


def validate_catalog(path: Path = TASKS_YAML) -> list[BenchValidationIssue]:
    """Validate benchmark catalog metadata and task contract."""

    issues: list[BenchValidationIssue] = []
    catalog = load_catalog(path)
    tasks = load_tasks(path)

    if catalog.get("suite_id") != SUITE_ID:
        issues.append(BenchValidationIssue("<catalog>", "suite_id", f"must equal {SUITE_ID!r}"))
    if catalog.get("schema_version") != SCHEMA_VERSION:
        issues.append(BenchValidationIssue("<catalog>", "schema_version", f"must equal {SCHEMA_VERSION}"))
    if not catalog.get("default_target_package"):
        issues.append(BenchValidationIssue("<catalog>", "default_target_package", "is required"))

    ids: list[str] = []
    for task in tasks:
        tid = str(task.get("id") or "<missing-id>")
        ids.append(tid)
        if not _is_task_id(tid):
            issues.append(BenchValidationIssue(tid, "id", "must match B-NNN"))
        for field in ("name", "tier", "mark", "category", "call_style", "rationale", "call", "assertions", "target_package"):
            if field not in task or task.get(field) in (None, ""):
                issues.append(BenchValidationIssue(tid, field, "is required"))
        if task.get("mark") not in ALLOWED_MARKS:
            issues.append(BenchValidationIssue(tid, "mark", f"must be one of {sorted(ALLOWED_MARKS)}"))
        if task.get("tier") not in ALLOWED_TIERS:
            issues.append(BenchValidationIssue(tid, "tier", f"must be one of {sorted(ALLOWED_TIERS)}"))
        if task.get("call_style") not in ALLOWED_CALL_STYLES:
            issues.append(BenchValidationIssue(tid, "call_style", f"must be one of {sorted(ALLOWED_CALL_STYLES)}"))
        if not isinstance(task.get("call"), dict):
            issues.append(BenchValidationIssue(tid, "call", "must be a mapping"))
        assertions = task.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            issues.append(BenchValidationIssue(tid, "assertions", "must be a non-empty list"))
        else:
            for j, assertion in enumerate(assertions):
                if not isinstance(assertion, dict):
                    issues.append(BenchValidationIssue(tid, f"assertions.{j}", "must be a mapping"))
                    continue
                if "path" not in assertion:
                    issues.append(BenchValidationIssue(tid, f"assertions.{j}.path", "is required"))
                op = assertion.get("op")
                if op not in ALLOWED_OPS:
                    issues.append(BenchValidationIssue(tid, f"assertions.{j}.op", f"must be one of {sorted(ALLOWED_OPS)}"))
                if op == "between" and "bounds" not in assertion:
                    issues.append(BenchValidationIssue(tid, f"assertions.{j}.bounds", "required for op=between"))
                if op in {"eq", "ne", "gt", "ge", "lt", "le", "approx", "approx_pct", "contains", "startswith", "len_gte", "len_eq"} and "expected" not in assertion:
                    issues.append(BenchValidationIssue(tid, f"assertions.{j}.expected", f"required for op={op}"))

    duplicates = [tid for tid, n in Counter(ids).items() if n > 1]
    for tid in duplicates:
        issues.append(BenchValidationIssue(tid, "id", "duplicate task id"))

    numeric_ids = sorted(int(tid.split("-")[1]) for tid in ids if _is_task_id(tid))
    expected_ids = list(range(1, len(numeric_ids) + 1))
    if numeric_ids != expected_ids:
        missing = sorted(set(expected_ids) - set(numeric_ids))
        extra = sorted(set(numeric_ids) - set(expected_ids))
        issues.append(
            BenchValidationIssue(
                "<catalog>",
                "id",
                f"task ids must be contiguous from B-001; missing={missing[:10]} extra={extra[:10]}",
            )
        )

    return issues


def _is_task_id(value: str) -> bool:
    return len(value) == 5 and value.startswith("B-") and value[2:].isdigit()


def certification_payload(
    *,
    path: Path = TASKS_YAML,
    results: dict[str, str] | None = None,
    git_sha: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable suite certification payload."""

    catalog = load_catalog(path)
    tasks = load_tasks(path)
    results = results or {}
    issues = validate_catalog(path)
    marks = Counter(t.get("mark") for t in tasks)
    categories = Counter(t.get("category") for t in tasks)
    tiers = Counter(str(t.get("tier")) for t in tasks)
    fixture_tasks = [t for t in tasks if t.get("mark") == "bench"]
    passed = sum(1 for t in fixture_tasks if results.get(t["id"]) == "passed")
    failed = sum(1 for t in fixture_tasks if results.get(t["id"]) in {"failed", "error"})
    skipped = sum(1 for t in fixture_tasks if results.get(t["id"]) == "skipped")

    return {
        "suite_id": catalog.get("suite_id", SUITE_ID),
        "schema_version": catalog.get("schema_version", SCHEMA_VERSION),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha,
        "catalog_path": str(path),
        "default_target_package": catalog.get("default_target_package", DEFAULT_TARGET_PACKAGE),
        "data_access_policy": catalog.get("data_access_policy"),
        "task_count": len(tasks),
        "fixture_task_count": marks.get("bench", 0),
        "live_task_count": marks.get("bench_live", 0),
        "marks": dict(sorted(marks.items())),
        "categories": dict(sorted(categories.items())),
        "tiers": dict(sorted(tiers.items())),
        "first_task_id": tasks[0]["id"] if tasks else None,
        "last_task_id": tasks[-1]["id"] if tasks else None,
        "schema_valid": not issues,
        "schema_issues": [i.as_dict() for i in issues],
        "results": {
            "provided": bool(results),
            "fixture_passed": passed,
            "fixture_failed": failed,
            "fixture_skipped": skipped,
            "fixture_total": len(fixture_tasks),
            "fixture_pass_rate": (passed / len(fixture_tasks)) if fixture_tasks else None,
        },
    }
