"""
Run records for the session run log (ADR-001, slice 1a).

Every tool call that resolves a session is recorded as a sealed
``aihydro.run/2`` record (``aihydro_core.records.RunRecord``) attached to the
call's run-log row. The row keeps its legacy dict shape; the record rides in
``entry["record"]``.

What a record claims, and what it does not:

- It identifies *which tool* ran, *which version*, with *which arguments*
  (digest), producing *which output* (digest), in *which environment*
  (digest), and optionally which earlier runs it consumed (``parents``).
- It does not say the computation is reproducible, correct, or appropriate.
  ``record_digest`` only makes later edits to the record detectable.

Limits of the seal (read before relying on a record):

- A seal proves *integrity*, not *origin*. Anyone who can write the run log
  can build a record that verifies.
- Insert-only rows protect against cooperating writers (stale snapshots,
  accidental rewrites), not against an adversary with access to the SQLite
  file, who can also delete or rebuild rows.
- A deleted sealed row is not detectable: there is no hash chain or signed
  head yet. (swatplus-builder's hash-chained ledger is the model for that.)
- A writer that pre-seals its own record, including ``run_python`` code or any
  same-user process, authors its own provenance. The middleware leaves an
  already-sealed row untouched and does not overwrite it.
- A row the middleware could not read or seal within the lock budget is retried
  on the next recorded call of the session and by ``export_session`` (see
  "Lazy re-seal" below), only while its body is unchanged. Otherwise it is
  marked ``record_status: unsealable``. The retry set is process-local.
- ``input_digest`` covers the arguments the tool received after argument
  repair (``ArgRepairMiddleware``), not the effective parameters after
  defaults, so a call that omits a default and one that passes it explicitly
  digest differently. When repair changed the call, ``extra.arg_repairs`` lists
  each repair and ``extra.supplied_input_digest`` digests the arguments as
  the caller sent them.
- ``output_digest`` covers what the caller received (``result["data"]``), not
  arrays a tool read from or wrote to disk.
- ``extra.product_substituted`` / ``requested_product`` / ``served_product`` are present only when a pinned product failed and a
  different one served (data_fetch with an opt-in ``fallback``).
- ``extra.watershed_reliability`` (``"unreliable"``) is present only when the call consumed a session watershed the delineation
  marked unreliable (the result then carries ``watershed_reliability_warning``).
- ``extra.attempt_trail`` is the attempt trail: which sources a call tried (each with its outcome and
  reason), which one served it, and the limitations it reported (``quality_flags``), on success and on failure
  (see ``attempt_trail``). It lives in ``extra``, so the record seal covers it. Records written by earlier
  versions have no such key; absence there means "not recorded", never "no source was tried". Sealed rows are
  insert-only and are not rewritten, so they are not backfilled. A call that reports no ``fallback_history``
  gets no key, and its record digest is the one earlier versions produced.

``build_run_record`` never raises. When a digest cannot be computed the record
is still sealed, the digest is ``None`` and ``record_error`` says why.
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from aihydro_core.records import (
    RunRecord,
    digest_or_error,
    environment_fingerprint,
    input_ref,
    is_digest,
)

log = logging.getLogger("ai_hydro.session.run_records")

# --------------------------------------------------------------------------- #
# What goes into digests
# --------------------------------------------------------------------------- #

# Secret-shaped argument names are never digested (a digest of a secret is a
# guessing oracle). Same family as tools_execution.py::_scrub_env.
SECRET_INPUT_KEY_PATTERN = re.compile(
    r"(_key$|_token$|_secret$|credential|_password$)", re.IGNORECASE
)

# ``session_id`` is a top-level field of the run-log row, so it is excluded:
# two sessions that call a tool with the same arguments share an input_digest.
_NON_INPUT_KEYS = frozenset({"ctx", "session", "session_id"})

# Added by post_run and by the recording middleware, not produced by the tool.
TRANSPORT_KEYS = frozenset({"_run_id", "_record_error", "_arg_repairs", "quality_flags", "next_steps"})

#: Tool dependencies whose installed versions go into the environment digest.
ENV_DISTRIBUTIONS = (
    "aihydro-tools", "aihydro-core", "aihydro-data", "aihydro-watershed",
    "aihydro-lsh", "aihydro-modelling", "numpy", "pandas", "scipy", "xarray",
    "dataretrieval",
)


def scrub_arguments(value: Any, _top: bool = True) -> Any:
    """Return ``value`` without private, ``ctx``/session and secret-shaped keys.

    Applied recursively to nested dicts. Strings, including long ones, are kept
    in full: the digest is the only thing stored.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            skey = key if isinstance(key, str) else str(key)
            if not skey or skey.startswith("_"):
                continue
            if _top and skey in _NON_INPUT_KEYS:
                continue
            if SECRET_INPUT_KEY_PATTERN.search(skey):
                continue
            out[skey] = scrub_arguments(item, _top=False)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_arguments(item, _top=False) for item in value]
    return value


def output_payload(result: Any) -> Any:
    """The part of a tool result the output digest covers.

    ``result["data"]`` when the tool returned one; otherwise the result minus
    transport keys.
    """
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, (dict, list)):
            return data
        return {k: v for k, v in result.items() if k not in TRANSPORT_KEYS}
    return result


# --------------------------------------------------------------------------- #
# Environment and tool version
# --------------------------------------------------------------------------- #

_ENV_LOCK = threading.Lock()
_ENV_CACHE: Optional[Tuple[Dict[str, Any], str]] = None


def process_environment() -> Tuple[Dict[str, Any], str]:
    """``(fingerprint, env_digest)`` for this process, computed once."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        with _ENV_LOCK:
            if _ENV_CACHE is None:
                _ENV_CACHE = environment_fingerprint(ENV_DISTRIBUTIONS)
    return _ENV_CACHE


def _distribution_version() -> Optional[str]:
    return process_environment()[0]["distributions"].get("aihydro-tools")


def _package_version() -> Optional[str]:
    """``ai_hydro.__version__`` of the running source, or None if unknown."""
    try:
        from ai_hydro import __version__
    except Exception:
        return None
    return __version__ if isinstance(__version__, str) and __version__ and __version__ != "unknown" else None


def resolve_tool_version(result: Any) -> Tuple[Optional[str], str]:
    """``(version, version_source)`` of the code serving the call.

    ``ai_hydro.__version__`` ("package"): the running source of the host that
    executed and recorded the tool, whichever package computed the result. The
    installed ``aihydro-tools`` distribution metadata is only a last-resort
    fallback ("distribution"), since it can lag the source (stale editable-install
    dist-info). A result's own ``meta.tool`` / ``meta.version`` (the producer, which
    may be another package such as aihydro-watershed) is not the tool version; it is
    kept in ``extra.producer`` / ``extra.producer_version`` (see
    :func:`producer_fields`). ``result`` is unused and kept for call compatibility.
    """
    version = _package_version()
    if version:
        return version, "package"
    version = _distribution_version()
    return version, ("distribution" if version else "unavailable")


def producer_fields(result: Any) -> Dict[str, Any]:
    """``{producer, producer_version}`` from a result's ``meta`` (absent keys omitted)."""
    meta = result.get("meta") if isinstance(result, dict) else None
    out: Dict[str, Any] = {}
    if isinstance(meta, dict):
        tool, ver = meta.get("tool"), meta.get("version")
        if isinstance(tool, str) and tool:
            out["producer"] = tool
        if isinstance(ver, str) and ver:
            out["producer_version"] = ver
    return out


def version_check(result: Any) -> Optional[Dict[str, Any]]:
    """Cross-check the versions available for a record; None when they agree.

    Returned, with ``version_mismatch: True`` and every value seen, when

    * the ``aihydro-tools`` distribution metadata differs from
      ``ai_hydro.__version__`` (stale editable-install ``dist-info``), or
    * the producer's ``meta.version`` differs from the ``__version__`` of the
      already-imported package named by ``meta.tool``.

    Neither value is silently preferred: the record keeps both. The producer
    version is always included when the result states one.
    """
    package = _package_version()
    dist = _distribution_version()
    prod = producer_fields(result)
    producer_v = prod.get("producer_version")
    module_v: Optional[str] = None
    if producer_v and prod.get("producer"):
        mod = sys.modules.get(prod["producer"].split(".", 1)[0])
        pv = getattr(mod, "__version__", None)
        module_v = pv if isinstance(pv, str) and pv else None
    dist_bad = bool(package and dist and package != dist)
    producer_bad = bool(producer_v and module_v and producer_v != module_v)
    if not (dist_bad or producer_bad):
        return None
    return {"version_mismatch": True, "package_version": package,
            "distribution_version": dist, "producer_version": producer_v,
            "producer_module_version": module_v}


# --------------------------------------------------------------------------- #
# Building records
# --------------------------------------------------------------------------- #

