from __future__ import annotations

"""Steward stage D2: the apply executor.

This is the single module in RecallWeave that may write into a registered
source, and it is a *pure executor*: its inputs are an operator-approved
proposal (whose compiled edit script pins every target by content hash) and a
write policy. It contains no classifier, performs no synthesis, and never
re-anchors or fuzzy-patches — any precondition mismatch aborts the entire
proposal, before or during execution.

Import isolation: no other module in this package may import this one at
module level (``cli.py`` imports it inside the ``steward-apply`` dispatch
branch only), so the engine's "no write path back into notes" property stays
provable from the static import graph. Engine receipts keep reporting
``vault_writes: 0`` truthfully; apply receipts carry their own
``steward_vault_mutations`` counter.

Transaction model (per proposal, all-or-nothing):

1. validate the proposal (schema, machine-local, binding, no conflicts);
2. resolve the write policy for every edit — technical determinism never
   implies authorization, and this invocation's mode must cover the level;
3. preflight every edit before the first write: containment, link checks,
   precondition hash, predicted post-bytes hash, writability, disk space;
4. journal the full intent (fsync) including planned backup names;
5. per edit: verified backup copy into the state directory, then a guarded
   temp-write + ``os.replace`` of the target, then an on-disk post-hash
   check;
6. on any failure: reverse-order restore from the journaled backups, each
   restore re-hashed — an unverified rollback is a claim, not a fact; a
   failed restore is reported loudly with retained backup paths.

Backups live under the steward state directory, not beside the target: a
retained backup directory inside a source would be re-indexed as notes on the
next sweep. Deletion is never ``unlink``: ``move_to_trash`` copies the bytes
into the state trash (hash-verified) before removing the original.
"""

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .parser import parse_note
from .policy import RESERVED_DIRECTORY_NAMES
from .safe_write import is_link_like
from .steward_git import GitError, check_apply_preconditions, commit_applied
from .steward_policy import (
    MUTATION_CLASSES_SET,
    WritePolicy,
    resolve_level,
)
from .steward_propose import _rebuild_bytes, _split_lines_keepends
from .steward_sources import SourceRegistry
from .steward_validate import (
    ValidationError,
    rebuild_receipt,
    source_manifest,
    validate_l0_l2,
    validate_l1,
    validate_l3,
    _section_shape,
)
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    guard_within,
    lock_state,
)

import re

_PROPOSAL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

APPLY_RECEIPT_KIND = "apply_receipt"
JOURNAL_KIND = "apply_journal"

# Edit shapes this executor can run today. replace_whole_section stays in the
# schema but is refused until the post-apply validation gates (S10) can
# re-issue the citations it invalidates.
_EXECUTABLE_CLASSES = frozenset(
    {"create_new_file", "append_at_eof", "fix_unresolved_link", "move_to_trash"}
)

_SYNC_ROOT_MARKERS = (".dropbox", ".dropbox.cache", ".stfolder", ".stversions")
_SYNC_PATH_FRAGMENTS = ("Mobile Documents", "com~apple~CloudDocs")

_FREE_SPACE_SLACK_BYTES = 8 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_timestamp(iso: str) -> str:
    value = datetime.fromisoformat(iso)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flatten(relative_path: str) -> str:
    return relative_path.replace("/", "__")


def source_in_sync_root(root: Path) -> str | None:
    """Return the marker that identifies ``root`` as living under a sync
    service, or None. Rollback semantics are undefined while another process
    may rewrite the same tree, so apply refuses these without an explicit
    override."""

    resolved = root.resolve()
    text = resolved.as_posix()
    for fragment in _SYNC_PATH_FRAGMENTS:
        if fragment in text:
            return fragment
    for ancestor in (resolved, *resolved.parents):
        for marker in _SYNC_ROOT_MARKERS:
            if (ancestor / marker).exists():
                return marker
    return None


class ApplyError(ValueError):
    """A refusal or failure in the apply pipeline."""


class RollbackError(ApplyError):
    """Rollback itself failed; backups are retained and named."""


