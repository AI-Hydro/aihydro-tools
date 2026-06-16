#!/usr/bin/env python3
"""
HydroResearch-Bench scorecard generator.

Usage:
    python bench/gen_scorecard.py [--run] [--out hrb_scorecard.html]

Without --run: generates a task-catalog scorecard from tasks.yaml only
               (all tasks shown as "pending").
With --run:    executes pytest with --junit-xml, then overlays actual
               pass/fail results onto the catalog.

The output is a single self-contained HTML file with no external deps.
"""
from __future__ import annotations

import argparse
import html
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

BENCH_DIR = Path(__file__).parent
TASKS_YAML = BENCH_DIR / "tasks.yaml"
TESTS_FILE = BENCH_DIR.parent / "tests" / "test_bench.py"

# Category display order and labels
CATEGORY_ORDER = [
    "validator",
    "signatures",
    "ledger",
    "enforcement",
    "auditor",
    "uncertainty",
    "capsule",
    "prereg",
    "validators",
    "experiments",
    "registry",
    "skeptic",
    "literature",
    "hrb",
    "knowledge",
    "watershed",
]

CATEGORY_LABEL = {
    "validator": "Water Balance Validators",
    "signatures": "Hydrological Signatures",
    "ledger": "Claim Ledger",
    "enforcement": "Enforcement Middleware",
    "auditor": "Answer Auditor",
    "uncertainty": "Uncertainty Gating",
    "capsule": "Capsule & Defensibility",
    "prereg": "Pre-registration",
    "validators": "Advanced Validators",
    "experiments": "Experiments",
    "registry": "Claim Registry",
    "skeptic": "Skeptic Agent",
    "literature": "Literature Grounding",
    "hrb": "HydroResearch-Bench E2E",
    "knowledge": "Knowledge Cards",
    "watershed": "Live Watershed (network)",
}


def load_tasks() -> list[dict[str, Any]]:
    raw = yaml.safe_load(TASKS_YAML.read_text(encoding="utf-8"))
    return raw.get("tasks", [])


