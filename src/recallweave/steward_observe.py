from __future__ import annotations

import hashlib
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .parser import parse_note
from .policy import RESERVED_DIRECTORY_NAMES
from .steward_checkpoint import (
    CheckpointEntry,
    CheckpointError,
    load_checkpoint,
    save_checkpoint,
)
from .steward_sources import SourceRegistry, StewardSource
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    StateLock,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
)

CHANGE_BATCH_KIND = "change_batch"

_CHUNK_SIZE = 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _file_timestamp(iso: str) -> str:
    # Microsecond precision: two runs in the same wall-clock second must not
    # collide on artifact names (a collision let an assessed batch be silently
    # overwritten and its replacement skipped as already assessed).
    value = datetime.fromisoformat(iso)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _walk_markdown(source_root: Path, skipped: Counter[str]) -> list[Path]:
    """Mirror index._markdown_files but keep per-file stat reachable later.

    Reserved directories are pruned in place; symlinks, hardlinks, paths that
    resolve outside the root, and duplicate resolved targets are skipped and
    counted by reason. Only *.md candidates are returned."""
    candidates: list[tuple[Path, Path]] = []
    for root, directory_names, file_names in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        kept_directories = []
        for name in directory_names:
            directory = root_path / name
            if directory.is_symlink():
                skipped["symlink"] += 1
                continue
            if name.casefold() in RESERVED_DIRECTORY_NAMES:
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = root_path / name
            if path.suffix.casefold() != ".md":
                continue
            if path.is_symlink():
                skipped["symlink"] += 1
                continue
            try:
                if path.stat(follow_symlinks=False).st_nlink > 1:
                    skipped["hardlink"] += 1
                    continue
                resolved = path.resolve(strict=True)
            except OSError:
                skipped["unreadable_path"] += 1
                continue
            if not _inside(resolved, source_root):
                skipped["outside_vault"] += 1
                continue
            candidates.append((path, resolved))

    paths: list[Path] = []
    seen_resolved_paths: set[str] = set()
    for path, resolved in sorted(
        candidates,
        key=lambda item: item[0].relative_to(source_root).as_posix().casefold(),
    ):
        resolved_key = os.path.normcase(str(resolved))
        if resolved_key in seen_resolved_paths:
            skipped["duplicate_resolved_path"] += 1
            continue
        seen_resolved_paths.add(resolved_key)
        paths.append(path)
    return paths


def _admitted_paths(source: StewardSource, root: Path, skipped: Counter[str]) -> list[Path]:
    if source.type == "file":
        if root.suffix.casefold() != ".md":
            return []
        if root.is_symlink():
            skipped["symlink"] += 1
            return []
        try:
            if root.stat(follow_symlinks=False).st_nlink > 1:
                skipped["hardlink"] += 1
                return []
        except OSError:
            skipped["unreadable_path"] += 1
            return []
        return [root]
    return _walk_markdown(root, skipped)


def _relative_for(source: StewardSource, root: Path, path: Path) -> str:
    if source.type == "file":
        return root.name
    return path.relative_to(root).as_posix()


def _entry_from_prior(prior: dict) -> CheckpointEntry:
    return CheckpointEntry(
        relative_path=prior["relative_path"],
        content_hash=prior["content_hash"],
        size=int(prior["size"]),
        mtime_ns=int(prior["mtime_ns"]),
        file_dev=int(prior["file_dev"]),
        file_ino=int(prior["file_ino"]),
    )


def _change(relative: str, change_type: str, previous: str | None, current: str | None) -> dict:
    return {
        "relative_path": relative,
        "change_type": change_type,
        "previous_content_hash": previous,
        "current_content_hash": current,
    }


def _source_missing_receipt(
    source: StewardSource,
    generated_at: str,
    registry_sha256: str | None,
) -> dict:
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": CHANGE_BATCH_KIND,
        "operation": "steward_observe",
        "generated_at": generated_at,
        "source": source.name,
        "registry_sha256": registry_sha256,
        "error": "source_missing",
        "changes": [],
        "rename_candidates": [],
        "change_summary": {"added": 0, "modified": 0, "removed": 0},
        "skipped": {},
        "changed_during_observe": [],
        "network_calls": 0,
        "vault_writes": 0,
    }


