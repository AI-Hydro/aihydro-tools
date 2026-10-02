"""``aihydro-approve`` — the human approval channel for claim promotion (ADR-002a).

    aihydro-approve <session_id> <claim_id> [--approver NAME] [--statement TEXT]

Loads the claim read-only through the public session API, prints its text,
scope, evidence spans, limitations and revision digest, and asks the human to
type the first 12 hex characters of that digest. Only then is a sealed
approval record appended to ``$AIHYDRO_HOME/approvals/``.

It refuses to run unless stdin and stdout are both a TTY, so an agent that
shells out non-interactively cannot approve. There is deliberately no
``--yes``: a flag that skips the human step would be an agent-reachable
bypass. This is a guard against accidental or naive non-interactive use, not
a security boundary against code running as the same OS user (see
docs/evidence-integrity.md, "Human approval").

Exit codes: 0 approved (or already approved); 1 claim/session problem;
2 usage error; 3 not interactive; 4 confirmation not given.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Callable, Optional, Sequence, TextIO

from aihydro_core.records import Actor

from ai_hydro.approval.records import (
    approvals_dir,
    claim_revision_digest,
    claim_revision_fields,
    find_approval,
)
from ai_hydro.approval.writer import write_approval

EXIT_OK, EXIT_CLAIM, EXIT_USAGE, EXIT_NOT_TTY, EXIT_DECLINED = 0, 1, 2, 3, 4
_CONFIRM_HEX = 12
_ELIGIBLE = ("supported", "weakly_supported")


def _is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _render(session_id: str, claim_id: str, fields: dict, rev: str) -> str:
    scope = fields["scope"]
    lines = [
        "",
        f"Claim {claim_id}  (session {session_id})",
        "-" * 72,
        f"Text        : {fields['text']}",
        f"Status      : {fields['status']}    Confidence: {fields['confidence']}",
        f"Scope       : basins={scope.get('basins')}  period={scope.get('period')}  "
        f"metric={scope.get('metric')}  forcing={scope.get('forcing')}",
        "Evidence    :",
    ]
    spans = fields["evidence_spans"]
    if spans:
        for sp in spans:
            extra = "".join(f"  {k}={sp[k]}" for k in ("metric_ref", "page", "passage_hash") if sp.get(k) is not None)
            lines.append(f"  - {sp['source_type']}: {sp['source_id']}{extra}")
    else:
        lines.append("  (none)")
    lines.append("Limitations :")
    if fields["limitations"]:
        lines += [f"  - {lim}" for lim in fields["limitations"]]
    else:
        lines.append("  (none)")
    lines += ["-" * 72, f"Revision digest: {rev}", ""]
    if fields["status"] not in _ELIGIBLE:
        lines.append(f"WARNING: status '{fields['status']}' is not eligible for promotion "
                     f"({', '.join(_ELIGIBLE)}); this approval will not promote it as-is.")
        lines.append("")
    return "\n".join(lines)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    load_claim: Optional[Callable[[str, str], Optional[dict]]] = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    parser = argparse.ArgumentParser(
        prog="aihydro-approve",
        description="Record a human approval for one claim revision so it can be promoted "
                    "to the global registry. Interactive terminal only.",
    )
    parser.add_argument("session_id")
    parser.add_argument("claim_id")
    parser.add_argument("--approver", default=None, help="approver name (default: the OS user)")
    parser.add_argument("--statement", default=None, help="approval statement stored in the record")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed usage
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE

    if not (_is_tty(stdin) and _is_tty(stdout)):
        print("aihydro-approve: refusing to run non-interactively. Approval must be given by a "
              "human at a terminal (stdin and stdout must be a TTY).", file=stderr)
        return EXIT_NOT_TTY

    approver_id = (args.approver or getpass.getuser() or "").strip()
    if not approver_id:
        print("aihydro-approve: could not determine the approver; pass --approver NAME.", file=stderr)
        return EXIT_USAGE

    try:
        if load_claim is not None:
            claim = load_claim(args.session_id, args.claim_id)
        else:
            from ai_hydro.session.store import HydroSession
            claim = HydroSession.load(args.session_id).claims.get(args.claim_id)  # read-only
        if not claim:
            print(f"aihydro-approve: claim '{args.claim_id}' not found in session "
                  f"'{args.session_id}'.", file=stderr)
            return EXIT_CLAIM
        fields = claim_revision_fields(claim)
        rev = claim_revision_digest(claim)
    except Exception as exc:
        print(f"aihydro-approve: cannot load claim: {exc}", file=stderr)
        return EXIT_CLAIM

    print(_render(args.session_id, args.claim_id, fields, rev), file=stdout)

    existing = find_approval(args.session_id, args.claim_id, rev)
    if existing:
        print(f"Already approved by {existing['approver']['id']} at {existing['approved_at']} "
              f"(record {existing['record_digest']}). Nothing to do.", file=stdout)
        return EXIT_OK

    want = rev.split(":", 1)[1][:_CONFIRM_HEX]
    stdout.write(f"{approver_id}, to approve this exact claim revision, type its digest prefix "
                 f"({_CONFIRM_HEX} hex characters after 'sha256:'): ")
    stdout.flush()
    answer = stdin.readline().strip().lower()
    if answer.startswith("sha256:"):
        answer = answer[len("sha256:"):]
    if answer != want:
        print("Confirmation did not match. No approval recorded.", file=stderr)
        return EXIT_DECLINED

    statement = args.statement or f"Approved claim {args.claim_id} at revision {rev[:19]}."
    record = write_approval(args.session_id, args.claim_id, rev,
                            Actor(kind="human", id=approver_id), statement)
    print(f"Approved. Record {record['record_digest']} written under {approvals_dir()}.", file=stdout)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