def _require_clean_relative_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path or ":" in path.split("/")[0]:
        raise ApplyError(f"Invalid relative path in edit: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise ApplyError(f"Invalid relative path in edit: {path!r}")


def _require_root_identity(source: Any) -> Path:
    """Re-verify the registered root at mutation time, exactly as observation
    does: link-likeness and pinned (dev, ino) identity. A root swapped after
    registry admission must never rebind the mutation boundary."""

    from .safe_write import path_identity

    if is_link_like(source.root):
        raise ApplyError(
            f"Source root {source.name!r} is now a symlink; refusing to write."
        )
    try:
        resolved = source.root.resolve(strict=True)
    except OSError as error:
        raise ApplyError(
            f"Source root for {source.name!r} is unavailable "
            f"({type(error).__name__}); refusing to write."
        ) from error
    if source.root_dev is not None and source.root_ino is not None:
        try:
            identity = path_identity(resolved)
        except OSError as error:
            raise ApplyError(
                f"Source root identity for {source.name!r} cannot be "
                f"verified ({type(error).__name__}); refusing to write."
            ) from error
        if identity != (source.root_dev, source.root_ino):
            raise ApplyError(
                f"Source root for {source.name!r} is no longer the directory "
                "the registry admitted (identity changed); refusing to write. "
                "Reload the registry deliberately if the move was yours."
            )
    return resolved


def _guarded_unlink(target: Path, boundary: Path) -> None:
    """Remove ``target`` with the same rigor as guarded writes.

    Where the platform supports dir_fd operations, the parent directory is
    reached by an O_NOFOLLOW openat chain from the boundary root, so a
    directory swapped for a symlink at ANY point between validation and the
    unlink syscall cannot redirect the deletion. Elsewhere, the parent chain
    is rechecked immediately before a pathname unlink (best available)."""

    _recheck_parent_chain(target, boundary)
    if is_link_like(target):
        raise ApplyError(f"Refusing to unlink a symlink or junction: {target}")
    relative = target.absolute().relative_to(boundary.absolute())
    parts = relative.parts
    use_dir_fd = (
        os.unlink in os.supports_dir_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
    if not use_dir_fd:
        target.unlink()
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds: list[int] = []
    try:
        fd = os.open(boundary, os.O_RDONLY | os.O_DIRECTORY)
        fds.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, flags, dir_fd=fd)
            fds.append(fd)
        os.unlink(parts[-1], dir_fd=fds[-1])
    except OSError as error:
        raise ApplyError(
            f"Guarded unlink failed for {target} ({type(error).__name__})."
        ) from error
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _remove_created_dirs(created_dirs: list[Path], boundary: Path) -> None:
    """Remove directories this apply created, deepest first, on rollback.

    Only empty directories are removed and only those inside the boundary;
    a directory that has since gained other content is left in place."""

    resolved_boundary = boundary.resolve()
    for directory in sorted(set(created_dirs), key=lambda p: len(p.parts), reverse=True):
        try:
            if is_link_like(directory):
                continue
            resolved = directory.resolve()
            if not (
                resolved != resolved_boundary
                and resolved_boundary in resolved.parents
            ):
                continue
            if any(directory.iterdir()):
                continue
            directory.rmdir()
        except OSError:
            continue


def _validate_proposal(proposal: Any) -> None:
    if not isinstance(proposal, dict):
        raise ApplyError("Proposal must be a JSON object.")
    for key in ("schema_version", "kind", "proposal_id", "source", "edits"):
        if key not in proposal:
            raise ApplyError(f"Proposal is missing required key: {key}")
    proposal_id = proposal["proposal_id"]
    # proposal_id is embedded into state filenames (journal, backup dir,
    # receipt); constrain it to the same slug shape as source names so it
    # cannot carry path separators or traversal.
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.fullmatch(
        proposal_id
    ):
        raise ApplyError(
            f"Proposal id {proposal_id!r} is not a valid identifier "
            "([A-Za-z0-9._:-], no separators)."
        )
    if proposal["schema_version"] != STEWARD_SCHEMA_VERSION:
        raise ApplyError(
            f"Unsupported proposal schema_version {proposal['schema_version']!r}."
        )
    if proposal["kind"] != "proposal":
        raise ApplyError(f"Unsupported proposal kind {proposal['kind']!r}.")
    if proposal.get("status") == "applied":
        raise ApplyError(
            f"Proposal {proposal['proposal_id']} is already applied; refusing "
            "a double apply."
        )
    if not isinstance(proposal["edits"], list) or not proposal["edits"]:
        raise ApplyError(
            f"Proposal {proposal['proposal_id']} carries no executable edits "
            "(advisory proposals are review material, not apply input)."
        )
    conflicts = proposal.get("conflicts_with") or []
    if conflicts:
        raise ApplyError(
            f"Proposal {proposal['proposal_id']} conflicts with "
            f"{sorted(conflicts)}; resolve the conflict before applying."
        )


def _validate_edit(edit: Any) -> None:
    if not isinstance(edit, dict):
        raise ApplyError("Each edit must be a JSON object.")
    mutation_class = edit.get("mutation_class")
    if mutation_class not in MUTATION_CLASSES_SET:
        raise ApplyError(f"Unknown mutation class in edit: {mutation_class!r}")
    if mutation_class not in _EXECUTABLE_CLASSES:
        raise ApplyError(
            f"Mutation class {mutation_class!r} is not executable in this "
            "version."
        )
    path = edit.get("relative_path")
    if not isinstance(path, str):
        raise ApplyError(f"Edit has no relative_path: {edit!r}")
    _require_clean_relative_path(path)


def _resolve_target(source_root: Path, relative_path: str, database: Path) -> Path:
    target = source_root / relative_path
    directories = {part.casefold() for part in Path(relative_path).parts[:-1]}
    if directories.intersection(RESERVED_DIRECTORY_NAMES):
        raise ApplyError(
            f"Refusing an edit inside a reserved directory: {relative_path}"
        )
    current = source_root
    for part in Path(relative_path).parts[:-1]:
        current = current / part
        if is_link_like(current):
            raise ApplyError(
                f"Refusing an edit through a symlinked directory: {current}"
            )
    if is_link_like(target):
        raise ApplyError(f"Refusing to edit a symlink or junction: {target}")
    if target.exists():
        resolved = target.resolve()
        resolved_root = source_root.resolve()
        if not (resolved == resolved_root or resolved_root in resolved.parents):
            raise ApplyError(
                f"Edit target escapes the source root: {relative_path}"
            )
        try:
            if database.exists() and os.path.samefile(target, database):
                raise ApplyError(
                    "Refusing an edit that targets the RecallWeave database."
                )
        except OSError:
            pass
    return target


def _compute_post_bytes(edit: dict[str, Any], current: bytes | None) -> bytes | None:
    """Return the exact bytes the target must hold after this edit.

    None means the edit removes the file (move_to_trash). Every branch is a
    pure function of (edit, current bytes); nothing is synthesized."""

    mutation_class = edit["mutation_class"]
    if mutation_class == "move_to_trash":
        return None
    if mutation_class == "create_new_file":
        text = edit.get("replacement_text")
        if not isinstance(text, str):
            raise ApplyError("create_new_file edit carries no replacement_text.")
        return text.encode("utf-8")
    if current is None:
        raise ApplyError(
            f"Edit {edit['mutation_class']} targets a file that does not exist."
        )
    if mutation_class == "append_at_eof":
        text = edit.get("replacement_text")
        if not isinstance(text, str):
            raise ApplyError("append_at_eof edit carries no replacement_text.")
        return current + text.encode("utf-8")
    # fix_unresolved_link
    anchor = edit.get("anchor") or {}
    line_no = anchor.get("line")
    old_text = anchor.get("old_text")
    replacement = edit.get("replacement_text")
    if (
        not isinstance(line_no, int)
        or isinstance(line_no, bool)
        or not isinstance(old_text, str)
        or not isinstance(replacement, str)
        or not old_text
    ):
        raise ApplyError(f"Malformed fix_unresolved_link anchor: {anchor!r}")
    try:
        raw = current.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ApplyError(
            "Edit target is not valid UTF-8; refusing to rewrite it."
        ) from error
    pieces = _split_lines_keepends(raw)
    if line_no < 1 or line_no > len(pieces):
        raise ApplyError(
            f"Anchor line {line_no} is out of range for the target."
        )
    line_text, _ending = pieces[line_no - 1]
    if line_text.count(old_text) != 1:
        raise ApplyError(
            "Anchor text does not occur exactly once on its line; refusing "
            "an ambiguous rewrite."
        )
    new_line_text = line_text.replace(old_text, replacement, 1)
    return _rebuild_bytes(pieces, line_no - 1, new_line_text, current)


def _preflight_edit(
    edit: dict[str, Any],
    source: Any,
    database: Path,
) -> dict[str, Any]:
    """Verify one edit end-to-end without writing; return its execution plan."""

    _validate_edit(edit)
    source_root = source.root
    target = _resolve_target(source_root, edit["relative_path"], database)
    mutation_class = edit["mutation_class"]

    current: bytes | None = None
    if mutation_class == "create_new_file":
        if target.exists():
            raise ApplyError(
                f"create_new_file target already exists: {edit['relative_path']}"
            )
    else:
        try:
            current = target.read_bytes()
        except OSError as error:
            raise ApplyError(
                f"Edit target is unreadable: {edit['relative_path']} "
                f"({type(error).__name__})"
            ) from error
        precondition = edit.get("precondition_content_hash")
        if not isinstance(precondition, str):
            raise ApplyError(
                f"Edit carries no precondition hash: {edit['relative_path']}"
            )
        if _sha256_bytes(current) != precondition:
            raise ApplyError(
                f"Precondition hash mismatch for {edit['relative_path']}: the "
                "file changed since the proposal was compiled. Re-run "
                "steward-observe, steward-assess and steward-propose."
            )

    # Admission is IndexPolicy, only -- for writes exactly as for reads. A
    # target the source policy would not admit (outside the allowlist, over
    # the size cap, or frontmatter-denied) is not part of the appliable
    # corpus and may not be mutated or deleted, whatever a proposal claims.
    admission_size = len(current) if current is not None else 0
    allowed, reason = source.policy.path_allowed(
        edit["relative_path"], admission_size
    )
    if not allowed:
        raise ApplyError(
            f"Edit target {edit['relative_path']} is not admitted by the "
            f"source policy ({reason}); refusing to mutate outside the "
            "admitted corpus."
        )
    if current is not None and source.policy.deny_frontmatter:
        try:
            note = parse_note(target, source_root)
        except (UnicodeError, RecursionError, OSError):
            raise ApplyError(
                f"Cannot verify admission frontmatter for "
                f"{edit['relative_path']}; refusing the edit."
            ) from None
        allowed, reason = source.policy.frontmatter_allowed(
            note.frontmatter, valid=note.frontmatter_valid
        )
        if not allowed:
            raise ApplyError(
                f"Edit target {edit['relative_path']} is frontmatter-denied "
                f"by the source policy ({reason}); refusing to mutate outside "
                "the admitted corpus."
            )

    post = _compute_post_bytes(edit, current)
    predicted = edit.get("predicted_post_hash")
    if post is not None:
        if not isinstance(predicted, str):
            raise ApplyError(
                f"Edit carries no predicted post hash: {edit['relative_path']}"
            )
        if _sha256_bytes(post) != predicted:
            raise ApplyError(
                f"Computed post-state for {edit['relative_path']} does not "
                "match the proposal's predicted hash; refusing to write bytes "
                "the operator did not approve."
            )

    parent = target.parent if target.parent.exists() else source_root
    if not os.access(parent, os.W_OK):
        raise ApplyError(f"Edit target parent is not writable: {parent}")
    if mutation_class != "move_to_trash":
        # The apply temp name is predictable; a pre-existing file there would
        # both block the write and, worse, block the rollback restore. Catch
        # it in preflight so the whole proposal refuses before any mutation
        # rather than failing mid-transaction.
        temp = target.parent / f".{target.name}.steward-apply.tmp"
        if temp.exists() or is_link_like(temp):
            raise ApplyError(
                f"Refusing to apply: a file already occupies the temporary "
                f"path {temp}. Remove it and retry."
            )
    usage = shutil.disk_usage(parent)
    needed = (len(post) if post is not None else 0) + _FREE_SPACE_SLACK_BYTES
    if usage.free < needed:
        raise ApplyError("Insufficient free disk space for the apply.")

    return {
        "edit": edit,
        "target": target,
        "current": current,
        "post": post,
    }


def _recheck_parent_chain(target: Path, boundary: Path | None) -> None:
    """Re-run the parent symlink/containment check at the mutation boundary.

    Preflight already validated the chain, but a directory can be swapped for
    a symlink between preflight and the write; this recheck immediately
    before each mutation shrinks that race to the syscall window, and the
    temp file itself is created with O_NOFOLLOW|O_EXCL so a planted symlink
    at the temp name cannot redirect the write either."""

    if boundary is None:
        return
    resolved_boundary = boundary.resolve()
    try:
        relative = target.absolute().relative_to(boundary.absolute())
    except ValueError:
        raise ApplyError(
            f"Mutation target escaped its boundary: {target}"
        ) from None
    current = boundary
    for part in relative.parts[:-1]:
        current = current / part
        if is_link_like(current):
            raise ApplyError(
                f"Refusing a write through a symlinked directory: {current}"
            )
    parent = target.parent
    if parent.exists():
        resolved_parent = parent.resolve()
        if not (
            resolved_parent == resolved_boundary
            or resolved_boundary in resolved_parent.parents
        ):
            raise ApplyError(
                f"Mutation target's parent escaped its boundary: {target}"
            )


def _guarded_replace(target: Path, data: bytes, boundary: Path | None = None) -> None:
    """Write ``data`` to ``target`` via a same-directory fsync'd temp file and
    an atomic ``os.replace``. The parent chain is re-validated at this
    mutation boundary (not only at preflight); recovery comes from the
    journaled state-directory backup, so no in-source backup rotation is
    performed (a retained backup directory inside a source would be
    re-indexed as notes)."""

    _recheck_parent_chain(target, boundary)
    if is_link_like(target):
        raise ApplyError(f"Refusing to replace a symlink or junction: {target}")
    temp = target.parent / f".{target.name}.steward-apply.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # O_EXCL means a pre-existing file (a planted collision, a real note, or
    # another process's temp) makes this raise rather than be destroyed: a
    # write outside the approved edit set is never acceptable, so refuse.
    try:
        fd = os.open(temp, flags, 0o644)
    except FileExistsError as error:
        raise ApplyError(
            f"Refusing to apply: a file already occupies the temporary path "
            f"{temp}. Remove it and retry."
        ) from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _write_backup(
    backup_dir: Path, name: str, data: bytes, *, within: Path
) -> Path:
    backup_path = backup_dir / name
    guard_within(backup_path, within)
    backup_dir.mkdir(parents=True, exist_ok=True)
    with open(backup_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if _sha256_bytes(backup_path.read_bytes()) != _sha256_bytes(data):
        raise ApplyError(f"Backup verification failed: {backup_path}")
    return backup_path


def _rollback(
    completed: list[dict[str, Any]],
    journal_path: Path,
    journal: dict[str, Any],
    journal_dir: Path,
    boundary: Path,
) -> None:
    """Reverse-order verified restore of every completed operation."""

    failures: list[str] = []
    for op in reversed(completed):
        target = Path(op["target"])
        try:
            if op["had_file"]:
                backup = Path(op["backup_path"])
                data = backup.read_bytes()
                if _sha256_bytes(data) != op["content_hash_before"]:
                    failures.append(
                        f"{op['relative_path']}: backup hash mismatch, backup "
                        f"retained at {backup}"
                    )
                    continue
                _guarded_replace(target, data, boundary=boundary)
                restored = target.read_bytes()
                if _sha256_bytes(restored) != op["content_hash_before"]:
                    failures.append(
                        f"{op['relative_path']}: restore verification failed, "
                        f"backup retained at {backup}"
                    )
            else:
                try:
                    _guarded_unlink(target, boundary)
                except ApplyError:
                    if target.exists():
                        raise
        except (OSError, ApplyError) as error:
            # A guard refusal (e.g. a symlink planted mid-rollback) is a
            # failed restore like any other: record it, keep restoring the
            # rest, and surface the retained backup loudly.
            failures.append(
                f"{op['relative_path']}: restore failed "
                f"({type(error).__name__}), backup retained at "
                f"{op.get('backup_path')}"
            )
    journal["status"] = "rollback_failed" if failures else "rolled_back"
    journal["rollback_failures"] = failures
    atomic_write_json(journal_path, journal, within=journal_dir)
    if failures:
        raise RollbackError(
            "Rollback could not fully restore the source; retained backups: "
            + "; ".join(failures)
        )


def apply_proposal(
    proposal: dict[str, Any],
    *,
    registry: SourceRegistry,
    state_dirs: dict[str, Path],
    database: Path,
    policy: WritePolicy,
    mode: str,
    execute: bool,
    allow_sync_root: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Execute (or dry-run) one approved proposal, all-or-nothing.

    ``mode`` is the invocation form: "per_item" (the operator named this
    proposal id) or "per_class" (the operator approved a mutation class).
    Both are forms of require_approval; auto_apply additionally allows the
    sweep to run append-only classes without a per-run acknowledgement.
    """

    _validate_proposal(proposal)
    generated_at = now if now is not None else _utc_now()
    proposal_id = proposal["proposal_id"]

    source = next(
        (item for item in registry.sources if item.name == proposal["source"]),
        None,
    )
    if source is None:
        raise ApplyError(
            f"Proposal {proposal_id} names source {proposal['source']!r} which "
            "is not in the active registry."
        )
    if source.mode != "appliable":
        raise ApplyError(
            f"Source {source.name!r} is registered as {source.mode!r}; only an "
            "appliable source accepts writes. Update the registry deliberately "
            "to change that."
        )
    _require_root_identity(source)
    recorded_sha = proposal.get("registry_sha256")
    # Null fails closed when the active registry has a digest: an edited or
    # legacy proposal must not bypass registry binding.
    if registry.registry_sha256 is not None and (
        recorded_sha != registry.registry_sha256
    ):
        raise ApplyError(
            f"Proposal {proposal_id} was compiled under a different source "
            "registry (registry_sha256 mismatch); re-run the pipeline."
        )

    marker = None if allow_sync_root else source_in_sync_root(source.root)
    if marker is not None:
        raise ApplyError(
            f"Source {source.name!r} appears to live under a sync service "
            f"(marker: {marker}); rollback semantics are undefined there. "
            "Pass --allow-sync-root to override deliberately."
        )

    # Git is an additional record, never the primary rollback; this refusal
    # runs before any preflight or journal write, so a git-state problem
    # leaves nothing behind to clean up.
    touched_relative_paths = [
        edit["relative_path"]
        for edit in proposal["edits"]
        if isinstance(edit, dict) and isinstance(edit.get("relative_path"), str)
    ]
    git_info = check_apply_preconditions(
        source.root, touched_relative_paths, require_git=policy.require_git
    )

    if len(proposal["edits"]) > policy.max_files_per_apply:
        raise ApplyError(
            f"Proposal {proposal_id} touches {len(proposal['edits'])} files; "
            f"the write policy caps an apply at {policy.max_files_per_apply}."
        )

    # Policy resolution for every edit, before any other work: determinism
    # never implies authorization.
    for edit in proposal["edits"]:
        _validate_edit(edit)
        frontmatter = None
        if policy.protected_frontmatter:
            # Protected-frontmatter classes are enforced at apply time against
            # the target's CURRENT frontmatter; an unreadable or unparseable
            # target fails closed as protected.
            target = source.root / edit["relative_path"]
            if target.is_file():
                try:
                    note = parse_note(target, source.root)
                except (UnicodeError, RecursionError, OSError):
                    raise ApplyError(
                        f"Cannot verify protected frontmatter for "
                        f"{edit['relative_path']}; refusing the edit."
                    ) from None
                if not note.frontmatter_valid:
                    raise ApplyError(
                        f"Target {edit['relative_path']} has unparseable "
                        "frontmatter under a protected-frontmatter policy; "
                        "refusing the edit."
                    )
                frontmatter = note.frontmatter
        level, reason = resolve_level(
            policy,
            mutation_class=edit["mutation_class"],
            source_name=source.name,
            relative_path=edit["relative_path"],
            frontmatter=frontmatter,
        )
        if mode == "auto":
            allowed = level == "auto_apply"
        else:
            allowed = (
                level == "auto_apply"
                or (
                    level == "require_approval"
                    and mode in ("per_item", "per_class")
                )
            )
        if not allowed:
            raise ApplyError(
                f"Write policy resolves {edit['mutation_class']!r} on "
                f"{edit['relative_path']!r} to {level!r} ({reason}); this "
                "invocation cannot authorize it."
            )

    plans = [
        _preflight_edit(edit, source, database)
        for edit in proposal["edits"]
    ]

    stamp = _file_timestamp(generated_at)
    receipt_ref = f"{stamp}-{proposal_id}.json"
    planned_ops = []
    for index, plan in enumerate(plans):
        planned_ops.append(
            {
                "relative_path": plan["edit"]["relative_path"],
                "mutation_class": plan["edit"]["mutation_class"],
                "content_hash_before": (
                    _sha256_bytes(plan["current"])
                    if plan["current"] is not None
                    else None
                ),
                "content_hash_after": (
                    _sha256_bytes(plan["post"]) if plan["post"] is not None else None
                ),
                "backup_name": f"{index}-{_flatten(plan['edit']['relative_path'])}",
            }
        )

    if not execute:
        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": APPLY_RECEIPT_KIND,
            "operation": "steward_apply",
            "generated_at": generated_at,
            "proposal_id": proposal_id,
            "source": source.name,
            "dry_run": True,
            "applied": False,
            "mode": mode,
            "policy_sha256": policy.policy_sha256,
            "edits": planned_ops,
            "steward_vault_mutations": 0,
            "network_calls": 0,
            "vault_writes": 0,
            "git": {"used": git_info["git_used"], "commit": None},
        }

    # Pre-apply evidence for the validation gates (L1/L2/L3): structure
    # shapes of the touched files, the whole-source manifest, and a rebuild
    # receipt of the source as it stands right now.
    state_root_dir = state_dirs["journal"].parent
    preapply_shapes = {}
    for plan in plans:
        relative = plan["edit"]["relative_path"]
        if plan["current"] is not None:
            try:
                preapply_shapes[relative] = _section_shape(source.root, relative)
            except (UnicodeError, RecursionError, OSError):
                preapply_shapes[relative] = None
    preapply_shapes = {
        key: value for key, value in preapply_shapes.items() if value is not None
    }
    manifest_before = source_manifest(source)
    receipt_before = rebuild_receipt(source, state_root_dir)

    journal_dir = state_dirs["journal"]
    backups_root = state_dirs["backups"]
    trash_root = state_dirs["trash"]
    backup_dir = backups_root / f"{stamp}-{proposal_id}"
    journal_path = journal_dir / f"{stamp}-{proposal_id}.json"
    journal: dict[str, Any] = {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": JOURNAL_KIND,
        "proposal_id": proposal_id,
        "source": source.name,
        "generated_at": generated_at,
        "status": "intent",
        "backup_dir": backup_dir.name,
        "operations": [dict(op, state="planned") for op in planned_ops],
        "rollback_failures": [],
    }
    # Journal-first: the fsync'd intent record is the recovery source of
    # truth and must exist before the first mutation.
    atomic_write_json(journal_path, journal, within=journal_dir)

    completed: list[dict[str, Any]] = []
    created_dirs: list[Path] = []
    mutations = 0
    try:
        for index, plan in enumerate(plans):
            edit = plan["edit"]
            target: Path = plan["target"]
            op = journal["operations"][index]

            # Re-verify the precondition immediately before the write: the
            # preflight-to-write window must not admit a drifted file.
            if plan["current"] is not None:
                live = target.read_bytes()
                if _sha256_bytes(live) != op["content_hash_before"]:
                    raise ApplyError(
                        f"Target {edit['relative_path']} changed between "
                        "preflight and write; aborting the proposal."
                    )

            had_file = plan["current"] is not None
            backup_path: Path | None = None
            if had_file:
                backup_path = _write_backup(
                    backup_dir,
                    op["backup_name"],
                    plan["current"],
                    within=backups_root,
                )

            # The op joins the rollback set BEFORE its mutation: a failure
            # anywhere after this point (including a post-write verification
            # failure) must restore this target, and restoring an unmutated
            # file is a harmless byte-identical write.
            completed.append(
                {
                    "relative_path": edit["relative_path"],
                    "target": str(target),
                    "had_file": had_file,
                    "backup_path": str(backup_path) if backup_path else None,
                    "content_hash_before": op["content_hash_before"],
                }
            )
            op["state"] = "in_progress"
            atomic_write_json(journal_path, journal, within=journal_dir)

            if edit["mutation_class"] == "move_to_trash":
                trash_dir = trash_root / f"{stamp}-{proposal_id}"
                trash_path = _write_backup(
                    trash_dir, op["backup_name"], plan["current"], within=trash_root
                )
                _guarded_unlink(target, source.root)
                op["trash_path"] = str(trash_path.relative_to(trash_root))
            else:
                if not target.parent.exists():
                    _recheck_parent_chain(target, source.root)
                    # Record directories this apply creates, deepest first,
                    # so rollback can remove them and the transaction stays
                    # all-or-nothing.
                    missing = []
                    probe = target.parent
                    while not probe.exists():
                        missing.append(probe)
                        probe = probe.parent
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.extend(missing)
                _guarded_replace(target, plan["post"], boundary=source.root)
                written = target.read_bytes()
                if _sha256_bytes(written) != op["content_hash_after"]:
                    raise ApplyError(
                        f"Post-write verification failed for "
                        f"{edit['relative_path']}."
                    )

            mutations += 1
            op["state"] = "done"
            atomic_write_json(journal_path, journal, within=journal_dir)

        # Post-apply validation gates run inside the transaction: a failure
        # here raises and the verified rollback below restores every target.
        validate_l0_l2(plans, source, preapply_shapes)
        manifest_after = source_manifest(source)
        validate_l3(manifest_before, manifest_after, plans)
        receipt_after = rebuild_receipt(source, state_root_dir)
        index_deltas = validate_l1(receipt_before, receipt_after, plans)
    except ApplyError:
        _rollback(completed, journal_path, journal, journal_dir, boundary=source.root)
        _remove_created_dirs(created_dirs, source.root)
        raise
    except ValidationError as error:
        _rollback(completed, journal_path, journal, journal_dir, boundary=source.root)
        _remove_created_dirs(created_dirs, source.root)
        raise ApplyError(
            f"Validation failed and the apply was rolled back: {error}"
        ) from error
    except Exception as error:
        _rollback(completed, journal_path, journal, journal_dir, boundary=source.root)
        _remove_created_dirs(created_dirs, source.root)
        raise ApplyError(
            f"Apply failed and was rolled back: {type(error).__name__}: {error}"
        ) from error

    journal["status"] = "applied"
    journal["receipt_ref"] = receipt_ref
    atomic_write_json(journal_path, journal, within=journal_dir)

    if git_info["git_used"] and execute:
        try:
            commit_result = commit_applied(
                source.root,
                [edit["relative_path"] for edit in proposal["edits"]],
                proposal_id=proposal_id,
                journal_ref=journal_path.name,
            )
            git_receipt: dict[str, Any] = {
                "used": True,
                "head_before": git_info["head"],
                "commit": commit_result["commit"],
                "branch": git_info["branch"],
            }
        except GitError as error:
            # The apply already succeeded and is journaled; a failure to
            # record it in git is reported, not raised.
            git_receipt = {
                "used": True,
                "commit": None,
                "commit_error": str(error),
            }
    else:
        git_receipt = {"used": False}

    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": APPLY_RECEIPT_KIND,
        "operation": "steward_apply",
        "generated_at": generated_at,
        "proposal_id": proposal_id,
        "source": source.name,
        "dry_run": False,
        "applied": True,
        "mode": mode,
        "policy_sha256": policy.policy_sha256,
        "edits": planned_ops,
        "journal_ref": journal_path.name,
        "backup_dir": backup_dir.name,
        "validation": {"passed": True, "index_deltas": index_deltas},
        "steward_vault_mutations": mutations,
        "network_calls": 0,
        "vault_writes": mutations,
        "git": git_receipt,
    }


def _validated_journal_ops(
    journal: dict,
    journal_name: str,
    *,
    source: Any,
    state_dirs: dict[str, Path],
    states: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Validate a journal document before any mutation it could drive.

    Journals are steward-authored, but they are on-disk state an operator or
    another tool can edit; every path they carry is re-validated for shape,
    containment, and symlink-safety before recovery or revert acts on it."""

    backup_dir_name = journal.get("backup_dir")
    if (
        not isinstance(backup_dir_name, str)
        or "/" in backup_dir_name
        or "\\" in backup_dir_name
        or backup_dir_name in ("", ".", "..")
    ):
        raise ApplyError(
            f"Journal {journal_name} carries an invalid backup_dir."
        )
    backups_root = state_dirs["backups"]
    backup_dir = backups_root / backup_dir_name
    resolved_source = source.root.resolve()

    completed: list[dict[str, Any]] = []
    for op in journal.get("operations", []):
        if not isinstance(op, dict):
            raise ApplyError(f"Journal {journal_name} carries a malformed operation.")
        state = op.get("state")
        if state not in ("planned", "in_progress", "done"):
            raise ApplyError(
                f"Journal {journal_name} operation has invalid state {state!r}."
            )
        relative = op.get("relative_path")
        if not isinstance(relative, str):
            raise ApplyError(f"Journal {journal_name} operation has no path.")
        _require_clean_relative_path(relative)
        if op.get("mutation_class") not in _EXECUTABLE_CLASSES:
            raise ApplyError(
                f"Journal {journal_name} operation has an invalid class."
            )
        backup_name = op.get("backup_name")
        if (
            not isinstance(backup_name, str)
            or "/" in backup_name
            or "\\" in backup_name
            or backup_name in ("", ".", "..")
        ):
            raise ApplyError(
                f"Journal {journal_name} operation has an invalid backup name."
            )
        for hash_key in ("content_hash_before", "content_hash_after"):
            value = op.get(hash_key)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ApplyError(
                    f"Journal {journal_name} operation has an invalid {hash_key}."
                )

        target = source.root / relative
        # Symlink-safe containment of the restore target.
        current = source.root
        for part in Path(relative).parts[:-1]:
            current = current / part
            if is_link_like(current):
                raise ApplyError(
                    f"Journal {journal_name}: restore path passes through a "
                    f"symlinked directory: {current}"
                )
        if is_link_like(target):
            raise ApplyError(
                f"Journal {journal_name}: restore target is a symlink: {target}"
            )
        if target.exists():
            resolved_target = target.resolve()
            if not (
                resolved_target == resolved_source
                or resolved_source in resolved_target.parents
            ):
                raise ApplyError(
                    f"Journal {journal_name}: restore target escapes the "
                    f"source root: {relative}"
                )

        backup_path = backup_dir / backup_name
        if backup_path.exists():
            guard_within(backup_path, backups_root)

        if state not in states:
            continue
        completed.append(
            {
                "relative_path": relative,
                "target": str(target),
                "had_file": op.get("content_hash_before") is not None,
                "backup_path": str(backup_path),
                "content_hash_before": op.get("content_hash_before"),
                "content_hash_after": op.get("content_hash_after"),
                "state": state,
                "_op": op,
            }
        )
    return completed


# Applied proposals per sweep --apply leg are capped: automation must not
# turn a large backlog into one unreviewable batch.
MAX_AUTO_APPLIES_PER_SWEEP = 10


def sweep_auto_apply(
    registry: SourceRegistry,
    state_dirs: dict[str, Path],
    database: Path,
    *,
    write_policy: WritePolicy,
) -> dict[str, Any]:
    """The sweep's --apply leg: execute pending proposals whose EVERY edit
    resolves to auto_apply under the write policy. Anything needing approval
    stays pending for steward-apply. Assumes the caller holds no state lock
    conflicts (apply_latest-style locking is managed by the caller)."""

    proposals_dir = state_dirs["proposals"]
    receipts_dir = state_dirs["receipts"]
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in sorted(proposals_dir.glob("*.json")):
        if len(applied) >= MAX_AUTO_APPLIES_PER_SWEEP:
            skipped.append({"proposal": path.name, "reason": "sweep_cap"})
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"proposal": path.name, "reason": "unreadable"})
            continue
        if document.get("status") == "applied":
            continue
        edits = document.get("edits") or []
        if not edits:
            continue
        source = next(
            (
                item
                for item in registry.sources
                if item.name == document.get("source")
            ),
            None,
        )
        if source is None:
            skipped.append({"proposal": path.name, "reason": "unknown_source"})
            continue
        eligible = all(
            isinstance(edit, dict)
            and resolve_level(
                write_policy,
                mutation_class=edit.get("mutation_class", ""),
                source_name=source.name,
                relative_path=edit.get("relative_path", ""),
                frontmatter=None,
            )[0]
            == "auto_apply"
            for edit in edits
        )
        if not eligible:
            skipped.append({"proposal": path.name, "reason": "not_auto_apply"})
            continue
        try:
            receipt = apply_proposal(
                document,
                registry=registry,
                state_dirs=state_dirs,
                database=database,
                policy=write_policy,
                mode="auto",
                execute=True,
            )
        except (ApplyError, RollbackError) as error:
            failures.append(
                {
                    "proposal": document.get("proposal_id"),
                    "error": type(error).__name__,
                    "rolled_back": not isinstance(error, RollbackError),
                }
            )
            continue
        updated = dict(document)
        updated["status"] = "applied"
        updated["applied_receipt_ref"] = receipt.get("journal_ref")
        atomic_write_json(path, updated, within=proposals_dir)
        receipt_name = (
            f"{_file_timestamp(receipt['generated_at'])}-"
            f"{document['proposal_id']}.json"
        )
        atomic_write_json(
            receipts_dir / receipt_name, receipt, within=receipts_dir
        )
        applied.append(
            {
                "proposal": document.get("proposal_id"),
                "mutations": receipt.get("steward_vault_mutations", 0),
                "journal_ref": receipt.get("journal_ref"),
                "git_commit": (receipt.get("git") or {}).get("commit"),
            }
        )
    return {
        "applied": applied,
        "skipped": skipped,
        "failures": failures,
        "mutations": sum(item["mutations"] for item in applied),
    }


def _incomplete_journals(journal_dir: Path) -> list[Path]:
    incomplete: list[Path] = []
    for path in sorted(journal_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            incomplete.append(path)
            continue
        if document.get("status") in ("intent", None):
            incomplete.append(path)
    return incomplete


def recover_journal(
    journal_name: str,
    *,
    registry: SourceRegistry,
    state_dirs: dict[str, Path],
) -> dict[str, Any]:
    """Verified rollback of an interrupted apply, driven by its journal."""

    if "/" in journal_name or "\\" in journal_name or journal_name in ("", ".", ".."):
        raise ApplyError(f"Invalid journal name: {journal_name!r}")
    journal_dir = state_dirs["journal"]
    journal_path = journal_dir / journal_name
    if not journal_path.is_file():
        raise ApplyError(f"No such journal: {journal_name}")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("status") not in ("intent",):
        raise ApplyError(
            f"Journal {journal_name} has status {journal.get('status')!r}; "
            "only an interrupted (intent) journal can be recovered."
        )
    source = next(
        (item for item in registry.sources if item.name == journal.get("source")),
        None,
    )
    if source is None:
        raise ApplyError(
            f"Journal {journal_name} names an unknown source "
            f"{journal.get('source')!r}."
        )
    _require_root_identity(source)
    candidates = _validated_journal_ops(
        journal,
        journal_name,
        source=source,
        state_dirs=state_dirs,
        states=("done", "in_progress"),
    )
    # Two passes: classify EVERY operation first (no mutation), so a drift
    # anywhere refuses the whole recovery before anything is touched; only a
    # fully clean journal proceeds to restores and deletions.
    completed = []
    creates_to_remove: list[Path] = []
    creates_removed = 0
    drifted: list[str] = []
    for item in candidates:
        state = item.pop("state")
        mutation_class = (item.pop("_op", None) or {}).get("mutation_class")
        content_hash_after = item.pop("content_hash_after", None)
        backup_exists = Path(item["backup_path"]).exists()
        target = Path(item["target"])
        live: str | None = None
        if target.is_file():
            live = _sha256_bytes(target.read_bytes())

        # Recovery is hash-pinned like every other write. A target matching
        # the journaled post-apply state is restorable; one already matching
        # the pre-apply state needs nothing; anything else was edited (or
        # torn) after the crash, and recovery refuses rather than destroy
        # that newer work -- the backup path is named so the operator can
        # decide by hand.
        if state == "done" or (state == "in_progress" and backup_exists):
            if item["content_hash_before"] is not None:
                if mutation_class == "move_to_trash" and live is None:
                    # The unlink landed before the crash: restore the
                    # original bytes from the verified backup.
                    if backup_exists:
                        completed.append(item)
                elif content_hash_after is not None and live == content_hash_after:
                    completed.append(item)
                elif live == item["content_hash_before"]:
                    pass  # already at pre-apply bytes; nothing to undo
                elif state == "in_progress" and live is None:
                    pass  # the mutation never landed
                else:
                    drifted.append(
                        f"{item['relative_path']} (backup: {item['backup_path']})"
                    )
            else:
                # A completed create: delete only the exact planned bytes.
                if live is not None and live == content_hash_after:
                    creates_to_remove.append(target)
                elif live is not None:
                    drifted.append(
                        f"{item['relative_path']} (created then edited; left in place)"
                    )
        elif state == "in_progress" and item["content_hash_before"] is None:
            if live is not None and content_hash_after is not None:
                if live == content_hash_after:
                    creates_to_remove.append(target)
    if drifted:
        raise ApplyError(
            "Refusing to recover: these files changed after the interrupted "
            f"apply and a restore would overwrite that newer work: "
            f"{sorted(drifted)}. Inspect the named backups and resolve by "
            "hand, then remove or edit the journal."
        )
    for target in creates_to_remove:
        _guarded_unlink(target, source.root)
        creates_removed += 1
    _rollback(completed, journal_path, journal, journal_dir, boundary=source.root)
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": "apply_recovery_receipt",
        "operation": "steward_apply",
        "generated_at": _utc_now(),
        "journal_ref": journal_name,
        "operations_rolled_back": len(completed),
        "creates_removed": creates_removed,
        "network_calls": 0,
        "vault_writes": len(completed) + creates_removed,
    }


def revert_journal(
    journal_name: str,
    *,
    registry: SourceRegistry,
    state_dirs: dict[str, Path],
) -> dict[str, Any]:
    """Operator regret: verified restore of a successfully APPLIED journal.

    Rollback is not undo — the index must be rebuilt afterwards and exported
    artifacts are unaffected — but every byte the apply wrote is restored
    from its verified backup."""

    if "/" in journal_name or "\\" in journal_name or journal_name in ("", ".", ".."):
        raise ApplyError(f"Invalid journal name: {journal_name!r}")
    journal_dir = state_dirs["journal"]
    journal_path = journal_dir / journal_name
    if not journal_path.is_file():
        raise ApplyError(f"No such journal: {journal_name}")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("status") != "applied":
        raise ApplyError(
            f"Journal {journal_name} has status {journal.get('status')!r}; "
            "only an applied journal can be reverted (use --recover for an "
            "interrupted one)."
        )
    source = next(
        (item for item in registry.sources if item.name == journal.get("source")),
        None,
    )
    if source is None:
        raise ApplyError(
            f"Journal {journal_name} names an unknown source "
            f"{journal.get('source')!r}."
        )
    _require_root_identity(source)
    candidates = _validated_journal_ops(
        journal,
        journal_name,
        source=source,
        state_dirs=state_dirs,
        states=("done",),
    )
    # A revert is hash-pinned like every other write: the live target must
    # still hold the journaled post-apply bytes, or the operator edited it
    # since and the revert would destroy that newer work.
    drifted: list[str] = []
    for item in candidates:
        target = Path(item["target"])
        expected_after = item.get("content_hash_after")
        if expected_after is None:
            if target.exists():
                drifted.append(item["relative_path"])
            continue
        if not target.is_file():
            drifted.append(item["relative_path"])
            continue
        if _sha256_bytes(target.read_bytes()) != expected_after:
            drifted.append(item["relative_path"])
    if drifted:
        raise ApplyError(
            "Refusing to revert: these files changed after the apply and a "
            f"revert would overwrite that newer work: {sorted(drifted)}. "
            "Re-run the steward pipeline instead."
        )
    completed = [
        {
            key: value
            for key, value in item.items()
            if key not in ("state", "_op", "content_hash_after")
        }
        for item in candidates
    ]
    _rollback(completed, journal_path, journal, journal_dir, boundary=source.root)
    journal["status"] = "reverted"
    atomic_write_json(journal_path, journal, within=journal_dir)
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": "apply_revert_receipt",
        "operation": "steward_apply",
        "generated_at": _utc_now(),
        "journal_ref": journal_name,
        "operations_reverted": len(completed),
        "network_calls": 0,
        "vault_writes": len(completed),
    }


