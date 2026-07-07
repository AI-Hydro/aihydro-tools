# HRB Independent Replication Guide

CI re-running the fixture scorecard on every push (`bench.yml`) is **not**
independent replication — it's the platform's own CI grading the platform's
own tasks against the platform's own oracle. That doesn't make the scorecard
wrong, but it does mean the "79/79 passing" number, on its own, cannot answer
a reviewer's next question: *has anyone outside the maintainer actually run
this and gotten the same result?*

This document is the fixed, minimal procedure for a genuinely independent
party (a co-author, a reviewer, a colleague on a machine you don't control)
to reproduce the fixture scorecard from scratch. **Running this yourself does
not count as independent replication** — the value is specifically in someone
else running it and reporting back.

## What this does and does not establish

- **Does establish:** the 78 offline (`bench`-marked) fixture tasks are
  deterministic and produce the same pass/fail outcome on a machine you don't
  control, on the pinned dependency versions below. This is what "78 tasks
  passing" can defensibly mean in a paper.
- **Does not establish:** correctness of the underlying hydrology (HRB tasks
  encode *expected tool behavior*, not independently-verified ground truth —
  see `BENCHMARK.md`'s "Governance rule"), or anything about the 1 live
  (`bench_live`, network-dependent) task, which is excluded here specifically
  because it is not reproducible on an arbitrary machine without live API
  access and credentials.

## Prerequisites

- A machine the maintainer has not configured (a fresh clone, ideally a fresh
  virtualenv/container — the entire point is "not the machine this was
  developed on").
- Python 3.11 (the version `bench.yml` CI pins; other supported versions —
  this package declares `requires-python >= 3.9` — may also work, but 3.11 is
  the version this guide's expected numbers were generated against).
- No network access required for the steps below (all 78 tasks in scope are
  `mark: bench`, explicitly offline per `BENCHMARK.md` "Scope").

## Procedure

```bash
# 1. Clone at a specific, citable commit (do not use a moving branch tip)
git clone https://github.com/AI-Hydro/aihydro-tools.git
cd aihydro-tools
git checkout <commit-sha>          # the SHA the paper/claim cites
git rev-parse HEAD                 # record this — it must match what you checked out

# 2. Fresh environment
python3.11 -m venv .venv-replication
source .venv-replication/bin/activate
python -m pip install --upgrade pip

# 3. Install with pinned extras (same as CI — see .github/workflows/bench.yml)
pip install -e ".[all,dev]" pyyaml
pip freeze > replication-environment.txt   # record exact resolved versions

# 4. Validate the task catalog itself hasn't drifted from its schema
pytest tests/test_bench_schema.py -q

# 5. Run the offline fixture suite
pytest tests/test_bench.py -m bench -v 2>&1 | tee replication-run.log

# 6. Generate the certification payload (git SHA + task counts + pass/fail)
python bench/gen_scorecard.py --run --out replication-scorecard.html \
    --json-out replication-certification.json
```

## What to report back

If you are the independent party running this, send the maintainer (or
attach to the reproducibility claim):

1. `git rev-parse HEAD` output (step 1) — confirms which commit you tested.
2. `replication-environment.txt` (step 3) — exact resolved dependency
   versions; a future drift in a transitive dependency is the most likely
   cause if a re-run someday disagrees with this one.
3. `replication-run.log` (step 5) — full pytest output.
4. `replication-certification.json` (step 6) — machine-readable pass/fail
   counts, git SHA, and schema-validity flag.
5. Your OS + Python patch version + date run.

## Recording that replication happened

This guide existing is not itself evidence that replication happened — that
requires a person other than the maintainer actually running it. Record
completed replications here (append, do not overwrite):

| Date | Replicator | Commit SHA | Result | Environment file |
|---|---|---|---|---|
| _(none yet)_ | | | | |

**Status as of this remediation pass:** the guide is written and the
procedure has been dry-run internally, but no third-party replication has
been recorded yet. Until a row exists above, "78 tasks pass" should continue
to be described as CI-verified, not independently replicated — do not upgrade
this claim in the paper without an actual entry in the table.
