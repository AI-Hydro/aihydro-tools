"""Capability flags the server advertises (``aihydro://capabilities``).

A host detects what this server can do by reading these flags, never by comparing version
numbers: an older server simply lacks a flag and the host falls back to its previous behaviour.
Add a flag in the same change as the feature; never reuse or repurpose one.
"""
from __future__ import annotations

CAPABILITIES: tuple[str, ...] = (
    # binding (2.8.2)
    "binding.v2",                       # many chats -> one study; chat_bindings.v2.json
    "binding.chat_id_default",          # aihydro_rebind_chat / aihydro_chat_status default to the injected chat id
    "binding.start_session_binds",      # start_session(session_id) binds the chat
    "binding.workspace_single_study",   # one study in the workspace resolves (rule workspace_single_study)
    "binding.candidates_in_error",      # SESSION_RESOLUTION_FAILED carries labelled candidates
    "binding.session_binding_field",    # results carry session_binding when a call changed the binding
    "binding.recent_fallback_off",      # the global most-recent-study guess is opt-in
    "context.meta_workspace_only",      # active_workspace.json is ignored when the call carries its own context
    # studies (2.8.2)
    "study.labels",
    "study.list",                       # aihydro://study/list
    "study.by_artifact",                # aihydro://study/by-artifact
    "artifact.recorder",                # artifact_manifest records every workspace file / pushed layer
    # map provenance (T-M1)
    "map.provenance_envelope",          # pushed layers carry chat_id, study_id, _run_id, tool, source, retrieved_at
    "map.layer_from_run_artifact",      # map_add_layer_from_run reads the run store + recorded artifact; add_layer_from_run carries provenance
    "map.claim_links",                  # bind_claim_to_item: a ledger link, never the claim status
    "map.state_scope",                  # map_get_state(scope="chat"|"project") lists items with provenance
    "jobs.context_echo",                # background jobs carry the submitting call's context and echo it in status.json
)
