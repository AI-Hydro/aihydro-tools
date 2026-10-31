"""Passage search benchmark: BM25 (FTS5) against a grep-style baseline and the retired substring scorer.

    python bench/literature/run.py [--gold bench/literature/gold_seed.json] [--k 5] [--json out.json]

Corpus and questions come from a gold file (see ``gold_seed.json``). A passage is relevant to a
question when its text contains the question's ``answer_phrase`` (case-insensitive). Reported:
Recall@1/3/5 per ranker and the gap, in percentage points, between BM25 and the grep baseline.

Rankers (all see the same passages):

* ``bm25``    ``ai_hydro.knowledge.passages.search_passages`` (the product path).
* ``grep``    what an agent can do with ``rg -ci`` and no index: for each query word (same tokenising, no
              stemming), does the passage contain it as a case-insensitive substring; rank by distinct words
              matched, then total occurrences, then file order. No IDF, no length normalisation.
* ``legacy``  the scorer retired by this change (substring overlap divided by passage length), copied
              here only so the comparison can be re-run.

Kill rule from the plan: if ``grep`` is within 5 points of ``bm25`` at Recall@5 on the human-written gold
set, delete the index code and give the agent the text. The seed set cannot decide that.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def corpus_files(patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        hits = sorted(Path(p) for p in glob.glob(str(ROOT / pat), recursive=True))
        out.extend(h for h in hits if h.is_file() and h not in out)
    return out


def rank_grep(passages: list[dict], terms: list[str]) -> list[dict]:
    scored = []
    for i, p in enumerate(passages):
        low = p["text"].lower()
        counts = [low.count(t) for t in terms]
        distinct = sum(1 for c in counts if c)
        if distinct:
            scored.append((-distinct, -sum(counts), i, p))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored]


def rank_legacy(passages: list[dict], query: str) -> list[dict]:
    qt = [t.lower() for t in re.split(r"\W+", query) if len(t) >= 3]
    if not qt:
        return passages

    def score(text: str) -> float:
        words = text.lower().split()
        return (sum(1 for w in words if any(t in w for t in qt)) / len(words)) if words else 0.0

    return [p for p, _ in sorted(((p, score(p["text"])) for p in passages), key=lambda x: x[1], reverse=True)]


def evaluate(gold: dict, ks=(1, 3, 5)) -> dict:
    from ai_hydro.knowledge import passages as P

    files = corpus_files(gold["corpus"])
    saved = (P.KNOWLEDGE_DIR, P.PASSAGE_INDEX_PATH)
    try:
        return _evaluate(P, gold, files, ks)
    finally:
        P.KNOWLEDGE_DIR, P.PASSAGE_INDEX_PATH = saved


def _evaluate(P, gold: dict, files: list, ks) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        P.KNOWLEDGE_DIR = Path(tmp)
        P.PASSAGE_INDEX_PATH = Path(tmp) / "passage_index.jsonl"
        built = P.build_passage_index(files)
        passages = P.load_all_passages()
        k_max = max(ks)
        hits = {"bm25": {k: 0 for k in ks}, "grep": {k: 0 for k in ks}, "legacy": {k: 0 for k in ks}}
        misses = {"bm25": [], "grep": [], "legacy": []}
        n_gold_passages = []
        for q in gold["questions"]:
            phrase = q["answer_phrase"].lower()
            relevant = {p["passage_hash"] for p in passages if phrase in p["text"].lower()}
            n_gold_passages.append(len(relevant))
            terms = P.query_terms(q["question"])
            ranked = {
                "bm25": P.search_passages(q["question"], n=k_max),
                "grep": rank_grep(passages, terms)[:k_max],
                "legacy": rank_legacy(passages, q["question"])[:k_max],
            }
            for name, rs in ranked.items():
                first = next((i + 1 for i, r in enumerate(rs) if r["passage_hash"] in relevant), None)
                for k in ks:
                    hits[name][k] += int(first is not None and first <= k)
                if first is None or first > 5:
                    misses[name].append(q["id"])
        n = len(gold["questions"])
        recall = {name: {f"recall@{k}": round(100.0 * h[k] / n, 1) for k in ks} for name, h in hits.items()}
        return {
            "n_questions": n, "n_passages": built["n_passages"], "n_docs": built["n_docs"],
            "questions_with_no_relevant_passage": [q["id"] for q, c in zip(gold["questions"], n_gold_passages) if c == 0],
            "recall_percent": recall,
            "bm25_minus_grep_at_5_points": round(recall["bm25"]["recall@5"] - recall["grep"]["recall@5"], 1),
            "missed_at_5": misses,
        }


def _git_head() -> str | None:
    import subprocess

    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", default=str(ROOT / "bench/literature/gold_seed.json"))
    ap.add_argument("--json", default=None, help="write the result here")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    res = evaluate(gold)
    res["gold"] = str(Path(args.gold).name)
    res["code_commit"] = _git_head()
    res["sqlite_version"] = __import__("sqlite3").sqlite_version
    print(json.dumps(res, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
