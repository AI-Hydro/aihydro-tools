"""``aihydro-approve`` — the human approval channel for claim promotion (ADR-002a).

    aihydro-approve <session_id> <claim_id> [--approver NAME] [--statement TEXT] [--key PATH]
    aihydro-approve enroll <pubkey-file> [--principal NAME] [--valid-after D] [--valid-before D] [--user-trust]
    aihydro-approve revoke <fingerprint> [--reason TEXT]

Loads the claim read-only through the public session API, prints the fields
the approval binds (text, type, status, confidence and rationale, scope,
evidence spans and the fingerprint of each retained evidence record,
limitations, prereg id, uncertainty flag) and the revision digest, and asks the
human to type the first 12 hex characters of that digest. Only then is a sealed
approval record appended to ``$AIHYDRO_HOME/approvals/``.

It refuses to run unless stdin and stdout are both a TTY, so an agent that
shells out non-interactively cannot approve. There is deliberately no
``--yes``: a flag that skips the human step would be an agent-reachable
bypass. This is a guard against accidental or naive non-interactive use, not
a security boundary against code running as the same OS user: such a process
can drive this CLI through a pseudo-terminal or append a sealed record itself
(both demonstrated by the slice-1 review). Records are labelled
``channel: "cli_same_user"`` for that reason (docs/evidence-integrity.md,
"Human approval").

Signing (ADR-002b, fails closed). This CLI is the canonical approval channel. When an
``allowed_signers`` trust root exists (system ``/etc/aihydro/allowed_signers``
or ``$AIHYDRO_HOME/trust/allowed_signers``), the confirmed approval is signed
with an enrolled SSH key (``--key``, else ``~/.ssh/id_*``, else an enrolled
ssh-agent key) via ``ssh-keygen -Y sign`` and stored as ``aihydro.approval/2``;
a hardware (``sk-``) key makes the human touch the authenticator for each
approval. With no trust root nothing is recorded (exit 5) unless the developer
opts out with AIHYDRO_REQUIRE_SIGNED=0, which writes a labelled unsigned v1
record. ``--approver`` must equal the enrolled principal of the signing key
(the verifier refuses a mismatch). It never stores private keys and ``enroll`` never runs sudo: it prints
the line and command for the owner to apply.

Exit codes: 0 approved (or already approved); 1 claim/session problem;
2 usage error; 3 not interactive; 4 confirmation not given; 5 signing needed
but unavailable (no key, key not enrolled, signing refused).
"""
from __future__ import annotations

import argparse
import getpass
import shlex
import sys
from typing import Optional, Sequence, TextIO

from aihydro_core.records import Actor

from ai_hydro.approval.records import (
    approvals_dir,
    find_approval,
    session_claim_revision,
)
from ai_hydro.approval.signing import (
    SigningError,
    append_user_trust,
    enrol_line,
    read_pubkey_file,
    write_revocation,
)
from ai_hydro.approval.trust import SYSTEM_TRUST_FILE, require_signed, trust_root
from ai_hydro.approval.writer import write_approval, write_signed_approval

EXIT_OK, EXIT_CLAIM, EXIT_USAGE, EXIT_NOT_TTY, EXIT_DECLINED, EXIT_SIGNING = 0, 1, 2, 3, 4, 5
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
        f"Type        : {fields['claim_type']}    Pre-registration: {fields['prereg_id'] or '(none)'}",
        f"Status      : {fields['status']}    Confidence: {fields['confidence']}    "
        f"Uncertainty verified: {fields['uncertainty_verified']}",
        f"Rationale   : {fields['confidence_rationale']}",
        f"Scope       : basins={scope.get('basins')}  period={scope.get('period')}  "
        f"metric={scope.get('metric')}  forcing={scope.get('forcing')}",
        "Basin refs  : " + (
            "; ".join(f"{r.get('label')} -> {r.get('id')}" for r in scope["basin_refs"])
            if scope.get("basin_refs") else "(none: basins are unbound labels)"),
        "Evidence    :",
    ]
    spans = fields["evidence_spans"]
    versions = fields["evidence_versions"]
    if spans:
        for sp in spans:
            extra = "".join(f"  {k}={sp[k]}" for k in ("metric_ref", "page", "passage_hash") if sp.get(k) is not None)
            lines.append(f"  - {sp['source_type']}: {sp['source_id']}{extra}")
            lines.append(f"      retained-record fingerprint: {versions.get(sp['source_id'], '(none)')}")
    else:
        lines.append("  (none)")
    lines.append("Limitations :")
    if fields["limitations"]:
        lines += [f"  - {lim}" for lim in fields["limitations"]]
    else:
        lines.append("  (none)")
    lines += ["-" * 72, f"Revision digest: {rev}", ""]
    if any(str(v).startswith("unresolved:") for v in versions.values()):
        lines.append("WARNING: some evidence cannot be resolved from the retained session; "
                     "promotion will be refused until it can.")
        lines.append("")
    if fields["status"] not in _ELIGIBLE:
        lines.append(f"WARNING: status '{fields['status']}' is not eligible for promotion "
                     f"({', '.join(_ELIGIBLE)}); this approval will not promote it as-is.")
        lines.append("")
    return "\n".join(lines)


