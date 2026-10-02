"""Human claim approval (ADR-002a): authority that cannot be conferred by tool arguments.

Channel ``cli_same_user``: this blocks unintended or naive self-approval, not a
process running as the same OS user (it can forge a sealed record or drive the
CLI through a pty). See ``records.py`` and docs/evidence-integrity.md.

This package ``__init__`` exposes only the *read* side. The writer
(``ai_hydro.approval.writer``) and the CLI (``ai_hydro.approval.cli``,
``aihydro-approve``) and the signing side (``ai_hydro.approval.signing``) are deliberately not imported here, so importing
``ai_hydro.approval`` from an MCP tool module never loads a code path that can
create an approval.
"""
from ai_hydro.approval.records import (  # noqa: F401
    APPROVAL_REQUIRED,
    ApprovalRequiredError,
    approval_stamp,
    approve_command,
    approvals_dir,
    claim_revision_digest,
    claim_revision_fields,
    find_approval,
    get_approval,
    verified_channel,
    verify_record,
)
