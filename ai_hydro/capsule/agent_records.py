"""Agent run records for the capsule (orchestration Stage 1).

Writes ``agents/<id>.json``, one file per recorded subagent run of the study (see
``ai_hydro.session.agent_runs``). A capsule with no agent runs gets no ``agents/`` directory and
no manifest section, so it is byte-for-byte what it was before.

Privacy: the stored record was path-scrubbed when it was written; this is the second layer
(``scrub_value`` again, counted if it still changed anything). The prompt text is withheld unless
``include_prompts`` is set; ``prompt_sha256`` always stays, so a reader can match a prompt they hold.

The files are ordinary capsule files: the manifest lists and hashes them, so replay's file-hash
check covers them. ``record_digest`` inside each file is the server's digest of the stored row.
It shows the record is unchanged since it was stored, not that the host's account is true:
``asserted_by`` is ``"client"`` and the run records that name the agent (``extra.agent``) are the
independent trace.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_hydro.session import agent_runs as _ar

AGENTS_DIR = "agents"
SCHEMA = "aihydro.capsule.agents/1"


def collect_agent_runs(session_id: str, capsule_dir: str | Path,
                       workspace_dir: str | Path | None = None, *,
                       include_prompts: bool = False) -> tuple[list[dict], dict]:
    """Write ``agents/<id>.json`` and return ``(entries, counts)``.

    ``entries``: one dict per run (``agent_run_id``, ``file``, ``record_digest``, ``verified``).
    ``counts``: ``runs``, ``verified``, ``failed`` (stored row whose digest no longer matches; it is
    still exported, flagged), ``scrubbed_on_export``, ``prompts_included``.
    """
    from ai_hydro.session.refs import scrub_value

    rows = _ar.read_all(session_id, strict=True)
    counts: dict[str, Any] = {"runs": 0, "verified": 0, "failed": 0, "scrubbed_on_export": 0,
                              "prompts_included": bool(include_prompts)}
    if not rows:
        return [], counts

    out_dir = Path(capsule_dir) / AGENTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    used: set[str] = set()
    for rid in sorted(rows):
        row = rows[rid]
        ok = _ar.verify_row(row)
        counts["runs"] += 1
        counts["verified" if ok else "failed"] += 1
        doc = json.loads(json.dumps(row))
        rec = doc.get("record") if isinstance(doc.get("record"), dict) else {}
        doc["record"], withheld = _ar.export_record(rec, include_prompt=include_prompts)
        if withheld:
            # record_digest covers the stored row WITH these fields; this copy cannot re-hash to it
            doc["withheld_fields"] = withheld
            doc["prompt_withheld"] = "prompt" in withheld or ("prompt" in rec and not include_prompts)
        clean = scrub_value(doc, workspace_dir)
        if json.dumps(clean, sort_keys=True) != json.dumps(doc, sort_keys=True):
            counts["scrubbed_on_export"] += 1
            clean["scrubbed_on_export"] = True
        # "unchanged since stored": the stored row still hashes to its record_digest. Not a statement
        # that the host's account is true, and not a re-hash of THIS file when fields were withheld.
        clean["digest_verified_at_export"] = ok
        stem = _ar.file_stem(rid)
        n = 1
        name = stem
        while name in used:
            n += 1
            name = f"{stem}-{n}"
        used.add(name)
        rel = f"{AGENTS_DIR}/{name}.json"
        (Path(capsule_dir) / rel).write_text(
            json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        entries.append({"agent_run_id": rid, "file": rel,
                        "record_digest": row.get("record_digest"), "verified": ok})
    return entries, counts


def manifest_section(entries: list[dict], counts: dict) -> dict | None:
    """The ``agent_runs`` section of capsule_manifest.json, or ``None`` when the study has none."""
    if not entries:
        return None
    return {
        "schema": SCHEMA,
        **counts,
        "runs_listed": entries,
        "note": ("Reported by the host that ran the agents (asserted_by: client). The digest shows the "
                 "stored record is unchanged, not that it is true; the sealed run records that name an "
                 "agent (extra.agent, also client-asserted) are the trace of what each call did. "
                 "digest_verified_at_export means the stored row was unchanged since it was stored. Only an "
                "allowlist of fields is exported (withheld_fields names the rest); the prompt text is "
                "withheld unless the export asked for it, and its hash stays."),
    }
