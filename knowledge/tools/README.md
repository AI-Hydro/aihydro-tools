# Tool Documentation

Reference material about AI-Hydro's tools.

## Files

- **camels_tools.json** - CAMELS-specific tool documentation
- **AUTHORING_GUIDE.md** - how to add a new MCP tool so it is born compliant
  (naming, tier, parameters, session resolution, checklist)
- **skills/mcp-tool-authoring/** - the live-loadable skill version of the guide

## "Tier" means exactly one thing

A tool's **tier** is its execution tier, defined once in
`ai_hydro/mcp/app.py::TOOL_TIERS` (the single source of truth):

- **Tier 1** — produces a citable scientific result/artifact; automatically
  *hot* (full schema injected); may carry a post-run validator.
- **Tier 2** — workflow / data acquisition.
- **Tier 3** — infrastructure / discovery / state.

This is *not* a complexity or library-vs-wrapper axis. How tools compose is a
separate question — see the three primitives (MCP tools / skills / package
knowledge) in `AGENT_EXECUTION_MODEL.md` and `DESIGN_PRINCIPLES.md`.

> Historical note: earlier `tier1_libraries.json` / `tier2_wrappers.json` /
> `tier3_tools.json` files used "tier" for a library/wrapper/workflow split that
> fed the (now-removed) RAG engine. They were unused and deleted; this is the
> only meaning of "tier" now.

See [../CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidelines and
[AUTHORING_GUIDE.md](AUTHORING_GUIDE.md) to add a tool.