def observe_source(
    source: StewardSource,
    state_dirs: dict[str, Path],
    *,
    registry_sha256: str | None,
    now: str | None = None,
) -> dict:
    """Observe one source and return its change batch document.

    The checkpoint is updated after the batch is computed so a later run over an
    unchanged tree reports an empty change list. Nothing outside the state dir
    is written."""
    generated_at = now if now is not None else _utc_now()

    try:
        resolved_root = source.root.resolve(strict=True)
    except OSError:
        return _source_missing_receipt(source, generated_at, registry_sha256)
    if source.type == "file":
        if not resolved_root.is_file():
            return _source_missing_receipt(source, generated_at, registry_sha256)
    elif not resolved_root.is_dir():
        return _source_missing_receipt(source, generated_at, registry_sha256)

    checkpoint_invalid = False
    prior_entries: dict[str, dict] = {}
    try:
        checkpoint_payload = load_checkpoint(state_dirs, source.name)
    except CheckpointError:
        checkpoint_payload = None
        checkpoint_invalid = True
    if checkpoint_payload is not None:
        prior_entries = {
            entry["relative_path"]: entry for entry in checkpoint_payload["entries"]
        }

    skipped: Counter[str] = Counter()
    admitted = _admitted_paths(source, resolved_root, skipped)

    changes: dict[str, dict] = {}
    new_entries: dict[str, CheckpointEntry] = {}
    changed_during_observe: list[str] = []
    entry_stats: dict[str, tuple[int, int]] = {}
    policy_excluded: set[str] = set()

    for path in admitted:
        relative = _relative_for(source, resolved_root, path)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            skipped["unreadable_path"] += 1
            continue
        size = int(info.st_size)
        mtime_ns = int(info.st_mtime_ns)
        dev = int(info.st_dev)
        ino = int(info.st_ino)

        allowed, reason = source.policy.path_allowed(relative, size)
        if not allowed:
            skipped[reason or "policy"] += 1
            policy_excluded.add(relative)
            continue

        # The full IndexPolicy applies, frontmatter denial included: a note
        # the index would refuse must not be hashed or recorded by path
        # anywhere in steward state. Parsing mirrors indexing's fail-closed
        # behavior; with no deny rules configured, frontmatter_allowed always
        # admits and the parse is skipped.
        if source.policy.deny_frontmatter:
            try:
                note = parse_note(path, resolved_root)
            except UnicodeError:
                skipped["unsupported_encoding"] += 1
                policy_excluded.add(relative)
                continue
            except RecursionError:
                skipped["unparseable_frontmatter"] += 1
                policy_excluded.add(relative)
                continue
            allowed, reason = source.policy.frontmatter_allowed(
                note.frontmatter, valid=note.frontmatter_valid
            )
            if not allowed:
                skipped[reason or "frontmatter_policy"] += 1
                policy_excluded.add(relative)
                continue

        # Every admitted file is hashed on every sweep. A size-and-mtime gate
        # would be cheaper, but equal-length bytes with a restored mtime would
        # then pass as unchanged — an integrity sweep may not treat stat
        # equality as proof of byte equality.
        prior = prior_entries.get(relative)
        current_hash = _hash_file(path)
        try:
            after = path.stat(follow_symlinks=False)
        except OSError:
            changed_during_observe.append(relative)
            if prior is not None:
                new_entries[relative] = _entry_from_prior(prior)
            continue
        if (
            int(after.st_size) != size
            or int(after.st_mtime_ns) != mtime_ns
            or int(after.st_dev) != dev
            or int(after.st_ino) != ino
        ):
            changed_during_observe.append(relative)
            if prior is not None:
                new_entries[relative] = _entry_from_prior(prior)
            continue

        entry_stats[relative] = (dev, ino)
        new_entries[relative] = CheckpointEntry(
            relative_path=relative,
            content_hash=current_hash,
            size=size,
            mtime_ns=mtime_ns,
            file_dev=dev,
            file_ino=ino,
        )
        if prior is None:
            changes[relative] = _change(relative, "added", None, current_hash)
        elif prior["content_hash"] != current_hash:
            changes[relative] = _change(
                relative, "modified", prior["content_hash"], current_hash
            )

    removed_paths = sorted(set(prior_entries) - set(new_entries) - policy_excluded)
    for relative in removed_paths:
        changes[relative] = _change(
            relative, "removed", prior_entries[relative]["content_hash"], None
        )

    change_records = sorted(changes.values(), key=lambda record: record["relative_path"])

    added_records = [r for r in change_records if r["change_type"] == "added"]
    removed_records = [r for r in change_records if r["change_type"] == "removed"]
    added_by_hash: dict[str, list[dict]] = defaultdict(list)
    for record in added_records:
        added_by_hash[record["current_content_hash"]].append(record)
    rename_candidates: list[dict] = []
    for record in removed_records:
        previous = record["previous_content_hash"]
        if previous is None or previous not in added_by_hash:
            continue
        matches = added_by_hash[previous]
        removed_dev, removed_ino = (
            int(prior_entries[record["relative_path"]]["file_dev"]),
            int(prior_entries[record["relative_path"]]["file_ino"]),
        )
        inode_match = any(
            entry_stats[match["relative_path"]] == (removed_dev, removed_ino)
            for match in matches
        )
        rename_candidates.append(
            {
                "removed_path": record["relative_path"],
                "added_paths": sorted(match["relative_path"] for match in matches),
                "content_hash": previous,
                "inode_match": inode_match,
            }
        )

    save_checkpoint(
        state_dirs,
        source.name,
        sorted(new_entries.values(), key=lambda entry: entry.relative_path),
        generated_at=generated_at,
        registry_sha256=registry_sha256,
    )

    receipt: dict[str, Any] = {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": CHANGE_BATCH_KIND,
        "operation": "steward_observe",
        "generated_at": generated_at,
        "source": source.name,
        "registry_sha256": registry_sha256,
        "changes": change_records,
        "rename_candidates": rename_candidates,
        "change_summary": {
            "added": sum(1 for r in change_records if r["change_type"] == "added"),
            "modified": sum(1 for r in change_records if r["change_type"] == "modified"),
            "removed": sum(1 for r in change_records if r["change_type"] == "removed"),
        },
        "skipped": dict(sorted(skipped.items())),
        "changed_during_observe": sorted(changed_during_observe),
        "network_calls": 0,
        "vault_writes": 0,
    }
    if checkpoint_invalid:
        receipt["checkpoint_invalid"] = True
    return receipt


