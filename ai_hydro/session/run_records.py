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
- ``input_digest`` covers the arguments as received, not the effective
  parameters after defaults, so a call that omits a default and one that
  passes it explicitly digest differently.
- ``output_digest`` covers what the caller received (``result["data"]``), not
  arrays a tool read from or wrote to disk.

``build_run_record`` never raises. When a digest cannot be computed the record
is still sealed, the digest is ``None`` and ``record_error`` says why.
"""
from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import re
import threading
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
TRANSPORT_KEYS = frozenset({"_run_id", "_record_error", "quality_flags", "next_steps"})

#: Tool dependencies whose installed versions go into the environment digest.
ENV_DISTRIBUTIONS = (
    "aihydro-tools", "aihydro-core", "aihydro-data", "aihydro-watershed",
    "aihydro-lsh", "aihydro-modelling", "numpy", "pandas", "scipy", "xarray",
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
    """``(version, version_source)``.

    ``meta.version`` of the result (the producing package's own version) wins;
    then the running package's own ``ai_hydro.__version__`` ("package"); the
    installed ``aihydro-tools`` distribution metadata is only a fallback, since
    it can lag the source (stale editable-install dist-info).
    """
    meta = result.get("meta") if isinstance(result, dict) else None
    if isinstance(meta, dict):
        version = meta.get("version")
        if isinstance(version, str) and version:
            return version, "result_meta"
    version = _package_version()
    if version:
        return version, "package"
    version = _distribution_version()
    return version, ("distribution" if version else "unavailable")


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
    total = recorded = verified = unbound = with_error = 0
    problems: List[Dict[str, Any]] = []
    for run_id, entry in (run_log or {}).items():
        total += 1
        check = verify_run_log_entry(entry)
        if not check["has_record"]:
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
        "legacy_unrecorded": total - recorded,
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
    input_refs: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    notes: Dict[str, Any] = dataclasses.field(default_factory=dict)
    failed_rows: List[Tuple[str, str, str, str]] = dataclasses.field(default_factory=list)  # (session, run, writer, reason)


_CAPTURE: contextvars.ContextVar[Optional[CallCapture]] = contextvars.ContextVar(
    "AIHYDRO_CALL_CAPTURE", default=None
)


def begin_capture() -> Tuple[CallCapture, "contextvars.Token"]:
    capture = CallCapture()
    return capture, _CAPTURE.set(capture)


def end_capture(token: "contextvars.Token") -> None:
    _CAPTURE.reset(token)


def note_row_written(session_id: str, run_id: str, writer: str) -> None:
    """Called by the run-log writer after a row was stored. No-op outside a call."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.rows.append((session_id, run_id, writer or "unknown"))


def note_row_failed(session_id: str, run_id: str, writer: str, reason: str) -> None:
    """Called by the run-log writer when its row could not be stored. No-op outside a call."""
    capture = _CAPTURE.get()
    if capture is not None:
        capture.failed_rows.append((session_id, run_id, writer or "unknown", reason))


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
    if capture.rows:
        sid = capture.rows[0][0]
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


_URL_QUERY = re.compile(r"\?[^\s\"']*")
_SECRET_ASSIGN = re.compile(r"(\b[\w-]*(?:key|token|secret|credential|password)[\w-]*\s*[=:]\s*)[^\s,;\"']+", re.IGNORECASE)


def scrub_error_text(text: Any, limit: int = 200, workspace_dir: Any = None) -> str:
    """Error text safe to keep in a row: absolute paths replaced by refs,
    URL query strings and secret-shaped assignments redacted, truncated."""
    from ai_hydro.session.refs import scrub_paths

    out = _URL_QUERY.sub("?<redacted>", scrub_paths(str(text), workspace_dir))
    out = _SECRET_ASSIGN.sub(r"\1<redacted>", out)
    return out[:limit]


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
        if failure is not None:
            detail = f"{type(failure).__name__}: {failure}"
        try:
            from ai_hydro.session.store import HydroSession
            _ws = HydroSession.load(session_id).workspace_dir
        except Exception:
            _ws = None
        entry["error_summary"] = scrub_error_text(detail, workspace_dir=_ws)
    return entry


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


def _record_call(*, tool, arguments, result, failure, capture, chat_id, id_factory,
                 duration_ms, mcp_client, output_bytes, context=None) -> RecordOutcome:
    from ai_hydro.session import store

    context = context or {}
    deadline = store._new_deadline()   # one budget for every read and write of this call
    session_id, resolution = resolve_call_session_rule(
        arguments, result, chat_id, capture, context.get("study_id"))
    if session_id is None:
        count("no_session")
        return RecordOutcome("no_session")

    rows: List[Tuple[str, str, str]] = []
    seen = set()
    for sid, rid, writer in capture.rows:
        if (sid, rid) not in seen:
            seen.add((sid, rid))
            rows.append((sid, rid, writer))
    declared = result.get("_run_id") if isinstance(result, dict) else None
    if isinstance(declared, str) and declared and (session_id, declared) not in seen:
        rows.append((session_id, declared, "post_run"))
    lost: Dict[Tuple[str, str], str] = {}
    for sid, rid, writer, reason in capture.failed_rows:
        lost[(sid, rid)] = scrub_error_text(reason)
        if (sid, rid) not in seen:
            seen.add((sid, rid))
            rows.append((sid, rid, writer))
    created = False
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
        requested = [
            v.strip() for v in (
                (arguments.get("session_id") if isinstance(arguments, dict) else None),
                context.get("study_id"),
            ) if isinstance(v, str) and v.strip()
        ]
        if any(v != sid for v in requested):
            extra["context_mismatch"] = True
        extra.update(capture.notes)
        if resolution:
            extra["session_resolution"] = resolution
        stored = False
        lost_reason = lost.get((sid, rid))
        known = [f"writer_row_lost: {lost_reason}"] if lost_reason else []
        if lost_reason:
            problems.append(f"{rid}: writer_row_lost: {lost_reason}")
        for _attempt in range(3):
            try:
                entry = store._run_log_read_one(sid, rid, deadline=deadline, strict=True)
            except Exception as exc:
                # Could not look: never write a minimal row over a row we cannot see.
                problems.append(f"{rid}: record_not_stored (row unreadable: {scrub_error_text(exc)})")
                break
            if entry is None:
                entry = _minimal_entry(rid, tool, sid, result, failure)
                extra["entry"] = "minimal"
            if _run_log_has_record(entry):
                stored = True          # already sealed; never touch it
                break
            # Seal the privacy-scrubbed body (what the store will keep), so
            # extra.entry_digest matches the stored row.
            entry = store._scrub_row_body(sid, entry)
            record = build_run_record(
                run_id=rid, tool=tool, session_id=sid, arguments=arguments,
                result=result, status=status, parents=capture.parents,
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
                break
        else:
            problems.append(f"{rid}: record_not_stored (row kept changing)")
        if stored:
            run_ids.append(rid)

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
    )


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


def is_recorded_tool(name: str) -> bool:
    """True when calls to ``name`` are recorded (i.e. not exempt)."""
    return name not in RECORD_EXEMPT
