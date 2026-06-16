"""
Assertion evaluator for aihydro-bench.

An "oracle" is a list of assertions drawn from tasks.yaml. Each assertion
specifies a JSON-path into the tool result dict, an operator, and an
expected value or bound. The evaluator returns a structured report so
test failures show exactly which assertion failed and why.

Supported operators:
  eq        — exact equality
  ne        — not equal
  between   — lo <= value <= hi  (bounds: [lo, hi])
  gt        — value > expected
  ge        — value >= expected
  lt        — value < expected
  approx    — |value - expected| <= tol  (tol: absolute, default 1e-6)
  approx_pct— |value - expected| / |expected| <= pct  (pct: 0-1, default 0.05)
  present   — value is not None
  absent    — key is absent or value is None
  contains  — expected in value  (works for str and list)
  startswith— str value starts with expected
  len_gte   — len(value) >= expected  (path="" = whole result)
  len_eq    — len(value) == expected  (path="" = whole result)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AssertionResult:
    assertion_index: int
    passed: bool
    path: str
    op: str
    actual: Any
    message: str


def _get_nested(d: Any, path: str) -> Any:
    """
    Resolve a dot-separated path into a nested dict or list.

    Empty path returns d unchanged (useful when the result IS the value,
    e.g. a bare list returned by list_claims).
    Integer path segments index into lists: 'quality_flags.0.status'
    resolves as result['quality_flags'][0]['status'].
    Returns None if any step is missing or the index is out of range.
    """
    if not path:
        return d
    parts = path.split(".")
    cur: Any = d
    for part in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def evaluate(result: dict, assertions: list[dict]) -> list[AssertionResult]:
    """
    Evaluate all assertions against a tool result dict.

    Returns one AssertionResult per assertion in order.
    """
    outcomes: list[AssertionResult] = []

    for i, a in enumerate(assertions):
        path = a["path"]
        op = a["op"]
        actual = _get_nested(result, path)
        expected = a.get("expected")
        passed = False
        msg = ""

        try:
            if op == "eq":
                passed = actual == expected
                msg = f"{path} == {expected!r}; got {actual!r}"

            elif op == "ne":
                passed = actual != expected
                msg = f"{path} != {expected!r}; got {actual!r}"

            elif op == "between":
                lo, hi = a["bounds"]
                passed = actual is not None and lo <= actual <= hi
                msg = f"{path} in [{lo}, {hi}]; got {actual!r}"

            elif op == "gt":
                passed = actual is not None and actual > expected
                msg = f"{path} > {expected!r}; got {actual!r}"

            elif op == "ge":
                passed = actual is not None and actual >= expected
                msg = f"{path} >= {expected!r}; got {actual!r}"

            elif op == "lt":
                passed = actual is not None and actual < expected
                msg = f"{path} < {expected!r}; got {actual!r}"

            elif op == "approx":
                tol = a.get("tol", 1e-6)
                passed = actual is not None and abs(actual - expected) <= tol
                msg = f"|{path} - {expected}| <= {tol}; got {actual!r}"

            elif op == "approx_pct":
                pct = a.get("pct", 0.05)
                if expected == 0:
                    passed = actual == 0
                else:
                    passed = actual is not None and abs(actual - expected) / abs(expected) <= pct
                msg = f"{path} within {pct*100:.0f}% of {expected}; got {actual!r}"

            elif op == "present":
                passed = actual is not None
                msg = f"{path} is present; got {actual!r}"

            elif op == "absent":
                passed = actual is None
                msg = f"{path} is absent; got {actual!r}"

            elif op == "contains":
                passed = expected in actual if actual is not None else False
                msg = f"{path} contains {expected!r}; got {actual!r}"

            elif op == "startswith":
                passed = isinstance(actual, str) and actual.startswith(expected)
                msg = f"{path} starts with {expected!r}; got {actual!r}"

            elif op == "len_gte":
                try:
                    passed = actual is not None and len(actual) >= expected
                    msg = f"len({path}) >= {expected!r}; got len={len(actual) if actual is not None else None!r}"
                except TypeError:
                    passed = False
                    msg = f"len({path}) >= {expected!r}; {actual!r} has no len()"

            elif op == "len_eq":
                try:
                    passed = actual is not None and len(actual) == expected
                    msg = f"len({path}) == {expected!r}; got len={len(actual) if actual is not None else None!r}"
                except TypeError:
                    passed = False
                    msg = f"len({path}) == {expected!r}; {actual!r} has no len()"

            else:
                passed = False
                msg = f"Unknown operator: {op!r}"

        except Exception as exc:
            passed = False
            msg = f"Assertion error: {exc}"

        outcomes.append(AssertionResult(
            assertion_index=i,
            passed=passed,
            path=path,
            op=op,
            actual=actual,
            message=msg,
        ))

    return outcomes


def assert_all(result: dict, assertions: list[dict], task_id: str) -> None:
    """
    Raise AssertionError if any assertion fails, with a human-readable report.
    """
    outcomes = evaluate(result, assertions)
    failures = [o for o in outcomes if not o.passed]
    if failures:
        lines = [f"BENCH TASK {task_id} — {len(failures)} assertion(s) failed:"]
        for f in failures:
            lines.append(f"  [{f.assertion_index}] FAIL  {f.message}")
        for o in outcomes:
            if o.passed:
                lines.append(f"  [{o.assertion_index}] pass  {o.message}")
        raise AssertionError("\n".join(lines))
