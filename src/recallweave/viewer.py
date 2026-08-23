from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import connect
from .safe_write import _install_non_replacing, install, prepare_destination, verify_destination


VIEWER_SCHEMA_VERSION = "recallweave.viewer.v1"
MAX_SUMMARY_CHARACTERS = 280
MAX_EVIDENCE_CHARACTERS = 500


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _excerpt(value: str | None, limit: int) -> str:
    if not value:
        return ""
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _edge_evidence(
    raw: str,
    *,
    source_path: str,
    include_excerpts: bool,
) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    source_evidence = parsed.get("source_evidence")
    if not isinstance(source_evidence, dict):
        source_evidence = {}
    target_evidence = parsed.get("target_evidence")
    if not isinstance(target_evidence, dict):
        target_evidence = {}
    line = parsed.get("line")

    def bounded_side(
        side: dict[str, Any],
        *,
        fallback_citation: str | None = None,
        fallback_passage: str | None = None,
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        citation = side.get("citation")
        if not isinstance(citation, str):
            citation = fallback_citation
        if isinstance(citation, str):
            result["citation"] = citation
        if include_excerpts:
            passage = side.get("passage")
            if not isinstance(passage, str):
                passage = fallback_passage
            if isinstance(passage, str):
                result["passage"] = _excerpt(passage, MAX_EVIDENCE_CHARACTERS)
        return result

    fallback_citation = f"{source_path}:{line}" if isinstance(line, int) else None
    source_text = parsed.get("source_text")
    source = bounded_side(
        source_evidence,
        fallback_citation=fallback_citation,
        fallback_passage=source_text if isinstance(source_text, str) else None,
    )
    target = bounded_side(target_evidence)

    evidence: dict[str, Any] = {}
    if source:
        evidence["source_evidence"] = source
        # Preserve the v1 flat fields while consumers move to bilateral evidence.
        if "citation" in source:
            evidence["citation"] = source["citation"]
        if include_excerpts and "passage" in source:
            evidence["source_text"] = source["passage"]
    if target:
        evidence["target_evidence"] = target
    shared_terms = parsed.get("shared_terms")
    if isinstance(shared_terms, list):
        evidence["shared_terms"] = [
            str(term) for term in shared_terms if isinstance(term, str)
        ][:12]
    explanation = parsed.get("explanation")
    if isinstance(explanation, str):
        evidence["explanation"] = _excerpt(explanation, MAX_EVIDENCE_CHARACTERS)

    return evidence


def build_viewer_document(
    database: Path,
    *,
    include_candidates: bool = True,
    include_excerpts: bool = False,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, browser-safe graph document from a RecallWeave index."""

    with connect(database, readonly=True) as connection:
        note_rows = connection.execute(
            """
            SELECT n.id, n.relative_path, n.title, n.tags_json, n.status, n.domain,
                   COUNT(s.id) AS section_count,
                   (
                       SELECT first.text
                       FROM sections first
                       WHERE first.note_id = n.id
                       ORDER BY first.id
                       LIMIT 1
                   ) AS first_section
            FROM notes n
            LEFT JOIN sections s ON s.note_id = n.id
            GROUP BY n.id
            ORDER BY n.relative_path COLLATE NOCASE
            """
        ).fetchall()
        paths = {int(row["id"]): str(row["relative_path"]) for row in note_rows}
        edge_where = "" if include_candidates else "WHERE e.is_verified = 1"
        edge_rows = connection.execute(
            f"""
            SELECT e.id, e.source_note_id, e.target_note_id, e.kind,
                   e.is_verified, e.score, e.evidence_json
            FROM edges e
            {edge_where}
            ORDER BY e.is_verified DESC, e.score DESC, e.id
            """
        ).fetchall()
        unresolved = int(
            connection.execute("SELECT COUNT(*) FROM unresolved_links").fetchone()[0]
        )

    nodes = []
    for row in note_rows:
        node = {
            "id": str(row["relative_path"]),
            "title": str(row["title"]),
            "path": str(row["relative_path"]),
            "status": str(row["status"] or ""),
            "domain": str(row["domain"] or "Unclassified"),
            "summary": (
                _excerpt(str(row["first_section"] or ""), MAX_SUMMARY_CHARACTERS)
                if include_excerpts
                else ""
            ),
            "tags": _json_list(str(row["tags_json"])),
            "section_count": int(row["section_count"]),
        }
        nodes.append(node)

    edges = []
    for row in edge_rows:
        source_path = paths[int(row["source_note_id"])]
        target_path = paths[int(row["target_note_id"])]
        edges.append(
            {
                "id": f"edge-{int(row['id'])}",
                "source": source_path,
                "target": target_path,
                "kind": str(row["kind"]),
                "verified": bool(row["is_verified"]),
                "score": float(row["score"]),
                "evidence": _edge_evidence(
                    str(row["evidence_json"]),
                    source_path=source_path,
                    include_excerpts=include_excerpts,
                ),
            }
        )

    includes_note_derived_terms = any(
        bool(edge["evidence"].get("shared_terms")) for edge in edges
    )
    includes_passage_text = any(bool(node["summary"]) for node in nodes) or any(
        bool(side.get("passage"))
        for edge in edges
        for side in (
            edge["evidence"].get("source_evidence", {}),
            edge["evidence"].get("target_evidence", {}),
        )
        if isinstance(side, dict)
    )
    includes_paths_titles_tags = bool(nodes)
    metadata_only = not includes_passage_text and not includes_note_derived_terms
    if includes_passage_text:
        export_profile = "graph_with_bounded_passage_text"
    elif includes_note_derived_terms:
        export_profile = "graph_metadata_and_note_derived_terms"
    elif includes_paths_titles_tags:
        export_profile = "graph_metadata"
    else:
        export_profile = "empty_graph"
    return {
        "schema_version": VIEWER_SCHEMA_VERSION,
        "title": title or f"RecallWeave graph — {database.stem}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "edges": edges,
        "diagnostics": {"unresolved_links": unresolved},
        "privacy": {
            "export_profile": export_profile,
            "requested_profile": (
                "with_bounded_passage_text"
                if include_excerpts
                else "without_passage_text"
            ),
            "metadata_only": metadata_only,
            "includes_excerpts": includes_passage_text,
            "includes_passage_text": includes_passage_text,
            "includes_note_derived_terms": includes_note_derived_terms,
            "includes_paths_titles_tags": includes_paths_titles_tags,
            "generated_locally": True,
        },
    }


def export_viewer_graph(
    database: Path,
    output: Path,
    *,
    include_candidates: bool = True,
    include_excerpts: bool = False,
    title: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    database = database.expanduser().resolve()
    output = Path(os.path.abspath(output.expanduser()))
    protected_message = "Viewer output cannot replace the RecallWeave database."
    guard = prepare_destination(
        output,
        database,
        force=force,
        label="Viewer output",
        protected_target_message=protected_message,
    )

    document = build_viewer_document(
        database,
        include_candidates=include_candidates,
        include_excerpts=include_excerpts,
        title=title,
    )
    verify_destination(
        output,
        database,
        guard,
        label="Viewer output",
        protected_target_message=protected_message,
    )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    replacement_backup: str | None = None
    try:
        with handle:
            json.dump(document, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        verify_destination(
            output,
            database,
            guard,
            label="Viewer output",
            protected_target_message=protected_message,
        )
        replacement_backup = install(
            temporary,
            output,
            guard,
            label="Viewer output",
            installer=_install_non_replacing,
            install_failed_message=(
                "Viewer export installation failed; the previous output was restored."
            ),
            install_failed_retained_message=(
                "Viewer export installation failed and the previous output could not "
                "be restored without overwriting another file."
            ),
        )
    finally:
        temporary.unlink(missing_ok=True)

    privacy = document["privacy"]
    return {
        "schema_version": VIEWER_SCHEMA_VERSION,
        "operation": "export_viewer",
        "output": str(output),
        "notes": len(document["nodes"]),
        "edges": len(document["edges"]),
        "candidate_edges_requested": include_candidates,
        "candidate_edges_included": any(
            not edge["verified"] for edge in document["edges"]
        ),
        "replacement_mode": (
            "two_phase_recoverable" if guard["output_existed"] else "non_replacing"
        ),
        "replacement_backup": replacement_backup,
        "export_profile": privacy["export_profile"],
        "requested_profile": privacy["requested_profile"],
        "metadata_only": privacy["metadata_only"],
        "excerpts_requested": include_excerpts,
        "excerpts_included": privacy["includes_excerpts"],
        "passage_text_included": privacy["includes_passage_text"],
        "note_derived_terms_included": privacy["includes_note_derived_terms"],
        "paths_titles_tags_included": privacy["includes_paths_titles_tags"],
    }
