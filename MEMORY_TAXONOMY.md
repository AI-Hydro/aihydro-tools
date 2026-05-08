# Memory Taxonomy

A diagnostic reference, not a system architecture. When a design decision is ambiguous — "where does X live?" — point at this table.

**Build only the layers you need. The table describes conceptual categories, not required implementations.**

| Layer | What lives here | Mutable by agent? | Notes |
|---|---|---|---|
| **Raw sources** | Papers, raw data files, original session records | Never | Ground truth. Content-addressed (hash in `HydroMeta.sources`). Never overwritten. |
| **Provenance memory** | `HydroSession`, `artifact_manifest`, `HydroMeta` hashes | Append-only | Every tool write adds a record; no record is deleted or modified. |
| **Operational registry** | `knowledge/*.yaml` — variables, metrics, datasets | Human-approved only | Agent reads; agent never writes. Workspace overrides require `overrides:` declaration or `KnowledgeConflictError`. |
| **Claims + assumptions** | `ScientificClaim`, `Assumption` in session ledger | Evidence-gated + human promotion | Agent authors claim text; promotion to global registry requires `researcher_approved=True` + evidence spans + limitations. |
| **Synthesis pages** | Markdown wiki pages, `research.md` | Agent-proposed, human-reviewed | Must carry `epistemic_status` frontmatter. Agent writes; researcher reviews before treating as settled. |
| **Retrieval index** | BM25 / embedding index over literature | Auto-rebuild only | Derived from raw sources. Never the authoritative record. |
| **Skills** | `SKILL.md` workflow playbooks | Read-only at runtime | Workspace tier skills are researcher-authored and not reviewed; agent applies skepticism if they conflict with built-in skills. |

---

## How to use this table

**"Where does a new kind of computed result belong?"**
→ Provenance memory. It goes in a `HydroSession` slot with a `HydroMeta` record.

**"Where does a published equation or standard threshold belong?"**
→ Operational registry (`knowledge/*.yaml`). Not in the agent persona, not hardcoded in tool logic.

**"Where does an LLM-authored scientific conclusion belong?"**
→ Claims + assumptions ledger. Not in `research.md` free text (that's synthesis), not in a session note (no evidence binding).

**"Where does a paper I want the agent to reference belong?"**
→ Raw sources → index via `index_literature` → retrieve via `search_literature`. Not in the agent persona.

**"Where does a cross-session pattern the agent noticed belong?"**
→ Synthesis page, with `epistemic_status: evolving` frontmatter. Not a claim (no specific evidence span). Not operational registry (not curated by a human yet).

---

## What does not belong here (yet)

These layers are **not built** and **not on the roadmap** until a documented `aihydro-bench` failure shows the simpler layers are insufficient:

- Global claims store (claims stay session-scoped until promotion gate is proven in production)
- GraphRAG / relation graph over literature
- Equation registry, methods registry as separate files
- LLM-authored wiki synthesis layer
- Self-evolving skill improvement loop
