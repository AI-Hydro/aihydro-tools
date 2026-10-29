"""Map items: what the map shows, with the provenance each item was stamped with.

An *item* is a map layer. Its identity is the ``layer_id``. Two sources feed one list:

* the study recorder (``ai_hydro.session.artifact_recorder``): every layer a tool pushed is a
  ``layer:<id>`` entry in its study's artifact manifest, with ``chat_id``, ``run_id``, ``tool``,
  ``source`` and ``retrieved_at``;
* the host layer catalog (``~/.aihydro/map_layer_catalog.json``): what the panel holds right now.

Claim links are ledger records, kept in the study next to the claims they name:
``session.extra["map_item_claim_links"] = {item_id: [claim_id, ...]}``. A link is the only thing
stored. A claim's status is read from the claim when somebody asks; it is never copied onto the
item, the manifest entry or the link, so it cannot go stale on the map.
"""
from __future__ import annotations

from typing import Any

from ai_hydro.mcp.map_layer_catalog import MAP_LAYER_CATALOG_FILE, read_layer_catalog
from ai_hydro.session.project_key import project_key
from ai_hydro.session.study_index import MANIFEST_KIND, study_summaries

LINKS_KEY = "map_item_claim_links"
SCOPES = ("chat", "project")


def claim_ids_for(session_extra: dict[str, Any] | None, item_id: str) -> list[str]:
    links = (session_extra or {}).get(LINKS_KEY)
    ids = links.get(item_id) if isinstance(links, dict) else None
    return [str(c) for c in ids] if isinstance(ids, list) else []


def add_link(session_extra: dict[str, Any], item_id: str, claim_id: str) -> bool:
    """Record ``claim_id -> item_id``. True when the link is new. Nothing but the two ids is stored."""
    links = session_extra.setdefault(LINKS_KEY, {})
    ids = links.setdefault(item_id, [])
    if claim_id in ids:
        return False
    ids.append(claim_id)
    return True


def _layer_entries(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in (summary.get("manifest") or {}).values()
            if isinstance(e, dict) and e.get("kind") == MANIFEST_KIND and e.get("type") == "map_layer"]


def _item(entry: dict[str, Any], in_host: bool | None) -> dict[str, Any]:
    return {
        "item_id": entry.get("layer_id"),
        "name": entry.get("name"),
        "layer_type": entry.get("layer_type"),
        "study_id": entry.get("study_id"),
        "run_id": entry.get("run_id"),
        "tool": entry.get("tool"),
        "chat_id": entry.get("chat_id"),
        "source": entry.get("source"),
        "retrieved_at": entry.get("retrieved_at"),
        "sha256": entry.get("sha256"),
        "in_host_catalog": in_host,
        "claim_ids": [],
    }


def collect(scope: str, *, chat_id: str | None, workspace: str | None) -> dict[str, Any]:
    """Items for ``scope`` ("chat" or "project") plus how the scope was resolved.

    ``chat``: layers stamped with this chat id. ``project``: layers of every study whose workspace
    has this project key (``project_key``). Layers recorded before the envelope existed carry no
    ``chat_id``; they are listed by id under ``unattributed_item_ids`` for the chat scope, never
    guessed into a chat.
    """
    info: dict[str, Any] = {"scope": scope, "resolved": False, "items": [], "unattributed_item_ids": []}
    if scope == "chat":
        if not chat_id:
            info["note"] = "no chat context: pass chat_id or call through a client that sends it"
            return info
        info["chat_id"] = chat_id
    else:
        key = project_key(workspace)
        if not key:
            info["note"] = "no workspace context: pass workspace or call through a client that sends it"
            return info
        info["project_key"] = key

    catalog = read_layer_catalog()
    host_ids = {str(l.get("id")) for l in catalog.get("layers") or [] if isinstance(l, dict) and l.get("id")}
    host_known = MAP_LAYER_CATALOG_FILE.exists()

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for summary in study_summaries(include_hidden=True):
        if scope == "project" and project_key(summary.get("workspace_dir")) != info["project_key"]:
            continue
        for entry in _layer_entries(summary):
            lid = entry.get("layer_id")
            if not lid:
                continue
            if scope == "chat" and entry.get("chat_id") != chat_id:
                if not entry.get("chat_id") and chat_id in _bound_chats(summary["study_id"]):
                    info["unattributed_item_ids"].append(lid)
                continue
            if lid in seen:
                continue
            seen.add(lid)
            items.append(_item(entry, (lid in host_ids) if host_known else None))

    _attach_claim_links(items)
    info.update(resolved=True, items=items, host_catalog_available=host_known)
    return info


def _bound_chats(study_id: str) -> list[str]:
    try:
        from ai_hydro.session.chat_binding import get_binding_store

        return get_binding_store().lookup_chats(study_id)
    except Exception:
        return []


def _attach_claim_links(items: list[dict[str, Any]]) -> None:
    """Fill ``claim_ids`` from each item's study. Loads only the studies that have items."""
    from ai_hydro.session import HydroSession

    by_study: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_study.setdefault(str(it.get("study_id")), []).append(it)
    for sid, group in by_study.items():
        try:
            extra = HydroSession.load(sid).extra
        except Exception:
            continue
        for it in group:
            it["claim_ids"] = claim_ids_for(extra, str(it["item_id"]))
