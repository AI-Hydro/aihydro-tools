"""
Ledger event writer — pushes claim/assumption events to ~/.aihydro/ledger_events/.

The VS Code extension LedgerEventWatcher polls this directory and streams
ClaimUpdate messages to the webview so chat chips update in real-time.
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LEDGER_EVENTS_DIR = Path.home() / ".aihydro" / "ledger_events"


def write_ledger_event(payload: dict[str, Any]) -> bool:
    """Write a one-shot ledger event JSON file. Never raises."""
    try:
        _LEDGER_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        event_file = _LEDGER_EVENTS_DIR / f"{uuid.uuid4().hex}.json"
        event_file.write_text(json.dumps(payload), encoding="utf-8")
        log.debug("Ledger event written: %s %s", payload.get("change_type"), payload.get("claim_id"))
        return True
    except Exception as exc:
        log.warning("write_ledger_event failed (non-fatal): %s", exc)
        return False


def push_claim_event(
    *,
    change_type: str,
    session_id: str,
    claim_id: str,
    statement: str = "",
    status: str = "proposed",
    claim_type: str = "",
    confidence: str = "",
    evidence_spans: list[dict] | None = None,
    limitations: list[str] | None = None,
    created_at: str = "",
) -> bool:
    """Push a claim added/updated/removed event to the ledger events directory."""
    return write_ledger_event({
        "change_type": change_type,
        "session_id": session_id,
        "claim_id": claim_id,
        "statement": statement,
        "status": status,
        "claim_type": claim_type,
        "confidence": confidence,
        "evidence_spans": evidence_spans or [],
        "limitations": limitations or [],
        "created_at": created_at,
    })
