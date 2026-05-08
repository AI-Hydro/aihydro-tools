# Contributing to aihydro-tools

> AI-Hydro ensures the LLM operates inside a versioned, validated, reproducible hydrology environment. It does not teach the LLM hydrology.

Every contribution is evaluated against that sentence. If the justification is "this teaches the LLM more hydrology," the answer is no. If the justification is "this constrains the LLM to operate within a verifiable scientific contract," the answer is yes.

---

## Dev setup

```bash
git clone https://github.com/AI-Hydro/aihydro-tools
cd aihydro-tools
pip install -e ".[all,dev]"       # editable install — extension picks this up automatically
pytest tests/ -m "not live" -q    # baseline must pass before any change
aihydro-mcp --diagnose            # verify server starts with all tools
```

If you are also developing the VS Code extension, `pip install -e` in the extension's Python environment. Do **not** edit `python/ai_hydro/` inside the extension repo — that copy is stale and will be deleted. The extension auto-discovers the installed package.

---

## Before opening a PR

1. **Read `DESIGN_PRINCIPLES.md`** — the tier model, the deferral rule, and the load-bearing paths.
2. **Read `MEMORY_TAXONOMY.md`** — answers "where does X live?" before you build a new layer.
3. **Check the PR template** — `.github/PULL_REQUEST_TEMPLATE.md`. Capability additions require a benchmark failure reference. No failure reference = draft PR.

---

## Adding a new tool

1. Assign a tier (`"scientific"` / `"workflow"` / `"infrastructure"`). When in doubt, assign `"scientific"`.
2. If Tier 1 (scientific): add `citations` to `TOOL_CITATIONS` in `ai_hydro/citations.py`, add validator calls in the tool body, and populate `uncertainty` on numeric outputs.
3. Run `pytest tests/ -m "not live" -q` — must pass.
4. Add or update `tests/test_reference_gauges.py` if the tool produces numeric outputs a paper could cite.
5. Document the `aihydro-bench` failure that motivated the tool. This goes in the PR description.

## Adding a knowledge card

Cards live in `ai_hydro/knowledge/library_refs/`. Schema: `library`, `version_tested`, `purpose`, `install`, `field_mappings`, `gotchas`, `common_patterns`, `version_compatible`. See `pynhd.json` for a reference.

A card is appropriate when an `aihydro-bench` run shows the agent hallucinating an API call that a gotcha card would have prevented. Not before.

## Adding a skill

Skills live in `ai_hydro/skills/<domain>/<skill-name>/SKILL.md`. The linter (`ai_hydro/skills/registry.py`) requires `name`, `description`, `when_to_use`, and `domain` frontmatter fields. Workspace skills are researcher-authored and not reviewed — built-in skills go here.

## Plugin contributions

External packages register via `[project.entry-points."aihydro.tools"]`. See `README.md` for the plugin contract. Citation metadata is required for any Tier 1 plugin tool.

---

## Running the benchmark

```bash
# Once aihydro-bench exists (Sprint 1):
pytest tests/bench/ -m "bench" --bench-report
```

Every capability-addition PR must include a link to a passing benchmark run, or a documented failure that motivated the addition.

---

## Questions?

Open an issue. Reference the relevant `DESIGN_PRINCIPLES.md` section or `MEMORY_TAXONOMY.md` row if the question is architectural.