def entry_body_digest(entry: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Digest of a run-log row without its ``record``.

    Stored in the record as ``extra.entry_digest`` so that editing the legacy
    fields (``key_outputs``, ``evidence``, ...) of a sealed row is detectable.
    """
    return digest_or_error({k: v for k, v in entry.items() if k != "record"})


def build_run_record(
    *,
    run_id: str,
    tool: str,
    session_id: Optional[str],
    arguments: Any = None,
    result: Any = None,
    status: Optional[str] = None,
    parents: Iterable[str] = (),
    input_refs: Iterable[Dict[str, Any]] = (),
    extra: Optional[Dict[str, Any]] = None,
    entry: Optional[Dict[str, Any]] = None,
    actor: Optional[Dict[str, Any]] = None,
    output_skip_reason: Optional[str] = None,
    known_errors: Iterable[str] = (),
) -> RunRecord:
    """Build and seal one run record. Never raises.

    ``entry`` is the existing run-log row, if any; its body digest is bound
    into ``extra.entry_digest``. Failures to digest an input, output or the
    row set the corresponding digest to ``None`` and add to ``record_error``.
    ``output_skip_reason`` records that the output was deliberately not
    digested (for example because it is too large), as a ``record_error``.
    """
    errors: List[str] = [str(e) for e in known_errors]

    def _digest(label: str, value: Any) -> Optional[str]:
        try:
            value_digest, error = digest_or_error(value)
        except Exception as exc:  # defensive: digest_or_error should not raise
            value_digest, error = None, f"{type(exc).__name__}: {exc}"
        if error:
            errors.append(f"{label}: {error}")
        return value_digest

    try:
        if status is None:
            status = "error" if (isinstance(result, dict) and result.get("error")) else "ok"
        input_digest = _digest("input_digest", scrub_arguments(arguments or {}))
        output_digest = None
        if output_skip_reason:
            errors.append(f"output_digest: skipped ({output_skip_reason})")
        elif result is not None:
            output_digest = _digest("output_digest", output_payload(result))
        tool_version, version_source = resolve_tool_version(result)
        try:
            env_digest = process_environment()[1]
        except Exception as exc:
            env_digest = None
            errors.append(f"env_digest: {type(exc).__name__}: {exc}")
        merged_extra = dict(extra or {})
        try:
            vcheck = version_check(result)
        except Exception as exc:
            vcheck = None
            errors.append(f"version_check: {type(exc).__name__}: {exc}")
        for k, v in producer_fields(result).items():
            merged_extra.setdefault(k, v)
        if vcheck:
            merged_extra["version_check"] = vcheck
        if entry is not None:
            entry_digest = _digest("entry_digest", {k: v for k, v in entry.items() if k != "record"})
            if entry_digest:
                merged_extra["entry_digest"] = entry_digest
        record = RunRecord(
            run_id=run_id,
            tool=tool,
            tool_version=tool_version,
            version_source=version_source,
            session_id=session_id,
            status=status,
            input_digest=input_digest,
            input_refs=[dict(r) for r in input_refs],
            output_digest=output_digest,
            parents=[p for p in parents if p],
            env_digest=env_digest,
            actor=actor,
            record_error="; ".join(errors) if errors else None,
            extra=merged_extra,
        )
        return record.seal()
    except Exception as exc:  # last resort: a record that says it failed
        log.warning("build_run_record failed for %s: %s", run_id, exc)
        try:
            return RunRecord(
                run_id=run_id or "unknown",
                tool=tool or "unknown",
                session_id=session_id,
                status="error",
                record_error=f"build_run_record: {type(exc).__name__}: {exc}",
            ).seal()
        except Exception:  # pragma: no cover - constructor cannot fail on these
            raise


def verify_run_log_entry(entry: Any) -> Dict[str, Any]:
    """Integrity of one run-log row's record.

    ``has_record`` is False for legacy rows. ``record_ok`` is the record's own
    seal; ``entry_ok`` is the binding to the row's legacy fields (``None`` when
    the record carries no ``entry_digest``).
    """
    record = entry.get("record") if isinstance(entry, dict) else None
    if not isinstance(record, dict):
        return {"has_record": False, "record_ok": None, "entry_ok": None, "record_error": None}
    try:
        record_ok = RunRecord.from_dict(record).verify()
    except Exception:
        record_ok = False
    entry_ok = None
    expected = (record.get("extra") or {}).get("entry_digest") if isinstance(record.get("extra"), dict) else None
    if expected is not None:
        actual, _ = entry_body_digest(entry)
        entry_ok = actual == expected
    return {
        "has_record": True,
        "record_ok": record_ok,
        "entry_ok": entry_ok,
        "record_error": record.get("record_error"),
    }


def coverage_summary(run_log: Dict[str, Any]) -> Dict[str, Any]:
    """Record coverage of a ``{run_id: entry}`` run log (additive snapshot field)."""
    total = recorded = verified = unbound = with_error = unsealable = 0
    problems: List[Dict[str, Any]] = []
    for run_id, entry in (run_log or {}).items():
        total += 1
        check = verify_run_log_entry(entry)
        if not check["has_record"]:
            if isinstance(entry, dict) and entry.get("record_status") == "unsealable":
                unsealable += 1
                problems.append({"run_id": run_id, "problem": "unsealable",
                                 "reason": entry.get("record_status_reason")})
            continue
        recorded += 1
        if check["record_ok"] and check["entry_ok"] is True:
            verified += 1
        elif check["record_ok"] and check["entry_ok"] is None:
            unbound += 1       # sealed, but not bound to its row's legacy fields
        else:
            problems.append({"run_id": run_id, "problem": "record_digest_mismatch"
                             if not check["record_ok"] else "entry_modified_after_sealing"})
        if check["record_error"]:
            with_error += 1
            problems.append({"run_id": run_id, "problem": "record_error", "record_error": check["record_error"]})
    return {
        "schema": "aihydro.run/2",
        "run_log_rows": total,
        "v2_records": recorded,
        "v2_verified": verified,
        "v2_unbound": unbound,
        "legacy_unrecorded": total - recorded,   # includes the unsealable rows below
        "unsealable": unsealable,
        "record_errors": with_error,
        "coverage": round(recorded / total, 4) if total else None,
        "problems": problems[:50],
    }


# --------------------------------------------------------------------------- #
# Per-call capture: which rows a tool call wrote, and what it consumed
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class CallCapture:
    """Mutable record of one tool call, shared with worker threads by reference."""

    rows: List[Tuple[str, str, str]] = dataclasses.field(default_factory=list)  # (session, run, writer)
    parents: List[str] = dataclasses.field(default_factory=list)
    #: Run ids of the stored results the tool read through ``HydroSession.get``/``get_result`` (see
    #: ``note_slot_read``). Kept apart from ``parents`` so an explicit "this slot was not consumed"
    #: (``parent_unresolved``) can win over them.
    slot_parents: List[str] = dataclasses.field(default_factory=list)
    input_refs: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    notes: Dict[str, Any] = dataclasses.field(default_factory=dict)
    failed_rows: List[Tuple[str, str, str, str]] = dataclasses.field(default_factory=list)  # (session, run, writer, reason)
    refused_rows: List[Tuple[str, str, str, str]] = dataclasses.field(default_factory=list)  # (session, run, writer, outcome)
    #: Workspace files a tool wrote during the call: (study that owns the write or None, absolute path).
    files: List[Tuple[Optional[str], str]] = dataclasses.field(default_factory=list)
    #: Map layers a tool pushed during the call (see ``note_layer_pushed``).
    layers: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    #: What the middleware knew when the call began: ``tool``, ``arguments``, ``chat_id``, ``study_id``
    #: and ``id_factory``. A layer pusher needs them to name the run that will own the layer.
    call: Dict[str, Any] = dataclasses.field(default_factory=dict)
    #: ``(session, run id)`` minted early because a layer was pushed before any run row existed.
    #: ``post_run`` and ``_record_call`` adopt it, so the row the layer names is the row that is stored.
    reserved: Optional[Tuple[str, str]] = None


_CAPTURE: contextvars.ContextVar[Optional[CallCapture]] = contextvars.ContextVar(
    "AIHYDRO_CALL_CAPTURE", default=None
)


def begin_capture(**call: Any) -> Tuple[CallCapture, "contextvars.Token"]:
    capture = CallCapture(call=dict(call))
    return capture, _CAPTURE.set(capture)


def end_capture(token: "contextvars.Token") -> None:
    _CAPTURE.reset(token)


def note_effective_arguments(supplied: Any, effective: Any, repairs: Iterable[Dict[str, Any]]) -> None:
    """Record that argument repair changed the call, so the seal describes what ran.

    The run record digests ``effective`` (the arguments the tool received), not what the caller
    sent; ``extra.arg_repairs`` names every repair and ``extra.supplied_input_digest`` digests
    the original. No-op outside a recorded call.
    """
    capture = _CAPTURE.get()
    if capture is None:
        return
    from ai_hydro.session.refs import scrub_value

    capture.call["effective_arguments"] = effective
    capture.call["arguments"] = effective
    capture.notes.update(scrub_value({
        "arg_repairs": [{k: v for k, v in r.items() if k != "note"} for r in repairs]}))
    try:
        digest, _err = digest_or_error(scrub_arguments(supplied or {}))
        if digest:
            capture.notes["supplied_input_digest"] = digest
    except Exception as exc:   # the repair list still seals
        log.debug("supplied_input_digest skipped: %s", exc)


def note_row_written(session_id: str, run_id: str, writer: str) -> None:
    """Called by the run-log writer after a row was stored. No-op outside a call."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.rows.append((session_id, run_id, writer or "unknown"))


def row_written_this_call(session_id: str, run_id: str, writer: str) -> bool:
    """True when ``writer`` stored ``run_id`` during the call being recorded.

    False outside a recorded call (direct Python use, tests without a capture): nothing is
    then known about which rows belong to "this" execution.
    """
    capture = _CAPTURE.get()
    return capture is not None and (session_id, run_id, writer) in capture.rows


def note_file_written(path: Any, study_id: Optional[str] = None) -> None:
    """A tool wrote ``path`` into the workspace. No-op outside a recorded call.

    The study recorder (``ai_hydro.session.artifact_recorder``) turns every noted file into an
    ``artifact_manifest`` entry once the call is sealed. Called from the low-level write helpers
    (``HydroSession.write_workspace_file``, the map layer pushers), never from individual tools.
    """
    capture = _CAPTURE.get()
    if capture is not None and path:
        capture.files.append((study_id, str(path)))


