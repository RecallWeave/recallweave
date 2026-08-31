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
import os
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .index import _path_key, connect
from .parser import (
    MARKDOWN_LINK_RE,
    WIKILINK_RE,
    _markdown_target,
    normalize_name,
    parse_frontmatter,
)
from .policy import IndexPolicy
from .safe_write import is_link_like
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


_DIR_FD_PROPOSE = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def _open_referrer_fd(source_root: Path, relative: str) -> int:
    """Open a rename referrer WITHOUT following a symlink at any component, so a
    referrer (or an ancestor) replaced by a symlink cannot make proposal
    generation read bytes outside the registered source. Raises OSError on any
    symlinked/non-directory component, a missing leaf, or a failed open (the
    caller treats that as drift). Mirrors observe/apply's pinned reads."""

    parts = Path(relative).parts
    if not parts:
        raise OSError("empty relative path")
    if _DIR_FD_PROPOSE:
        fds: list[int] = [
            os.open(source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        ]
        try:
            for part in parts[:-1]:
                fds.append(
                    os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fds[-1],
                    )
                )
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fds[-1])
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
    # Pathname fallback (e.g. Windows): reject a symlink at any component.
    current = source_root
    for part in parts[:-1]:
        current = current / part
        if is_link_like(current):
            raise OSError(f"symlinked ancestor: {current}")
    full = source_root / relative
    if is_link_like(full):
        raise OSError(f"symlink leaf: {full}")
    return os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _read_fd_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _frontmatter_from_bytes(data: bytes) -> tuple[dict, bool]:
    """Frontmatter + validity from a referrer's already-read bytes, decoded as
    parse_note does, so admission is bound to the exact bytes (never a second
    pathname read that could follow a swapped symlink)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnicodeError("UTF-16 Markdown is not supported.")
    raw = data.decode("utf-8-sig", errors="strict")
    lines = re.split(r"\r\n|\r|\n", raw)
    frontmatter, _body_start, frontmatter_valid, _error = parse_frontmatter(lines)
    return frontmatter, frontmatter_valid


def _count_rename_occurrences(raw: str, old_path: str, referrer_path: str) -> int:
    """Count every wikilink or markdown link in ``raw`` that targets ``old_path``.

    WIKILINK_RE's group 1 is already the bare target (anchor and alias stripped).
    A markdown link matches when EITHER its path component or that component
    resolved relative to the referrer's directory maps to ``old_path`` under the
    index's key normalization -- so a relative form like ``[y](../Old.md)`` that
    the index resolves to ``Old.md`` is counted alongside the exact ``Old.md``.
    Missing this made the count return 1 for a referrer with both forms, so the
    compiled rewrite fixed one and left the other dangling."""

    old_stem = normalize_name(Path(old_path).stem)
    old_key = _path_key(old_path)
    source_parent = PurePosixPath(referrer_path).parent.as_posix()
    total = 0
    for line_text, _ending in _split_lines_keepends(raw):
        for match in WIKILINK_RE.finditer(line_text):
            if normalize_name(match.group(1).strip()) == old_stem:
                total += 1
        for match in MARKDOWN_LINK_RE.finditer(line_text):
            without_anchor = _markdown_target(match.group(1)).split("#", 1)[0]
            keys = {
                _path_key(without_anchor),
                _path_key(posixpath.join(source_parent, without_anchor)),
            }
            if old_key in keys:
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
    policy: Any,
) -> tuple[str, Any]:
    """Try to compile one fix_unresolved_link edit for a single authored edge.

    Returns ("ok", edit), ("drift", None) when the referrer's on-disk bytes
    no longer match the index, or ("skip", reason) for anything else that
    keeps this one edge from being safely, unambiguously rewritable."""

    expected_hash = notes_hash_by_path.get(referrer_path)
    if expected_hash is None:
        return ("skip", "referrer_not_in_index")

    # Read the referrer WITHOUT following a symlink at any component -- a
    # referrer (or an ancestor) replaced by a symlink must never redirect the
    # read outside the registered source -- and admit it through the SOURCE
    # policy from the fstat size BEFORE reading the bytes (the SQLite index may
    # have been built with a broader policy, so it can list referrers this
    # source's include_paths / deny terms / size cap exclude).
    try:
        fd = _open_referrer_fd(source_root, referrer_path)
    except OSError:
        return ("drift", None)
    try:
        try:
            referrer_size = os.fstat(fd).st_size
        except OSError:
            return ("drift", None)
        admitted, _reason = policy.path_allowed(referrer_path, referrer_size)
        if not admitted:
            return ("skip", "referrer_not_admitted")
        try:
            data = _read_fd_all(fd)
        except OSError:
            return ("drift", None)
    finally:
        os.close(fd)

    actual_hash = hashlib.sha256(data).hexdigest()
    if actual_hash != expected_hash:
        return ("drift", None)

    # Frontmatter-denied referrers are outside the admitted corpus; evaluate the
    # policy's frontmatter rule from the ALREADY-READ bytes (never a second
    # pathname read that could follow a swapped symlink).
    if policy.deny_frontmatter:
        try:
            frontmatter, frontmatter_valid = _frontmatter_from_bytes(data)
        except (UnicodeError, RecursionError):
            return ("skip", "referrer_frontmatter_unverifiable")
        allowed, _fm_reason = policy.frontmatter_allowed(
            frontmatter, valid=frontmatter_valid
        )
        if not allowed:
            return ("skip", "referrer_frontmatter_denied")

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
    if _count_rename_occurrences(raw, old_path, referrer_path) != 1:
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
        # A bare `[[New]]` only resolves unambiguously when NO OTHER note shares
        # the new basename. If another note does (e.g. `folder/New.md` renamed
        # while `other/New.md` exists), rewriting to the bare stem produces an
        # AMBIGUOUS link that the pre-apply rebuild still sees as unresolved --
        # which L1 permits at a zero delta, marking an ineffective rewrite
        # applied. The index reflects the pre-rename vault, so the rename target
        # itself is not in it; count same-stem notes other than the renamed-from
        # and renamed-to paths, and emit a path-qualified wikilink when any
        # collision exists.
        normalized_new_stem = normalize_name(new_stem)
        stem_collisions = sum(
            1
            for indexed_path in notes_hash_by_path
            if indexed_path not in (old_path, new_path)
            and normalize_name(Path(indexed_path).stem) == normalized_new_stem
        )
        if stem_collisions == 0:
            new_link_target = new_stem
        else:
            new_link_target = Path(new_path).with_suffix("").as_posix()
        m_start, m_end = match.span(0)
        g1_start, g1_end = match.span(1)
        old_text = line_text[m_start:m_end]
        replacement_text = (
            line_text[m_start:g1_start] + new_link_target + line_text[g1_end:m_end]
        )
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
    policy: Any,
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
            referrer_path,
            kind,
            evidence,
            old_path,
            new_path,
            source_root,
            notes_hash_by_path,
            policy,
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
    policy: Any = None,
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
    # The active source policy gates which referrers a rename rewrite may read
    # and emit. Fall back to a permissive default only when a caller does not
    # supply one (e.g. a focused unit test); propose_latest always passes the
    # registered source's policy so a broad index cannot leak excluded paths.
    admission_policy = policy if policy is not None else IndexPolicy()
    source = assessment.get("source")
    assessment_file = assessment.get("change_batch_ref")
    indexed_at = (assessment.get("index") or {}).get("indexed_at")

    # Bind compilation to the exact index snapshot the assessment ran against. If
    # the index was rebuilt between steward-assess and steward-propose its
    # `indexed_at` changes and its notes/edges may differ, so compiling rename or
    # advisory edits against it would use a snapshot the assessment never saw.
    # Refuse to compile for a mismatched snapshot (fail closed, no proposals);
    # re-running assess against the current index restores agreement.
    if indexed_at is not None:
        current_indexed_at = _current_indexed_at(database)
        if current_indexed_at is not None and current_indexed_at != indexed_at:
            return []

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
                    policy=admission_policy,
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


def _current_indexed_at(database: Path) -> str | None:
    """The current index's ``indexed_at`` (meta table), or None if unavailable."""
    try:
        with connect(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'indexed_at'"
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    value = row["value"] if hasattr(row, "keys") else row[0]
    return str(value) if value is not None else None


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
    # An assessment that references a batch MUST carry a well-formed digest for
    # it (assess_latest always writes one). A missing/null/malformed digest --
    # from a legacy, truncated, or hand-edited artifact -- must NOT silently load
    # whatever batch now occupies that filename (a rewritten batch could drive
    # proposals under an unbound assessment). Fail closed.
    if not (isinstance(pinned, str) and len(pinned) == 64):
        raise ValueError(
            f"Assessment {assessment_name} references change batch {batch_ref} "
            "without a well-formed change_batch_sha256; refusing to load an "
            "unbound batch. Re-run steward-assess before proposing."
        )
    actual = hashlib.sha256(batch_bytes).hexdigest()
    if actual != pinned:
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
        proposed_dir = dirs["proposed"]

        per_source: list[dict[str, Any]] = []
        all_computed: list[tuple[str, dict[str, Any], bool]] = []
        processed_assessments: list[str] = []

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
                # SKIP (do not raise on) an assessment from a prior registry
                # revision. Raising would make every steward-propose/-sweep fail
                # as long as a stale foreign assessment lingered in the state
                # dir -- and rerunning the pipeline never removes it -- so a
                # single in-place registry edit would wedge the pipeline. The
                # report, pruning, and auto-apply paths already skip foreign
                # artifacts; proposal generation does the same. (A null digest
                # fails closed as foreign when the active registry has one.)
                if (
                    registry.registry_sha256 is not None
                    and recorded_sha != registry.registry_sha256
                ):
                    continue

                batch = _load_assessment_batch(
                    assessment, assessment_path.name, changes_dir
                )

                skipped_drifted: list[str] = []
                proposals = propose_from_assessment(
                    assessment,
                    batch,
                    database,
                    source.root,
                    policy=source.policy,
                    now=generated_at,
                    skipped_drifted=skipped_drifted,
                )
                skipped_drifted_all.update(skipped_drifted)
                processed_assessments.append(assessment_path.name)
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

        # Ids of proposals already APPLIED on disk, keyed by proposal_id (NOT
        # filename): a re-emitted assessment gives the same deterministic id a
        # new filename, so a filename-based applied check would miss it and let
        # the terminal proposal poison a fresh counterpart's conflicts_with.
        applied_ids: set[str] = set()
        for existing_path in proposals_dir.glob("*.json"):
            try:
                doc = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                isinstance(doc, dict)
                and doc.get("status") == "applied"
                and isinstance(doc.get("proposal_id"), str)
            ):
                applied_ids.add(doc["proposal_id"])

        # PENDING proposals already on disk whose assessment was pruned (so they
        # were NOT recomputed into all_computed this run) must still take part in
        # conflict detection: otherwise a new proposal touching the same
        # (source, path) would not be linked to the surviving pending proposal,
        # and a class approval could apply one over the other. Collect them
        # (excluding applied/reverted and anything already recomputed by id),
        # include them in the conflict pass, and persist any conflicts_with change
        # back onto them. (#35)
        computed_ids = {
            proposal.get("proposal_id") for _fn, proposal, _new in all_computed
        }
        pruned_pending: list[tuple[Path, dict[str, Any]]] = []
        for existing_path in proposals_dir.glob("*.json"):
            try:
                doc = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            pid = doc.get("proposal_id")
            if not isinstance(pid, str) or pid in computed_ids or pid in applied_ids:
                continue
            if doc.get("status") in ("applied", "reverted"):
                continue
            pruned_pending.append((existing_path, doc))

        # Conflict-link across the computed set PLUS the pruned-pending on-disk
        # proposals, EXCLUDING proposals whose id is already applied on disk. An
        # applied proposal is terminal; if it were a pending counterpart, a new
        # proposal for the same path would carry the applied id in conflicts_with
        # and _validate_proposal would reject the new work permanently against a
        # counterpart that can never conflict.
        original_conflicts = {
            id(doc): list(doc.get("conflicts_with", [])) for _p, doc in pruned_pending
        }
        _assign_conflicts(
            [
                proposal
                for _fn, proposal, _new in all_computed
                if proposal.get("proposal_id") not in applied_ids
            ]
            + [doc for _p, doc in pruned_pending]
        )
        for existing_path, doc in pruned_pending:
            if doc.get("conflicts_with", []) != original_conflicts[id(doc)]:
                atomic_write_json(existing_path, doc, within=proposals_dir)

        # Proposal ids already present on disk under ANY filename. A re-emitted
        # batch (observe wrote a batch, then save_checkpoint failed, so the next
        # observe re-detected the same changes under a new timestamp) produces a
        # second assessment whose proposals carry the SAME deterministic
        # proposal_id under a different filename stem. Writing that duplicate
        # would let an apply select both -- the first mutating, the second
        # failing its now-stale precondition. Collapse by id: never write a
        # proposal whose id already exists.
        existing_by_id: dict[str, list[Path]] = {}
        for existing_path in proposals_dir.glob("*.json"):
            try:
                doc = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("proposal_id"), str):
                existing_by_id.setdefault(doc["proposal_id"], []).append(existing_path)

        def _sync_conflicts_onto(target_path: Path, computed: dict[str, Any]) -> bool:
            # Update ONLY conflicts_with on an existing pending proposal so both
            # sides declare a conflict; never rewrite an applied proposal.
            try:
                existing = json.loads(target_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if not isinstance(existing, dict) or existing.get("status") == "applied":
                return False
            if existing.get("conflicts_with") != computed.get("conflicts_with"):
                existing["conflicts_with"] = computed.get("conflicts_with", [])
                atomic_write_json(target_path, existing, within=proposals_dir)
                return True
            return False

        written = 0
        conflicts_synced = 0
        for filename, proposal, is_new in all_computed:
            path = proposals_dir / filename
            if is_new:
                pid = proposal.get("proposal_id")
                if pid in existing_by_id:
                    # Duplicate id from a re-emitted batch: don't write a second
                    # file, BUT still synchronize this run's freshly computed
                    # conflicts onto the existing file(s) for that id -- otherwise
                    # the on-disk copy could stay conflict-free and applyable
                    # while its counterpart declares the conflict.
                    for target_path in existing_by_id[pid]:
                        if _sync_conflicts_onto(target_path, proposal):
                            conflicts_synced += 1
                    continue
                atomic_write_json(path, proposal, within=proposals_dir)
                existing_by_id.setdefault(pid, []).append(path)
                written += 1
                continue
            # An EXISTING pending proposal (same filename) may have gained a
            # conflict with a newly written counterpart; sync it too.
            if _sync_conflicts_onto(path, proposal):
                conflicts_synced += 1

        # Durable per-assessment completion markers, written ONLY after every
        # proposal above is on disk. Pruning requires a marker before it may
        # delete an assessment/change batch, so a crash that persisted only some
        # of an assessment's proposals (no marker yet) can never make that
        # assessment prunable -- the rerun completes the set and then marks it.
        for assessment_name in sorted(set(processed_assessments)):
            atomic_write_json(
                proposed_dir / assessment_name,
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "propose_marker",
                    "assessment": assessment_name,
                    "registry_sha256": registry.registry_sha256,
                    "generated_at": generated_at,
                },
                within=proposed_dir,
            )

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
