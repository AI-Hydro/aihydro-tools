"""Capsule directory -> aihydro-core ``Bundle`` (2040 slice 5, P5.4).

A pure reader of a finished capsule (after ``capsule_manifest.json``, before
``bundle.json``): it lists the manifest files as bundle ``objects``, points a
bundle ``records[]`` entry at every sealed record that is *already in the
capsule*, and returns the sealed ``Bundle`` plus the records/bodies maps the
projection takes. It copies no record body and writes nothing.

What becomes a record (locations follow ``aihydro_core.records`` JSON Pointer
rules; each location file must be a bundle object, i.e. a manifest file):

* ``run``             one per run-log row with a sealed v2 record. Body = the
                      row, bound with ``aihydro.entry/1`` when the sealed record
                      names ``extra.entry_digest`` (an unbound row gets no
                      body, so nothing is projected from it). A
                      ``redacted_for_privacy`` / ``seal_mismatch_at_export`` row
                      keeps its entry but its body cannot match, so it is not
                      verified.
* ``claim_revision``  every row of ``records/claim_revisions.json`` (P5.3); a
                      corrupt claim carries no rows and is reported in ``info``.
* ``claim_view``      working (unsealed) claims from ``session.json`` that have
                      no sealed chain.
* ``basin_ref``       the BasinRef summary in a *bound* run row's
                      ``key_outputs`` (core accepts a content-addressed record
                      only inside a body bound to a verified sealed record).

Approval records are deliberately NOT bundle records yet: core treats
``approval`` as content-addressed, so it would never verify (it lies in no run
body) and would permanently degrade coverage. The approval files stay capsule
objects and ``replay.py`` verifies them with its own signer logic. No approver
or signer identity is projected (C9).

Gates are not derived here: ``rocrate_export`` sets ``bundle.gates`` to exactly
``aihydro_core.export.derive_gates`` (the allowlisted code is owned by
``ai_hydro.approval.records``; a test pins that the two agree). Coverage is not
guessed either: ``build_bundle`` takes it from the caller, which derives it by
running core's ``verify_crate`` on the capsule.

``objects`` are the manifest's files plus ``capsule_manifest.json`` and
``replay.py`` themselves, exactly (core ``VER-OBJECTS-MANIFEST``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from aihydro_core.records import (
    GATE_CODES,
    Bundle,
    digest,
    entry_digest,
    make_binding,
    make_coverage,
    make_location,
    make_object_entry,
    make_record_entry,
)
from aihydro_core.records.canonical import is_digest

from ai_hydro.approval.records import APPROVAL_REQUIRED
from ai_hydro.capsule.claim_records import FILENAME as _CLAIMS_FILE, RECORDS_DIR as _RECORDS_DIR

MANIFEST_FILE = "capsule_manifest.json"
REPLAY_FILE = "replay.py"
CLAIM_REVISIONS_PATH = f"{_RECORDS_DIR}/{_CLAIMS_FILE}"
_ZERO_DIGEST = "sha256:" + "0" * 64
assert GATE_CODES == (APPROVAL_REQUIRED,), "core's gate allowlist and the approval code owner disagree"


class AdapterError(ValueError):
    """The capsule cannot be turned into a Bundle (missing manifest, unreadable file)."""


def _read_json(root: Path, rel: str) -> Any:
    try:
        return json.loads((root / rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _role(path: str, data_paths: Mapping[str, str]) -> str:
    if path == MANIFEST_FILE:
        return "manifest"
    if path == REPLAY_FILE:
        return "verifier"
    if path in data_paths:
        return data_paths[path]
    if path == "run_log.json":
        return "run_log"
    if path == "session.json":
        return "session"
    if path == CLAIM_REVISIONS_PATH:
        return "claim_revisions"
    if path.startswith("approvals/"):
        return "approval"
    if path in ("README.md", "methods.md", "citations.bib", "environment.yml"):
        return "documentation"
    return "capsule_file"


def _data_roles(manifest: Mapping[str, Any]) -> dict:
    out: dict = {}
    for a in manifest.get("data_artifacts") or []:
        if not isinstance(a, dict):
            continue
        if a.get("path"):
            out[a["path"]] = a.get("role") or "served_data"
        ra = a.get("retained_artifact")
        if isinstance(ra, dict) and ra.get("path"):
            out[ra["path"]] = "retained_data"
    return out


def build_bundle(capsule_dir: "str | Path", *, files: Mapping[str, Mapping[str, Any]],
                 session_id: str, created_at: str, exporter: Mapping[str, Any],
                 replay: Mapping[str, Any], coverage: Optional[Mapping[str, Any]] = None,
                 claim_heads: Optional[Mapping[str, str]] = None,
                 gates: Optional[list] = None) -> tuple:
    """Return ``(bundle, records, bodies, info)``.

    ``files`` is core's ``scan_files`` map (bare-hex sha256, size, media_type).
    ``coverage`` defaults to "everything verified"; the exporter replaces it with
    the recomputed coverage on its second pass. ``info`` carries
    ``redacted_ids`` (ids whose body or record is withheld for privacy; losing
    them is partial coverage, not an integrity failure), ``claims_not_carried``
    (corrupt chains) and ``claim_heads``.
    """
    root = Path(capsule_dir)
    manifest = _read_json(root, MANIFEST_FILE)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise AdapterError(f"{MANIFEST_FILE} is missing or unreadable in {root.name}")
    data_roles = _data_roles(manifest)
    listed = sorted(m["path"] for m in manifest["files"])
    objects = []
    for path in sorted(set(listed) | {MANIFEST_FILE, REPLAY_FILE}):
        f = files.get(path)
        if f is None:
            raise AdapterError(f"manifest lists {path!r} but the capsule has no such file")
        objects.append(make_object_entry(path, "sha256:" + f["sha256"], f["size"], _role(path, data_roles),
                                         media_type=f.get("media_type")))
    in_objects = set(listed) | {MANIFEST_FILE, REPLAY_FILE}

    entries: list = []
    records: dict = {}
    bodies: dict = {}
    redacted_ids: set = set()
    info: dict = {"claims_not_carried": {}, "redacted_ids": [], "claim_heads": {}}

    def add(entry: dict, rec: Any = None, body: Any = None) -> None:
        entries.append(entry)
        key = f"{entry['kind']}:{entry['id']}"
        if rec is not None:
            records[key] = rec
        if body is not None:
            bodies[key] = body

    # ---- runs and basin refs (run_log.json)
    run_log = _read_json(root, "run_log.json") if "run_log.json" in in_objects else None
    basins: dict = {}
    if isinstance(run_log, dict):
        for run_id in sorted(run_log):
            row = run_log[run_id]
            if not isinstance(row, dict):
                continue
            rec_loc = make_location("run_log.json", [run_id, "record"])
            body_loc = make_location("run_log.json", [run_id])
            record = row.get("record") if isinstance(row.get("record"), dict) else None
            stub = bool(row.get("redacted_for_privacy")) or row.get("integrity") == "seal_mismatch_at_export"
            if stub:
                rd = (record or {}).get("record_digest") if record else row.get("record_digest")
                rd = rd if is_digest(rd) else row.get("record_digest")
                if not is_digest(rd):
                    continue
                ed = row.get("entry_digest") or ((record or {}).get("extra") or {}).get("entry_digest")
                add(make_record_entry("run", run_id, rd, rec_loc, body_loc,
                                      {"scheme": "aihydro.entry/1", "digest": ed if is_digest(ed) else _ZERO_DIGEST}))
                if row.get("redacted_for_privacy"):
                    redacted_ids.add(run_id)
                continue
            if record is None or not is_digest(record.get("record_digest")):
                continue                                   # legacy row: not a sealed record
            bound = isinstance((record.get("extra") or {}).get("entry_digest"), str)
            body = {k: v for k, v in row.items() if k != "record"}
            add(make_record_entry("run", run_id, record["record_digest"], rec_loc,
                                  body_loc if bound else None, make_binding(body) if bound else None),
                record, row if bound else None)
            br = (row.get("key_outputs") or {}).get("basin_ref") if isinstance(row.get("key_outputs"), dict) else None
            if (bound and isinstance(br, dict) and br.get("schema") == "aihydro.basin_ref/1"
                    and isinstance(br.get("id"), str) and br["id"] not in basins):
                basins[br["id"]] = (br, make_location("run_log.json", [run_id, "key_outputs", "basin_ref"]))
    for bid, (br, loc) in sorted(basins.items()):
        add(make_record_entry("basin_ref", bid, digest(br), loc), br)

    # ---- claim revisions (P5.3) and working views
    sealed_claims: set = set()
    doc = _read_json(root, CLAIM_REVISIONS_PATH) if CLAIM_REVISIONS_PATH in in_objects else None
    for cid, item in sorted(((doc or {}).get("claims") or {}).items()) if isinstance(doc, dict) else []:
        if not isinstance(item, dict) or item.get("status") != "ok" or not isinstance(item.get("rows"), list):
            info["claims_not_carried"][cid] = (item or {}).get("error") if isinstance(item, dict) else "unreadable"
            continue
        sealed_claims.add(cid)
        rows = item["rows"]
        stubs = [r for r in rows if isinstance(r, dict) and r.get("redacted_for_privacy")]
        if rows and isinstance(rows[-1], dict) and is_digest(rows[-1].get("revision_digest")):
            info["claim_heads"][cid] = rows[-1]["revision_digest"]
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not is_digest(row.get("record_digest")):
                continue
            rid = f"{cid}@{row.get('revision')}"
            add(make_record_entry("claim_revision", rid, row["record_digest"],
                                  make_location(CLAIM_REVISIONS_PATH, ["claims", cid, "rows", i])),
                None if row.get("redacted_for_privacy") else row)
            if stubs:
                redacted_ids.add(rid)          # a withheld row breaks its claim's chain: partial, not tampered
    session = _read_json(root, "session.json") if "session.json" in in_objects else None
    for cid, claim in sorted((session.get("claims") or {}).items()) if isinstance(session, dict) and isinstance(
            session.get("claims"), dict) else []:
        if isinstance(claim, dict) and cid not in sealed_claims:
            add(make_record_entry("claim_view", cid, None, None,
                                  make_location("session.json", ["claims", cid]), make_binding(claim)),
                None, claim)

    sealed_n = sum(1 for e in entries if e["kind"] != "claim_view")
    cov = dict(coverage) if coverage is not None else make_coverage(sealed_n, sealed_n, [])
    kwargs: dict = {}
    if claim_heads is not None or info["claim_heads"]:
        kwargs["unknown"] = {"claim_heads": dict(sorted((claim_heads or info["claim_heads"]).items()))}
    bundle = Bundle(session_id=session_id, objects=objects, records=entries, created_at=created_at,
                    exporter=dict(exporter), replay=dict(replay), coverage=cov,
                    gates=list(gates) if gates else None, **kwargs).seal()
    info["redacted_ids"] = sorted(redacted_ids)
    info["sealed_records"] = sealed_n
    return bundle, records, bodies, info
