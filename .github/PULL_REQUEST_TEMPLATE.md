<!-- One sentence: what does this PR do? -->

## Summary


---

## Adding capability? Required justification.

*Skip this section entirely for bug fixes, docs, refactors, and test-only changes.*

Check **one** type and fill the failure reference:

- [ ] New `@mcp.tool()` — `tier` field assigned in tool registration metadata (`"scientific"` / `"workflow"` / `"infrastructure"`)
- [ ] New knowledge registry file or library card — existing files were insufficient for a documented failure
- [ ] New memory layer or session field — existing session schema was insufficient for a documented failure
- [ ] New validator — catches a real failure that passed undetected without it

**Benchmark failure reference** — paste the `aihydro-bench` task ID, session trace ID, or CI run link showing the simpler approach failed:

```
Failure reference: ___________________
```

> **No failure reference = this PR stays in draft. Do not merge until one is documented.**
> Hypothetical failures do not count. The failure must appear in an `aihydro-bench` output or a reproducible session trace.

This rule exists because `@mcp.tool()` count grew from 11 to 56 without it. See `DESIGN_PRINCIPLES.md`.

---

## Checklist

- [ ] `pytest tests/ -m "not live" -q` passes
- [ ] Tests added or updated for every changed behaviour
- [ ] `CHANGELOG.md` updated (for user-facing changes)
- [ ] No API keys, credentials, or secrets committed
- [ ] If new tool: `aihydro-bench` still passes (link CI run or paste local output)
- [ ] If schema change to `HydroSession` or `ScientificClaim`: migration shim added and tested

## Screenshots / output

<!-- Paste relevant terminal output, NSE/KGE values, or `aihydro-bench` diff if applicable. -->
