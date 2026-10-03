"""Claim revision records for the capsule (2040 slice 5, P5.3).

The sealed claim revision chain lives in ``<session>.claims.sqlite3``. This
module reads it read-only (``claim_revisions.history_readonly``; the store is
never written, migrated or WAL-converted) and writes ``records/claim_revisions.json``
into a capsule directory, so a reviewer can re-verify each claim's chain from
the capsule alone (``claim_chain_verify``).

Sealed rows are carried verbatim. A row that holds a local absolute path cannot
be scrubbed without breaking its seal, so it becomes a ``redacted_for_privacy``
stub that keeps ``revision_digest``/``record_digest`` and the chain links (the
same pattern as ``privacy.export_run_log``). A chain is judged per claim and
fails closed per claim: one corrupt claim is exported as ``status: corrupt``
(no rows, error text scrubbed) and does not hide the others.

A seal proves integrity, not origin, and a chain cannot show that its own tail
was cut; each entry therefore records ``head_revision_digest`` so the manifest
or registry can pin it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_hydro.capsule.privacy import REDACTED_KEY
from ai_hydro.session.refs import scrub_paths, scrub_value

SCHEMA = "aihydro.capsule.claim_revisions/1"
RECORDS_DIR = "records"
FILENAME = "claim_revisions.json"


def _stub(row: dict) -> dict:
    return {
        REDACTED_KEY: True,
        "session_id": row.get("session_id"),
        "claim_id": row.get("claim_id"),
        "revision": row.get("revision"),
        "supersedes": row.get("supersedes"),
        "revision_digest": row.get("revision_digest"),
        "record_digest": row.get("record_digest"),
        "recorded_at": row.get("recorded_at"),
        "reason": "sealed claim revision contained an absolute local path; "
                  "a scrubbed copy would not verify, so the body is withheld",
    }


def collect_claim_revisions(session_id: str, capsule_dir: str | Path,
                            workspace_dir: str | Path | None = None) -> tuple[list[dict], dict]:
    """Write ``records/claim_revisions.json`` and return ``(entries, counts)``.

    ``entries`` is one dict per claim (sorted by ``claim_id``):
    ``{claim_id, status: "ok"|"corrupt", revisions, head_revision, head_revision_digest,
    redacted_revisions, error?}``. ``counts``: ``claims``, ``ok``, ``corrupt``,
    ``revisions``, ``redacted``, ``source`` (``absent`` when the session has no
    store; then no file is written and ``entries`` is empty).
    """
    from ai_hydro.session import claim_revisions as cr

    hist, source = cr.history_readonly(session_id)
    counts: dict[str, Any] = {"claims": 0, "ok": 0, "corrupt": 0, "revisions": 0,
                              "redacted": 0, "source": source}
    if not hist:
        return [], counts

    claims: dict[str, dict] = {}
    entries: list[dict] = []
    for cid in sorted(hist):
        item = hist[cid]
        counts["claims"] += 1
        if not item.get("ok"):
            counts["corrupt"] += 1
            error = scrub_paths(str(item.get("error", "")), workspace_dir)
            claims[cid] = {"status": "corrupt", "error": error}
            entries.append({"claim_id": cid, "status": "corrupt", "revisions": 0,
                            "head_revision": None, "head_revision_digest": None,
                            "redacted_revisions": [], "error": error})
            continue
        rows = sorted(item["rows"], key=lambda r: r["revision"])
        out_rows: list[dict] = []
        redacted: list[int] = []
        for row in rows:
            clean = scrub_value(row, workspace_dir)
            if json.dumps(clean, sort_keys=True, default=str) == json.dumps(row, sort_keys=True, default=str):
                out_rows.append(row)                  # verbatim: no absolute path
            else:
                out_rows.append(_stub(row))
                redacted.append(row["revision"])
        counts["ok"] += 1
        counts["revisions"] += len(rows)
        counts["redacted"] += len(redacted)
        claims[cid] = {"status": "ok", "rows": out_rows}
        head = rows[-1]
        entries.append({"claim_id": cid, "status": "ok", "revisions": len(rows),
                        "head_revision": head["revision"],
                        "head_revision_digest": head["revision_digest"],
                        "redacted_revisions": redacted})

    out_dir = Path(capsule_dir) / RECORDS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = {"schema": SCHEMA, "session_id": session_id, "claims": claims}
    (out_dir / FILENAME).write_text(
        json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return entries, counts
