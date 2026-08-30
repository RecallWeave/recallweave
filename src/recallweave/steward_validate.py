from __future__ import annotations

"""Steward post-apply validation gates (L0-L3).

These gates run INSIDE the apply transaction, after the last edit and before
the journal reaches status "applied": a gate failure raises, and the
executor's verified reverse-order rollback restores every target. Rollback is
not undo — it restores bytes, not the index (rebuild it), not artifacts
already exported, and not operator confidence — but a gate failure never
leaves a half-validated estate behind.

L0 — per-file: the written file re-parses, and its policy admissibility is
     unchanged (an edit may not silently vanish a note from the index by
     tripping a frontmatter denial).
L2 — structure preservation: the closed edit shapes promise line-range
     stability, and this gate proves it — a link fix keeps every heading and
     section range identical; an append keeps every pre-existing section
     start.
L3 — whole-source manifest: hashing every admitted file before and after,
     nothing the proposal did not name may change. This is the only gate
     that catches a bug in the writer itself.
L1 — index rebuild deltas: the source is rebuilt into a temporary index
     before and after, and the receipt deltas must stay inside each edit
     class's declared bounds. Candidate-edge counts are deliberately not
     asserted: discovery re-ranks on every rebuild, so "same notes,
     different candidates" does not imply a vault change.
"""

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from .index import build_index
from .parser import parse_note
from .steward_observe import _admitted_paths, _relative_for
from collections import Counter