def run_pytest(extra_args: list[str]) -> dict[str, str]:
    """
    Run pytest against the bench tests and return a map of {test_node_id → status}.
    status is one of: "passed", "failed", "error", "skipped".
    """
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        xml_path = tmp.name

    cmd = [
        sys.executable, "-m", "pytest",
        str(TESTS_FILE),
        "-m", "bench",
        "--junit-xml", xml_path,
        "--tb=no", "-q", "--no-header",
    ] + extra_args

    print(f"Running: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=False)
    print(f"Exit code: {result.returncode}", flush=True)

    if not Path(xml_path).exists():
        print("Warning: no JUnit XML produced — returning empty results", file=sys.stderr)
        return {}

    return _parse_junit(xml_path)


def _parse_junit(xml_path: str) -> dict[str, str]:
    """Parse JUnit XML → map of task_id → status."""
    tree = ET.parse(xml_path)
    results: dict[str, str] = {}
    for tc in tree.iter("testcase"):
        name = tc.get("name", "")
        # pytest names look like "test_bench[B-001-Water balance: humid ...]"
        # Extract the task id from the name
        task_id = _extract_task_id(name)
        if not task_id:
            continue
        if tc.find("failure") is not None:
            results[task_id] = "failed"
        elif tc.find("error") is not None:
            results[task_id] = "error"
        elif tc.find("skipped") is not None:
            results[task_id] = "skipped"
        else:
            results[task_id] = "passed"
    return results


def _extract_task_id(name: str) -> str | None:
    """Extract B-NNN from a pytest test node name."""
    import re
    m = re.search(r"\b(B-\d{3})\b", name)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

STYLE = """
:root {
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
  --border: #30363d; --fg: #e6edf3; --fg2: #8b949e;
  --pass: #3fb950; --fail: #f85149; --skip: #d29922;
  --pend: #58a6ff; --accent: #1f6feb;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg); font-size: 14px; line-height: 1.5;
  padding: 24px 32px;
}
h1 { font-size: 22px; font-weight: 700; margin-bottom: 4px; }
.subtitle { color: var(--fg2); font-size: 13px; margin-bottom: 24px; }
.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px; margin-bottom: 32px;
}
.stat-card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 16px; text-align: center;
}
.stat-card .num { font-size: 28px; font-weight: 700; }
.stat-card .lbl { font-size: 11px; color: var(--fg2); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 2px; }
.pass-num { color: var(--pass); }
.fail-num { color: var(--fail); }
.skip-num { color: var(--skip); }
.pend-num { color: var(--pend); }
.section { margin-bottom: 28px; }
.section-header {
  font-size: 13px; font-weight: 600; color: var(--fg2);
  text-transform: uppercase; letter-spacing: 0.08em;
  border-bottom: 1px solid var(--border); padding-bottom: 6px; margin-bottom: 8px;
}
table { width: 100%; border-collapse: collapse; }
th {
  text-align: left; font-size: 11px; font-weight: 600; color: var(--fg2);
  text-transform: uppercase; letter-spacing: 0.05em;
  padding: 6px 10px; border-bottom: 1px solid var(--border);
}
td { padding: 7px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--bg3); }
.task-id { font-family: monospace; color: var(--fg2); font-size: 12px; }
.badge {
  display: inline-block; border-radius: 4px; font-size: 11px;
  font-weight: 600; padding: 2px 7px; font-family: monospace;
}
.badge-pass { background: #1b3a2a; color: var(--pass); }
.badge-fail { background: #3d1a1a; color: var(--fail); }
.badge-error { background: #3d1a1a; color: #ff7b72; }
.badge-skip { background: #2d2200; color: var(--skip); }
.badge-pend { background: #1a2540; color: var(--pend); }
.badge-live { background: #1c2533; color: #79c0ff; border: 1px solid var(--border); }
.tier { font-size: 11px; color: var(--fg2); }
footer { margin-top: 32px; color: var(--fg2); font-size: 12px; }
"""


def _status_badge(status: str, mark: str = "bench") -> str:
    if mark == "bench_live":
        return '<span class="badge badge-live">live</span>'
    labels = {
        "passed":  ("pass",  "PASS"),
        "failed":  ("fail",  "FAIL"),
        "error":   ("error", "ERROR"),
        "skipped": ("skip",  "SKIP"),
        "pending": ("pend",  "PENDING"),
    }
    cls, txt = labels.get(status, ("pend", status.upper()))
    return f'<span class="badge badge-{cls}">{txt}</span>'


def render_html(tasks: list[dict], results: dict[str, str], ran: bool) -> str:
    total = len(tasks)
    passed = sum(1 for t in tasks if results.get(t["id"]) == "passed")
    failed = sum(1 for t in tasks if results.get(t["id"]) in ("failed", "error"))
    skipped = sum(1 for t in tasks if results.get(t["id"]) == "skipped")
    live = sum(1 for t in tasks if t.get("mark") == "bench_live")
    pending = total - passed - failed - skipped - live
    pct = f"{100 * passed // (total - live):.0f}%" if (total - live) > 0 else "—"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode_label = "benchmark run" if ran else "task catalog (no test run)"

    # Group tasks by category preserving CATEGORY_ORDER
    grouped: dict[str, list[dict]] = {}
    for cat in CATEGORY_ORDER:
        grouped[cat] = []
    for t in tasks:
        cat = t.get("category", "other")
        grouped.setdefault(cat, []).append(t)

    rows_html = ""
    for cat in list(grouped.keys()) + [c for c in grouped if c not in CATEGORY_ORDER]:
        cat_tasks = grouped.get(cat, [])
        if not cat_tasks:
            continue
        label = CATEGORY_LABEL.get(cat, cat.title())
        rows_html += f"""
<div class="section">
  <div class="section-header">{html.escape(label)} ({len(cat_tasks)})</div>
  <table>
    <thead><tr><th>ID</th><th>Task name</th><th>Tier</th><th>Status</th></tr></thead>
    <tbody>"""
        for t in cat_tasks:
            tid = t["id"]
            name = html.escape(t.get("name", tid))
            tier = t.get("tier", "—")
            mark = t.get("mark", "bench")
            status = results.get(tid, "pending")
            badge = _status_badge(status, mark)
            rows_html += f"""
      <tr>
        <td class="task-id">{tid}</td>
        <td>{name}</td>
        <td class="tier">T{tier}</td>
        <td>{badge}</td>
      </tr>"""
        rows_html += "\n    </tbody>\n  </table>\n</div>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HydroResearch-Bench Scorecard</title>
<style>{STYLE}</style>
</head>
<body>
<h1>HydroResearch-Bench (HRB)</h1>
<p class="subtitle">AI-Hydro platform · {mode_label} · {ts}</p>

<div class="summary-grid">
  <div class="stat-card"><div class="num">{total}</div><div class="lbl">Total tasks</div></div>
  <div class="stat-card"><div class="num pass-num">{passed}</div><div class="lbl">Passed</div></div>
  <div class="stat-card"><div class="num fail-num">{failed}</div><div class="lbl">Failed</div></div>
  <div class="stat-card"><div class="num skip-num">{skipped}</div><div class="lbl">Skipped</div></div>
  <div class="stat-card"><div class="num pend-num">{live}</div><div class="lbl">Live (network)</div></div>
  <div class="stat-card"><div class="num">{pct}</div><div class="lbl">Pass rate (fixture)</div></div>
</div>

{rows_html}

<footer>
  Generated by <code>bench/gen_scorecard.py</code> · AI-Hydro Platform ·
  Tasks defined in <code>bench/tasks.yaml</code>
</footer>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate HydroResearch-Bench scorecard")
    parser.add_argument("--run", action="store_true", help="Execute pytest before rendering")
    parser.add_argument("--out", default="hrb_scorecard.html", help="Output HTML path")
    parser.add_argument("pytest_args", nargs="*", help="Extra args forwarded to pytest")
    args = parser.parse_args()

    tasks = load_tasks()
    print(f"Loaded {len(tasks)} tasks from {TASKS_YAML}")

    results: dict[str, str] = {}
    if args.run:
        results = run_pytest(args.pytest_args)
        print(f"Parsed {len(results)} test results from JUnit XML")

    out_path = Path(args.out)
    out_path.write_text(render_html(tasks, results, ran=args.run), encoding="utf-8")
    print(f"Scorecard written → {out_path.resolve()}")


if __name__ == "__main__":
    main()
