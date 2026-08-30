from __future__ import annotations

"""Steward stage C+D1: read-only proposals with compiled, hash-pinned edits.

``propose_from_assessment`` turns one operator-actionable deterministic
assessment (produced by ``steward_assess``) into a list of *proposal*
documents. A proposal is a reviewable JSON record written into the steward
state directory's ``proposals/`` subdirectory -- it never writes to any
source or vault file, and every proposal carries ``policy_level ==
"propose_only"``. Apply (actually mutating a vault file) does not exist yet
in this milestone; nothing in this module, or in the documents it produces,
authorizes an apply step. Technical determinism never implies authorization.

Proposals v1 (this module) generates proposals for exactly four
assessment-derived situations, all structurally decidable from the index and
the change batch alone:

1. A ``DELETED`` note that is part of a clean rename (the change batch's
   ``rename_candidates`` has a unique content-hash pairing -- one
   ``added_path``): a compiled ``fix_unresolved_link`` edit is produced for
   every referrer whose authored wikilink/markdown link can be rewritten
   unambiguously, hash-pinned to the referrer's current on-disk bytes.
2. A ``DELETED`` note that still has inbound authored references but is
   *not* a clean rename (no rename candidate, an ambiguous one, or a rename
   candidate whose referrers could not be resolved to a compiled edit): an
   advisory ``review_dangling_references`` proposal (``edits: []``) listing
   the dangling referrers. Target resolution for a deleted note is not
   unique/decidable, so no automatic link rewrite is proposed.
3. A ``CITATION_BROKEN`` relation with reason ``range_mismatch`` or
   ``note_deleted``: an advisory ``review_broken_citations`` proposal.
   ``unreadable`` citations are left for a future generation (replacement
   text is not decidable from a byte-level fact alone).
4. A ``DUPLICATES_EXACT_BYTES`` relation: an advisory ``review_duplicates``
   proposal. Which copy is canonical is an operator decision.

Full generality for the closed edit-shape set (``create_new_file``,
``append_at_eof``, ``replace_whole_section``, ``move_to_trash``) arrives with
Apply in a later milestone; the document schema already supports arbitrary
mutation classes in ``edits``, but this generator only ever emits
``fix_unresolved_link`` edits, and only for case 1 above.

This module opens the RecallWeave index read-only and re-reads referrer
source files under ``source_root`` purely to verify hashes and locate the
exact link text to rewrite; it never writes to either. Its only writes (via
``propose_latest``) are into the steward state directory's ``proposals/``
subdirectory, using ``atomic_write_json``.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import connect
from .parser import MARKDOWN_LINK_RE, WIKILINK_RE, _markdown_target, normalize_name
from .steward_assess import ASSESSMENT_KIND
from .steward_policy import MUTATION_CLASSES_SET, PRINCIPAL_KEY_NAMES
from .steward_sources import SourceRegistry
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    lock_state,
)

PROPOSE_ASSERTER = "recallweave.steward.propose.v1"
PROPOSAL_KIND = "proposal"
POLICY_LEVEL = "propose_only"

ACTIONS = frozenset(
    {
        "fix_links_after_rename",
        "review_dangling_references",
        "review_broken_citations",
        "review_duplicates",
    }
)

_DECIDABLE_CITATION_REASONS = frozenset({"range_mismatch", "note_deleted"})
_LINE_BOUNDARY_RE = re.compile(r"\r\n|\r|\n")
_UTF8_BOM = b"\xef\xbb\xbf"

# Fields whose string value is a legitimate absolute local path (the on-disk
# RecallWeave index), not a vault-relative source reference. Every other
# string in a proposal document must be relative and remote-free.
_ABSOLUTE_PATH_ALLOWED_KEYS = frozenset({"database"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_assessment(assessment: Any) -> None:
    if not isinstance(assessment, dict):
        raise ValueError("assessment must be a JSON object.")
    if assessment.get("kind") != ASSESSMENT_KIND:
        raise ValueError(
            f"Unsupported assessment kind {assessment.get('kind')!r}; "
            f"expected {ASSESSMENT_KIND!r}."
        )
    if not isinstance(assessment.get("assessments"), list):
        raise ValueError("assessment 'assessments' must be a list.")


def _validate_machine_local(value: Any, *, key: str | None = None) -> None:
    """Refuse absolute source paths, identity keys, and remote values.

    ``key == "database"`` is the one field allowed to hold an absolute local
    path (the RecallWeave index itself is machine-local metadata, not a
    vault-relative source reference)."""

    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key in PRINCIPAL_KEY_NAMES:
                raise ValueError(
                    f"Identity-like key {item_key!r} is not allowed in a steward proposal."
                )
            _validate_machine_local(item_value, key=item_key)
    elif isinstance(value, list):
        for item in value:
            _validate_machine_local(item, key=key)
    elif isinstance(value, str):
        if "://" in value:
            raise ValueError(
                f"Remote or URL value is not allowed in a steward proposal: {value!r}"
            )
        if key not in _ABSOLUTE_PATH_ALLOWED_KEYS and Path(value).is_absolute():
            raise ValueError(
                f"Absolute source path is not allowed in a steward proposal: {value!r}"
            )


def _proposal_id(
    source: Any, relation: str, relative_path: str, hash_like_values: list[str], action: str
) -> str:
    parts = [str(source), relation, relative_path, action, *[str(v) for v in hash_like_values]]
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"prp-{digest[:16]}"


def _split_lines_keepends(raw: str) -> list[tuple[str, str]]:
    """Split physical lines like parser.parse_note, but keep each ending.

    Mirrors ``_LINE_SPLIT_RE.split`` (CRLF, CR, or LF) for line *numbering*
    (so line numbers agree with everything else in the index), while
    preserving exact bytes elsewhere in the file for a faithful rebuild."""

    pieces: list[tuple[str, str]] = []
    pos = 0
    for match in _LINE_BOUNDARY_RE.finditer(raw):
        pieces.append((raw[pos : match.start()], match.group(0)))
        pos = match.end()
    pieces.append((raw[pos:], ""))
    return pieces


def _rebuild_bytes(
    pieces: list[tuple[str, str]], index: int, new_text: str, original_data: bytes
) -> bytes:
    updated = list(pieces)
    _, ending = updated[index]
    updated[index] = (new_text, ending)
    new_raw = "".join(text + ending for text, ending in updated)
    if original_data.startswith(_UTF8_BOM):
        return _UTF8_BOM + new_raw.encode("utf-8")
    return new_raw.encode("utf-8")


def _source_name_from_artifact_filename(name: str) -> str | None:
    """Recover the exact source name from a ``<ts>-<source>.json`` artifact name.

    The timestamp component contains no hyphen, so the source name (which may
    itself contain hyphens) is everything after the first hyphen of the stem."""

    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    _, sep, source = stem.partition("-")
    return source if sep else None


def _dedup_skipped(skipped: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse duplicate skip records for the same referrer (a referrer with
    both a wikilink and a markdown-link edge is scanned once per edge) and
    return them in a stable order."""

    seen: dict[str, dict[str, str]] = {}
    for record in skipped:
        seen.setdefault(record["relative_path"], record)
    return sorted(seen.values(), key=lambda item: item["relative_path"])