def apply_latest(
    registry: SourceRegistry,
    state_root: Path,
    database: Path,
    *,
    write_policy: WritePolicy,
    proposal_id: str | None = None,
    approve_class: str | None = None,
    recover: str | None = None,
    revert: str | None = None,
    execute: bool = False,
    allow_sync_root: bool = False,
) -> dict[str, Any]:
    """CLI entry point: apply one named proposal, a mutation class, or recover.

    Exactly one of ``proposal_id``, ``approve_class``, ``recover`` must be
    given. Dry-run is the only mode without ``execute``."""

    chosen = [
        value for value in (proposal_id, approve_class, recover, revert) if value
    ]
    if len(chosen) != 1:
        raise ApplyError(
            "Exactly one of a proposal id, --approve-class, --recover, or "
            "--revert is required."
        )

    state_root = Path(state_root)
    database = Path(database)
    ensure_state_root_outside_sources(
        state_root, [source.root for source in registry.sources]
    )
    dirs = ensure_state_layout(state_root)

    with lock_state(state_root):
        if recover is not None:
            if not execute:
                raise ApplyError(
                    "--recover mutates the source (it restores backups); pass "
                    "--execute to confirm."
                )
            return recover_journal(
                recover, registry=registry, state_dirs=dirs
            )
        if revert is not None:
            if not execute:
                raise ApplyError(
                    "--revert mutates the source (it restores backups); pass "
                    "--execute to confirm."
                )
            return revert_journal(
                revert, registry=registry, state_dirs=dirs
            )

        incomplete = _incomplete_journals(dirs["journal"])
        if incomplete:
            raise ApplyError(
                "An earlier apply was interrupted; recover it first with "
                "--recover "
                + incomplete[0].name
            )

        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(dirs["proposals"].glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise ApplyError(
                    f"Proposal file {path.name} is not valid JSON: {error}"
                ) from error
            if document.get("status") == "applied":
                continue
            candidates.append((path, document))

        selected: list[tuple[Path, dict[str, Any]]] = []
        if proposal_id is not None:
            selected = [
                (path, document)
                for path, document in candidates
                if document.get("proposal_id") == proposal_id
            ]
            if not selected:
                raise ApplyError(
                    f"No pending proposal with id {proposal_id!r} was found."
                )
            mode = "per_item"
        else:
            if approve_class not in MUTATION_CLASSES_SET:
                raise ApplyError(
                    f"Unknown mutation class {approve_class!r}."
                )
            for path, document in candidates:
                edits = document.get("edits") or []
                if edits and all(
                    edit.get("mutation_class") == approve_class for edit in edits
                ):
                    selected.append((path, document))
            mode = "per_class"

        receipts: list[dict[str, Any]] = []
        for path, document in selected:
            receipt = apply_proposal(
                document,
                registry=registry,
                state_dirs=dirs,
                database=database,
                policy=write_policy,
                mode=mode,
                execute=execute,
                allow_sync_root=allow_sync_root,
            )
            receipts.append(receipt)
            if execute and receipt.get("applied"):
                updated = dict(document)
                updated["status"] = "applied"
                updated["applied_receipt_ref"] = receipt.get("journal_ref")
                atomic_write_json(path, updated, within=dirs["proposals"])
                receipt_name = f"{_file_timestamp(receipt['generated_at'])}-{document['proposal_id']}.json"
                atomic_write_json(
                    dirs["receipts"] / receipt_name,
                    receipt,
                    within=dirs["receipts"],
                )

        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "steward_apply_receipt",
            "operation": "steward_apply",
            "generated_at": _utc_now(),
            "dry_run": not execute,
            "mode": mode,
            "proposals": receipts,
            "steward_vault_mutations": sum(
                receipt.get("steward_vault_mutations", 0) for receipt in receipts
            ),
            "network_calls": 0,
            "vault_writes": sum(
                receipt.get("vault_writes", 0) for receipt in receipts
            ),
        }
