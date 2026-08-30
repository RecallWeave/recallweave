from __future__ import annotations

"""Steward stage B1: deterministic change assessment.

``assess_change_batch`` classifies each observed change (a ``change_batch``
document, produced by a future ``steward_observe`` stage) against the current
RecallWeave index. Every relation it can emit is a byte- or structure-level
fact about the vault and the index: a path is new, a path's bytes changed, a
path disappeared, two paths share identical bytes, a verified (authored) edge
touches a changed note, or a recorded citation no longer matches the text it
once pointed at. None of that is interpretation -- it says nothing about
whether a change confirms, extends, supersedes, or contradicts anything.

INTERPRETIVE_RELATIONS is reserved for a future opt-in InterpretationProvider.
No code path in this module may emit a value from that set. A provider may
only ADD proposal-layer records that reference these deterministic
assessments (by relative_path and relation) -- it may never rewrite, remove,
or reinterpret a deterministic assessment record produced here.

This module never writes to any source or vault file, and it opens the
RecallWeave index read-only (``connect(database, readonly=True)``). Its only
writes are into the steward state directory's ``assessments/`` subdirectory,
via ``atomic_write_json``.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import SCHEMA_VERSION as INDEX_SCHEMA_VERSION
from .parser import parse_note
from .index import connect
from .steward_sources import SourceRegistry
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    lock_state,
)

ASSESS_ASSERTER = "recallweave.steward.assess.v1"
ASSESSMENT_KIND = "assessment_batch"
STANDING_CAVEAT = (
    "Deterministic byte- and structure-level relations only; they do not "
    "judge meaning, support, or truth."
)
DETERMINISTIC_RELATIONS = frozenset(
    {
        "NEW",
        "DELETED",
        "MODIFIED",
        "DUPLICATES_EXACT_BYTES",
        "AUTHORED_REFERENCE_TOUCHED",
        "CITATION_BROKEN",
    }
)
# Reserved for a future opt-in InterpretationProvider. NO code path in this
# module may emit these; a provider may only ADD proposal-layer records that
# reference deterministic assessments, never rewrite them.
INTERPRETIVE_RELATIONS = frozenset(
    {"CONFIRMS", "EXTENDS", "SUPERSEDES", "CONTRADICTS", "UNCERTAIN"}
)

_BATCH_SCHEMA_VERSION = "recallweave.steward.v1"
_BATCH_KIND = "change_batch"
_REQUIRED_BATCH_KEYS = (
    "schema_version",
    "kind",
    "operation",
    "generated_at",
    "source",
    "registry_sha256",
    "changes",
    "rename_candidates",
    "change_summary",
    "skipped",
    "changed_during_observe",
    "network_calls",
    "vault_writes",
)
_CHANGE_TYPES = frozenset({"added", "modified", "removed"})
_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_lines(raw: str) -> list[str]:
    """Split physical lines exactly like parser.parse_note: CRLF, CR, or LF."""

    return _LINE_SPLIT_RE.split(raw)


def _validate_batch(batch: Any) -> None:
    if not isinstance(batch, dict):
        raise ValueError("change_batch must be a JSON object.")
    missing = [key for key in _REQUIRED_BATCH_KEYS if key not in batch]
    if missing:
        raise ValueError(
            f"change_batch is missing required key(s): {', '.join(missing)}"
        )
    if batch["schema_version"] != _BATCH_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported change_batch schema_version {batch['schema_version']!r}; "
            f"expected {_BATCH_SCHEMA_VERSION!r}."
        )
    if batch["kind"] != _BATCH_KIND:
        raise ValueError(
            f"Unsupported change_batch kind {batch['kind']!r}; expected {_BATCH_KIND!r}."
        )
    if not isinstance(batch["changes"], list):
        raise ValueError("change_batch 'changes' must be a list.")


def _extract_paths(items: Any) -> set[str]:
    result: set[str] = set()
    if not isinstance(items, list):
        return result
    for item in items:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict) and isinstance(item.get("relative_path"), str):
            result.add(item["relative_path"])
    return result


def _require_clean_relative_path(path: str) -> None:
    """Reject absolute, traversal, or platform-ambiguous relative paths.

    Change batches are steward-authored, but they are also on-disk state an
    operator (or another tool) can edit; a path like ``../secret.md`` must
    never drive a read outside the source root."""

    if not path or path.startswith("/") or "\\" in path or ":" in path.split("/")[0]:
        raise ValueError(f"Invalid relative path in change record: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError(f"Invalid relative path in change record: {path!r}")


def _record(relation: str, relative_path: str, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "relation": relation,
        "decidability": "deterministic",
        "asserter": ASSESS_ASSERTER,
        "reproducible": True,
        "relative_path": relative_path,
        "inputs": inputs,
        "standing_caveat": STANDING_CAVEAT,
    }


def assess_change_batch(
    batch: dict,
    database: Path,
    source_root: Path,
    *,
    now: str | None = None,
    policy: Any = None,
    expected_source: str | None = None,
    expected_registry_sha256: str | None = None,
    source_is_file: bool = False,
) -> dict:
    """Classify one change_batch document against the RecallWeave index.

    Deterministic only: every relation emitted comes from comparing content
    hashes, section text/line ranges, and verified edges already on record.
    Opens the index read-only; re-reads changed source files under
    ``source_root`` for citation checking, but never writes to them.

    For a ``type: "file"`` source, ``source_root`` is the file itself and the
    observed relative path is its filename, so the directory that bounds path
    resolution is the file's PARENT. Pass ``source_is_file=True`` so that
    ``<parent>/<filename>`` is read, rather than ``<file>/<filename>`` (which
    would falsely report every section as an unreadable citation and starve the
    proposal compiler of referrer text).
    """

    _validate_batch(batch)
    database = Path(database)
    source_root = Path(source_root)
    path_base = source_root.parent if source_is_file else source_root
    generated_at = now if now is not None else _utc_now()

    if expected_source is not None and batch.get("source") != expected_source:
        raise ValueError(
            f"Change batch claims source {batch.get('source')!r} but is being "
            f"assessed for source {expected_source!r}; refusing a cross-source "
            "assessment. Re-run steward-observe for this registry."
        )
    if (
        expected_registry_sha256 is not None
        and batch.get("registry_sha256") != expected_registry_sha256
    ):
        # A null digest fails closed too: an edited or legacy batch must not
        # bypass binding to the current registry.
        raise ValueError(
            "Change batch was recorded under a different source registry "
            "(registry_sha256 mismatch); re-run steward-observe with the "
            "current registry before assessing."
        )

    def _path_admitted(relative: str) -> bool:
        # Project every index-derived path through the FULL active source
        # policy -- path rules, size cap, and frontmatter denial -- so an
        # index built over a broader corpus cannot leak excluded paths into
        # emitted assessments. Anything whose eligibility cannot be
        # affirmatively established is redacted (fail closed).
        if policy is None:
            return True
        try:
            _require_clean_relative_path(relative)
        except ValueError:
            return False
        full = path_base / relative
        try:
            resolved = full.resolve(strict=True)
        except OSError:
            return False
        resolved_source = path_base.resolve()
        if not (
            resolved == resolved_source or resolved_source in resolved.parents
        ):
            return False
        try:
            size = full.stat().st_size
        except OSError:
            return False
        allowed, _reason = policy.path_allowed(relative, size)
        if not allowed:
            return False
        if policy.deny_frontmatter:
            try:
                note = parse_note(full, path_base)
            except (UnicodeError, RecursionError, OSError):
                return False
            allowed, _reason = policy.frontmatter_allowed(
                note.frontmatter, valid=note.frontmatter_valid
            )
            if not allowed:
                return False
        return True

    redacted_out_of_policy = 0

    connection = connect(database, readonly=True)
    try:
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }
        index_schema_version = meta.get("schema_version")
        if index_schema_version != INDEX_SCHEMA_VERSION:
            raise ValueError(
                f"Index schema_version {index_schema_version!r} is not "
                f"{INDEX_SCHEMA_VERSION!r}; re-index the vault with "
                "'recallweave index' before running steward-assess."
            )
        indexed_at = meta.get("indexed_at")

        notes_by_path: dict[str, dict[str, Any]] = {}
        notes_by_id: dict[int, str] = {}
        for row in connection.execute("SELECT id, relative_path, content_hash FROM notes"):
            note_id = int(row["id"])
            path = str(row["relative_path"])
            notes_by_path[path] = {
                "id": note_id,
                "content_hash": str(row["content_hash"]),
            }
            notes_by_id[note_id] = path

        skip_paths = _extract_paths(batch.get("changed_during_observe"))

        assessments: list[dict[str, Any]] = []
        summary: dict[str, int] = {
            "index_current": 0,
            "never_indexed": 0,
            "skipped_changed_during_observe": 0,
        }
        for relation in DETERMINISTIC_RELATIONS:
            summary[relation] = 0

        content_drifted: list[str] = []
        baseline_divergence: list[str] = []
        added_or_modified: list[dict[str, Any]] = []
        touched_note_ids: dict[str, int] = {}

        for change in batch["changes"]:
            path = change.get("relative_path")
            change_type = change.get("change_type")
            if not isinstance(path, str) or change_type not in _CHANGE_TYPES:
                raise ValueError(f"Invalid change record: {change!r}")
            _require_clean_relative_path(path)
            if path in skip_paths:
                summary["skipped_changed_during_observe"] += 1
                continue

            current_hash = change.get("current_content_hash")
            previous_hash = change.get("previous_content_hash")
            index_row = notes_by_path.get(path)

            if change_type in ("added", "modified"):
                added_or_modified.append(change)
                if index_row is None:
                    assessments.append(
                        _record(
                            "NEW",
                            path,
                            {
                                "current_content_hash": current_hash,
                                "previous_content_hash": previous_hash,
                                "index_content_hash": None,
                                "index_indexed_at": indexed_at,
                            },
                        )
                    )
                elif current_hash == index_row["content_hash"]:
                    summary["index_current"] += 1
                else:
                    if previous_hash != index_row["content_hash"]:
                        baseline_divergence.append(path)
                    assessments.append(
                        _record(
                            "MODIFIED",
                            path,
                            {
                                "current_content_hash": current_hash,
                                "previous_content_hash": previous_hash,
                                "index_content_hash": index_row["content_hash"],
                                "index_indexed_at": indexed_at,
                            },
                        )
                    )
                    touched_note_ids[path] = index_row["id"]
            else:  # removed
                if index_row is None:
                    summary["never_indexed"] += 1
                else:
                    assessments.append(
                        _record(
                            "DELETED",
                            path,
                            {
                                "current_content_hash": None,
                                "previous_content_hash": previous_hash,
                                "index_content_hash": index_row["content_hash"],
                                "index_indexed_at": indexed_at,
                            },
                        )
                    )
                    touched_note_ids[path] = index_row["id"]

        # DUPLICATES_EXACT_BYTES: compared across every added/modified change
        # in this batch, regardless of whether it produced a NEW/MODIFIED
        # relation above (an index-current path can still coincide in bytes
        # with a different indexed path, which is worth flagging).
        # Index every added OR modified record by its current hash. If two
        # existing notes are modified in the same observation to identical new
        # bytes (and neither new hash is in the index yet), both must appear in
        # duplicate_in_batch; keying on "added" alone would silently drop the
        # deterministic DUPLICATES_EXACT_BYTES finding for that pair.
        added_hashes: dict[str, list[str]] = {}
        for change in added_or_modified:
            current_hash = change.get("current_content_hash")
            if current_hash is not None:
                added_hashes.setdefault(current_hash, []).append(
                    change["relative_path"]
                )

        for change in added_or_modified:
            path = change["relative_path"]
            current_hash = change.get("current_content_hash")
            if current_hash is None:
                continue
            duplicate_matches = [
                other_path
                for other_path, info in notes_by_path.items()
                if other_path != path and info["content_hash"] == current_hash
            ]
            duplicate_of = sorted(
                other_path
                for other_path in duplicate_matches
                if _path_admitted(other_path)
            )
            redacted_out_of_policy += len(duplicate_matches) - len(duplicate_of)
            duplicate_in_batch = sorted(
                {
                    other_path
                    for other_path in added_hashes.get(current_hash, [])
                    if other_path != path
                }
            )
            if duplicate_of or duplicate_in_batch:
                index_row = notes_by_path.get(path)
                assessments.append(
                    _record(
                        "DUPLICATES_EXACT_BYTES",
                        path,
                        {
                            "current_content_hash": current_hash,
                            "previous_content_hash": change.get("previous_content_hash"),
                            "index_content_hash": index_row["content_hash"]
                            if index_row is not None
                            else None,
                            "index_indexed_at": indexed_at,
                            "duplicate_of": duplicate_of,
                            "duplicate_in_batch": duplicate_in_batch,
                        },
                    )
                )

        # AUTHORED_REFERENCE_TOUCHED: verified edges touching a deleted or
        # modified note, exposed only as the other note's path.
        for path, note_id in sorted(touched_note_ids.items()):
            rows = connection.execute(
                """
                SELECT source_note_id, target_note_id, kind
                FROM edges
                WHERE is_verified = 1 AND (source_note_id = ? OR target_note_id = ?)
                """,
                (note_id, note_id),
            ).fetchall()
            authored_edges: list[dict[str, str]] = []
            for row in rows:
                source_id = int(row["source_note_id"])
                target_id = int(row["target_note_id"])
                if source_id == note_id:
                    other_id, direction = target_id, "outbound"
                else:
                    other_id, direction = source_id, "inbound"
                other_path = notes_by_id.get(other_id)
                if other_path is None:
                    continue
                if not _path_admitted(other_path):
                    redacted_out_of_policy += 1
                    continue
                authored_edges.append(
                    {
                        "other_path": other_path,
                        "direction": direction,
                        "kind": str(row["kind"]),
                    }
                )
            authored_edges.sort(
                key=lambda item: (item["other_path"], item["direction"], item["kind"])
            )
            if authored_edges:
                index_row = notes_by_path.get(path)
                assessments.append(
                    _record(
                        "AUTHORED_REFERENCE_TOUCHED",
                        path,
                        {
                            "index_content_hash": index_row["content_hash"]
                            if index_row is not None
                            else None,
                            "index_indexed_at": indexed_at,
                            "authored_edges": authored_edges,
                        },
                    )
                )

        # CITATION_BROKEN
        for change in batch["changes"]:
            path = change.get("relative_path")
            if not isinstance(path, str) or path in skip_paths:
                continue
            change_type = change.get("change_type")
            index_row = notes_by_path.get(path)
            if index_row is None:
                continue

            if change_type == "removed":
                sections = connection.execute(
                    "SELECT heading, line_start, line_end, text FROM sections "
                    "WHERE note_id = ? ORDER BY line_start",
                    (index_row["id"],),
                ).fetchall()
                broken = [
                    {
                        "citation": f"{path}:{row['line_start']}-{row['line_end']}",
                        "heading": str(row["heading"]),
                        "reason": "note_deleted",
                    }
                    for row in sections
                ]
                if broken:
                    assessments.append(
                        _record(
                            "CITATION_BROKEN",
                            path,
                            {
                                "current_content_hash": None,
                                "previous_content_hash": change.get(
                                    "previous_content_hash"
                                ),
                                "index_content_hash": index_row["content_hash"],
                                "index_indexed_at": indexed_at,
                                "broken_citations": broken,
                            },
                        )
                    )
                continue

            if change_type != "modified":
                continue
            current_hash = change.get("current_content_hash")
            if current_hash == index_row["content_hash"]:
                continue  # index-current: nothing changed to check
            sections = connection.execute(
                "SELECT heading, line_start, line_end, text FROM sections "
                "WHERE note_id = ? ORDER BY line_start",
                (index_row["id"],),
            ).fetchall()
            if not sections:
                continue

            full_path = path_base / path
            try:
                resolved_target = full_path.resolve(strict=True)
            except OSError:
                resolved_target = None
            resolved_source = path_base.resolve()
            if resolved_target is not None and not (
                resolved_target == resolved_source
                or resolved_source in resolved_target.parents
            ):
                raise ValueError(
                    f"Change record path escapes the source root: {path!r}"
                )
            try:
                data = full_path.read_bytes()
            except OSError as error:
                broken = [
                    {
                        "citation": f"{path}:{row['line_start']}-{row['line_end']}",
                        "heading": str(row["heading"]),
                        "reason": "unreadable",
                        "error_type": type(error).__name__,
                    }
                    for row in sections
                ]
                assessments.append(
                    _record(
                        "CITATION_BROKEN",
                        path,
                        {
                            "current_content_hash": current_hash,
                            "previous_content_hash": change.get("previous_content_hash"),
                            "index_content_hash": index_row["content_hash"],
                            "index_indexed_at": indexed_at,
                            "broken_citations": broken,
                        },
                    )
                )
                continue

            actual_hash = hashlib.sha256(data).hexdigest()
            if actual_hash != current_hash:
                # Fail closed: the batch's claimed current bytes no longer
                # match what is on disk. Do not assess citations; the operator
                # must re-observe.
                content_drifted.append(path)
                continue

            try:
                raw = data.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError as error:
                broken = [
                    {
                        "citation": f"{path}:{row['line_start']}-{row['line_end']}",
                        "heading": str(row["heading"]),
                        "reason": "unreadable",
                        "error_type": type(error).__name__,
                    }
                    for row in sections
                ]
                assessments.append(
                    _record(
                        "CITATION_BROKEN",
                        path,
                        {
                            "current_content_hash": current_hash,
                            "previous_content_hash": change.get("previous_content_hash"),
                            "index_content_hash": index_row["content_hash"],
                            "index_indexed_at": indexed_at,
                            "broken_citations": broken,
                        },
                    )
                )
                continue

            lines = _split_lines(raw)
            broken = []
            for row in sections:
                line_start = int(row["line_start"])
                line_end = int(row["line_end"])
                if line_start < 1 or line_end > len(lines) or line_start > line_end:
                    broken.append(
                        {
                            "citation": f"{path}:{line_start}-{line_end}",
                            "heading": str(row["heading"]),
                            "reason": "range_mismatch",
                        }
                    )
                    continue
                span = "\n".join(lines[line_start - 1 : line_end])
                if span != str(row["text"]):
                    broken.append(
                        {
                            "citation": f"{path}:{line_start}-{line_end}",
                            "heading": str(row["heading"]),
                            "reason": "range_mismatch",
                        }
                    )
            if broken:
                assessments.append(
                    _record(
                        "CITATION_BROKEN",
                        path,
                        {
                            "current_content_hash": current_hash,
                            "previous_content_hash": change.get("previous_content_hash"),
                            "index_content_hash": index_row["content_hash"],
                            "index_indexed_at": indexed_at,
                            "broken_citations": broken,
                        },
                    )
                )

        assessments.sort(key=lambda item: (item["relative_path"], item["relation"]))
        for item in assessments:
            summary[item["relation"]] = summary.get(item["relation"], 0) + 1
        summary["redacted_out_of_policy"] = redacted_out_of_policy

        # The assessment document is an INTERNAL pipeline artifact that
        # propose_from_assessment reads in full to compile edits (like the
        # change batch, it is not a bounded human-facing read-output). It must
        # therefore stay complete: truncating it here would permanently drop the
        # proposals for the omitted records. Presentation bounding lives in the
        # sweep report projection instead.
        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": ASSESSMENT_KIND,
            "operation": "steward_assess",
            "generated_at": generated_at,
            "source": batch.get("source"),
            "registry_sha256": batch.get("registry_sha256"),
            "change_batch_ref": None,
            "change_batch_sha256": None,
            "index": {
                "indexed_at": indexed_at,
                "schema_version": index_schema_version,
                # Persisted artifacts stay machine-local: filename only, never
                # an absolute path.
                "database": database.name,
            },
            "assessments": assessments,
            # Every path this batch reassessed, INCLUDING ones that produced no
            # deterministic relation (e.g. a modified note restored byte-for-byte
            # to the index -> index_current, or a never-indexed NEW note later
            # removed). The report uses this to CLEAR a path's prior finding when
            # a later batch reassessed it to nothing; without it a resolved
            # finding would persist forever.
            "covered_paths": sorted(
                {
                    change.get("relative_path")
                    for change in batch.get("changes") or []
                    if isinstance(change.get("relative_path"), str)
                }
            ),
            "summary": summary,
            "content_drifted": sorted(set(content_drifted)),
            "baseline_divergence": sorted(set(baseline_divergence)),
            "network_calls": 0,
            "vault_writes": 0,
        }
    finally:
        connection.close()


def _source_name_from_batch_filename(name: str) -> str | None:
    """Recover the exact source name from a ``<ts>-<source>.json`` batch name.

    The timestamp component (``%Y%m%dT%H%M%S%fZ``) contains no hyphen, so the
    source name -- which may itself contain hyphens -- is everything after the
    first hyphen of the stem. Returns ``None`` if the name has no hyphen."""

    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    _, sep, source = stem.partition("-")
    return source if sep else None


def assess_latest(registry: SourceRegistry, state_root: Path, database: Path) -> dict:
    """For each registered source, assess every not-yet-assessed batch.

    "Not yet assessed" is bound by content digest: a source's
    ``changes/<ts>-<source>.json`` batches are each assessed unless an
    ``assessments/<ts>-<source>.json`` already records that exact batch digest.
    Every unassessed batch is processed in timestamp order (not just the
    newest), so a change observed before an unchanged run is never skipped.
    Runs under the steward state lock, since it both reads and writes the
    steward state directory.
    """

    state_root = Path(state_root)
    database = Path(database)
    generated_at = _utc_now()

    # The RecallWeave index is single-vault and, by design, stores no absolute
    # root (only relative paths + content hashes), so it cannot attribute a note
    # to a source. Assessing more than one source against the same shared index
    # would classify one source's `Note.md` against another source's hash, and
    # let it inherit that source's authored edges and citations -- producing
    # false MODIFIED / AUTHORED_REFERENCE_TOUCHED / proposal records. Until the
    # index can identify sources, a multi-source registry must be assessed with
    # a per-source index; refuse the unsound shared-index case rather than
    # silently cross-contaminate. (See steward_assess finding: scope note
    # lookups to their source.)
    if len(registry.sources) > 1:
        raise ValueError(
            "steward-assess cannot assess a registry with more than one source "
            "against a single shared index: the index cannot attribute notes to "
            "a source, so classifications would cross-contaminate. Assess each "
            "source against its own index (one source per registry for now)."
        )

    ensure_state_root_outside_sources(
        state_root, [source.root for source in registry.sources]
    )

    dirs = ensure_state_layout(state_root)
    with lock_state(state_root):
        changes_dir = dirs["changes"]
        assessments_dir = dirs["assessments"]

        assessed: list[dict[str, Any]] = []
        skipped_sources: list[dict[str, Any]] = []

        for source in registry.sources:
            # Match the source's batches by the EXACT recorded name, not a glob
            # suffix: `*-a.json` also matches `<ts>-x-a.json` (source "x-a"),
            # which would pull another source's batch into "a"'s assessment and,
            # once assess_change_batch rejected the cross-source artifact, block
            # the whole run. The timestamp component carries no hyphen, so the
            # name is exactly what follows the first hyphen of the stem.
            batches = sorted(
                path
                for path in changes_dir.glob(f"*-{source.name}.json")
                if _source_name_from_batch_filename(path.name) == source.name
            )
            if not batches:
                skipped_sources.append(
                    {"source": source.name, "reason": "no_change_batch"}
                )
                continue
            # Process EVERY not-yet-assessed batch in timestamp order, not just
            # the newest: if observation ran twice before assessment, taking only
            # batches[-1] would permanently skip the earlier batch (e.g. a
            # deletion recorded in one observation, then an unchanged observation
            # producing an empty newest batch -- the deletion would be lost).
            assessed_any = False
            all_already_assessed = True
            for batch_path in batches:
                batch_bytes = batch_path.read_bytes()
                batch_sha256 = hashlib.sha256(batch_bytes).hexdigest()
                assessment_path = assessments_dir / batch_path.name
                if assessment_path.exists():
                    # Bind by batch content digest, not filename existence: a
                    # rewritten batch under a colliding name must be re-assessed.
                    try:
                        existing = json.loads(
                            assessment_path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        existing = None
                    if (
                        isinstance(existing, dict)
                        and existing.get("change_batch_sha256") == batch_sha256
                    ):
                        continue
                all_already_assessed = False
                try:
                    batch = json.loads(batch_bytes.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as error:
                    raise ValueError(
                        f"Change batch {batch_path.name} is not valid JSON: {error}"
                    ) from error
                result = assess_change_batch(
                    batch,
                    database,
                    source.root,
                    policy=source.policy,
                    expected_source=source.name,
                    expected_registry_sha256=registry.registry_sha256,
                    source_is_file=(source.type == "file"),
                )
                result["change_batch_ref"] = batch_path.name
                result["change_batch_sha256"] = batch_sha256
                atomic_write_json(assessment_path, result, within=assessments_dir)
                assessed.append(
                    {
                        "source": source.name,
                        "change_batch_ref": batch_path.name,
                        "assessment_ref": assessment_path.name,
                        "summary": result["summary"],
                    }
                )
                assessed_any = True
            if not assessed_any and all_already_assessed:
                skipped_sources.append(
                    {"source": source.name, "reason": "already_assessed"}
                )

        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "steward_assess_receipt",
            "operation": "steward_assess",
            "generated_at": generated_at,
            "assessed": assessed,
            "skipped_sources": skipped_sources,
            "network_calls": 0,
            "vault_writes": 0,
        }
