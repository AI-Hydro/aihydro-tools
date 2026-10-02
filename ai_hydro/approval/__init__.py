"""Human claim approval (ADR-002a): authority that the model cannot mint.

This package ``__init__`` exposes only the *read* side. The writer
(``ai_hydro.approval.writer``) and the CLI (``ai_hydro.approval.cli``,
``aihydro-approve``) are deliberately not imported here, so importing
``ai_hydro.approval`` from an MCP tool module never loads a code path that can
create an approval.
"""
from ai_hydro.approval.records import (  # noqa: F401
    APPROVAL_REQUIRED,
    ApprovalRequiredError,
    approve_command,
    approvals_dir,
    claim_revision_digest,
    claim_revision_fields,
    find_approval,
    get_approval,
    verify_record,
)