def _count_rename_occurrences(raw: str, old_path: str) -> int:
    """Count every wikilink or markdown link in ``raw`` that targets ``old_path``.

    WIKILINK_RE's group 1 is already the bare target (anchor and alias
    stripped); a markdown link matches when its path component (before any
    ``#`` fragment) equals ``old_path``."""

    old_stem = normalize_name(Path(old_path).stem)
    total = 0
    for line_text, _ending in _split_lines_keepends(raw):
        for match in WIKILINK_RE.finditer(line_text):
            if normalize_name(match.group(1).strip()) == old_stem:
                total += 1
        for match in MARKDOWN_LINK_RE.finditer(line_text):
            if _markdown_target(match.group(1)).split("#", 1)[0] == old_path:
                total += 1
    return total


def _resolve_rename_edit(
    referrer_path: str,
    kind: str,
    evidence: dict[str, Any],
    old_path: str,
    new_path: str,
    source_root: Path,
    notes_hash_by_path: dict[str, str],
) -> tuple[str, Any]:
    """Try to compile one fix_unresolved_link edit for a single authored edge.

    Returns ("ok", edit), ("drift", None) when the referrer's on-disk bytes
    no longer match the index, or ("skip", reason) for anything else that
    keeps this one edge from being safely, unambiguously rewritable."""

    expected_hash = notes_hash_by_path.get(referrer_path)
    if expected_hash is None:
        return ("skip", "referrer_not_in_index")

    full_path = source_root / referrer_path
    try:
        data = full_path.read_bytes()
    except OSError:
        return ("drift", None)

    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        return ("drift", None)

    try:
        raw = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return ("skip", "undecodable")

    # The index records at most one edge per (referrer, target, kind), so it
    # cannot see a referrer that links to the renamed note more than once (on
    # another line, or via both a wikilink and a markdown link). Rewriting only
    # the recorded occurrence would leave the others dangling, and emitting a
    # second same-file edit would fail this file's precondition at apply time.
    # Re-scan the whole hash-pinned referrer: unless there is exactly one
    # occurrence to rewrite, refuse to compile a partial edit and hand the
    # referrer to the operator for manual review instead.
    if _count_rename_occurrences(raw, old_path) != 1:
        return ("skip", "multiple_occurrences_manual_review")

    try:
        line_no = int(evidence.get("line", 0))
    except (TypeError, ValueError):
        return ("skip", "invalid_evidence_line")
    target_text = str(evidence.get("target_text", ""))

    pieces = _split_lines_keepends(raw)
    if line_no < 1 or line_no > len(pieces):
        return ("skip", "line_out_of_range")
    line_text, _ending = pieces[line_no - 1]

    if kind == "wikilink":
        matches = [
            match
            for match in WIKILINK_RE.finditer(line_text)
            if match.group(1).strip() == target_text
        ]
        if len(matches) != 1:
            return ("skip", "link_not_found_verbatim")
        if normalize_name(target_text) != normalize_name(Path(old_path).stem):
            return ("skip", "target_not_filename_form")
        match = matches[0]
        new_stem = Path(new_path).stem
        m_start, m_end = match.span(0)
        g1_start, g1_end = match.span(1)
        old_text = line_text[m_start:m_end]
        replacement_text = line_text[m_start:g1_start] + new_stem + line_text[g1_end:m_end]
    elif kind == "markdown_link":
        matches = [
            match
            for match in MARKDOWN_LINK_RE.finditer(line_text)
            if _markdown_target(match.group(1)) == target_text
        ]
        if len(matches) != 1:
            return ("skip", "link_not_found_verbatim")
        target_path_part = target_text.split("#", 1)[0]
        if target_path_part != old_path:
            return ("skip", "target_not_path_form")
        match = matches[0]
        if match.group(1).strip() != target_text:
            return ("skip", "markdown_link_form_unsupported")
        anchor_suffix = target_text[len(target_path_part) :]
        new_target_text = new_path + anchor_suffix
        m_start, m_end = match.span(0)
        g1_start, g1_end = match.span(1)
        old_text = line_text[m_start:m_end]
        replacement_text = (
            line_text[m_start:g1_start] + new_target_text + line_text[g1_end:m_end]
        )
    else:
        return ("skip", "unsupported_kind")

    new_line_text = line_text[:m_start] + replacement_text + line_text[m_end:]
    predicted_bytes = _rebuild_bytes(pieces, line_no - 1, new_line_text, data)
    predicted_post_hash = hashlib.sha256(predicted_bytes).hexdigest()

    edit = {
        "mutation_class": "fix_unresolved_link",
        "relative_path": referrer_path,
        "precondition_content_hash": expected_hash,
        "anchor": {"line": line_no, "old_text": old_text},
        "replacement_text": replacement_text,
        "predicted_post_hash": predicted_post_hash,
    }
    assert edit["mutation_class"] in MUTATION_CLASSES_SET
    return ("ok", edit)