def observe_registry(registry: SourceRegistry, state_root: Path) -> dict:
    """Observe every source in the registry under a single state lock.

    Each source's change batch is written to changes/<UTC>-<source>.json and its
    checkpoint is rotated (persisted) by observe_source. Returns the combined
    receipt; a missing source root is reported without writing a batch or
    touching its checkpoint."""
    ensure_state_root_outside_sources(
        state_root, [source.root for source in registry.sources]
    )
    state_dirs = ensure_state_layout(state_root)
    generated_at = _utc_now()
    receipts: list[dict] = []
    with StateLock(state_root):
        for source in registry.sources:
            batch = observe_source(
                source,
                state_dirs,
                registry_sha256=registry.registry_sha256,
                now=generated_at,
            )
            if batch.get("error") == "source_missing":
                receipts.append(batch)
                continue
            filename = f"{_file_timestamp(generated_at)}-{source.name}.json"
            atomic_write_json(
                state_dirs["changes"] / filename,
                batch,
                within=state_dirs["changes"],
            )
            receipts.append(batch)
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": "observe_receipt",
        "operation": "steward_observe",
        "generated_at": generated_at,
        "registry_sha256": registry.registry_sha256,
        "sources": receipts,
        "network_calls": 0,
        "vault_writes": 0,
    }