def note_layer_pushed(layer_id: str, *, name: Optional[str] = None, layer_type: Optional[str] = None,
                      paths: Iterable[Any] = (), content_sha256: Optional[str] = None,
                      envelope: Optional[Dict[str, Any]] = None) -> None:
    """A tool pushed map layer ``layer_id``; ``paths`` are the files it points at (PNG, manifest).

    ``envelope`` is the provenance the layer was stamped with (``map_events.provenance_envelope``);
    the study recorder keeps it on the manifest entry so ``map_get_state`` can answer for a chat or a project.
    """
    capture = _CAPTURE.get()
    if capture is not None and layer_id:
        capture.layers.append({
            "layer_id": str(layer_id), "name": name, "layer_type": layer_type,
            "paths": [str(p) for p in paths if p], "content_sha256": content_sha256,
            "envelope": dict(envelope) if envelope else None,
        })


def run_ref_for_layer() -> Tuple[Optional[str], Optional[str]]:
    """``(study id, run id)`` of the run row that will own whatever the current call pushes.

    Outside a recorded call: ``(None, None)``. Order: the row a writer already stored during this call
    (a slot's ``put_result`` row, which ``post_run`` adopts); the id reserved earlier in this call; a new
    reservation in the call's session. The reservation is the id the sealed row will carry (``reserved_run_id``).
    """
    capture = _CAPTURE.get()
    if capture is None:
        return None, None
    if capture.rows:
        sid, rid, _writer = capture.rows[-1]
        return sid, rid
    if capture.reserved:
        return capture.reserved
    call = capture.call
    factory = call.get("id_factory")
    if not (call.get("tool") and callable(factory)):
        return None, None
    try:
        sid, _rule = resolve_call_session_rule(
            call.get("arguments"), None, call.get("chat_id"), capture, call.get("study_id"))
        if not sid:
            return None, None
        capture.reserved = (sid, factory(call["tool"], sid))
    except Exception as exc:        # a layer without a run reference is honest; a failed push is not
        log.debug("run id reservation skipped: %s", exc)
        return None, None
    return capture.reserved


def reserved_run_id(session_id: str) -> Optional[str]:
    """The run id reserved in this call for ``session_id`` (see ``run_ref_for_layer``), else None."""
    capture = _CAPTURE.get()
    if capture is not None and capture.reserved and capture.reserved[0] == session_id:
        return capture.reserved[1]
    return None


def call_context() -> Dict[str, Any]:
    """Tool name and chat id of the call being recorded (empty outside one)."""
    capture = _CAPTURE.get()
    return dict(capture.call) if capture is not None else {}


def note_row_failed(session_id: str, run_id: str, writer: str, reason: str) -> None:
    """Called by the run-log writer when its row could not be stored. No-op outside a call."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.failed_rows.append((session_id, run_id, writer or "unknown", reason))


def note_row_refused(session_id: str, run_id: str, writer: str, outcome: str) -> None:
    """Called by the run-log writer when a sealed row kept its body against the
    writer's different one (``refused``/``stale``). No-op outside a call."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.refused_rows.append((session_id, run_id, writer or "unknown", outcome))


def note_slot_read(run_id: Optional[str], produced_by_tool: Optional[str] = None) -> None:
    """The running tool read a stored study result that run ``run_id`` produced.

    Called from ``HydroSession.get`` / ``get_result``, so the edge is recorded where the data is read
    and no tool has to remember to declare it. No-op outside a recorded call, for tools that are not
    recorded (status and view tools), and when the tool reads its own earlier output (a cache check).
    """
    capture = _CAPTURE.get()
    if capture is None or not isinstance(run_id, str) or not run_id:
        return
    tool = capture.call.get("tool")
    if tool and not is_recorded_tool(tool):
        return
    if produced_by_tool and produced_by_tool == tool:
        return
    if run_id not in capture.slot_parents:
        capture.slot_parents.append(run_id)


def declare_lineage(
    parents: Iterable[str] = (),
    input_refs: Iterable[Dict[str, Any]] = (),
    **notes: Any,
) -> bool:
    """Tell the recording middleware what the running tool consumed.

    Returns False (and does nothing) when no recording is active, e.g. a tool
    called directly from Python. ``notes`` land in ``record.extra``.
    """
    capture = _CAPTURE.get()
    if capture is None:
        return False
    for parent in parents:
        if parent and parent not in capture.parents:
            capture.parents.append(parent)
    # Privacy: free-form notes and refs are sealed into record.extra / input_refs,
    # so they pass the same path scrubber as run-log row bodies (idempotent).
    from ai_hydro.session.refs import scrub_value

    capture.input_refs.extend(scrub_value(dict(r)) for r in input_refs)
    capture.notes.update(scrub_value(notes))
    return True


def parent_edge_for_run(session_id: str, run_id: Optional[str], role: str = "served_data") -> Optional[Dict[str, Any]]:
    """Build ``{"parent": run_id, "input_ref": {...}}`` for a consumed run.

    Reads the producer's sealed record from the run log. Returns ``None`` when
    ``run_id`` is falsy or not retained in the session. The ``digest`` in the
    input ref is the producer's recorded ``output_digest``, or absent when the
    producer has no record (it was written outside the recording middleware):
    the edge is then an honest reference without a content digest.
    """
    if not run_id:
        return None
    from ai_hydro.session import store

    row = store._run_log_read_one(session_id, run_id)
    if row is None:
        return None
    record = row.get("record") if isinstance(row.get("record"), dict) else {}
    output_digest = record.get("output_digest")
    return {
        "parent": run_id,
        "input_ref": input_ref(run_id, output_digest if is_digest(output_digest) else None, role=role),
    }


# --------------------------------------------------------------------------- #
# Counters (process-wide; coverage and no_session labels)
# --------------------------------------------------------------------------- #

_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {}


