"""
Knowledge Registry MCP tools.

Expose the scientific contract layer (variables, metrics, datasets, equations)
to the agent. These tools allow the agent to understand what scientific
objects exist, how they are defined, and what constraints apply.
"""
from __future__ import annotations

import logging
from ai_hydro.mcp.app import mcp
from ai_hydro.knowledge.loader import get_registry
from ai_hydro.mcp.helpers import _tool_error_to_dict

log = logging.getLogger("ai_hydro.mcp")


@mcp.tool()
def get_variable_definition(variable_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return the canonical definition of a hydrological variable.
    
    Use this to understand units, symbols, and standard names for variables 
    like streamflow, precipitation, or PET.
    """
    try:
        registry = get_registry(workspace_dir)
        var = registry.variables.get(variable_id)
        if not var:
            return _tool_error_to_dict(ValueError(f"Variable '{variable_id}' not found in registry."))
        return var.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_variables(workspace_dir: str | None = None) -> list[dict]:
    """Return a summary of all known hydrological variables."""
    try:
        registry = get_registry(workspace_dir)
        return [
            {
                "id": v.id,
                "name": v.names[0] if v.names else v.id,
                "standard_units": v.standard_units,
                "description": v.description
            }
            for v in registry.variables.values()
        ]
    except Exception as exc:
        log.error("Failed to list variables: %s", exc)
        return []


@mcp.tool()
def get_metric_definition(metric_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return the structured definition of a model evaluation metric (e.g. KGE, NSE).
    
    Includes formula, required inputs, expected output ranges, and validation rules.
    """
    try:
        registry = get_registry(workspace_dir)
        metric = registry.metrics.get(metric_id)
        if not metric:
            return _tool_error_to_dict(ValueError(f"Metric '{metric_id}' not found in registry."))
        return metric.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_metrics(domain: str | None = None, workspace_dir: str | None = None) -> list[dict]:
    """Return all known metrics, optionally filtered by domain."""
    try:
        registry = get_registry(workspace_dir)
        metrics = list(registry.metrics.values())
        if domain:
            metrics = [m for m in metrics if m.domain == domain]
        
        return [
            {
                "id": m.id,
                "name": m.name,
                "domain": m.domain,
                "category": m.category,
                "description": m.description
            }
            for m in metrics
        ]
    except Exception as exc:
        log.error("Failed to list metrics: %s", exc)
        return []


@mcp.tool()
def get_dataset_info(dataset_id: str, workspace_dir: str | None = None) -> dict:
    """
    Return metadata and limitations for a hydrological dataset (e.g. USGS NWIS, gridMET).
    
    Use this to understand spatial/temporal coverage and citation requirements.
    """
    try:
        registry = get_registry(workspace_dir)
        ds = registry.datasets.get(dataset_id)
        if not ds:
            return _tool_error_to_dict(ValueError(f"Dataset '{dataset_id}' not found in registry."))
        return ds.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def list_known_datasets(domain: str | None = None, workspace_dir: str | None = None) -> list[dict]:
    """Return all known datasets, optionally filtered by domain."""
    try:
        registry = get_registry(workspace_dir)
        datasets = list(registry.datasets.values())
        if domain:
            datasets = [d for d in datasets if d.domain == domain]
        
        return [
            {
                "id": d.id,
                "name": d.name,
                "domain": d.domain,
                "category": d.category,
                "description": d.description
            }
            for d in datasets
        ]
    except Exception as exc:
        log.error("Failed to list datasets: %s", exc)
        return []


@mcp.tool()
def get_equation_definition(equation_id: str, workspace_dir: str | None = None) -> dict:
    """Return the definition, formula, and assumptions for a scientific equation."""
    try:
        registry = get_registry(workspace_dir)
        eq = registry.equations.get(equation_id)
        if not eq:
            return _tool_error_to_dict(ValueError(f"Equation '{equation_id}' not found in registry."))
        return eq.model_dump()
    except Exception as exc:
        return _tool_error_to_dict(exc)


# ---------------------------------------------------------------------------
# Phase 2.4 — Passage-level literature index
# ---------------------------------------------------------------------------

@mcp.tool()
def index_passages(papers_dir: str | None = None) -> dict:
    """
    Build a passage-level index of the papers library.

    Splits each .md/.txt/.bib document into ~200-word passages, computes a
    deterministic passage_hash (SHA-256[:16]) for each, and writes the index
    to ~/.aihydro/knowledge/passage_index.jsonl.

    passage_hash can be used as EvidenceSpan(source_type="paper",
    source_id=passage_hash) to create verifiable literature citations, and as
    [lit:passage_hash] in research interpretations for auditor verification.

    papers_dir: path to the papers/literature directory. Defaults to the
    platform papers directory at <project_root>/papers/.
    """
    try:
        from pathlib import Path as _Path
        from ai_hydro.knowledge.embeddings import build_passage_index

        if papers_dir:
            root = _Path(papers_dir).expanduser().resolve()
        else:
            # Default: papers/ next to the aihydro-tools package root
            root = _Path(__file__).parents[3] / "papers"

        if not root.exists():
            return {
                "error": True,
                "message": (
                    f"Papers directory not found: {root}. "
                    "Pass papers_dir explicitly or place documents under papers/."
                ),
            }

        exts = {".md", ".txt", ".bib", ".rst", ".pdf"}
        paths = [p for p in root.rglob("*") if p.suffix.lower() in exts and p.is_file()]
        if not paths:
            return {
                "error": True,
                "message": f"No indexable documents found under {root}.",
                "searched_dir": str(root),
            }

        result = build_passage_index(paths)
        result["searched_dir"] = str(root)
        result["n_files_found"] = len(paths)
        result["_note"] = (
            f"Indexed {result['n_passages']} passages from {result['n_docs']} documents. "
            "Use search_passages to query, or resolve_passage to look up by hash. "
            "Cite as EvidenceSpan(source_type='paper', source_id=passage_hash) "
            "and inline as [lit:<passage_hash>]."
        )
        return result
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def search_passages_tool(query: str, n: int = 5) -> dict:
    """
    Query the passage index by keyword and return the top-n matching passages.

    Each result includes a passage_hash suitable for use in EvidenceSpan
    (source_type="paper", source_id=passage_hash) and as [lit:passage_hash]
    in research interpretations.

    Run index_passages first to build the index.
    """
    try:
        from ai_hydro.knowledge.embeddings import search_passages, PASSAGE_INDEX_PATH

        if not PASSAGE_INDEX_PATH.exists():
            return {
                "indexed": False,
                "n_matches": 0,
                "matches": [],
                "message": "Passage index not found. Run index_passages first.",
            }

        results = search_passages(query, n=n)
        return {
            "indexed": True,
            "n_matches": len(results),
            "matches": [
                {
                    "passage_hash": r["passage_hash"],
                    "doc_name": r["doc_name"],
                    "chunk_idx": r["chunk_idx"],
                    "excerpt": r["text"][:300] + ("…" if len(r["text"]) > 300 else ""),
                    "cite_as": f"[lit:{r['passage_hash']}]",
                    "evidence_span": {
                        "source_type": "paper",
                        "source_id": r["passage_hash"],
                        "description": f"{r['doc_name']} passage {r['chunk_idx']}",
                    },
                }
                for r in results
            ],
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)


@mcp.tool()
def resolve_passage(passage_hash: str) -> dict:
    """
    Look up a passage by its hash. Returns the full passage text and source.

    Used to verify that a [lit:passage_hash] citation in a research
    interpretation corresponds to a real indexed passage.
    """
    try:
        from ai_hydro.knowledge.embeddings import resolve_passage_hash, PASSAGE_INDEX_PATH

        if not PASSAGE_INDEX_PATH.exists():
            return {
                "found": False,
                "passage_hash": passage_hash,
                "message": "Passage index not found. Run index_passages first.",
            }

        rec = resolve_passage_hash(passage_hash)
        if rec is None:
            return {
                "found": False,
                "passage_hash": passage_hash,
                "message": (
                    f"No passage with hash '{passage_hash}' in the index. "
                    "The passage may not have been indexed yet — re-run index_passages."
                ),
            }
        return {
            "found": True,
            "passage_hash": passage_hash,
            "doc_name": rec["doc_name"],
            "chunk_idx": rec["chunk_idx"],
            "source_path": rec["source_path"],
            "text": rec["text"],
            "indexed_at": rec["indexed_at"],
        }
    except Exception as exc:
        return _tool_error_to_dict(exc)
