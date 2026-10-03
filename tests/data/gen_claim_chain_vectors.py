"""Regenerate tests/data/claim_chain_vectors.json from aihydro-core (the reference).

Run: /opt/miniconda3/bin/python tests/data/gen_claim_chain_vectors.py
Expected verdicts are computed by core's verify_claim_revision_dict / verify_chain,
never written by hand. Nothing here is a research result; all content is synthetic.
"""
import copy
import json
from pathlib import Path

from aihydro_core.records import ClaimRevision, digest, verify_chain, verify_claim_revision_dict

OUT = Path(__file__).with_name("claim_chain_vectors.json")
ACTOR = {"kind": "package", "id": "aihydro-tools"}


def chain(session="s1", claim="c1", n=3, contents=None, actor=ACTOR, extra=None):
    rows, prev = [], None
    for i in range(n):
        content = copy.deepcopy((contents or [{"statement": "synthetic %d" % i, "status": "proposed"}] * n)[i])
        rev = ClaimRevision(
            session_id=session, claim_id=claim, revision=i,
            supersedes=prev.revision_digest if prev else None,
            revision_digest=digest(content), content=content,
            cause={"tool": "add_claim", "reason": "created" if i == 0 else "redefined"},
            actor=copy.deepcopy(actor), recorded_at="2026-01-0%dT00:00:00+00:00" % (i + 1),
            unknown=dict(extra or {}))
        rev.seal()
        rows.append(rev.to_dict())
        prev = rev
    return rows


def cases():
    base = chain()
    yield "valid_chain", base
    yield "single_revision", chain(n=1)
    yield "tail_truncated_still_valid", base[:2]
    yield "unknown_fields_roundtrip", chain(extra={"future_field": {"a": [1, 2.5, None]}})
    yield "numeric_edge_content", chain(contents=[
        {"x": 1.0, "big": 2 ** 60, "neg0": -0.0, "tiny": 1e-9, "huge": 1e30, "$k": {"$m": 1}, "s": "é \n"}] * 3)
    r = copy.deepcopy(base); r[1]["content"]["status"] = "supported"
    yield "content_edited", r
    r = copy.deepcopy(base); r[2]["actor"]["id"] = "someone-else"
    yield "actor_edited", r
    yield "middle_dropped", [base[0], base[2]]
    yield "reordered", [base[1], base[0], base[2]]
    yield "empty_chain", []
    r = copy.deepcopy(base); r[1]["supersedes"] = "sha256:" + "0" * 64
    yield "bad_supersedes_resealed_no", r
    other = chain(claim="c2")
    yield "mixed_claim_id", [base[0], base[1], other[2]]
    yield "mixed_session_id", [base[0], chain(session="s2")[1], base[2]]
    r = copy.deepcopy(base); r[0]["actor"]["kind"] = "robot"
    yield "bad_actor_kind", r
    r = copy.deepcopy(base); del r[1]["recorded_at"]
    yield "missing_recorded_at", r
    r = copy.deepcopy(base); r[2]["record_digest"] = "not-a-digest"
    yield "bad_record_digest", r
    r = copy.deepcopy(base); r[0]["record_digest"] = None
    yield "unsealed", r
    r = copy.deepcopy(base); r[0]["revision"] = True
    yield "revision_is_bool", r
    r = copy.deepcopy(base); r[1]["supersedes"] = None
    yield "later_revision_no_supersedes", r
    r = copy.deepcopy(base); r[0]["supersedes"] = base[2]["revision_digest"]
    yield "rev0_supersedes", r
    r = copy.deepcopy(base); r[1]["cause"] = {"tool": "", "reason": "x"}
    yield "empty_cause_tool", r
    r = copy.deepcopy(base); r[1]["cause"]["run_id"] = ""
    yield "empty_run_id", r
    r = copy.deepcopy(base); del r[0]["actor"]
    yield "missing_actor", r
    r = copy.deepcopy(base); r[1]["content"] = ["not", "a", "dict"]
    yield "content_not_dict", r
    r = copy.deepcopy(base); r[0]["schema"] = "aihydro.claim_revision_record/9"
    yield "schema_changed", r
    r = copy.deepcopy(base); r[0]["added_unknown"] = 1
    yield "unknown_field_added", r
    r = copy.deepcopy(base); r[0]["session_id"] = ""
    yield "empty_session_id", r


def main():
    vectors = []
    for name, rows in cases():
        row_ok = [verify_claim_revision_dict(x) for x in rows]
        chain_ok = False
        try:
            chain_ok = bool(rows) and verify_chain([ClaimRevision.from_dict(x) for x in rows])
        except Exception:
            chain_ok = False
        vectors.append({"name": name, "rows": rows, "row_ok": row_ok, "chain_ok": chain_ok})
    OUT.write_text(json.dumps({"schema": "aihydro.test.claim_chain_vectors/1", "vectors": vectors},
                              indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("wrote", OUT, len(vectors), "vectors")


if __name__ == "__main__":
    main()
