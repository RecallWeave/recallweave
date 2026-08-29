from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import connect
from .safe_write import _install_non_replacing, install, prepare_destination, verify_destination


VIEWER_SCHEMA_VERSION = "recallweave.viewer.v2"
MAX_SUMMARY_CHARACTERS = 280
MAX_EVIDENCE_CHARACTERS = 500
_SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
_REQUIRED_PREVIOUS_NODE_FIELDS = ("id", "title", "path")


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


def _nullable_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        text = str(value)
    else:
        text = value
    if text != text.strip():
        return None
    text = text.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            parsed = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        utc = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return None
    return utc.isoformat() + "Z"


def _content_hash(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _edge_evidence(
    raw: str,
    *,
    source_path: str,
    target_path: str,
    include_excerpts: bool,
    mutual_neighbor_ids: list[str] | None = None,
    shared_tags: list[str] | None = None,
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
    lexical_terms: list[str] = []
    if isinstance(shared_terms, list):
        lexical_terms = [
            str(term) for term in shared_terms if isinstance(term, str)
        ][:12]
        evidence["shared_terms"] = lexical_terms
    explanation = parsed.get("explanation")
    if isinstance(explanation, str):
        evidence["explanation"] = _excerpt(explanation, MAX_EVIDENCE_CHARACTERS)

    # Prefer index-derived tag intersection over any producer-claimed list.
    tag_terms = list(shared_tags or [])[:12]
    if not tag_terms:
        claimed = parsed.get("shared_tags")
        if isinstance(claimed, list):
            tag_terms = [str(tag) for tag in claimed if isinstance(tag, str)][:12]

    signals: dict[str, Any] = {}
    if lexical_terms:
        signals["lexical_terms"] = lexical_terms
    if tag_terms:
        signals["shared_tags"] = tag_terms
    if mutual_neighbor_ids:
        signals["mutual_neighbor_ids"] = mutual_neighbor_ids[:12]
    if signals:
        evidence["signals"] = signals

    return evidence


def _mutual_neighbors(
    adjacency: dict[str, set[str]], source: str, target: str
) -> list[str]:
    shared = adjacency.get(source, set()) & adjacency.get(target, set())
    shared.discard(source)
    shared.discard(target)
    return sorted(shared)


def _intersecting_tags(
    tags_by_id: dict[str, list[str]], source: str, target: str
) -> list[str]:
    """Deterministic shared tags from endpoint node tags (index claims)."""
    source_tags = {tag for tag in tags_by_id.get(source, []) if isinstance(tag, str) and tag}
    target_tags = {tag for tag in tags_by_id.get(target, []) if isinstance(tag, str) and tag}
    return sorted(source_tags & target_tags)


def _aggregate_content_digest(nodes: list[dict[str, Any]]) -> str:
    parts = [
        f"{node['id']}:{node.get('content_hash') or ''}"
        for node in sorted(
            (item for item in nodes if isinstance(item.get("id"), str)),
            key=lambda item: str(item["id"]),
        )
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _valid_predecessor_content_hash(value: object) -> bool:
    """Null hashes are legacy-ok; non-null must be lowercase hex SHA-256."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return _SHA256_HEX.fullmatch(value) is not None


def _valid_predecessor_export_history(value: object, *, node_count: int) -> bool:
    if not isinstance(value, dict):
        return False
    required_keys = (
        "export_id",
        "previous_content_hash",
        "node_content_hashes_changed",
        "node_content_hashes_unchanged",
        "nodes_added",
        "nodes_removed",
    )
    if any(key not in value for key in required_keys):
        return False
    export_id = value["export_id"]
    if not isinstance(export_id, str) or not export_id.strip():
        return False
    if not _valid_predecessor_content_hash(value["previous_content_hash"]):
        return False
    for field in (
        "node_content_hashes_changed",
        "node_content_hashes_unchanged",
        "nodes_added",
        "nodes_removed",
    ):
        count = value[field]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
    changed = value["node_content_hashes_changed"]
    unchanged = value["node_content_hashes_unchanged"]
    added = value["nodes_added"]
    removed = value["nodes_removed"]
    prior_hash = value["previous_content_hash"]
    overlap = changed + unchanged
    # Match Atlas claim_conflict: first-export vs subsequent-export accounting.
    if prior_hash is None:
        if added != node_count or overlap != 0 or removed != 0:
            return False
    elif overlap + added != node_count:
        return False
    return True


def _valid_previous_viewer_nodes(
    previous_document: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """Return prior nodes only when the previous document is a usable predecessor.

    Recognized schemas may carry an empty node list (valid empty export). A
    predecessor must include an ``edges`` array; viewer.v2 also requires a
    complete ``export_history`` object. Non-empty node lists must supply unique
    ids plus required title/path strings and schema-appropriate content hashes
    before any history digest is derived.
    """
    if not isinstance(previous_document, dict):
        return None
    schema = previous_document.get("schema_version")
    if schema not in {
        "recallweave.viewer.v1",
        "recallweave.viewer.v2",
    }:
        return None
    raw_nodes = previous_document.get("nodes")
    if not isinstance(raw_nodes, list):
        return None
    edges = previous_document.get("edges")
    if not isinstance(edges, list):
        return None
    if schema == "recallweave.viewer.v2" and not _valid_predecessor_export_history(
        previous_document.get("export_history"),
        node_count=len(raw_nodes),
    ):
        return None
    if not raw_nodes:
        return []

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    require_content_hash_key = schema == "recallweave.viewer.v2"
    for item in raw_nodes:
        if not isinstance(item, dict):
            return None
        for field in _REQUIRED_PREVIOUS_NODE_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                return None
        node_id = str(item["id"])
        if node_id in seen_ids:
            return None
        seen_ids.add(node_id)
        if require_content_hash_key and "content_hash" not in item:
            return None
        if not _valid_predecessor_content_hash(item.get("content_hash")):
            return None
        validated.append(item)
    return validated


def _normalize_vault_label(value: str | None) -> str | None:
    if value is None:
        return None
    label = " ".join(str(value).split()).strip()[:120]
    if not label or label.startswith(".") or ".." in label:
        return None
    if "/" in label or "\\" in label or ":" in label:
        return None
    if "obsidian" in label.casefold():
        return None
    return label


def _export_history(
    previous_document: dict[str, Any] | None, nodes: list[dict[str, Any]]
) -> dict[str, Any]:
    current_hashes = {
        str(node["id"]): node.get("content_hash")
        for node in nodes
        if isinstance(node.get("id"), str)
    }
    prior_nodes = _valid_previous_viewer_nodes(previous_document)
    previous_hashes: dict[str, str | None] = {}
    if prior_nodes is not None:
        for node in prior_nodes:
            previous_hashes[str(node["id"])] = node.get("content_hash")

    added = sum(1 for node_id in current_hashes if node_id not in previous_hashes)
    removed = sum(1 for node_id in previous_hashes if node_id not in current_hashes)
    changed = 0
    unchanged = 0
    for node_id, digest in current_hashes.items():
        if node_id not in previous_hashes:
            continue
        if previous_hashes[node_id] and digest and previous_hashes[node_id] == digest:
            unchanged += 1
        else:
            changed += 1

    previous_digest = (
        _aggregate_content_digest(prior_nodes) if prior_nodes is not None else None
    )

    return {
        "export_id": str(uuid.uuid4()),
        "previous_content_hash": previous_digest,
        "node_content_hashes_changed": changed,
        "node_content_hashes_unchanged": unchanged,
        "nodes_added": added,
        "nodes_removed": removed,
    }


def build_viewer_document(
    database: Path,
    *,
    include_candidates: bool = True,
    include_excerpts: bool = False,
    title: str | None = None,
    vault_name: str | None = None,
    previous_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, browser-safe graph document from a RecallWeave index."""

    with connect(database, readonly=True) as connection:
        note_rows = connection.execute(
            """
            SELECT n.id, n.relative_path, n.title, n.tags_json, n.status, n.domain,
                   n.created_at, n.modified_at, n.content_hash,
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
        policy_row = connection.execute(
            "SELECT value FROM meta WHERE key = 'policy_config_sha256'"
        ).fetchone()

    nodes = []
    tags_by_id: dict[str, list[str]] = {}
    for row in note_rows:
        tags = _json_list(str(row["tags_json"]))
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
            "tags": tags,
            "section_count": int(row["section_count"]),
            "created_at": _nullable_timestamp(row["created_at"]),
            "modified_at": _nullable_timestamp(row["modified_at"]),
            "content_hash": _content_hash(row["content_hash"]),
        }
        nodes.append(node)
        tags_by_id[node["id"]] = tags

    adjacency: dict[str, set[str]] = {node["id"]: set() for node in nodes}
    for row in edge_rows:
        if not bool(row["is_verified"]):
            continue
        source_path = paths[int(row["source_note_id"])]
        target_path = paths[int(row["target_note_id"])]
        adjacency.setdefault(source_path, set()).add(target_path)
        adjacency.setdefault(target_path, set()).add(source_path)

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
                    target_path=target_path,
                    include_excerpts=include_excerpts,
                    mutual_neighbor_ids=_mutual_neighbors(
                        adjacency, source_path, target_path
                    ),
                    shared_tags=_intersecting_tags(
                        tags_by_id, source_path, target_path
                    ),
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
    document: dict[str, Any] = {
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
        "export_history": _export_history(previous_document, nodes),
    }
    if vault_name is not None:
        normalized_vault = _normalize_vault_label(vault_name)
        if normalized_vault is None:
            raise ValueError(
                "vault_name must be a vault label, not a filesystem path or URL fragment."
            )
        document["vault_name"] = normalized_vault
    if policy_row is not None and str(policy_row[0]).strip():
        document["policy_config_sha256"] = str(policy_row[0]).strip()
    return document


def export_viewer_graph(
    database: Path,
    output: Path,
    *,
    include_candidates: bool = True,
    include_excerpts: bool = False,
    title: str | None = None,
    vault_name: str | None = None,
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

    previous_document: dict[str, Any] | None = None
    if guard.get("output_existed") and output.is_file():
        try:
            loaded = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            loaded = None
        if isinstance(loaded, dict) and _valid_previous_viewer_nodes(loaded) is not None:
            previous_document = loaded

    document = build_viewer_document(
        database,
        include_candidates=include_candidates,
        include_excerpts=include_excerpts,
        title=title,
        vault_name=vault_name,
        previous_document=previous_document,
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