def count(label: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[label] = _STATS.get(label, 0) + n


def stats_snapshot() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    with _STATS_LOCK:
        _STATS.clear()


# --------------------------------------------------------------------------- #
# Recording one tool call
# --------------------------------------------------------------------------- #

#: Outputs larger than this (serialized) are not digested; the record says so.
MAX_DIGEST_BYTES = 64 * 1024 * 1024


@dataclasses.dataclass
class RecordOutcome:
    """What ``record_call`` did. ``record_error`` is shown to the caller."""

    label: str                       # "recorded" | "no_session" | "error"
    session_id: Optional[str] = None
    run_ids: List[str] = dataclasses.field(default_factory=list)
    record_error: Optional[str] = None
    #: The run id the caller should cite for this call (the tool's own ``_run_id``
    #: when it declared one, else the call's primary row); None unless stored.
    call_run_id: Optional[str] = None
    #: Set when the call re-served a cached result and a reuse record was written:
    #: ``{reused_from, computed_at, origin_match}``. The middleware discloses it to the agent.
    reuse: Optional[Dict[str, Any]] = None


def _session_exists(session_id: str) -> bool:
    from ai_hydro.session import store

    try:
        json_path = store._SESSIONS_DIR / f"{store._safe_filename_component(session_id)}.json"
        return json_path.exists() or store._run_log_db_path(session_id).exists()
    except Exception:
        return False


def resolve_call_session_rule(
    arguments: Any, result: Any, chat_id: Optional[str], capture: CallCapture,
    meta_study_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """``(session_id, rule)`` for a call; ``(None, None)`` when unresolved.

    Never creates a session. ``rule`` is one of ``explicit_arg``, ``meta``,
    ``chat_binding``, ``result``, ``writer_row`` (a row written during the call
    named a session none of the requested ones matched) or, when the tool recorded how it resolved the
    session itself (``note_session_resolution``), that rule (``auto_create``...).

    Order: the session of any row a writer stored during the call; an explicit
    ``session_id`` argument; the ``study_id`` sent in ``_meta``; a
    ``session_id`` in the result; the chat binding. Candidates from all but
    the first must already exist on disk.
    """
    noted = capture.notes.get("session_resolution")
    candidates: List[Tuple[Any, str]] = []
    if isinstance(arguments, dict):
        candidates.append((arguments.get("session_id"), "explicit_arg"))
    candidates.append((meta_study_id, "meta"))
    # A row a writer lost or was refused still names its session.
    written = capture.rows or [r[:3] for r in capture.failed_rows] or [r[:3] for r in capture.refused_rows]
    if written:
        sid = written[0][0]
        rule = noted
        if rule is None:
            rule = next(
                (r for c, r in candidates
                 if isinstance(c, str) and c.strip() == sid), None)
        return sid, rule or "writer_row"
    if isinstance(result, dict):
        candidates.append((result.get("session_id"), "result"))
    if chat_id:
        try:
            from ai_hydro.session.chat_binding import get_binding_store

            candidates.append((get_binding_store().lookup_study(chat_id), "chat_binding"))
        except Exception:
            pass
    for candidate, rule in candidates:
        if isinstance(candidate, str) and candidate.strip():
            sid = candidate.strip()
            if sid != "map" and _session_exists(sid):
                return sid, (noted or rule)
    return None, None


def resolve_call_session(
    arguments: Any, result: Any, chat_id: Optional[str], capture: CallCapture,
    meta_study_id: Optional[str] = None,
) -> Optional[str]:
    """The session a call belongs to, or None. See ``resolve_call_session_rule``."""
    return resolve_call_session_rule(arguments, result, chat_id, capture, meta_study_id)[0]


def note_session_resolution(rule: str) -> None:
    """Record which ``_resolve_session`` rule a tool call used (first wins)."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.notes.setdefault("session_resolution", rule)


_URL_QUERY = re.compile(r"(?P<url>https?://(?P<host>[^/\s?\"'#]+)[^\s?\"']*)?\?[^\s\"']*", re.IGNORECASE)
#: Hosts whose query strings are citation identifiers, not credentials (DOI resolvers and scholarly indexes).
_CITATION_HOSTS = frozenset({
    "doi.org", "dx.doi.org", "api.crossref.org", "crossref.org", "api.openalex.org", "openalex.org",
    "api.semanticscholar.org", "arxiv.org", "export.arxiv.org", "eutils.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
})
_MAILTO = re.compile(r"(\bmailto=)[^&\s\"']+", re.IGNORECASE)
_SECRET_ASSIGN = re.compile(r"(\b[\w-]*(?:key|token|secret|credential|password)[\w-]*\s*[=:]\s*)[^\s,;\"']+", re.IGNORECASE)
_FILE_URL = re.compile(r"\bfile://[^\s\"']*", re.IGNORECASE)
_HOME_PATH = re.compile(r"(?<![\w/.:~-])~/[^\s\"',;/]+/[^\s\"',;]+")     # ~/dir/.../file: directories dropped (~/file stays)
_AUTH_HEADER = re.compile(r"(\b(?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;\"']+", re.IGNORECASE)
#: A bare ``Bearer <token>`` is redacted when the body is 16+ characters of ``[A-Za-z0-9._~+/=-]`` (digit or not), or 8+
#: with a digit. Accepted pass-throughs (not redacted, by design): plain short words ("Bearer authentication"), a bare
#: ``Basic`` outside an Authorization header ("Basic Authentication"), a short ``sk-`` with punctuation
#: (``sk-SoilGrids-250m-clay``), and ``?sig=``/any query string on a citation host (doi.org, crossref, ...).
_BEARER = re.compile(
    r"\b(bearer)\s+(?:(?=[A-Za-z0-9._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{8,}|[A-Za-z0-9._~+/=-]{16,})", re.IGNORECASE)
_URL_USERINFO = re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@\"']+:[^/\s@\"']+@", re.IGNORECASE)
#: Known key prefixes. An ``sk-`` key has a single unbroken base62 body of 20+ characters (or a ``proj``/``ant``/``live``/
#: ``test`` segment before it), so a product name such as ``sk-SoilGrids-250m-clay`` is not a key.
_TOKEN_PREFIX = re.compile(
    r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-(?:(?:proj|ant|live|test)-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{20,})"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[0-9A-Z]{16})"
)


def _redact_query(m: "re.Match") -> str:
    host = (m.group("host") or "").lower().rsplit("@", 1)[-1].split(":")[0]
    if host in _CITATION_HOSTS:
        return m.group(0)
    return (m.group("url") or "") + "?<redacted>"


def scrub_error_text(text: Any, limit: int = 200, workspace_dir: Any = None) -> str:
    """Error text safe to keep in a row: absolute paths (also ``file://`` URLs and ``~/`` paths, kept only as a
    basename), URL query strings, URL userinfo (``user:pw@``), ``Authorization``/``Bearer`` credentials, known
    token prefixes (``ghp_``, ``gho_``, ``sk-``, ``xox``, ``AKIA``) and secret-shaped assignments redacted, truncated.
    One scrubber for every error text and attempt reason a record keeps (``error_summary`` included)."""
    from ai_hydro.session.refs import scrub_paths

    out = str(text)
    out = _AUTH_HEADER.sub(r"\1<redacted>", out)
    out = _BEARER.sub(r"\1 <redacted>", out)
    out = _URL_USERINFO.sub(r"\1<redacted>@", out)
    out = _TOKEN_PREFIX.sub("<redacted>", out)
    out = _FILE_URL.sub(lambda m: "file://<path>/" + m.group(0).rstrip("/").rsplit("/", 1)[-1], out)
    out = scrub_paths(out, workspace_dir)
    out = _HOME_PATH.sub(lambda m: "~/<path>/" + m.group(0).rstrip("/").rsplit("/", 1)[-1], out)
    out = _URL_QUERY.sub(_redact_query, out)
    out = _MAILTO.sub(r"\1<redacted>", out)
    out = _SECRET_ASSIGN.sub(r"\1<redacted>", out)
    return out[:limit]


#: Bounds on the attempt trail kept in a record (a record is small and sealed; a runaway history must not bloat it).
MAX_TRAIL_ATTEMPTS = 32
MAX_TRAIL_LIMITATIONS = 16
MAX_TRAIL_LABELS = 64
_CACHE_FLAGS = ("cache_hit", "_cache_hit", "_cached")


def _history_in(holder: Any) -> Optional[List[Any]]:
    """A ``fallback_history`` list of a result dict: top level, ``data`` or ``details``."""
    if not isinstance(holder, dict):
        return None
    for h in (holder, holder.get("data"), holder.get("details")):
        history = h.get("fallback_history") if isinstance(h, dict) else None
        if isinstance(history, (list, tuple)) and history:
            return list(history)
    return None


def _history_of(result: Any, failure: Optional[BaseException]) -> Optional[List[Any]]:
    """The ``fallback_history`` a result or a failure carries (error ``details``, or an attribute on the exception)."""
    found = _history_in(result)
    if found:
        return found
    for holder in (getattr(failure, "details", None), {"fallback_history": getattr(failure, "fallback_history", None)}):
        found = _history_in(holder)
        if found:
            return found
    return None


def _is_batch(result: Any) -> bool:
    """A batch result: ``batch: true`` or ``results``/``errors`` keyed by label."""
    return isinstance(result, dict) and (
        result.get("batch") is True
        or isinstance(result.get("results"), dict) or isinstance(result.get("errors"), dict))


def _per_label(result: Any) -> Dict[str, Any]:
    """``{label: result-or-error dict}`` of a batch (``results`` and ``errors`` keyed by label), else ``{}``."""
    out: Dict[str, Any] = {}
    if isinstance(result, dict):
        for key in ("results", "errors"):
            group = result.get(key)
            if isinstance(group, dict):
                for label, item in group.items():
                    if isinstance(item, dict):
                        out.setdefault(str(label), item)
    return out


def _limitations_of(result: Any) -> List[str]:
    """String quality flags a result reports (delineation: ``data.quality_flags``). Validator dicts are not limitations."""
    found: List[str] = []
    if isinstance(result, dict):
        for holder in (result.get("data"), result):
            flags = holder.get("quality_flags") if isinstance(holder, dict) else None
            if isinstance(flags, (list, tuple)):
                found.extend(f for f in flags if isinstance(f, str) and f.strip())
    return list(dict.fromkeys(found))


def _real_name(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() and value.strip().lower() != "unknown" else None


def _data_products(data: Dict[str, Any]) -> List[str]:
    """Distinct product names in ``data``'s ``*_product`` keys (``landcover_product``, ``soil_product``)."""
    return [n for n in dict.fromkeys(
        _real_name(v) for k, v in data.items() if isinstance(k, str) and k.endswith("_product")) if n]


def _declared_sources(result: Any) -> List[Dict[str, str]]:
    """The source(s) a successful result declares itself: one served entry each, or ``[]`` (no declared source).

    In order of preference, the first that names anything wins: top-level ``product``/``source``
    (``get_data_fetch_result``, forcing), ``meta.product``/``meta.source`` (soil, CAMELS), ``data.source``,
    HydroResult ``meta.sources[*].name`` (streamflow) and ``meta.upstream.backend``. A joined ``"A | B"`` value is
    several sources.
    """
    if not isinstance(result, dict):
        return []
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    upstream = meta.get("upstream") if isinstance(meta.get("upstream"), dict) else {}
    listed = [_real_name(i.get("name")) for i in (meta.get("sources") if isinstance(meta.get("sources"), list) else [])
              if isinstance(i, dict)]
    data_upstream = data.get("upstream") if isinstance(data.get("upstream"), dict) else {}
    data_products = _data_products(data)
    pairs: List[Tuple[List[str], Optional[str]]] = [
        ([_real_name(result.get("product")) or ""], _real_name(result.get("source"))),
        ([_real_name(meta.get("product")) or ""], _real_name(meta.get("source"))),
        # data-level products ("landcover_product", "soil_product", ...) are each a declared source; ``data.source``
        # is the provider of the one that is ``data.product``, or its own entry when none matches.
        (data_products, _real_name(data.get("source"))),
        # The backend that actually served the call outranks the static list of sources a tool declares
        # (streamflow with AIHYDRO_USGS_BACKEND=legacy_waterservices still lists the Water Data API).
        ([""], _real_name(data_upstream.get("backend")) or _real_name(upstream.get("backend"))),
        ([n for n in listed if n], None),
    ]
    for products, source in pairs:
        names = [p for p in products if p] or ([source] if source else [])
        names = [n.strip() for name in names for n in name.split("|") if n.strip()]
        if not names:
            continue
        out: List[Dict[str, str]] = []
        for n in dict.fromkeys(names):
            row = {"product": n, "outcome": "served", "reason": ""}
            if source and source != n and any(products):
                if products is not data_products or n == _real_name(data.get("product")):
                    row["source"] = source
            out.append(row)
        return out
    return []


def _stamp_data_nature(attempts: List[Dict[str, str]], result: Any) -> None:
    """Add ``data_nature`` to the served attempt(s) of a result that records one (in place).

    aihydro-data >= 0.2.10 puts ``data_nature`` (one of six classes) on the ``data_fetch`` result,
    beside ``spatial_support``; it describes the product that served. It is copied as recorded onto
    the served attempt whose product is the result's ``product`` (the only served attempt, when
    there is just one). A result without the field (earlier aihydro-data) leaves the trail, and so
    the record digest, exactly as before; nothing is inferred from a product name.
    """
    from ai_hydro.data.nature import nature_of

    nature = nature_of(result)
    if not nature or not isinstance(result, dict):
        return
    served = [a for a in attempts if a.get("outcome") == "served" or str(a.get("outcome", "")).startswith("succeeded")]
    product = _real_name(result.get("product"))
    match = [a for a in served if product and a.get("source") == product] or (served if len(served) == 1 else [])
    for a in match:
        a["data_nature"] = nature


def _acquires(tool: Optional[str]) -> bool:
    """True for a tool annotated ``acquires = true`` (it fetches external data), from the annotation table."""
    if not tool:
        return False
    try:
        from ai_hydro.session.lineage import _annotation_classes

        return tool in _annotation_classes()[0]
    except Exception:
        return False


def _normalize(history: List[Any], workspace_dir: Any):
    """``(attempts, source_used, omitted)`` of a history: ``{source, outcome, reason}`` rows, scrubbed and bounded."""
    attempts: List[Dict[str, str]] = []
    used: List[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        name = item.get("product") or item.get("method") or item.get("source") or "unknown"
        row: Dict[str, str] = {
            "source": scrub_error_text(name, limit=80, workspace_dir=workspace_dir),
            "outcome": scrub_error_text(item.get("outcome") or "unknown", limit=40, workspace_dir=workspace_dir),
            "reason": scrub_error_text(item.get("reason") or "", workspace_dir=workspace_dir),
        }
        provider = item.get("source")
        if item.get("product") and isinstance(provider, str) and provider and provider != item.get("product"):
            row["provider"] = scrub_error_text(provider, limit=80, workspace_dir=workspace_dir)
        if isinstance(item.get("variable"), str) and item["variable"]:
            row["variable"] = scrub_error_text(item["variable"], limit=80, workspace_dir=workspace_dir)
        if row["outcome"] == "served" or row["outcome"].startswith("succeeded"):
            used.append(row["source"])
        attempts.append(row)
    return attempts[:MAX_TRAIL_ATTEMPTS], list(dict.fromkeys(used)), max(0, len(attempts) - MAX_TRAIL_ATTEMPTS)


def attempt_trail(result: Any, failure: Optional[BaseException] = None, workspace_dir: Any = None, *,
                  tool: Optional[str] = None, served_from: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The attempt trail of one call, or ``None`` when the call reports no source information.

    One general rule for every tool, success or failure.

    - ``basis: "fallback_history"``: the result (top level or ``data``) or the failure (``details`` or an
      attribute) carries a ``fallback_history`` (aihydro-data fetches, aihydro-watershed delineation, forcing).
      Each entry is kept as ``{source, outcome, reason}`` (plus ``provider`` and ``variable``), in order, at most
      ``MAX_TRAIL_ATTEMPTS`` (``attempts_omitted`` counts the rest). ``reason`` is scrubbed (``scrub_error_text``).
    - ``basis: "declared_source"``: a successful call of a tool annotated ``acquires = true`` whose result declares
      its source (``product``/``source``, ``data.source``, ``meta.sources``, ``meta.upstream.backend``) and no
      history: one served attempt. That is the declared source, not a recorded chain of attempts.
    - ``data_nature``: on the served attempt, when the result records one (aihydro-data >= 0.2.10; one of six
      classes, copied as recorded, never inferred). Absent for a result without the field.
    - A batch fetch (``results``/``errors`` keyed by label) gets one trail per label under ``labels``.
    - ``source_used``: the one source that served, else ``None``; when more than one served, ``sources_used`` lists them
      in order and ``source_used`` is ``None``. ``limitations``: the string ``quality_flags``.
      ``error_code``: the failure's code.
    - ``served_from``: ``"cache"`` (the result says ``cache_hit``) or ``"reuse"`` (the record is a reuse row).
      Then ``attempts`` is empty, the origin's attempts are ``origin_attempts``, and ``source_used`` is the
      origin's. A cache or reuse hit never presents the origin's attempts as this call's.

    Returns ``None`` when there is nothing to record, so such a call's record (and digest) is unchanged.
    """
    ws = workspace_dir() if callable(workspace_dir) else workspace_dir
    if _is_batch(result):
        # A batch never takes a trail of its own (``product: "geomorphic"`` is not a source). Each label carries
        # its own: its history, or the source its own result declares; an errored label never reads as served.
        labels = _per_label(result)
        per: Dict[str, Any] = {}
        for label, item in list(labels.items())[:MAX_TRAIL_LABELS]:
            sub = attempt_trail(item, None, ws, tool=tool, served_from=served_from)
            if sub:
                per[scrub_error_text(label, limit=80, workspace_dir=ws)] = sub
        if not per:
            return None
        trail: Dict[str, Any] = {"basis": "fallback_history", "labels": per}
        if len(labels) > MAX_TRAIL_LABELS:
            trail["labels_omitted"] = len(labels) - MAX_TRAIL_LABELS
        return trail

    history = _history_of(result, failure)
    basis = "fallback_history"
    ok = failure is None and not (isinstance(result, dict) and result.get("error"))
    if not history:
        if not (ok and _acquires(tool)):
            return None
        history = _declared_sources(result)
        basis = "declared_source"
        if not history:
            return None
    attempts, served, omitted = _normalize(history, ws)
    if not attempts:
        return None
    _stamp_data_nature(attempts, result)
    # One source served: ``source_used``. Several served (a call that used more than one source, such as land cover
    # plus soil, or one product per variable): ``sources_used`` in order and ``source_used`` null, never "the last".
    used = served[0] if len(served) == 1 else None
    many = {"sources_used": served} if len(served) > 1 else {}
    flags = [scrub_error_text(f, limit=80, workspace_dir=ws) for f in _limitations_of(result)[:MAX_TRAIL_LIMITATIONS]]
    cache_hit = isinstance(result, dict) and any(
        result.get(k) is True or (isinstance(result.get("data"), dict) and result["data"].get(k) is True)
        for k in _CACHE_FLAGS)
    origin = served_from or ("cache" if cache_hit and ok else None)
    if origin:
        trail = {"basis": basis, "served_from": origin, "attempts": [], "origin_attempts": attempts,
                 "source_used": used, **many, "limitations": flags}
    else:
        trail = {"basis": basis, "attempts": attempts, "source_used": used, **many, "limitations": flags}
    if omitted:
        trail["attempts_omitted"] = omitted
    code = result.get("code") if isinstance(result, dict) else None
    code = code or getattr(failure, "code", None)
    if isinstance(code, str) and code and not ok:
        trail["error_code"] = scrub_error_text(code, limit=80, workspace_dir=ws)
    return trail


def _origin_trail(session_id: str, origin_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """The reuse row's origin's ``extra.attempt_trail`` as a reuse trail (its attempts become ``origin_attempts``)."""
    if not origin_id:
        return None
    try:
        from ai_hydro.session import store

        row = store._run_log_read_one(session_id, origin_id)
        t = (((row or {}).get("record") or {}).get("extra") or {}).get("attempt_trail")
    except Exception:
        return None
    if not isinstance(t, dict) or "labels" in t:
        return None
    return {"basis": t.get("basis", "fallback_history"), "served_from": "reuse", "attempts": [],
            "origin_attempts": list(t.get("origin_attempts") or t.get("attempts") or []),
            "source_used": t.get("source_used"),
            **({"sources_used": list(t["sources_used"])} if t.get("sources_used") else {}),
            "limitations": list(t.get("limitations") or []), "derived_from": origin_id}


def recorded_call(tool: str, fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Call ``fn(**kwargs)`` (a tool implementation) and record the call.

    For callers outside the MCP server, such as the map CLI, that invoke tool
    functions directly and therefore bypass ``RunRecordMiddleware``. Same record
    contract; the tool's exception, if any, propagates after being recorded.
    Never raises on account of recording.
    """
    capture, token = begin_capture()
    failure: Optional[BaseException] = None
    result: Any = None
    try:
        result = fn(**kwargs)
    except Exception as exc:
        failure = exc
    finally:
        end_capture(token)
    try:
        from ai_hydro.mcp.enforcement import _generate_unique_run_id

        record_call(
            tool=tool, arguments=kwargs,
            result=({"error": True, "message": str(failure)} if failure is not None else result),
            failure=failure, capture=capture, chat_id=None,
            id_factory=_generate_unique_run_id, mcp_client="direct_call",
        )
    except Exception as exc:  # pragma: no cover - record_call does not raise
        log.warning("recorded_call(%s): %s", tool, exc)
    if failure is not None:
        raise failure
    return result


def _minimal_entry(run_id: str, tool: str, session_id: str, result: Any, failure: Optional[BaseException]) -> Dict[str, Any]:
    from datetime import datetime, timezone

    entry: Dict[str, Any] = {
        "run_id": run_id,
        "tool_name": tool,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "key_outputs": {},
        "minimal": True,   # written by the recording middleware, not by the tool
    }
    if failure is not None or (isinstance(result, dict) and result.get("error")):
        entry["error"] = True
        if isinstance(result, dict):
            detail = result.get("code") or result.get("message") or ""
        else:
            detail = ""
        from ai_hydro.mcp.errors import envelope_from_exception
        if failure is not None and not (detail and envelope_from_exception(failure) is not None):
            # A failure carrying its envelope summarises like the dict-error result it replaces.
            detail = f"{type(failure).__name__}: {failure}"
        try:
            from ai_hydro.session.store import HydroSession
            _ws = HydroSession.load(session_id).workspace_dir
        except Exception:
            _ws = None
        entry["error_summary"] = scrub_error_text(detail, workspace_dir=_ws)
    return entry


# --------------------------------------------------------------------------- #
# Reuse records (session study 01031500, S2)
# --------------------------------------------------------------------------- #
#
# A tool that answers from the session cache (``_cached`` / ``_cache_hit``) computes
# nothing and writes no run-log row. Without help the middleware stores a *minimal* row
# with empty ``key_outputs``: a claim that cites it has nothing to resolve, and the answer
# presents June's numbers as if they were this session's computation.
#
# One general mechanism, no per-tool code: a cache-served call gets a first-class *reuse*
# row that copies the original run's ``key_outputs``/``evidence``, names it in
# ``derived_from`` (also a sealed ``parents`` edge), keeps the ORIGINAL ``computed_at`` and
# sets ``reused: true``. The middleware then tells the agent in the response itself.

#: Result keys by which tools say "served from the session cache".
REUSE_FLAGS = ("_cached", "_cache_hit")


def is_reuse_result(result: Any) -> bool:
    """True for a successful dict result that was served from the session cache."""
    return (isinstance(result, dict) and not result.get("error")
            and any(result.get(k) is True for k in REUSE_FLAGS))


def _leaf(name: Any) -> str:
    return str(name or "").rsplit(".", 1)[-1]


#: Fields that say WHICH thing a result describes. Aliases are folded onto the first name.
_IDENTITY_KEYS = {"gauge_id": "gauge_id", "basin_gauge_id": "gauge_id", "feature_id": "feature_id",
                  "index": "index", "index_name": "index", "basin_ref": "basin_ref", "site_id": "gauge_id"}


def _norm_identity(name: str, val: Any) -> str:
    v = str(val).strip()
    if name == "index":
        return v.upper()
    if name == "gauge_id":
        v = v[5:] if v.upper().startswith("USGS-") else v
        return v.zfill(8) if v.isdigit() else v
    return v


def _identity(*sources: Any) -> Dict[str, Any]:
    """Identity fields found in the given dicts (first source wins per field).

    ``None`` and empty values are not identity: a field with no value is absent.
    """
    out: Dict[str, Any] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key, val in src.items():
            name = _IDENTITY_KEYS.get(key)
            if name is None or name in out:
                continue
            if isinstance(val, dict):
                val = val.get("id")
            if isinstance(val, (str, int)) and not isinstance(val, bool) and str(val).strip() != "":
                out[name] = _norm_identity(name, val)
    return out


def _scalar_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    return a == b


def _is_evidence(v: Any) -> bool:
    """Whether an equal value says anything: not a bool, 0, None or an empty string."""
    if v is None or isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return float(v) != 0.0
    return isinstance(v, str) and v.strip() != ""


def origin_agrees(result: Dict[str, Any], row: Dict[str, Any]) -> bool:
    """Does ``row`` describe what the cached ``result`` serves?

    Identity first: if either side carries identity fields (basin_ref, gauge_id, feature_id,
    index, ...), both must carry exactly the same fields with equal values. A field present on
    one side only is "unresolved", never a reason to fall back to value matching. When neither
    side has identity fields, the two must share at least TWO scalars that are evidence (not
    bool, 0, None or empty) and every shared scalar must be equal.
    """
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    mine = _identity(result, data, meta.get("params"), meta)
    theirs = _identity(row.get("key_outputs"), row.get("inputs"), ev.get("data"))
    if mine or theirs:
        return set(mine) == set(theirs) and all(mine[k] == theirs[k] for k in mine)
    ko = row.get("key_outputs") if isinstance(row.get("key_outputs"), dict) else {}
    common = [k for k, v in data.items() if k in ko and isinstance(v, (str, int, float, bool))
              and isinstance(ko[k], (str, int, float, bool))]
    if not all(_scalar_equal(data[k], ko[k]) for k in common):
        return False
    return sum(1 for k in common if _is_evidence(data[k])) >= 2


def find_origin_row(session_id: str, tool: str, result: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """``(run_id, row, how)``: the run-log row that computed what a cached result serves.

    Preference: an id the result itself names (``source_run_id``, ``meta.run_id``,
    ``_run_id``), ``how="run_id"``; else the row of the same tool whose ``timestamp`` equals
    the slot's ``meta.computed_at`` (``"computed_at"``); else the latest earlier row of that
    tool (``"latest_by_tool"``, an inference). The last two are accepted only when
    :func:`origin_agrees`. Minimal rows and earlier reuse rows are never origins. ``how`` is
    ``"unresolved"`` when nothing qualifies.
    """
    from ai_hydro.session import store

    def usable(row: Any) -> bool:
        return (isinstance(row, dict) and not row.get("reused") and not row.get("minimal")
                and bool(row.get("key_outputs") or row.get("evidence")))

    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    for rid in (result.get("source_run_id"), meta.get("run_id"), result.get("_run_id")):
        if isinstance(rid, str) and rid:
            row = store._run_log_read_one(session_id, rid)
            if usable(row):
                return rid, row, "run_id"
    leaves = {_leaf(tool), _leaf(meta.get("tool"))} - {""}
    matches = [(rid, row) for rid, row in store._run_log_read_all(session_id).items()
               if usable(row) and _leaf(row.get("tool_name")) in leaves and origin_agrees(result, row)]
    computed_at = meta.get("computed_at")
    if computed_at:
        exact = [m for m in matches if m[1].get("timestamp") == computed_at]
        if exact:
            return exact[-1][0], exact[-1][1], "computed_at"
    if matches:
        return matches[-1][0], matches[-1][1], "latest_by_tool"
    return None, None, "unresolved"


def _reuse_context(session_id: str, tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    from copy import deepcopy

    origin_id, origin_row, how = find_origin_row(session_id, tool, result)
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    computed_at = meta.get("computed_at") or (origin_row or {}).get("timestamp")
    if origin_row is not None:
        key_outputs = deepcopy(origin_row.get("key_outputs") or {})
        evidence = deepcopy(origin_row.get("evidence")) if origin_row.get("evidence") is not None else None
        inputs = deepcopy(origin_row.get("inputs")) if origin_row.get("inputs") else None
    else:
        from ai_hydro.mcp.helpers import _lean_key_outputs
        from ai_hydro.session.evidence import capture_result_evidence
        key_outputs = _lean_key_outputs(result)
        evidence = capture_result_evidence(result)
        inputs = None
    return {"origin_id": origin_id, "computed_at": computed_at, "how": how,
            "key_outputs": key_outputs, "evidence": evidence, "inputs": inputs}


def _reuse_entry(run_id: str, tool: str, session_id: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    from datetime import datetime, timezone

    entry: Dict[str, Any] = {
        "run_id": run_id,
        "tool_name": tool,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "key_outputs": ctx["key_outputs"],
        "reused": True,
        "derived_from": ctx["origin_id"],
        "computed_at": ctx["computed_at"],
        "origin_match": ctx["how"],
    }
    if ctx.get("evidence") is not None:
        entry["evidence"] = ctx["evidence"]
    if ctx.get("inputs"):
        entry["inputs"] = ctx["inputs"]
    return entry


def reuse_disclosure(run_id: str, reuse: Dict[str, Any]) -> str:
    """The sentence the agent must act on, prepended to the tool's own ``_note``."""
    origin, when, how = reuse.get("reused_from"), reuse.get("computed_at"), reuse.get("origin_match")
    head = "REUSED, NOT RECOMPUTED: this result was served from the session cache and nothing was re-run. "
    if not origin:
        return (head + f"The original run could not be identified in the run log (time {when or 'unrecorded'}); "
                f"run {run_id} records only the values served in this response, not a verified original. "
                "Disclose the reuse and that its origin is unverified. To recompute, call clear_session "
                "for this slot first.")
    src = f"computed at {when or 'an unrecorded time'} (run {origin})"
    if how in ("run_id", "computed_at"):
        return (head + f"It was {src}. Disclose the reuse and its date in your answer. Cite run {run_id}, "
                "whose key_outputs are copied from the original. To recompute, call clear_session for this "
                "slot first.")
    return (head + f"The original run was INFERRED, not recorded: the latest {how} candidate whose identity "
            f"agrees with this response is {origin} ({src}). Disclose the reuse and that its origin is inferred. "
            f"Run {run_id} copies that candidate's key_outputs. To recompute, call clear_session for this "
            "slot first.")


def record_call(
    *,
    tool: str,
    arguments: Any,
    result: Any,
    failure: Optional[BaseException],
    capture: CallCapture,
    chat_id: Optional[str],
    id_factory: Callable[[str, str], str],
    duration_ms: Optional[float] = None,
    mcp_client: Optional[str] = None,
    output_bytes: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
) -> RecordOutcome:
    """Attach a sealed v2 record to every row the call wrote, or create one.

    Never raises. ``result`` is the tool's result as a dict (or ``None``);
    ``failure`` is the exception the tool raised, if any.
    """
    # Argument repair runs inside the recorded call: seal the arguments that actually ran.
    arguments = capture.call.get("effective_arguments", arguments)
    try:
        return _record_call(
            tool=tool, arguments=arguments, result=result, failure=failure,
            capture=capture, chat_id=chat_id, id_factory=id_factory,
            duration_ms=duration_ms, mcp_client=mcp_client, output_bytes=output_bytes,
            context=context,
        )
    except Exception as exc:
        log.warning("record_call failed for tool %s: %s", tool, exc)
        count("record_failed")
        return RecordOutcome("error", record_error=f"record_call: {type(exc).__name__}: {exc}")


def _record_artifacts(capture: CallCapture, tool: str, session_id: Optional[str],
                      run_id: Optional[str]) -> None:
    """The one place every file a tool wrote and every layer it pushed enters the study manifest."""
    if not capture.files and not capture.layers:
        return
    try:
        from ai_hydro.session.artifact_recorder import record_artifacts

        n = record_artifacts(capture, tool=tool, call_session_id=session_id, run_id=run_id)
        count("artifacts_recorded", n)
    except Exception as exc:        # provenance bookkeeping never breaks a tool call
        log.warning("artifact recording failed for %s: %s", tool, exc)
        count("artifact_record_failed")


def _record_call(*, tool, arguments, result, failure, capture, chat_id, id_factory,
                 duration_ms, mcp_client, output_bytes, context=None) -> RecordOutcome:
    from ai_hydro.session import store

    context = context or {}
    deadline = store._new_deadline()   # one budget for every read and write of this call
    session_id, resolution = resolve_call_session_rule(
        arguments, result, chat_id, capture, context.get("study_id"))
    if session_id is None:
        count("no_session")
        _record_artifacts(capture, tool, None, None)
        return RecordOutcome("no_session")

    rows: List[Tuple[str, str, str]] = []
    seen = set()
    for sid, rid, writer in capture.rows:
        if (sid, rid) not in seen:
            seen.add((sid, rid))
            rows.append((sid, rid, writer))
    # A cache-served result computed nothing: it gets a reuse row, not the original's id.
    reuse_ctx: Optional[Dict[str, Any]] = None
    if (failure is None and not capture.rows and not capture.failed_rows
            and not capture.refused_rows and is_reuse_result(result)):
        try:
            reuse_ctx = _reuse_context(session_id, tool, result)
        except Exception as exc:
            log.warning("reuse context failed for %s: %s", tool, exc)
    declared = None if reuse_ctx else (result.get("_run_id") if isinstance(result, dict) else None)
    if isinstance(declared, str) and declared and (session_id, declared) not in seen:
        rows.append((session_id, declared, "post_run"))
    lost: Dict[Tuple[str, str], str] = {}
    for sid, rid, writer, reason in capture.failed_rows:
        lost[(sid, rid)] = scrub_error_text(reason)
        if (sid, rid) not in seen:
            seen.add((sid, rid))
            rows.append((sid, rid, writer))
    refused: Dict[Tuple[str, str], str] = {}
    for sid, rid, writer, outcome in capture.refused_rows:
        refused[(sid, rid)] = outcome
        if (sid, rid) not in seen:
            seen.add((sid, rid))
            rows.append((sid, rid, writer))
    created = False
    # A layer pushed before any row existed named this id; the row it names must exist.
    # The reservation may name another study than the one the call ended up in (a layer pushed under
    # session A, a result stored under B): the row is stored under its own study, so the id never dangles.
    if capture.reserved and capture.reserved not in seen and _session_exists(capture.reserved[0]):
        r_sid, r_rid = capture.reserved
        seen.add((r_sid, r_rid))
        rows.insert(0, (r_sid, r_rid, "middleware"))
        created = len(rows) == 1 and r_sid == session_id
    if not rows:
        rows.append((session_id, id_factory(tool, session_id), "middleware"))
        created = True

    call_id = declared if isinstance(declared, str) and declared else rows[-1][1]
    status = "error" if (failure is not None or (isinstance(result, dict) and result.get("error"))) else "ok"
    skip_reason = None
    if result is None and failure is None:
        skip_reason = "tool returned no content"
    if output_bytes is not None and output_bytes > MAX_DIGEST_BYTES:
        skip_reason = f"{output_bytes} bytes exceeds {MAX_DIGEST_BYTES}"

    trail: Optional[Dict[str, Any]] = None
    try:
        def _ws() -> Any:
            try:
                return store.HydroSession.load(session_id).workspace_dir
            except Exception:
                return None

        is_reuse = bool(reuse_ctx and created)
        trail = attempt_trail(result, failure, _ws, tool=tool, served_from="reuse" if is_reuse else None)
        if trail is None and is_reuse:
            trail = _origin_trail(session_id, reuse_ctx["origin_id"])
    except Exception as exc:  # the trail is additive: never cost the record
        log.warning("attempt trail skipped for %s: %s", tool, exc)

    problems: List[str] = []
    run_ids: List[str] = []
    for sid, rid, writer in rows:
        extra: Dict[str, Any] = {"writer": writer}
        if rid != call_id:
            extra["call_run_id"] = call_id
        if duration_ms is not None:
            extra["duration_ms"] = round(float(duration_ms), 1)
        if mcp_client:
            extra["mcp_client"] = mcp_client
        extra["context_source"] = context.get("source") or "none"
        if context.get("client"):
            extra["context_client"] = context["client"]
        if context.get("study_id"):
            extra["context_study_id"] = context["study_id"]
        if context.get("agent"):
            # Orchestration Stage 1: who the host said made this call. Client-asserted attribution, sealed
            # with the record; present only for a call that named an agent.
            extra["agent"] = {**context["agent"], "asserted_by": "client"}
        requested = [
            v.strip() for v in (
                (arguments.get("session_id") if isinstance(arguments, dict) else None),
                context.get("study_id"),
            ) if isinstance(v, str) and v.strip()
        ]
        if any(v != sid for v in requested):
            extra["context_mismatch"] = True
        extra.update(capture.notes)
        if trail is not None:
            extra["attempt_trail"] = trail
        if isinstance(result, dict) and result.get("product_substituted") is True:
            # A pinned product failed and another product served: sealed with the record, not only in the trail.
            extra["product_substituted"] = True
            extra["requested_product"] = result.get("requested_product", "")
            extra["served_product"] = result.get("product", "")
        if isinstance(result, dict) and result.get("watershed_reliability_warning"):
            extra["watershed_reliability"] = "unreliable"      # computed on a watershed the delineation marked unreliable
        if reuse_ctx and created:
            extra.update(reused=True, derived_from=reuse_ctx["origin_id"],
                         computed_at=reuse_ctx["computed_at"], origin_match=reuse_ctx["how"])
        if resolution:
            extra["session_resolution"] = resolution
        stored = False
        lost_reason = lost.get((sid, rid))
        known = [f"writer_row_lost: {lost_reason}"] if lost_reason else []
        if lost_reason:
            problems.append(f"{rid}: writer_row_lost: {lost_reason}")
        refused_outcome = refused.get((sid, rid))
        if refused_outcome:
            problems.append(f"{rid}: writer_row_refused: the tool wrote a different body after "
                            f"the row was sealed ({refused_outcome}); the sealed body was kept")
        row_absent = False
        for _attempt in range(3):
            try:
                entry = store._run_log_read_one(sid, rid, deadline=deadline, strict=True)
            except Exception as exc:
                # Could not look: never write a minimal row over a row we cannot see.
                reason = f"row unreadable: {scrub_error_text(exc)}"
                problems.append(f"{rid}: record_not_stored ({reason})")
                _register_pending(sid, rid, body=None, record=None, row_absent=False,
                                  reason=f"body not observed when the call finished ({reason})")
                break
            if entry is None:
                if reuse_ctx and created:
                    entry = _reuse_entry(rid, tool, sid, reuse_ctx)
                    extra["entry"] = "reuse"
                else:
                    entry = _minimal_entry(rid, tool, sid, result, failure)
                    extra["entry"] = "minimal"
                row_absent = True
            if _run_log_has_record(entry):
                stored = True          # already sealed; never touch it
                break
            # Seal the privacy-scrubbed body (what the store will keep), so
            # extra.entry_digest matches the stored row.
            entry = store._scrub_row_body(sid, entry)
            parents = list(capture.parents)
            if not capture.notes.get("parent_unresolved"):
                parents += [p for p in dict.fromkeys(capture.slot_parents) if p not in parents and p != rid]
            if "lineage_basis" not in extra and _slot_reads_cover(tool):
                extra["lineage_basis"] = LINEAGE_BASIS
            record = build_run_record(
                run_id=rid, tool=tool, session_id=sid, arguments=arguments,
                result=result, status=status,
                parents=([*parents, reuse_ctx["origin_id"]]
                         if reuse_ctx and created and reuse_ctx["origin_id"] else parents),
                input_refs=capture.input_refs, extra=extra, entry=entry,
                output_skip_reason=skip_reason, known_errors=known,
            )
            # known errors lead the record's error list and are already in problems
            own_error = (record.record_error or "")[len("; ".join(known)):].lstrip("; ") if known \
                else (record.record_error or "")
            if own_error:
                problems.append(f"{rid}: {own_error}")
            outcome = store._run_log_record(sid, rid, {**entry, "record": record.to_dict()},
                                            writer="middleware", deadline=deadline)
            if outcome in ("inserted", "replaced", "noop"):
                stored = True
                break
            if outcome != "stale":
                problems.append(f"{rid}: record_not_stored ({outcome})")
                if outcome == "error":      # transient (lock/IO): keep the finished seal to retry
                    _register_pending(sid, rid, body={k: v for k, v in entry.items() if k != "record"},
                                      record=record.to_dict(), row_absent=row_absent,
                                      reason=f"record_not_stored ({outcome})")
                break
        else:
            problems.append(f"{rid}: record_not_stored (row kept changing)")
            _register_pending(sid, rid, body=None, record=None, row_absent=False,
                              reason="row kept changing during the call; no body could be bound")
        if stored:
            run_ids.append(rid)

    # Lazy re-seal of earlier rows of this session, bounded and inside this
    # call's single deadline (skipped when the budget is already spent).
    reseal_unsealed_rows(session_id, deadline=deadline, limit=RESEAL_PER_CALL,
                         exclude=[rid for _sid, rid, _w in rows])

    _record_artifacts(capture, tool, session_id, call_id if call_id in run_ids else None)

    count("recorded", len(run_ids))
    if created:
        count("created_minimal")
    if problems:
        count("record_error", len(problems))
    return RecordOutcome(
        "recorded" if run_ids else "error",
        session_id=session_id,
        run_ids=run_ids,
        record_error="; ".join(problems) if problems else None,
        call_run_id=call_id if call_id in run_ids else None,
        reuse=({"reused_from": reuse_ctx["origin_id"], "computed_at": reuse_ctx["computed_at"],
                "origin_match": reuse_ctx["how"]}
               if reuse_ctx and created and call_id in run_ids else None),
    )


# --------------------------------------------------------------------------- #
# Lazy re-seal of rows the middleware could not seal (skeptic-slice3 R2)
# --------------------------------------------------------------------------- #
#
# When a call's row cannot be read or sealed within the lock budget, the
# material the seal needs exists only in that call: the arguments and result.
# It is kept here (process-local, bounded) as a finished, already-sealed record
# bound to the exact row body observed at the time. A later retry stores that
# same record only if the stored row body is still byte-for-byte that body.
# Nothing is recomputed, so no digest of an unobserved input or output can be
# invented. If the body was never observed (the read failed) or has changed, a
# faithful seal is impossible and the row is marked ``record_status:
# unsealable`` with the reason, so a capsule says so instead of showing a
# silent legacy row. The marker is only written to an unsealed row; a sealed
# body is never rewritten.
#
# Limit: the pending set does not survive a process restart. A row that was
# left unsealed by a process that is gone stays an ordinary unsealed row.

RESEAL_PER_CALL = 4          # rows retried per _record_call, after its own work
PENDING_MAX_PER_SESSION = 64

_PENDING_LOCK = threading.Lock()
_PENDING: Dict[str, Dict[str, "_PendingSeal"]] = {}


@dataclasses.dataclass
class _PendingSeal:
    run_id: str
    body: Optional[Dict[str, Any]]      # scrubbed row body observed (no ``record``), or None
    record: Optional[Dict[str, Any]]    # the sealed record built against ``body``, or None
    row_absent: bool                    # the middleware's own minimal row; the row did not exist
    reason: str                         # why it was not stored / why it cannot be sealed


def _register_pending(session_id: str, run_id: str, *, body: Optional[Dict[str, Any]],
                      record: Optional[Dict[str, Any]], row_absent: bool, reason: str) -> None:
    with _PENDING_LOCK:
        bucket = _PENDING.setdefault(session_id, {})
        if run_id not in bucket and len(bucket) >= PENDING_MAX_PER_SESSION:
            bucket.pop(next(iter(bucket)))         # drop the oldest; it stays an unsealed row
            count("reseal_pending_dropped")
        bucket[run_id] = _PendingSeal(run_id, body, record, row_absent, reason)


def pending_seal_ids(session_id: str) -> List[str]:
    with _PENDING_LOCK:
        return list(_PENDING.get(session_id, {}))


def reset_pending() -> None:
    with _PENDING_LOCK:
        _PENDING.clear()


def reseal_unsealed_rows(
    session_id: str, *, deadline: Optional[float] = None, limit: Optional[int] = None,
    exclude: Iterable[str] = (),
) -> Dict[str, Any]:
    """Retry sealing rows this process failed to seal; never raises.

    ``deadline`` is an absolute monotonic deadline (one per operation, as in
    ``_record_call``); default is a fresh lock-wait budget. ``limit`` caps the
    rows tried. Returns ``{"resealed": [...], "unsealable": [...],
    "pending": [...]}`` (run ids).
    """
    out: Dict[str, Any] = {"resealed": [], "unsealable": [], "pending": []}
    try:
        from ai_hydro.session import store

        if deadline is None:
            deadline = store._new_deadline()
        skip = set(exclude)
        with _PENDING_LOCK:
            todo = [p for rid, p in _PENDING.get(session_id, {}).items() if rid not in skip]
        tried = 0
        for p in todo:
            if limit is not None and tried >= limit:
                break
            if time.monotonic() >= deadline:
                break
            tried += 1
            done = _reseal_one(store, session_id, p, deadline)
            if done == "resealed":
                out["resealed"].append(p.run_id)
            elif done == "unsealable":
                out["unsealable"].append(p.run_id)
            if done in ("resealed", "unsealable", "gone"):
                with _PENDING_LOCK:
                    _PENDING.get(session_id, {}).pop(p.run_id, None)
        with _PENDING_LOCK:
            out["pending"] = list(_PENDING.get(session_id, {}))
        if out["resealed"]:
            count("resealed", len(out["resealed"]))
        if out["unsealable"]:
            count("unsealable", len(out["unsealable"]))
    except Exception as exc:
        log.warning("reseal_unsealed_rows(%s): %s", session_id, exc)
    return out


def _reseal_one(store, session_id: str, p: "_PendingSeal", deadline: float) -> str:
    """``resealed`` | ``unsealable`` | ``gone`` (nothing left to do) | ``retry``."""
    try:
        row = store._run_log_read_one(session_id, p.run_id, deadline=deadline, strict=True)
    except Exception:
        return "retry"                      # transient; keep it pending
    if row is not None and _run_log_has_record(row):
        return "gone"                       # somebody sealed it; never touch it
    if p.record is None or p.body is None:
        # The body was never observed (or kept changing): no seal can be bound to it.
        if row is None:
            return "gone"
        outcome = store._run_log_mark_unsealable(
            session_id, p.run_id, store._run_log_body_json(row), p.reason, deadline=deadline)
        return "unsealable" if outcome in ("marked", "noop") else "retry"
    expected = store._run_log_body_json(p.body)
    if row is None:
        if not p.row_absent:
            return "gone"                   # the row vanished; nothing to seal
    else:
        same = (store._run_log_body_json(row) == expected
                or store._run_log_body_json(store._scrub_row_body(session_id, row)) == expected)
        if not same or p.row_absent:
            reason = ("row appeared after the call; its body was never observed" if p.row_absent
                      else "row body changed after the call that could not seal it")
            outcome = store._run_log_mark_unsealable(
                session_id, p.run_id, store._run_log_body_json(row), reason, deadline=deadline)
            return "unsealable" if outcome in ("marked", "noop") else "retry"
    outcome = store._run_log_record(session_id, p.run_id, {**p.body, "record": p.record},
                                    writer="middleware", deadline=deadline)
    if outcome in ("inserted", "replaced", "noop"):
        return "resealed"
    if outcome in ("stale", "refused"):
        outcome = store._run_log_mark_unsealable(
            session_id, p.run_id, store._run_log_body_json(row),
            "row body changed after the call that could not seal it", deadline=deadline)
        return "unsealable" if outcome in ("marked", "noop") else "retry"
    return "retry"


def _run_log_has_record(entry: Any) -> bool:
    record = entry.get("record") if isinstance(entry, dict) else None
    return isinstance(record, dict) and bool(record.get("record_digest"))


# --------------------------------------------------------------------------- #
# Which tools are recorded
# --------------------------------------------------------------------------- #

#: Tools the recording middleware deliberately skips, with the reason. Every
#: other registered tool is recorded when its call resolves a session. The
#: coverage test (tests/test_run_record_coverage.py) fails when a registered
#: tool is neither recorded nor listed here, and when an entry here names a
#: tool that no longer exists.
#:
#: The criterion is whether a call produces or delivers a result a claim could
#: rest on. The extension lists every run-log row as an evidence candidate, so
#: rows for catalog lookups, polling, session views, UI state and lifecycle
#: calls would bury the real runs. Still recorded: every Tier-1 tool, data
#: fetches, claim/ledger/note writes, searches that return results the agent
#: quotes (literature, experiments, leaderboard), job result retrieval, and
#: geometry/ROI setters.
_CATALOG = "static catalog or documentation lookup; no session state read or written"
_VIEW = "read-only view or poll of session, job or registry state; produces no result a claim could cite"
_UI = "map or course UI state; no scientific result"
_LIFECYCLE = "session or profile lifecycle management; a row could resurrect a cleared session"

RECORD_EXEMPT: Dict[str, str] = {
    **{name: _CATALOG for name in (
        "aihydro_describe_capability", "describe_tool", "describe_tools",
        "list_available_tools", "list_known_datasets", "list_known_metrics",
        "list_known_variables", "get_dataset_info", "get_equation_definition",
        "get_metric_definition", "get_variable_definition", "get_library_reference",
        "list_relevant_clis", "list_available_workflows", "get_workflow_manifest",
        "list_spectral_indices", "list_skills", "load_skill", "data_list_products",
        "data_describe_product", "data_help", "data_validate_request",
        "preview_list_modules", "list_cached_citations",
    )},
    **{name: _VIEW for name in (
        "get_session_summary", "get_session_health", "get_session_raw_state",
        "list_claims", "list_assumptions", "list_registry_claims", "list_features",
        "aihydro_chat_status", "get_project_summary", "get_researcher_profile",
        "get_training_status", "list_jobs", "wait_for_job",
        "data_get_cache_status", "data_doctor", "delineation_doctor", "gee.status",
        "map_get_state", "map_list_layers", "preview_get_state",
        "preview_recent_events", "preview_get_pending_changes", "course_get_state",
        "course_get_curriculum",
    )},
    **{name: _UI for name in (
        "map_fit_extent", "map_fit_layer", "map_fly_to", "map_remove_layer",
        "map_set_basemap", "map_show", "show_on_map", "show_html_preview",
        "map_set_time_range", "map_apply_symbology", "map_update_layer",
        "map_add_layer_from_run", "preview_focus_cell", "course_navigate",
        "course_scaffold", "course_set_progress",
    )},
    **{name: _LIFECYCLE for name in (
        "clear_session", "archive_session", "merge_session_shards",
        "aihydro_rebind_chat", "start_project", "add_session_to_project",
        "update_researcher_profile", "save_skill", "data_invalidate_cache",
        "set_active_feature",
    )},
}


#: ``record.extra["lineage_basis"]``: every read of a stored study result by the tool went through
#: ``note_slot_read``. It does not cover files a tool reads directly, so it is withheld for tools
#: that run arbitrary code. Read by ``ai_hydro.session.lineage`` (same value).
LINEAGE_BASIS = "slot_reads"


def _slot_reads_cover(tool: str) -> bool:
    try:
        from ai_hydro.session.lineage import _annotation_classes

        return tool not in _annotation_classes()[1]
    except Exception:
        return False


def is_recorded_tool(name: str) -> bool:
    """True when calls to ``name`` are recorded (i.e. not exempt)."""
    return name not in RECORD_EXEMPT
