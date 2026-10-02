"""Privacy layer for exported capsules: no absolute local path leaves the machine.

New rows are scrubbed when they are written (``session.store._scrub_row_body``),
before they are sealed. This module is the second layer, for what is already on
disk and for the other exported text files.

A sealed row is never rewritten, and a scrubbed copy of a sealed row would no
longer verify. So a legacy sealed row that still holds an absolute path is
exported as a ``redacted_for_privacy`` stub carrying its ``record_digest``; the
standalone replay reports it as "redacted (not verifiable from capsule)", neither
PASS nor FAIL. A legacy row with no seal is exported as a scrubbed copy.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_hydro.session.refs import scrub_paths, scrub_value

REDACTED_KEY = "redacted_for_privacy"

# Exported files that must stay byte-identical: sealed/signed content or code.
_SKIP_DIRS = {"approvals", "data"}
_SKIP_NAMES = {"run_log.json", "replay.py", "capsule_manifest.json"}
_TEXT_SUFFIXES = {".md", ".txt", ".bib", ".yml", ".yaml", ".csv"}


def export_run_log(run_log: dict, workspace_dir: str | Path | None = None) -> tuple[dict, dict]:
    """Return ``(exportable run log, counts)``; ``counts`` = {"redacted": n, "scrubbed": n}."""
    out: dict[str, Any] = {}
    counts = {"redacted": 0, "scrubbed": 0}
    for run_id, row in (run_log or {}).items():
        if not isinstance(row, dict):
            out[run_id] = row
            continue
        clean = scrub_value(row, workspace_dir)
        if json.dumps(clean, sort_keys=True, default=str) == json.dumps(row, sort_keys=True, default=str):
            out[run_id] = row                      # verbatim: contained no absolute path
            continue
        record = row.get("record")
        if isinstance(record, dict) and record.get("record_digest"):
            counts["redacted"] += 1
            out[run_id] = {
                REDACTED_KEY: True,
                "run_id": run_id,
                "tool_name": row.get("tool_name"),
                "record_digest": record["record_digest"],
                "reason": "legacy sealed row contained an absolute local path; "
                          "a scrubbed copy would not verify, so the body is withheld",
            }
        else:
            counts["scrubbed"] += 1
            out[run_id] = clean
    return out, counts


def scrub_exported_text(capsule_dir: Path, workspace_dir: str | Path | None = None) -> int:
    """Second layer: scrub every other exported JSON/MD/text file in place.

    Returns how many files were changed. Sealed/signed content (run_log.json,
    approvals/, data/, replay.py) is left alone; run_log.json is handled by
    ``export_run_log``.
    """
    changed = 0
    for f in sorted(Path(capsule_dir).rglob("*")):
        if not f.is_file() or f.name in _SKIP_NAMES:
            continue
        rel = f.relative_to(capsule_dir)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        try:
            if f.suffix == ".json":
                raw = f.read_text(encoding="utf-8")
                new = json.dumps(scrub_value(json.loads(raw), workspace_dir), indent=2)
                if json.dumps(json.loads(raw), indent=2) == new:
                    continue
            elif f.suffix in _TEXT_SUFFIXES:
                raw = f.read_text(encoding="utf-8")
                new = scrub_paths(raw, workspace_dir)
                if new == raw:
                    continue
            else:
                continue
            f.write_text(new, encoding="utf-8")
            changed += 1
        except (OSError, ValueError):
            continue
    return changed
