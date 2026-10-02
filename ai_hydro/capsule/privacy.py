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
_TEXT_SUFFIXES = {".md", ".txt", ".bib", ".yml", ".yaml", ".csv", ".html", ".svg"}


def _verifies(row: dict) -> tuple[bool, dict]:
    """(record seal holds AND the raw body matches entry_digest, verify detail)."""
    from ai_hydro.session.run_records import verify_run_log_entry

    v = verify_run_log_entry(row)
    return bool(v.get("has_record") and v.get("record_ok") and v.get("entry_ok") is not False), v


def export_run_log(run_log: dict, workspace_dir: str | Path | None = None) -> tuple[dict, dict]:
    """Return ``(exportable run log, counts)``.

    ``counts``: ``redacted`` / ``scrubbed`` / ``seal_mismatch`` numbers and
    ``redacted_run_ids``. A path-bearing sealed row is verified against its RAW
    body first; redacting must never hide tampering:

    * verifies   -> ``redacted_for_privacy`` stub (session_id, timestamp,
                    record_digest, entry_digest and, when it holds no path,
                    the full ``record`` so replay can still check the seal);
    * mismatch   -> ``{"integrity": "seal_mismatch_at_export", ...}``, which
                    replay counts as a failure.
    """
    out: dict[str, Any] = {}
    counts: dict[str, Any] = {"redacted": 0, "scrubbed": 0, "seal_mismatch": 0, "redacted_run_ids": []}
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
            ok, detail = _verifies(row)
            if not ok:
                counts["seal_mismatch"] += 1
                out[run_id] = {
                    "integrity": "seal_mismatch_at_export",
                    "run_id": run_id,
                    "tool_name": row.get("tool_name"),
                    "record_digest": record.get("record_digest"),
                    "reason": "sealed row did not verify at export (record seal or entry_digest); "
                              "it also held a local path, so the body is withheld, not hidden",
                }
                continue
            counts["redacted"] += 1
            counts["redacted_run_ids"].append(run_id)
            stub: dict[str, Any] = {
                REDACTED_KEY: True,
                "run_id": run_id,
                "session_id": row.get("session_id"),
                "timestamp": row.get("timestamp"),
                "tool_name": row.get("tool_name"),
                "record_digest": record["record_digest"],
                "entry_digest": (record.get("extra") or {}).get("entry_digest"),
                "reason": "legacy sealed row contained an absolute local path; "
                          "a scrubbed copy would not verify, so the body is withheld",
            }
            if scrub_value(record, workspace_dir) == record:
                stub["record"] = record
            out[run_id] = stub
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