class ValidationError(ValueError):
    """A post-apply validation gate failed; the apply must roll back."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_manifest(source: Any) -> dict[str, str]:
    """Hash every policy-admitted file in the source (paths only, no content
    is retained). Used for the L3 pre/post comparison."""

    skipped: Counter[str] = Counter()
    root = source.root.resolve()
    manifest: dict[str, str] = {}
    for path in _admitted_paths(source, root, skipped):
        relative = _relative_for(source, root, path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        allowed, _reason = source.policy.path_allowed(relative, size)
        if not allowed:
            continue
        try:
            manifest[relative] = _sha256_file(path)
        except OSError:
            continue
    return manifest


def _admissibility(source: Any, relative: str, data: bytes) -> tuple[bool, str | None]:
    allowed, reason = source.policy.path_allowed(relative, len(data))
    if not allowed:
        return False, reason
    if source.policy.deny_frontmatter:
        target = source.root / relative
        try:
            note = parse_note(target, source.root)
        except (UnicodeError, RecursionError, OSError):
            return False, "unparseable_frontmatter"
        allowed, reason = source.policy.frontmatter_allowed(
            note.frontmatter, valid=note.frontmatter_valid
        )
        if not allowed:
            return False, reason
    return True, None


def _section_shape(source_root: Path, relative: str) -> tuple[list, list]:
    note = parse_note(source_root / relative, source_root)
    headings = [(h.line, h.level, h.text) for h in note.headings]
    sections = [(s.heading, s.line_start, s.line_end) for s in note.sections]
    return headings, sections


def validate_l0_l2(
    plans: list[dict[str, Any]],
    source: Any,
    preapply_shapes: dict[str, tuple[list, list]],
) -> None:
    """L0 (re-parse + admissibility parity) and L2 (structure preservation)
    for every mutated target that still exists."""

    for plan in plans:
        edit = plan["edit"]
        relative = edit["relative_path"]
        mutation_class = edit["mutation_class"]
        if mutation_class == "move_to_trash":
            continue
        target = source.root / relative

        try:
            post_note = parse_note(target, source.root)
        except (UnicodeError, RecursionError, OSError) as error:
            raise ValidationError(
                f"L0: {relative} no longer parses after the edit "
                f"({type(error).__name__})."
            ) from error

        allowed, reason = _admissibility(source, relative, plan["post"] or b"")
        if not allowed:
            raise ValidationError(
                f"L0: the edit made {relative} inadmissible to the index "
                f"({reason}); a write may not silently vanish a note."
            )

        post_headings = [(h.line, h.level, h.text) for h in post_note.headings]
        post_sections = [
            (s.heading, s.line_start, s.line_end) for s in post_note.sections
        ]
        prior = preapply_shapes.get(relative)
        if prior is None:
            continue  # created file: no prior structure to preserve
        pre_headings, pre_sections = prior

        if mutation_class == "fix_unresolved_link":
            if post_headings != pre_headings or post_sections != pre_sections:
                raise ValidationError(
                    f"L2: {relative} changed heading or section structure "
                    "under a line-preserving edit."
                )
        elif mutation_class == "append_at_eof":
            if post_headings[: len(pre_headings)] != pre_headings:
                raise ValidationError(
                    f"L2: {relative} lost or shifted a pre-existing heading "
                    "under an append-only edit."
                )
            for index, (heading, line_start, _line_end) in enumerate(pre_sections):
                if index >= len(post_sections):
                    raise ValidationError(
                        f"L2: {relative} lost a section under an append-only "
                        "edit."
                    )
                post_heading, post_start, _post_end = post_sections[index]
                if (post_heading, post_start) != (heading, line_start):
                    raise ValidationError(
                        f"L2: {relative} shifted a pre-existing section start "
                        "under an append-only edit."
                    )


def validate_l3(
    manifest_before: dict[str, str],
    manifest_after: dict[str, str],
    plans: list[dict[str, Any]],
) -> None:
    """Nothing the proposal did not name may change — the writer-bug catcher."""

    edited = {plan["edit"]["relative_path"] for plan in plans}
    changed = set()
    for relative in set(manifest_before) | set(manifest_after):
        if manifest_before.get(relative) != manifest_after.get(relative):
            changed.add(relative)
    rogue = sorted(changed - edited)
    if rogue:
        raise ValidationError(
            f"L3: files changed that the proposal never named: {rogue}."
        )
    missing = sorted(
        relative
        for plan in plans
        for relative in [plan["edit"]["relative_path"]]
        if plan["edit"]["mutation_class"] != "move_to_trash"
        and relative not in manifest_after
    )
    if missing:
        # Every non-trash edit target must be present in the admitted set after
        # apply, INCLUDING a create_new_file target that did not exist before: a
        # concurrent writer removing it between L0 and the post-apply manifest
        # would otherwise leave it absent from both manifests, so the earlier
        # `relative in manifest_before` guard let a create be marked applied
        # while the promised file no longer exists.
        raise ValidationError(
            f"L3: edited files are absent from the admitted set after apply: "
            f"{missing}."
        )


def rebuild_receipt(source: Any, state_root: Path) -> dict[str, Any]:
    """Rebuild the source into a throwaway index and return the receipt."""

    with tempfile.TemporaryDirectory(dir=str(state_root)) as scratch:
        database = Path(scratch) / "validate.sqlite"
        receipt = build_index(source.root, database, policy=source.policy)
        try:
            database.unlink()
        except OSError:
            pass
        return receipt


# Per-class bounds for the L1 receipt comparison. Each entry maps a receipt
# counter to the (min_delta, max_delta) the class may cause per edit.
_L1_CLASS_BOUNDS: dict[str, dict[str, tuple[int, int]]] = {
    "fix_unresolved_link": {
        "notes_indexed": (0, 0),
        "unresolved_links": (-1, 0),
        "verified_edges": (0, 1),
    },
    "append_at_eof": {
        "notes_indexed": (0, 0),
        "unresolved_links": (0, 0),
    },
    "create_new_file": {
        "notes_indexed": (0, 1),
    },
    "move_to_trash": {
        "notes_indexed": (-1, 0),
    },
}
_L1_COUNTERS = ("notes_indexed", "unresolved_links", "verified_edges")


def validate_l1(
    receipt_before: dict[str, Any],
    receipt_after: dict[str, Any],
    plans: list[dict[str, Any]],
) -> dict[str, int]:
    """Receipt deltas must stay inside the summed per-class bounds."""

    class_counts: Counter[str] = Counter(
        plan["edit"]["mutation_class"] for plan in plans
    )
    deltas: dict[str, int] = {}
    for counter in _L1_COUNTERS:
        before = int(receipt_before.get(counter, 0))
        after = int(receipt_after.get(counter, 0))
        delta = after - before
        deltas[counter] = delta
        low = sum(
            _L1_CLASS_BOUNDS.get(cls, {}).get(counter, (0, 0))[0] * count
            for cls, count in class_counts.items()
        )
        high = sum(
            _L1_CLASS_BOUNDS.get(cls, {}).get(counter, (0, 0))[1] * count
            for cls, count in class_counts.items()
        )
        if not (low <= delta <= high):
            raise ValidationError(
                f"L1: index counter {counter} moved by {delta}, outside the "
                f"bounds [{low}, {high}] this proposal's edit classes allow."
            )
    if receipt_before.get("skipped") != receipt_after.get("skipped"):
        raise ValidationError(
            "L1: the rebuild's skip profile changed; an edit altered which "
            "files the index admits."
        )
    return deltas