def _enroll(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    parser = argparse.ArgumentParser(prog="aihydro-approve enroll",
                                     description="Print the allowed_signers line (and command) that enrols a public key.")
    parser.add_argument("pubkey_file")
    parser.add_argument("--principal", default=None, help="identity in allowed_signers (default: the OS user)")
    parser.add_argument("--valid-after", default=None, help="YYYYMMDD[Z] or YYYYMMDDHHMM[SS][Z]")
    parser.add_argument("--valid-before", default=None, help="expiry, same format (use for key rotation)")
    parser.add_argument("--user-trust", action="store_true",
                        help="append to $AIHYDRO_HOME/trust/allowed_signers (user-writable, weaker) "
                             "instead of only printing the system command")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        pub = read_pubkey_file(args.pubkey_file)
    except (OSError, SigningError, ValueError) as exc:
        print(f"aihydro-approve enroll: {exc}", file=stderr)
        return EXIT_USAGE
    line = enrol_line(pub, args.principal or getpass.getuser() or "researcher",
                      args.valid_after, args.valid_before)
    print(f"Key {pub['key_type']}  {pub['fingerprint']}", file=stdout)
    print("\nallowed_signers line:\n  " + line, file=stdout)
    print("\nTo enrol it system-wide (root-owned trust root; this tool never runs sudo):\n"
          f"  sudo mkdir -p {shlex.quote(str(trust_root_dir()))} && echo {shlex.quote(line)} | "
          f"sudo tee -a {shlex.quote(SYSTEM_TRUST_FILE)} >/dev/null", file=stdout)
    if args.user_trust:
        path = append_user_trust(line)
        print(f"\nAppended to {path} (trust_root: user_writable; a process running as you can edit it).",
              file=stdout)
    else:
        print("\nOr pass --user-trust to append it to the user-writable fallback instead.", file=stdout)
    return EXIT_OK


def trust_root_dir():
    from pathlib import Path
    return Path(SYSTEM_TRUST_FILE).parent


def _revoke(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    parser = argparse.ArgumentParser(prog="aihydro-approve revoke",
                                     description="Revoke a signing key: every approval it signed stops verifying.")
    parser.add_argument("fingerprint", help="SHA256:... as printed by enroll / ssh-keygen -lf")
    parser.add_argument("--reason", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        rec = write_revocation(args.fingerprint, args.reason)
    except ValueError as exc:
        print(f"aihydro-approve revoke: {exc}", file=stderr)
        return EXIT_USAGE
    print(f"Recorded local revocation of {args.fingerprint} (record {rec['record_digest']}).\n"
          "WARNING: this local revocation file is deletable and forgeable by any process running as "
          "you; it is not a security control. Real revocation: set valid-before on that key's line "
          "in the SYSTEM allowed_signers (root-owned), or remove the line.", file=stdout)
    return EXIT_OK


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "enroll":
        return _enroll(argv[1:], stdout, stderr)
    if argv and argv[0] == "revoke":
        return _revoke(argv[1:], stdout, stderr)

    parser = argparse.ArgumentParser(
        prog="aihydro-approve",
        description="Record a human approval for one claim revision so it can be promoted "
                    "to the global registry. Interactive terminal only.",
    )
    parser.add_argument("session_id")
    parser.add_argument("claim_id")
    parser.add_argument("--approver", default=None, help="approver name (default: the OS user)")
    parser.add_argument("--statement", default=None, help="approval statement stored in the record")
    parser.add_argument("--key", default=None,
                        help="SSH key to sign with (default: ~/.ssh/id_* or an enrolled ssh-agent key)")
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
        from ai_hydro.session.store import HydroSession
        session = HydroSession.load(args.session_id)              # read-only: never saved
        if not session.claims.get(args.claim_id):
            print(f"aihydro-approve: claim '{args.claim_id}' not found in session "
                  f"'{args.session_id}'.", file=stderr)
            return EXIT_CLAIM
        _, _, fields, rev = session_claim_revision(session, args.claim_id)
    except Exception as exc:
        print(f"aihydro-approve: cannot load claim: {exc}", file=stderr)
        return EXIT_CLAIM

    print(_render(args.session_id, args.claim_id, fields, rev), file=stdout)

    existing = find_approval(args.session_id, args.claim_id, rev, unconsumed_only=True)
    if existing:
        print(f"Already approved by {existing['approver']['id']} at {existing['approved_at']} "
              f"(record {existing['record_digest']}), not yet used. Nothing to do.", file=stdout)
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
    actor = Actor(kind="human", id=approver_id)
    root, label = trust_root()
    if not require_signed() and not args.key:      # explicit development opt-out only
        record = write_approval(args.session_id, args.claim_id, rev, actor, statement)
        print(f"Approved (UNSIGNED legacy record under explicit opt-out, channel cli_same_user: "
              f"not verified human identity). Record {record['record_digest']} written under "
              f"{approvals_dir()}.", file=stdout)
        return EXIT_OK
    try:
        record = write_signed_approval(args.session_id, args.claim_id, rev, actor, statement, key=args.key)
    except (SigningError, ValueError) as exc:
        print(f"aihydro-approve: no approval recorded: {exc}\n"
              "Approvals fail closed: enrol a signing key first (ssh-keygen -t ed25519-sk, then "
              "`aihydro-approve enroll <key>.pub`, add the printed line to the trust root) and pass "
              "--key if needed. Development only: AIHYDRO_REQUIRE_SIGNED=0 opts out.", file=stderr)
        return EXIT_SIGNING
    print(f"Approved and signed ({record['signer']['key_type']} {record['signer']['fingerprint']}, "
          f"trust root {label}). Record {record['record_digest']} written under {approvals_dir()}.",
          file=stdout)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
