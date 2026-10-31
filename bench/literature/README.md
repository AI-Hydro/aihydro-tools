# Passage search benchmark

Compares the product path (`ai_hydro.knowledge.passages.search_passages`, BM25 over SQLite FTS5) with a
grep-style baseline and the substring-overlap scorer it replaced.

```
python bench/literature/run.py                       # seed gold set
python bench/literature/run.py --gold my_gold.json   # a real one
```

## What the seed set is, and is not

`gold_seed.json` has 29 questions written by an AI agent from this repository's own prose docs. It is not a
hydrology literature set and was written by someone who had read the passages. It makes the harness runnable
and gives a first reading. It cannot make the keep-or-delete decision below. One question is 3.4 points.

## The decision it is for

Plan rule: if `grep` is within 5 points of BM25 at Recall@5 on the **human-written** gold set (30 to 50
questions from real hydrology papers), delete the index code and give the agent the text.

A gold file is JSON: `{"corpus": [globs relative to the repo root], "questions": [{"id", "question",
"answer_phrase"}]}`. A passage is relevant when its text contains `answer_phrase`, case-insensitive. Write the
question first, in your own words, from the paper, and take the phrase from the passage that answers it.

## Last run on the seed set (this machine, SQLite FTS5, 178 passages from 22 documents)

| ranker | Recall@1 | Recall@3 | Recall@5 |
|---|---|---|---|
| BM25 (FTS5) | 62.1 | 79.3 | 86.2 |
| grep-style (distinct words matched, no index) | 62.1 | 82.8 | 89.7 |
| retired substring scorer | 24.1 | 34.5 | 34.5 |

BM25 minus grep at Recall@5: -3.5 points (one question). On this set the kill rule would fire. See `DECISIONS.md`.