def _document_shell(
    *,
    source: Any,
    action: str,
    generated_at: str,
    assessment_refs: list[dict[str, Any]],
    database: Path,
    indexed_at: Any,
    edits: list[dict[str, Any]],
    evidence: dict[str, Any],
    non_actions: list[str],
    notes_affected: list[str],
    id_salt: list[str] = (),
) -> dict[str, Any]:
    id_inputs = set(id_salt) | {
        edit["precondition_content_hash"] for edit in edits
    } | {edit["predicted_post_hash"] for edit in edits}
    proposal_id = _proposal_id(
        source,
        assessment_refs[0]["relation"] if assessment_refs else action,
        assessment_refs[0]["relative_path"] if assessment_refs else "",
        sorted(id_inputs),
        action,
    )
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": PROPOSAL_KIND,
        "proposal_id": proposal_id,
        "operation": "steward_propose",
        "generated_at": generated_at,
        "source": source,
        "action": action,
        "policy_level": POLICY_LEVEL,
        "assessment_refs": assessment_refs,
        # Persisted artifacts stay machine-local: filename only, never an
        # absolute path.
        "index": {"indexed_at": indexed_at, "database": Path(database).name},
        "edits": edits,
        "evidence": evidence,
        "non_actions": non_actions,
        "blast_radius": {
            "files_edited": len({edit["relative_path"] for edit in edits}),
            "notes_affected": sorted(set(notes_affected)),
            "predicted_citation_shifts": [],
        },
        "conflicts_with": [],
        "network_calls": 0,
        "vault_writes": 0,
    }


