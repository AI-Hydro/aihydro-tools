"""Write ``bundle.json``, ``ro-crate-metadata.json`` and ``manifest-sha256.txt`` for a capsule.

Order (slice 5 interface section 4): capsule + manifest are already written ->
build and seal the Bundle -> ``bundle.json`` -> ``scan_files`` -> ``to_rocrate``
-> ``write_crate`` -> ``write_manifest_sha256``. The three new files are written
last and are never in the capsule manifest or scrubbed (see ``manifest._SKIP_NAMES``,
``privacy._SKIP_NAMES``).

Honesty rules enforced here, not left to the caller:

* The replay level is *achieved*: ``checked_status`` is what the shared
  ``standalone_replay.assess`` (the code replay.py runs) establishes on this
  capsule right now, and ``status`` is ``min(manifest_status, checked_status)``,
  so a crate never claims more than the manifest.
* Coverage is recomputed by core's ``verify_crate`` on the capsule, not assumed.
  A record that is not verified only because privacy withheld it is declared
  partial coverage; any other unverified record is an integrity failure and drops
  ``checked_status`` to ``not_performed``.
* Fail closed on privacy: if ``scrub_value(crate) != crate`` nothing is written.
* The output directory is cleaned of old bundle/crate files first, and a capsule
  holding files its manifest does not list is refused (they would silently
  change the regenerated crate).

Integrity is not origin. A crate shows that content is unchanged since it was
sealed, not who made it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from aihydro_core.export import (
    BAGIT_FILE,
    CRATE_FILE,
    dumps_crate,
    load_inputs,
    scan_files,
    to_rocrate,
    validate_crate,
    verify_crate,
    write_manifest_sha256,
)
from aihydro_core.export.rocrate import derive_gates
from aihydro_core.export.rocrate_validate import errors as _validate_errors
from aihydro_core.records import make_coverage, min_replay_status, read_legacy_replay_status
from aihydro_core.records.run import utc_now

from ai_hydro.capsule import standalone_replay as sr
from ai_hydro.capsule.bundle_adapter import build_bundle
from ai_hydro.session.refs import scrub_value

BUNDLE_FILE = "bundle.json"
MANIFEST_FILE = "capsule_manifest.json"
REPLAY_FILE = "replay.py"
_OWN_OUTPUTS = (BUNDLE_FILE, CRATE_FILE, BAGIT_FILE)


class CrateExportError(RuntimeError):
    """The crate was not written (and no partial crate files are left behind)."""


def _tools_version() -> str:
    from ai_hydro import __version__

    return __version__


def clean_outputs(capsule_dir: Path) -> None:
    for name in _OWN_OUTPUTS:
        try:
            (capsule_dir / name).unlink()
        except FileNotFoundError:
            pass


def _check_clean(capsule_dir: Path) -> dict:
    manifest = json.loads((capsule_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    listed = {m["path"] for m in manifest["files"]}
    try:
        on_disk = set(scan_files(capsule_dir))
    except ValueError as exc:                       # symlinks are never part of a capsule
        raise CrateExportError(str(exc)) from exc
    stray = sorted(on_disk - listed - {MANIFEST_FILE, REPLAY_FILE})
    if stray:
        raise CrateExportError(f"capsule holds files its manifest does not list (not a clean export): {stray[:5]}")
    return manifest


def export_crate(capsule_dir: "str | Path", *, session_id: str, workspace_dir: "str | Path | None" = None,
                 claim_entries: Optional[list] = None, live: bool = True, license: Optional[str] = None,
                 created_at: Optional[str] = None) -> dict:
    """Build and write the bundle, crate and BagIt manifest. Returns a summary dict.

    Raises :class:`CrateExportError` (after removing any partial output) when the
    capsule is not clean, the privacy check fails, or the result does not verify
    for a reason other than recorded integrity failures.
    """
    root = Path(capsule_dir)
    clean_outputs(root)
    try:
        return _export(root, session_id, workspace_dir, claim_entries, live, license, created_at)
    except BaseException:
        clean_outputs(root)
        raise


def _export(root: Path, session_id: str, workspace_dir, claim_entries, live: bool, license, created_at) -> dict:
    manifest = _check_clean(root)
    if not (root / REPLAY_FILE).is_file():
        raise CrateExportError("replay.py must be written before the crate (it is the named verifier)")
    created_at = created_at or utc_now()

    # ---- what is achieved now: the same assessment replay.py runs, minus the crate that does not exist yet
    assessment = sr.assess(root, live=live, out=lambda *_a, **_k: None, check_crate=False)
    manifest_status, _complete = read_legacy_replay_status(str(manifest.get("replay_status", "not_performed")))
    checked = assessment["status"]
    exporter = {"name": "aihydro-tools", "version": _tools_version()}
    assessor = {"name": "replay.py", "version": _tools_version(),
                "sha256": hashlib.sha256((root / REPLAY_FILE).read_bytes()).hexdigest()}
    heads = {e["claim_id"]: e["head_revision_digest"] for e in (claim_entries or [])
             if e.get("status") == "ok" and e.get("head_revision_digest")} or None

    def replay_for(checked_status: str) -> dict:
        return {"status": min_replay_status(manifest_status, checked_status).value,
                "manifest_status": manifest_status.value, "checked_status": checked_status,
                "assessor": assessor}

    def make(coverage, checked_status, gates=None):
        files = scan_files(root)
        return files, build_bundle(root, files=files, session_id=session_id, created_at=created_at,
                                   exporter=exporter, replay=replay_for(checked_status),
                                   coverage=coverage, claim_heads=heads, gates=gates)

    def write_bundle(bundle) -> None:
        (root / BUNDLE_FILE).write_text(json.dumps(bundle.to_dict(), indent=2, sort_keys=True) + "\n",
                                        encoding="utf-8")

    # ---- pass 1: provisional coverage; core's verifier tells us what is actually verified
    files, (bundle, _r, _b, info) = make(None, checked)
    write_bundle(bundle)
    probe = verify_crate(root)
    bad = set(probe.unverifiable_ids)
    unexplained = sorted(bad - set(info["redacted_ids"]))
    structural = [f for f in probe.failures
                  if f.rule in ("VER-BUNDLE-IDENTITY", "VER-BUNDLE-SEAL", "VER-FILE-DIGEST", "VER-OBJECTS-MANIFEST",
                                "VER-UNLISTED-FILE", "VER-ASSESSOR")]
    if structural:
        raise CrateExportError("bundle does not match the capsule files: "
                               + "; ".join(f"{f.rule}: {f.message}" for f in structural[:3]))
    if unexplained or not assessment["integrity_ok"]:
        checked = "not_performed"
    cov = make_coverage(probe.records_verified, probe.records_total, bad)

    # ---- pass 2: the recomputed coverage and achieved level, then gates exactly as core derives them
    files, (bundle, recs, bods, info) = make(cov, checked)
    gates = derive_gates(bundle, recs, bods)
    files, (bundle, _r, _b, info) = make(cov, checked, gates)
    write_bundle(bundle)
    records, bodies, files_now = load_inputs(root, bundle)
    crate = to_rocrate(bundle, records, bodies, files_now, license=license)
    if scrub_value(crate, workspace_dir) != crate:
        raise CrateExportError("privacy check failed: the crate holds a local path; nothing was written")
    (root / CRATE_FILE).write_bytes(dumps_crate(crate).encode("utf-8"))
    write_manifest_sha256(root)

    final = verify_crate(root)
    findings = _validate_errors(validate_crate(root))
    if findings:
        raise CrateExportError("crate failed validation: "
                               + "; ".join(f"{f.rule}: {f.message}" for f in findings[:3]))
    if not final.ok:
        raise CrateExportError("crate failed verification: "
                               + "; ".join(f"{f.rule}: {f.message}" for f in final.failures[:3]))
    return {
        "crate_file": str(root / CRATE_FILE), "bundle_file": str(root / BUNDLE_FILE),
        "bagit_file": str(root / BAGIT_FILE), "bundle_id": bundle.bundle_id,
        "replay_status": bundle.replay["status"], "checked_status": checked,
        "manifest_status": manifest_status.value, "coverage": bundle.coverage,
        "unexplained_unverifiable": unexplained, "claims_not_carried": info["claims_not_carried"],
        "claim_heads": heads or {}, "gates": gates,
    }


def _tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def convert_capsule(src: "str | Path", dst: "str | Path", *, live: bool = True,
                    license: Optional[str] = None) -> dict:
    """Out-of-place conversion of an existing capsule: copy ``src`` to ``dst``, then build the crate there.

    ``src`` is never written (its file digests are compared before and after). ``dst``
    must not exist or must be empty. The copy gets the current ``replay.py`` (which
    verifies the bundle and crate), because the old one predates them; ``replay.py``
    is not in the capsule manifest, so no sealed digest changes. A capsule exported
    before claim revisions were carried has no ``records/claim_revisions.json``: its
    claims appear as unsealed working views and ``claim_revisions`` is reported
    ``not_carried``.
    """
    import shutil

    src, dst = Path(src), Path(dst)
    if not (src / MANIFEST_FILE).is_file():
        raise CrateExportError(f"{src} is not a capsule (no {MANIFEST_FILE})")
    if dst.exists() and any(dst.iterdir()):
        raise CrateExportError(f"output directory {dst} is not empty; conversion is out of place")
    try:
        dst.resolve().relative_to(src.resolve())
        raise CrateExportError("output directory must not be inside the input capsule")
    except ValueError:
        pass
    before = _tree_digest(src)
    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False)
    clean_outputs(dst)
    (dst / REPLAY_FILE).write_text(sr.source_text(), encoding="utf-8")
    try:
        session = json.loads((dst / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        session = {}
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise CrateExportError("session.json has no session_id")
    carried = (dst / "records" / "claim_revisions.json").is_file()
    try:
        out = export_crate(dst, session_id=session_id, workspace_dir=session.get("workspace_dir"),
                           live=live, license=license)
    finally:
        if _tree_digest(src) != before:               # pragma: no cover - the copy cannot touch src
            raise CrateExportError("input capsule changed during conversion")
    out["claim_revisions"] = "carried" if carried else "not_carried"
    out["output_dir"] = str(dst)
    return out
