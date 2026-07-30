from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import connect


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


def _is_link_like(path: Path) -> bool:
    """Return True for symlinks and Windows junction-style reparse points."""

    try:
        info = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_tag = getattr(info, "st_reparse_tag", 0)
    link_tags = {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -2),
    }
    return reparse_tag in link_tags


def _validate_output_path(output: Path, database: Path) -> None:
    if _is_link_like(output):
        raise ValueError(f"Refusing to replace a symlink or junction: {output}")

    current = Path(output.anchor)
    for part in output.parent.parts[1:]:
        current /= part
        if _is_link_like(current):
            raise ValueError(
                f"Refusing viewer output through a symlinked parent: {current}"
            )

    if output.exists() and os.path.samefile(output, database):
        raise ValueError("Viewer output cannot replace the RecallWeave database.")


def _path_identity(path: Path) -> tuple[int, int]:
    info = path.stat(follow_symlinks=False)
    return int(info.st_dev), int(info.st_ino)


def _prepare_destination(
    output: Path,
    database: Path,
    *,
    force: bool,
) -> dict[str, Any]:
    _validate_output_path(output, database)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as error:
        raise ValueError(
            f"Viewer output parent is not a directory: {output.parent}"
        ) from error
    _validate_output_path(output, database)
    if not output.parent.is_dir():
        raise ValueError(f"Viewer output parent is not a directory: {output.parent}")

    output_existed = output.exists()
    if output_existed and not force:
        raise ValueError(
            f"Viewer output already exists: {output}. Pass --force to replace it."
        )
    return {
        "parent_identity": _path_identity(output.parent),
        "output_existed": output_existed,
        "output_identity": _path_identity(output) if output_existed else None,
    }


def _verify_destination(
    output: Path,
    database: Path,
    guard: dict[str, Any],
) -> None:
    _validate_output_path(output, database)
    try:
        parent_identity = _path_identity(output.parent)
    except FileNotFoundError as error:
        raise ValueError(
            f"Viewer output parent changed during export: {output.parent}"
        ) from error
    if parent_identity != guard["parent_identity"]:
        raise ValueError(
            f"Viewer output parent changed during export: {output.parent}"
        )

    if guard["output_existed"]:
        if not output.exists():
            raise ValueError(f"Viewer output changed during export: {output}")
        if _path_identity(output) != guard["output_identity"]:
            raise ValueError(f"Viewer output changed during export: {output}")
    elif output.exists() or _is_link_like(output):
        raise ValueError(
            f"Viewer output appeared during export and was not replaced: {output}"
        )


def _install_non_replacing(source: Path, destination: Path) -> None:
    """Move source into an absent destination without a replace window."""

    if os.name == "nt":
        # Windows rename is atomic and refuses an existing target.
        os.rename(source, destination)
    else:
        # POSIX rename replaces, so install with an exclusive hard link.
        os.link(source, destination)
        source.unlink()


def _restore_backup(backup: Path, output: Path) -> bool:
    """Restore without overwriting a late arrival; retain backup on failure."""

    if output.exists() or _is_link_like(output):
        return False
    try:
        _install_non_replacing(backup, output)
    except OSError:
        return False
    return True


def _replace_recoverably(
    temporary: Path,
    output: Path,
    expected_identity: tuple[int, int],
    expected_parent_identity: tuple[int, int],
) -> str:
    """Two-phase force replacement retaining every unapproved late arrival."""

    backup_directory = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.backup.", dir=output.parent)
    )
    backup = backup_directory / output.name
    if _path_identity(output.parent) != expected_parent_identity:
        try:
            backup_directory.rmdir()
        except OSError:
            pass
        raise ValueError(
            f"Viewer output parent changed during final replacement: {output.parent}"
        )
    try:
        os.rename(output, backup)
    except OSError:
        try:
            backup_directory.rmdir()
        except OSError:
            pass
        raise

    rotated_identity = _path_identity(backup)
    parent_changed = _path_identity(output.parent) != expected_parent_identity
    if rotated_identity != expected_identity or parent_changed:
        restored = _restore_backup(backup, output)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            raise ValueError(
                "Viewer output or parent changed during final replacement; "
                "the rotated file was restored and no export was installed."
            )
        raise ValueError(
            "Viewer output or parent changed during final replacement. "
            f"Backup retained at: {backup}"
        )

    if _path_identity(output.parent) != expected_parent_identity:
        restored = _restore_backup(backup, output)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            raise ValueError(
                "Viewer output parent changed during final replacement; "
                "the previous output was restored."
            )
        raise ValueError(
            "Viewer output parent changed during final replacement. "
            f"Backup retained at: {backup}"
        )

    try:
        _install_non_replacing(temporary, output)
    except OSError as error:
        restored = _restore_backup(backup, output)
        if restored:
            try:
                backup_directory.rmdir()
            except OSError:
                pass
            raise ValueError(
                "Viewer export installation failed; the previous output was restored."
            ) from error
        raise ValueError(
            "Viewer export installation failed and the previous output could not "
            f"be restored without overwriting another file. Backup retained at: {backup}"
        ) from error

    # Deliberately retain the approved old output. There is no cross-platform
    # compare-and-delete primitive that can prove this path was not swapped
    # between an identity check and unlink. Cleanup is therefore user-directed.
    return str(backup)


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
    guard = _prepare_destination(output, database, force=force)

    document = build_viewer_document(
        database,
        include_candidates=include_candidates,
        include_excerpts=include_excerpts,
        title=title,
    )
    _verify_destination(output, database, guard)
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
        _verify_destination(output, database, guard)
        if guard["output_existed"]:
            replacement_backup = _replace_recoverably(
                temporary,
                output,
                guard["output_identity"],
                guard["parent_identity"],
            )
        else:
            try:
                _install_non_replacing(temporary, output)
            except FileExistsError as error:
                raise ValueError(
                    f"Viewer output appeared during export and was not replaced: {output}"
                ) from error
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