def _build_rename_proposal(
    connection: Any,
    old_path: str,
    new_path: str,
    *,
    source: Any,
    source_root: Path,
    assessment_file: Any,
    generated_at: str,
    database: Path,
    indexed_at: Any,
    skipped_drifted: list[str] | None,
    added_content_hash: str | None = None,
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT id FROM notes WHERE relative_path = ?", (old_path,)
    ).fetchone()
    if row is None:
        return None
    old_note_id = int(row["id"])

    notes_by_id: dict[int, str] = {}
    notes_hash_by_path: dict[str, str] = {}
    for note_row in connection.execute("SELECT id, relative_path, content_hash FROM notes"):
        notes_by_id[int(note_row["id"])] = str(note_row["relative_path"])
        notes_hash_by_path[str(note_row["relative_path"])] = str(note_row["content_hash"])

    edge_rows = connection.execute(
        """
        SELECT source_note_id, kind, evidence_json FROM edges
        WHERE target_note_id = ? AND is_verified = 1
          AND kind IN ('wikilink', 'markdown_link')
        ORDER BY source_note_id
        """,
        (old_note_id,),
    ).fetchall()

    edits: list[dict[str, Any]] = []
    resolved: list[str] = []
    skipped: list[dict[str, str]] = []
    for edge_row in edge_rows:
        referrer_path = notes_by_id.get(int(edge_row["source_note_id"]))
        if referrer_path is None:
            continue
        kind = str(edge_row["kind"])
        evidence = json.loads(edge_row["evidence_json"])
        status, payload = _resolve_rename_edit(
            referrer_path, kind, evidence, old_path, new_path, source_root, notes_hash_by_path
        )
        if status == "ok":
            edits.append(payload)
            resolved.append(referrer_path)
        elif status == "drift":
            skipped.append({"relative_path": referrer_path, "reason": "content_hash_drift"})
            if skipped_drifted is not None:
                skipped_drifted.append(referrer_path)
        else:
            skipped.append({"relative_path": referrer_path, "reason": str(payload)})

    if not edits:
        return None

    edits.sort(key=lambda edit: (edit["relative_path"], edit["anchor"]["line"]))
    notes_affected = sorted(set(resolved))
    document = _document_shell(
        source=source,
        action="fix_links_after_rename",
        generated_at=generated_at,
        assessment_refs=[
            {"assessment_file": assessment_file, "relative_path": old_path, "relation": "DELETED"}
        ],
        database=database,
        indexed_at=indexed_at,
        edits=edits,
        evidence={
            "removed_path": old_path,
            "added_path": new_path,
            "resolved_referrers": notes_affected,
            "skipped_referrers": _dedup_skipped(skipped),
        },
        non_actions=[],
        notes_affected=notes_affected,
        id_salt=[old_path, new_path],
    )
    # Pin BOTH sides of the rename, not just each referrer's bytes: the old path
    # must still be absent and the new path must still exist with the candidate
    # content hash at apply time. Without this an added file deleted or
    # repurposed after observation would still drive link rewrites to a missing
    # or unrelated note (the L1 unresolved-link gate permits a zero delta).
    document["rename_preconditions"] = {
        "removed_path": old_path,
        "removed_absent": True,
        "added_path": new_path,
        "added_content_hash": added_content_hash,
    }
    return document


