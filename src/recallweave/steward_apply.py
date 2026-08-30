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
from .steward_policy import (
    MUTATION_CLASSES_SET,
    WritePolicy,
    resolve_level,
)
from .steward_propose import _rebuild_bytes, _split_lines_keepends
from .steward_sources import SourceRegistry
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    guard_within,
    lock_state,
)

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


def _validate_proposal(proposal: Any) -> None:
    if not isinstance(proposal, dict):
        raise ApplyError("Proposal must be a JSON object.")
    for key in ("schema_version", "kind", "proposal_id", "source", "edits"):
        if key not in proposal:
            raise ApplyError(f"Proposal is missing required key: {key}")
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
    source_root: Path,
    database: Path,
) -> dict[str, Any]:
    """Verify one edit end-to-end without writing; return its execution plan."""

    _validate_edit(edit)
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


def _guarded_replace(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` via a same-directory fsync'd temp file and
    an atomic ``os.replace``. The symlink and containment refusals ran in
    preflight; recovery comes from the journaled state-directory backup, so no
    in-source backup rotation is performed (a retained backup directory inside
    a source would be re-indexed as notes)."""

    if is_link_like(target):
        raise ApplyError(f"Refusing to replace a symlink or junction: {target}")
    temp = target.parent / f".{target.name}.steward-apply.tmp"
    with open(temp, "wb") as handle:
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
                _guarded_replace(target, data)
                restored = target.read_bytes()
                if _sha256_bytes(restored) != op["content_hash_before"]:
                    failures.append(
                        f"{op['relative_path']}: restore verification failed, "
                        f"backup retained at {backup}"
                    )
            else:
                target.unlink(missing_ok=True)
        except OSError as error:
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
    recorded_sha = proposal.get("registry_sha256")
    if registry.registry_sha256 is not None and recorded_sha is not None and (
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
        allowed = (
            level == "auto_apply"
            or (level == "require_approval" and mode in ("per_item", "per_class"))
        )
        if not allowed:
            raise ApplyError(
                f"Write policy resolves {edit['mutation_class']!r} on "
                f"{edit['relative_path']!r} to {level!r} ({reason}); this "
                "invocation cannot authorize it."
            )

    plans = [
        _preflight_edit(edit, source.root, database)
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
        }

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
                target.unlink()
                op["trash_path"] = str(trash_path.relative_to(trash_root))
            else:
                if not target.parent.exists():
                    # Parent chain was link-checked at preflight; creating it
                    # here keeps create_new_file usable for new subfolders.
                    target.parent.mkdir(parents=True, exist_ok=True)
                _guarded_replace(target, plan["post"])
                written = target.read_bytes()
                if _sha256_bytes(written) != op["content_hash_after"]:
                    raise ApplyError(
                        f"Post-write verification failed for "
                        f"{edit['relative_path']}."
                    )

            mutations += 1
            op["state"] = "done"
            atomic_write_json(journal_path, journal, within=journal_dir)
    except ApplyError:
        _rollback(completed, journal_path, journal, journal_dir)
        raise
    except Exception as error:
        _rollback(completed, journal_path, journal, journal_dir)
        raise ApplyError(
            f"Apply failed and was rolled back: {type(error).__name__}: {error}"
        ) from error

    journal["status"] = "applied"
    journal["receipt_ref"] = receipt_ref
    atomic_write_json(journal_path, journal, within=journal_dir)

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
        "steward_vault_mutations": mutations,
        "network_calls": 0,
        "vault_writes": mutations,
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
    backup_dir = state_dirs["backups"] / str(journal.get("backup_dir"))
    completed = []
    for op in journal.get("operations", []):
        state = op.get("state")
        backup_path = backup_dir / op["backup_name"]
        # "done" ops definitely mutated; an "in_progress" op may have (a
        # crash can land between the mutation and the journal update), so any
        # op whose backup exists is restored -- a verified byte-identical
        # restore of an unmutated file is harmless.
        if state == "done" or (state == "in_progress" and backup_path.exists()):
            completed.append(
                {
                    "relative_path": op["relative_path"],
                    "target": str(source.root / op["relative_path"]),
                    "had_file": op.get("content_hash_before") is not None,
                    "backup_path": str(backup_path),
                    "content_hash_before": op.get("content_hash_before"),
                }
            )
        elif state == "in_progress" and op.get("content_hash_before") is None:
            # A create that may have landed: remove the target if it matches
            # the planned post-state; leave anything else untouched.
            target = source.root / op["relative_path"]
            if target.is_file():
                if _sha256_bytes(target.read_bytes()) == op.get(
                    "content_hash_after"
                ):
                    target.unlink()
    _rollback(completed, journal_path, journal, journal_dir)
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": "apply_recovery_receipt",
        "operation": "steward_apply",
        "generated_at": _utc_now(),
        "journal_ref": journal_name,
        "operations_rolled_back": len(completed),
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
    execute: bool = False,
    allow_sync_root: bool = False,
) -> dict[str, Any]:
    """CLI entry point: apply one named proposal, a mutation class, or recover.

    Exactly one of ``proposal_id``, ``approve_class``, ``recover`` must be
    given. Dry-run is the only mode without ``execute``."""

    chosen = [value for value in (proposal_id, approve_class, recover) if value]
    if len(chosen) != 1:
        raise ApplyError(
            "Exactly one of a proposal id, --approve-class, or --recover is "
            "required."
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