def _build_dangling_proposal(
    path: str,
    inbound_edges: list[dict[str, str]],
    *,
    source: Any,
    assessment_file: Any,
    generated_at: str,
    database: Path,
    indexed_at: Any,
) -> dict[str, Any]:
    referrers = sorted(
        (
            {"other_path": edge["other_path"], "kind": edge["kind"]}
            for edge in inbound_edges
        ),
        key=lambda item: (item["other_path"], item["kind"]),
    )
    return _document_shell(
        source=source,
        action="review_dangling_references",
        generated_at=generated_at,
        assessment_refs=[
            {"assessment_file": assessment_file, "relative_path": path, "relation": "DELETED"}
        ],
        database=database,
        indexed_at=indexed_at,
        edits=[],
        evidence={"deleted_path": path, "referrers": referrers},
        non_actions=[
            "no automatic link rewrite: target resolution is not unique/decidable"
        ],
        notes_affected=[path],
        id_salt=sorted({item["other_path"] for item in referrers}),
    )


def _build_broken_citation_proposal(
    path: str,
    citations: list[dict[str, str]],
    *,
    source: Any,
    assessment_file: Any,
    generated_at: str,
    database: Path,
    indexed_at: Any,
) -> dict[str, Any]:
    return _document_shell(
        source=source,
        action="review_broken_citations",
        generated_at=generated_at,
        assessment_refs=[
            {
                "assessment_file": assessment_file,
                "relative_path": path,
                "relation": "CITATION_BROKEN",
            }
        ],
        database=database,
        indexed_at=indexed_at,
        edits=[],
        evidence={"relative_path": path, "citations": citations},
        non_actions=[
            "no automatic citation repair: replacement text is not decidable"
        ],
        notes_affected=[path],
        id_salt=sorted(citation["citation"] for citation in citations),
    )


def _build_duplicate_proposal(
    path: str,
    inputs: dict[str, Any],
    *,
    source: Any,
    assessment_file: Any,
    generated_at: str,
    database: Path,
    indexed_at: Any,
) -> dict[str, Any]:
    duplicate_of = list(inputs.get("duplicate_of") or [])
    duplicate_in_batch = list(inputs.get("duplicate_in_batch") or [])
    return _document_shell(
        source=source,
        action="review_duplicates",
        generated_at=generated_at,
        assessment_refs=[
            {
                "assessment_file": assessment_file,
                "relative_path": path,
                "relation": "DUPLICATES_EXACT_BYTES",
            }
        ],
        database=database,
        indexed_at=indexed_at,
        edits=[],
        evidence={
            "relative_path": path,
            "duplicate_of": sorted(duplicate_of),
            "duplicate_in_batch": sorted(duplicate_in_batch),
        },
        non_actions=[
            "no automatic merge or deletion: which copy is canonical is not decidable"
        ],
        notes_affected=[path, *duplicate_of, *duplicate_in_batch],
        id_salt=sorted({*duplicate_of, *duplicate_in_batch}),
    )


def _assign_conflicts(proposals: list[dict[str, Any]]) -> None:
    """Cross-link proposals whose edits touch the same (source, relative_path).

    Conflicts are keyed by (source, relative_path), not by path alone: two
    disjoint sources that each edit ``Alpha.md`` target different files and
    cannot overlap, so they must not be marked as conflicting (which would make
    validation refuse both). Recomputes ``conflicts_with`` from scratch on every
    call (not additive), so it is safe to call again over a larger, merged list.
    """

    key_to_ids: dict[tuple[Any, str], list[str]] = {}
    for proposal in proposals:
        source = proposal.get("source")
        touched = {edit["relative_path"] for edit in proposal.get("edits", [])}
        for path in touched:
            key_to_ids.setdefault((source, path), []).append(
                proposal["proposal_id"]
            )

    for proposal in proposals:
        source = proposal.get("source")
        touched = {edit["relative_path"] for edit in proposal.get("edits", [])}
        conflicting: set[str] = set()
        for path in touched:
            for other_id in key_to_ids.get((source, path), []):
                if other_id != proposal["proposal_id"]:
                    conflicting.add(other_id)
        proposal["conflicts_with"] = sorted(conflicting)


def propose_from_assessment(
    assessment: dict,
    batch: dict | None,
    database: Path,
    source_root: Path,
    *,
    now: str | None = None,
    skipped_drifted: list[str] | None = None,
) -> list[dict]:
    """Generate proposal documents for one source's latest assessment.

    ``batch`` is that source's change_batch document (the one the assessment
    was computed from), used only to read ``rename_candidates``; pass
    ``None`` when it is unavailable, which simply disables rename detection
    (every qualifying ``DELETED`` then falls back to a dangling-reference
    advisory). ``skipped_drifted`` is an optional caller-provided list that,
    when given, is appended with every referrer relative_path skipped because
    its on-disk bytes no longer match the index (a keyword-only addition
    used by ``propose_latest`` to build its receipt; it is not part of the
    document schema).

    Deterministic: identical inputs (and a frozen ``now``) produce
    byte-identical proposals, since every list embedded in the output is
    sorted before being returned.
    """

    _validate_assessment(assessment)
    database = Path(database)
    source_root = Path(source_root)
    generated_at = now if now is not None else _utc_now()
    source = assessment.get("source")
    assessment_file = assessment.get("change_batch_ref")
    indexed_at = (assessment.get("index") or {}).get("indexed_at")

    rename_map: dict[str, tuple[str, str | None]] = {}
    if batch is not None:
        for candidate in batch.get("rename_candidates") or []:
            added_paths = candidate.get("added_paths") or []
            # Content-hash uniqueness gates a compiled rename edit: exactly
            # one added file shares the removed file's bytes. Inode identity is
            # not consulted (not portable across inode-reusing filesystems);
            # the edit is hash-pinned and operator-reviewed regardless. The
            # shared content hash is carried so the proposal can pin the NEW
            # path's existence and bytes, not just each referrer's.
            if len(added_paths) == 1:
                rename_map[candidate["removed_path"]] = (
                    added_paths[0],
                    candidate.get("content_hash"),
                )

    by_relation: dict[str, list[dict[str, Any]]] = {}
    for item in assessment.get("assessments") or []:
        by_relation.setdefault(item["relation"], []).append(item)
    touched_by_path = {
        item["relative_path"]: item
        for item in by_relation.get("AUTHORED_REFERENCE_TOUCHED", [])
    }

    proposals: list[dict[str, Any]] = []
    connection = None

    def _connection() -> Any:
        nonlocal connection
        if connection is None:
            connection = connect(database, readonly=True)
        return connection

    try:
        for item in by_relation.get("DELETED", []):
            path = item["relative_path"]
            proposal: dict[str, Any] | None = None
            if path in rename_map:
                added_path, added_content_hash = rename_map[path]
                proposal = _build_rename_proposal(
                    _connection(),
                    path,
                    added_path,
                    source=source,
                    source_root=source_root,
                    assessment_file=assessment_file,
                    generated_at=generated_at,
                    database=database,
                    indexed_at=indexed_at,
                    skipped_drifted=skipped_drifted,
                    added_content_hash=added_content_hash,
                )
            if proposal is not None:
                proposals.append(proposal)
                continue

            touched = touched_by_path.get(path)
            inbound = [
                edge
                for edge in (touched["inputs"]["authored_edges"] if touched else [])
                if edge["direction"] == "inbound"
            ]
            if inbound:
                proposals.append(
                    _build_dangling_proposal(
                        path,
                        inbound,
                        source=source,
                        assessment_file=assessment_file,
                        generated_at=generated_at,
                        database=database,
                        indexed_at=indexed_at,
                    )
                )

        for item in by_relation.get("CITATION_BROKEN", []):
            filtered = [
                citation
                for citation in item["inputs"]["broken_citations"]
                if citation.get("reason") in _DECIDABLE_CITATION_REASONS
            ]
            if filtered:
                proposals.append(
                    _build_broken_citation_proposal(
                        item["relative_path"],
                        filtered,
                        source=source,
                        assessment_file=assessment_file,
                        generated_at=generated_at,
                        database=database,
                        indexed_at=indexed_at,
                    )
                )

        for item in by_relation.get("DUPLICATES_EXACT_BYTES", []):
            proposals.append(
                _build_duplicate_proposal(
                    item["relative_path"],
                    item["inputs"],
                    source=source,
                    assessment_file=assessment_file,
                    generated_at=generated_at,
                    database=database,
                    indexed_at=indexed_at,
                )
            )
    finally:
        if connection is not None:
            connection.close()

    _assign_conflicts(proposals)
    for proposal in proposals:
        # Bind each proposal to the registry its assessment ran under, so a
        # later apply can refuse a cross-registry artifact.
        proposal["registry_sha256"] = assessment.get("registry_sha256")
        _validate_machine_local(proposal)
    return proposals


def _load_assessment_batch(
    assessment: dict, assessment_name: str, changes_dir: Path
) -> dict | None:
    """Load and digest-verify the change batch an assessment was computed from."""

    batch_ref = assessment.get("change_batch_ref")
    if not isinstance(batch_ref, str):
        return None
    if "/" in batch_ref or "\\" in batch_ref or batch_ref in ("", ".", ".."):
        raise ValueError(
            f"Assessment {assessment_name} carries an invalid "
            f"change_batch_ref: {batch_ref!r}"
        )
    batch_path = changes_dir / batch_ref
    if not batch_path.is_file():
        return None
    batch_bytes = batch_path.read_bytes()
    pinned = assessment.get("change_batch_sha256")
    actual = hashlib.sha256(batch_bytes).hexdigest()
    if pinned is not None and actual != pinned:
        # The assessment pinned the exact batch it assessed; a batch rewritten
        # afterwards must never drive compiled edits.
        raise ValueError(
            f"Change batch {batch_ref} no longer matches the digest its "
            "assessment pinned; re-run steward-observe and steward-assess "
            "before proposing."
        )
    try:
        return json.loads(batch_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(
            f"Change batch {batch_ref} is not valid JSON: {error}"
        ) from error


def propose_latest(registry: SourceRegistry, state_root: Path, database: Path) -> dict:
    """For each registered source, propose from EVERY un-proposed assessment.

    Every ``assessments/<ts>-<source>.json`` for a source is processed in
    timestamp order (not just the newest): if assessment ran more than once
    before proposing, taking only the latest would permanently skip an earlier
    assessment's findings (e.g. a deletion recorded before an empty later run).
    A proposal set is deterministic in its (digest-pinned) assessment, so it is
    recomputed each run and only missing ``proposals/<ts>-<source>-<id>.json``
    files are written -- a crashed run completes on rerun, and an existing
    (possibly already-applied) proposal file is never overwritten. Runs under
    the steward state lock, since it both reads and writes the state directory.
    """

    state_root = Path(state_root)
    database = Path(database)
    generated_at = _utc_now()
    ensure_state_root_outside_sources(
        state_root, [source.root for source in registry.sources]
    )

    dirs = ensure_state_layout(state_root)
    with lock_state(state_root):
        assessments_dir = dirs["assessments"]
        changes_dir = dirs["changes"]
        proposals_dir = dirs["proposals"]

        per_source: list[dict[str, Any]] = []
        all_computed: list[tuple[str, dict[str, Any], bool]] = []

        for source in registry.sources:
            # Match the source's assessments by EXACT recorded name, not a glob
            # suffix (`*-a.json` also matches `<ts>-x-a.json`); the timestamp
            # component carries no hyphen, so the name is what follows the first.
            assessment_files = sorted(
                path
                for path in assessments_dir.glob(f"*-{source.name}.json")
                if _source_name_from_artifact_filename(path.name) == source.name
            )
            if not assessment_files:
                per_source.append({"source": source.name, "reason": "no_assessment"})
                continue

            created_here = 0
            skipped_drifted_all: set[str] = set()
            saw_any_proposal = False
            for assessment_path in assessment_files:
                try:
                    assessment = json.loads(
                        assessment_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as error:
                    raise ValueError(
                        f"Assessment {assessment_path.name} is not valid JSON: "
                        f"{error}"
                    ) from error
                if assessment.get("source") != source.name:
                    raise ValueError(
                        f"Assessment {assessment_path.name} claims source "
                        f"{assessment.get('source')!r} but was selected for "
                        f"source {source.name!r}; refusing a cross-source "
                        "proposal run."
                    )
                recorded_sha = assessment.get("registry_sha256")
                # A null recorded digest fails closed when the active registry
                # has one: an edited or legacy assessment must not bypass binding
                # to the current registry.
                if (
                    registry.registry_sha256 is not None
                    and recorded_sha != registry.registry_sha256
                ):
                    raise ValueError(
                        f"Assessment {assessment_path.name} was recorded under a "
                        "different source registry (registry_sha256 mismatch); "
                        "re-run steward-observe and steward-assess with the "
                        "current registry before proposing."
                    )

                batch = _load_assessment_batch(
                    assessment, assessment_path.name, changes_dir
                )

                skipped_drifted: list[str] = []
                proposals = propose_from_assessment(
                    assessment,
                    batch,
                    database,
                    source.root,
                    now=generated_at,
                    skipped_drifted=skipped_drifted,
                )
                skipped_drifted_all.update(skipped_drifted)
                if proposals:
                    saw_any_proposal = True
                for proposal in proposals:
                    filename = (
                        f"{assessment_path.stem}-{proposal['proposal_id']}.json"
                    )
                    is_new = not (proposals_dir / filename).exists()
                    all_computed.append((filename, proposal, is_new))
                    if is_new:
                        created_here += 1

            if not saw_any_proposal:
                per_source.append(
                    {
                        "source": source.name,
                        "proposals_created": 0,
                        "skipped_drifted": sorted(skipped_drifted_all),
                    }
                )
            elif created_here == 0:
                per_source.append(
                    {"source": source.name, "reason": "already_proposed"}
                )
            else:
                per_source.append(
                    {
                        "source": source.name,
                        "proposals_created": created_here,
                        "skipped_drifted": sorted(skipped_drifted_all),
                    }
                )

        # Conflict-link across the full computed set (existing + new) so a newly
        # written proposal references every counterpart.
        _assign_conflicts([proposal for _fn, proposal, _new in all_computed])

        written = 0
        conflicts_synced = 0
        for filename, proposal, is_new in all_computed:
            path = proposals_dir / filename
            if is_new:
                atomic_write_json(path, proposal, within=proposals_dir)
                written += 1
                continue
            # An EXISTING pending proposal may have gained a conflict with a
            # newly written counterpart. If so, sync only its conflicts_with so
            # both sides declare the conflict (an un-updated side would still be
            # applyable despite the overlap). Never rewrite an applied proposal.
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(existing, dict) or existing.get("status") == "applied":
                continue
            if existing.get("conflicts_with") != proposal.get("conflicts_with"):
                existing["conflicts_with"] = proposal.get("conflicts_with", [])
                atomic_write_json(path, existing, within=proposals_dir)
                conflicts_synced += 1

        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "steward_propose_receipt",
            "operation": "steward_propose",
            "generated_at": generated_at,
            "proposals_created": written,
            "conflicts_synced": conflicts_synced,
            "per_source": per_source,
            "network_calls": 0,
            "vault_writes": 0,
        }
