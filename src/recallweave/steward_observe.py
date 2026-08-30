from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .parser import parse_frontmatter, parse_note
from .safe_write import is_link_like, path_identity
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


_DIR_FD_OBSERVE = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def _open_note_fd(source: Any, resolved_root: Path, base: Path, relative: str) -> int:
    """Open a note for reading WITHOUT following a symlink at any component.

    Anchored, where possible, to the registry's HELD root descriptor
    (``source.root_fd``, opened O_NOFOLLOW at load and identity-pinned), so a
    root renamed-and-replaced after discovery cannot redirect the read into a
    replacement tree. For a ``type: file`` source the note IS that pinned
    descriptor. Falls back to a symlink-checked pathname descent from ``base``
    only when no held descriptor / dir_fd support is available. Raises OSError
    on any symlinked/non-directory component, a missing leaf, or a failed open.
    The caller fstats the fd (verifying a regular, single-link file) and reads
    it."""

    root_fd = getattr(source, "root_fd", None)
    # O_BINARY (Windows only; 0 elsewhere) keeps os.read from translating CRLF,
    # so the bytes read match st_size and the note's true content hash.
    read_flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )

    if source.type == "file":
        # The single admitted path is the root file itself, pinned at load.
        if root_fd is not None:
            return os.dup(root_fd)
        return os.open(resolved_root, read_flags)

    parts = Path(relative).parts
    if not parts:
        raise OSError("empty relative path")

    if root_fd is not None and _DIR_FD_OBSERVE:
        # Descend from a dup of the pinned root descriptor (not a fresh open of
        # the root pathname), so traversal starts from the identity-verified
        # inode regardless of a concurrent root swap.
        fds: list[int] = [os.dup(root_fd)]
        try:
            for part in parts[:-1]:
                fds.append(
                    os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fds[-1],
                    )
                )
            return os.open(parts[-1], read_flags, dir_fd=fds[-1])
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    if _DIR_FD_OBSERVE:
        fds = [os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
        try:
            for part in parts[:-1]:
                fds.append(
                    os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fds[-1],
                    )
                )
            return os.open(parts[-1], read_flags, dir_fd=fds[-1])
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # Pathname fallback (e.g. Windows): reject a symlink at any component.
    current = base
    for part in parts[:-1]:
        current = current / part
        if is_link_like(current):
            raise OSError(f"symlinked ancestor: {current}")
    full = base / relative
    if is_link_like(full):
        raise OSError(f"symlink leaf: {full}")
    return os.open(full, read_flags)


def _pinned_stat(fd: int) -> os.stat_result:
    return os.fstat(fd)


def _read_fd(fd: int, limit: int) -> bytes:
    # Read at most ``limit`` bytes (the policy-checked size) plus one, so a note
    # that grows after the size check cannot drive an unbounded read; the extra
    # byte lets the caller's after-fstat detect the growth and mark the note
    # changed-during-observe rather than committing a partial snapshot.
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = max(0, limit) + 1
    while remaining > 0:
        chunk = os.read(fd, min(_CHUNK_SIZE, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


_DIR_FD_RETRACT = (
    os.unlink in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def _retract_change_batch(changes_dir: Path, filename: str) -> bool:
    """Delete a just-written change batch and VERIFY it is gone, descriptor-
    relative to the pinned state root so a changes/ directory swapped for a
    symlink cannot redirect the unlink (or its existence check) outside the state
    tree. Returns True only when the batch is confirmed absent from the real
    changes directory."""

    state_root = changes_dir.parent
    if _DIR_FD_RETRACT:
        try:
            root_fd = os.open(
                state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError:
            return False
        try:
            try:
                changes_fd = os.open(
                    changes_dir.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError:
                return False
            try:
                try:
                    os.unlink(filename, dir_fd=changes_fd)
                except FileNotFoundError:
                    return True
                except OSError:
                    pass
                try:
                    os.stat(filename, dir_fd=changes_fd, follow_symlinks=False)
                    return False  # still present after the unlink attempt
                except FileNotFoundError:
                    return True
                except OSError:
                    return False
            finally:
                os.close(changes_fd)
        finally:
            os.close(root_fd)

    # Pathname fallback (e.g. Windows without dir_fd).
    try:
        (changes_dir / filename).unlink()
    except OSError:
        pass
    return not (changes_dir / filename).exists()


def _frontmatter_from_bytes(data: bytes) -> tuple[dict, bool]:
    """Frontmatter + validity from a note's raw bytes, matching parse_note's
    decoding, so the frontmatter-denial check uses exactly the hashed bytes."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnicodeError("UTF-16 Markdown is not supported.")
    raw = data.decode("utf-8-sig", errors="strict")
    lines = re.split(r"\r\n|\r|\n", raw)
    frontmatter, _body_start, frontmatter_valid, _error = parse_frontmatter(lines)
    return frontmatter, frontmatter_valid


def _walk_markdown(
    source_root: Path,
    skipped: Counter[str],
    traversal_failures: list[Path] | None = None,
) -> list[Path]:
    """Mirror index._markdown_files but keep per-file stat reachable later.

    Reserved directories are pruned in place; symlinks, hardlinks, paths that
    resolve outside the root, and duplicate resolved targets are skipped and
    counted by reason. Only *.md candidates are returned.

    A subtree whose ``scandir`` fails (a transient permission or I/O error) is
    recorded in ``traversal_failures`` rather than being silently omitted --
    the caller retains that subtree's prior checkpoint entries instead of
    reporting every note under it as deleted."""

    def _on_error(error: OSError) -> None:
        skipped["traversal_error"] += 1
        name = getattr(error, "filename", None)
        if traversal_failures is not None and isinstance(name, str):
            traversal_failures.append(Path(name))

    candidates: list[tuple[Path, Path]] = []
    for root, directory_names, file_names in os.walk(
        source_root, followlinks=False, onerror=_on_error
    ):
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


def _admitted_paths(
    source: StewardSource,
    root: Path,
    skipped: Counter[str],
    traversal_failures: list[Path] | None = None,
) -> list[Path]:
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
    return _walk_markdown(root, skipped, traversal_failures)


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
    return _source_error_receipt(
        source, generated_at, registry_sha256, "source_missing"
    )


def _source_error_receipt(
    source: StewardSource,
    generated_at: str,
    registry_sha256: str | None,
    error: str,
) -> dict:
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": CHANGE_BATCH_KIND,
        "operation": "steward_observe",
        "generated_at": generated_at,
        "source": source.name,
        "registry_sha256": registry_sha256,
        "error": error,
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

    # Re-verify the root at observation time: a root replaced by a symlink
    # (or by any other filesystem object) after the registry was loaded must
    # not rebind the source boundary.
    if is_link_like(source.root):
        return _source_error_receipt(
            source, generated_at, registry_sha256, "source_root_symlinked"
        )
    try:
        resolved_root = source.root.resolve(strict=True)
    except OSError:
        return _source_missing_receipt(source, generated_at, registry_sha256)
    if source.type == "file":
        if not resolved_root.is_file():
            return _source_missing_receipt(source, generated_at, registry_sha256)
    elif not resolved_root.is_dir():
        return _source_missing_receipt(source, generated_at, registry_sha256)
    if source.root_dev is not None and source.root_ino is not None:
        try:
            identity = path_identity(resolved_root)
        except OSError:
            return _source_missing_receipt(source, generated_at, registry_sha256)
        if identity != (source.root_dev, source.root_ino):
            return _source_error_receipt(
                source, generated_at, registry_sha256, "source_identity_changed"
            )

    checkpoint_invalid = False
    prior_entries: dict[str, dict] = {}
    try:
        checkpoint_payload = load_checkpoint(state_dirs, source.name)
    except CheckpointError:
        checkpoint_payload = None
        checkpoint_invalid = True
    if (
        checkpoint_payload is not None
        and checkpoint_payload.get("registry_sha256") != registry_sha256
    ):
        # The checkpoint was recorded under a different source registry (e.g. a
        # source root was repointed while keeping its name). Diffing the new
        # tree against the old baseline would emit false removals/additions and
        # bogus rename candidates. Rebaseline: ignore the stale checkpoint and
        # let this run establish a fresh baseline under the active digest.
        checkpoint_payload = None
        checkpoint_invalid = True
    if checkpoint_payload is not None:
        prior_entries = {
            entry["relative_path"]: entry for entry in checkpoint_payload["entries"]
        }

    skipped: Counter[str] = Counter()
    traversal_failures: list[Path] = []
    admitted = _admitted_paths(
        source, resolved_root, skipped, traversal_failures
    )

    # Re-verify the root's pinned identity AFTER enumeration. Directory walking
    # is by pathname, so a root renamed-and-replaced (e.g. by an empty dir)
    # during the walk would return the wrong file set and manufacture false
    # removals -- and then advance the checkpoint past them. Enumeration is only
    # trustworthy if the root is still the identity the registry pinned; if not,
    # emit a source_identity_changed error and touch nothing.
    if source.root_dev is not None and source.root_ino is not None:
        try:
            post_identity = path_identity(resolved_root)
        except OSError:
            return _source_missing_receipt(source, generated_at, registry_sha256)
        if post_identity != (source.root_dev, source.root_ino):
            return _source_error_receipt(
                source, generated_at, registry_sha256, "source_identity_changed"
            )

    changes: dict[str, dict] = {}
    new_entries: dict[str, CheckpointEntry] = {}
    changed_during_observe: list[str] = []
    policy_excluded: set[str] = set()

    # Base for symlink-free, descriptor-relative opens: a file source's note is
    # its own root, so descend from the parent; a folder source descends from
    # the root itself.
    hash_base = resolved_root.parent if source.type == "file" else resolved_root

    def _mark_changed_during(rel: str) -> None:
        changed_during_observe.append(rel)
        prior_entry = prior_entries.get(rel)
        if prior_entry is not None:
            new_entries[rel] = _entry_from_prior(prior_entry)

    for path in admitted:
        relative = _relative_for(source, resolved_root, path)
        # Open the file WITHOUT following a symlink at any component, so a parent
        # swapped for a symlink after discovery cannot redirect the stat/hash to
        # a file outside the source (which would be committed to the checkpoint
        # as a vault note). All metadata and bytes come from this one fd.
        try:
            fd = _open_note_fd(source, resolved_root, hash_base, relative)
        except OSError:
            # A previously checkpointed note that becomes unreadable, vanishes,
            # or is symlink-swapped between enumeration and open must NOT be
            # dropped silently: doing so omits it from new_entries, so the later
            # set difference emits a FALSE removal and advances the checkpoint
            # past it -- a subsequent stable run then reports it as newly added.
            # Route through _mark_changed_during so its prior entry is retained
            # and it is re-examined next run, exactly like a read failure after
            # the descriptor is opened.
            skipped["unreadable_path"] += 1
            _mark_changed_during(relative)
            continue
        try:
            try:
                info = _pinned_stat(fd)
            except OSError:
                # Post-open stat failure (a racing swap/removal): retain any
                # prior entry rather than let the set difference emit a false
                # removal and advance the checkpoint past it.
                skipped["unreadable_path"] += 1
                _mark_changed_during(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                # A symlink, directory, or special file at the leaf: not a note
                # this run. A previously checkpointed note swapped for one is not
                # confirmed REMOVED, so retain its prior entry instead of emitting
                # a false removal (which a stable run would then re-add).
                skipped["unreadable_path"] += 1
                _mark_changed_during(relative)
                continue
            # A hardlinked leaf (possibly planted after discovery) is not a vault
            # note. Enforce this on the opened descriptor on every platform:
            # fstat's st_nlink is reliable off the fd everywhere (NTFS reports
            # nNumberOfLinks; filesystems without hardlink support report 1), so
            # this stays fail-closed against a note swapped for a hardlink between
            # discovery and open even on the pathname fallback.
            if int(info.st_nlink) > 1:
                # A hardlinked leaf is refused (its content is never committed);
                # retain any prior entry so a note swapped for a hardlink is not
                # falsely reported as removed.
                skipped["hardlink"] += 1
                _mark_changed_during(relative)
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

            # Read the note's bytes ONCE from the pinned fd; both the hash AND
            # the frontmatter-denial check use exactly these bytes, so an
            # attacker cannot swap the pathname between admission and hashing to
            # get excluded content committed under a permissive incarnation. A
            # re-fstat after the read detects a mid-read change.
            prior = prior_entries.get(relative)
            try:
                data = _read_fd(fd, size)
                after = _pinned_stat(fd)
            except OSError:
                # A read or post-read stat failure at hash time is an unreadable
                # note, counted like the open/stat failures above, and its prior
                # entry is retained rather than false-removed.
                skipped["unreadable_path"] += 1
                _mark_changed_during(relative)
                continue
            if len(data) != size:
                # The file grew or shrank between the size check and the read;
                # the after-fstat below also detects this, but bail now so a
                # grown file's over-limit bytes are never hashed/committed.
                _mark_changed_during(relative)
                continue

            # The full IndexPolicy applies, frontmatter denial included: a note
            # the index would refuse must not be hashed or recorded by path
            # anywhere in steward state. Parsed from the pinned bytes.
            if source.policy.deny_frontmatter:
                try:
                    frontmatter, frontmatter_valid = _frontmatter_from_bytes(data)
                except UnicodeError:
                    skipped["unsupported_encoding"] += 1
                    policy_excluded.add(relative)
                    continue
                except RecursionError:
                    skipped["unparseable_frontmatter"] += 1
                    policy_excluded.add(relative)
                    continue
                allowed, reason = source.policy.frontmatter_allowed(
                    frontmatter, valid=frontmatter_valid
                )
                if not allowed:
                    skipped[reason or "frontmatter_policy"] += 1
                    policy_excluded.add(relative)
                    continue

            current_hash = hashlib.sha256(data).hexdigest()
            if (
                int(after.st_size) != size
                or int(after.st_mtime_ns) != mtime_ns
                or int(after.st_dev) != dev
                or int(after.st_ino) != ino
            ):
                _mark_changed_during(relative)
                continue
        finally:
            os.close(fd)

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

    # A subtree whose traversal failed this run was not scanned, so its files
    # are absent from new_entries through no deletion. Retain those prior entries
    # (carry them into the new checkpoint) and exclude them from removed
    # detection, so a transient directory I/O error cannot manufacture false
    # deletions, dangling references, and a re-added storm on the next run.
    if traversal_failures:
        failed_prefixes = []
        for failed in traversal_failures:
            try:
                failed_prefixes.append(
                    failed.resolve().relative_to(resolved_root).as_posix()
                )
            except (OSError, ValueError):
                continue

        def _under_failed(rel: str) -> bool:
            return any(
                rel == prefix or rel.startswith(prefix + "/")
                for prefix in failed_prefixes
            )

        for rel, entry in prior_entries.items():
            if rel not in new_entries and rel not in policy_excluded and _under_failed(rel):
                new_entries[rel] = _entry_from_prior(entry)

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
    removed_by_hash: dict[str, list[dict]] = defaultdict(list)
    for record in removed_records:
        previous = record["previous_content_hash"]
        if previous is not None:
            removed_by_hash[previous].append(record)
    rename_candidates: list[dict] = []
    for record in removed_records:
        previous = record["previous_content_hash"]
        if previous is None or previous not in added_by_hash:
            continue
        # The removal must be UNIQUE for its content hash. If two or more removed
        # notes share these bytes, the rename mapping is ambiguous: each would
        # otherwise be paired with the same addition and BOTH compiled as clean
        # renames (propose only checks len(added_paths) == 1). Emit no candidate
        # for such a hash -- the deleted notes fall back to dangling-reference
        # advisories instead.
        if len(removed_by_hash[previous]) != 1:
            continue
        matches = added_by_hash[previous]
        # Rename candidacy is content-hash based only. Inode identity is NOT
        # used: on filesystems that reuse inode numbers (e.g. ext4), a removed
        # file's number is promptly reused by an unrelated new file, which
        # would produce a spurious "same inode" signal. Content-hash equality
        # is the portable, honest basis; a unique pairing (one removed, one
        # added with equal bytes) is what a compiled rename edit keys on, and
        # every such edit is independently hash-pinned and operator-reviewed.
        rename_candidates.append(
            {
                "removed_path": record["relative_path"],
                "added_paths": sorted(match["relative_path"] for match in matches),
                "content_hash": previous,
            }
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

    # Persist the change batch BEFORE advancing the checkpoint. If this order
    # were reversed and the process crashed in between, the checkpoint would
    # already describe the new tree while no batch recorded the changes; the
    # next run would then diff the new tree against the advanced checkpoint,
    # see no differences, and permanently lose those modifications, deletions,
    # and additions before assessment ever saw them. With batch-first, a crash
    # merely re-observes against the old checkpoint and re-emits the batch
    # (digest-bound assessment deduplicates), never losing a change.
    # Final identity re-check immediately before committing. The per-note reads
    # above use the pinned descriptor, which -- if the root was renamed away and
    # replaced after enumeration -- now reads the renamed-away tree. That content
    # is no longer within the REGISTERED pathname, so persisting its batch or
    # advancing the checkpoint would commit out-of-scope data. Re-verify the
    # registered pathname still resolves to the pinned identity right before the
    # writes; on mismatch or disappearance, fail closed and write neither.
    if source.root_dev is not None and source.root_ino is not None:
        try:
            commit_identity = path_identity(resolved_root)
        except OSError:
            return _source_missing_receipt(source, generated_at, registry_sha256)
        if commit_identity != (source.root_dev, source.root_ino):
            return _source_error_receipt(
                source, generated_at, registry_sha256, "source_identity_changed"
            )

    # Create-only, never-overwriting batch naming. Deriving the name solely
    # from the timestamp is not collision-proof (clock rollback, an injected
    # `now`, or two runs sharing a microsecond), and atomic_write_json REPLACES.
    # A later empty run could otherwise overwrite an earlier run's real,
    # not-yet-assessed batch and lose its changes. Pick the first name that does
    # not yet exist, keeping the source name after the first hyphen so the
    # assess/propose filename parsers still recover it exactly.
    stamp = _file_timestamp(generated_at)
    filename = f"{stamp}-{source.name}.json"
    nonce = 1
    # Zero-padded so lexical filename order matches numeric order past nine
    # collisions (a plain "_10" would sort before "_2").
    while (state_dirs["changes"] / filename).exists():
        filename = f"{stamp}_{nonce:04d}-{source.name}.json"
        nonce += 1
    batch_path = state_dirs["changes"] / filename
    atomic_write_json(batch_path, receipt, within=state_dirs["changes"])

    # Re-verify identity AFTER the batch is on disk and BEFORE it can influence
    # assessment (the checkpoint has not advanced yet). If the registered root
    # was renamed/replaced while the batch was being written, RETRACT the batch
    # (delete it) and advance nothing, so no out-of-scope snapshot ever becomes
    # visible to assessment and the checkpoint is never advanced under a changed
    # identity.
    if source.root_dev is not None and source.root_ino is not None:
        try:
            post_write_identity = path_identity(resolved_root)
        except OSError:
            post_write_identity = None
        if post_write_identity != (source.root_dev, source.root_ino):
            # Retract descriptor-relative to the pinned state root: a pathname
            # unlink (and the pathname existence check) could be redirected if
            # changes/ were swapped for a symlink, deleting a same-named file
            # outside the state tree and then accepting that as a successful
            # retraction. Anchoring the unlink+verify to the state root's
            # O_NOFOLLOW descriptor keeps the deletion inside the state tree.
            retracted = _retract_change_batch(state_dirs["changes"], filename)
            # Retraction must be VERIFIED. If the out-of-scope batch is somehow
            # still on disk, do NOT return a benign error receipt -- assessment
            # would consume the invalid snapshot. Raise a blocking error so the
            # whole run fails closed instead.
            if not retracted:
                raise OSError(
                    f"Refusing to continue: the source root for {source.name!r} "
                    "changed during observation and its out-of-scope change "
                    f"batch {batch_path.name} could not be retracted."
                )
            if post_write_identity is None:
                return _source_missing_receipt(source, generated_at, registry_sha256)
            return _source_error_receipt(
                source, generated_at, registry_sha256, "source_identity_changed"
            )

    save_checkpoint(
        state_dirs,
        source.name,
        sorted(new_entries.values(), key=lambda entry: entry.relative_path),
        generated_at=generated_at,
        registry_sha256=registry_sha256,
    )

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
    receipts: list[dict] = []
    with StateLock(state_root):
        # Allocate the run timestamp AFTER acquiring the lock. If two observe
        # processes stamped the same wall-clock time before either locked, they
        # would compute identical batch filenames; the second, running after the
        # first advanced the checkpoint, would see an unchanged tree and
        # atomically REPLACE the first's batch with an empty one, discarding
        # those changes before assessment. Stamping under the lock serialises
        # the runs onto distinct timestamps.
        generated_at = _utc_now()
        for source in registry.sources:
            batch = observe_source(
                source,
                state_dirs,
                registry_sha256=registry.registry_sha256,
                now=generated_at,
            )
            # observe_source persists the change batch itself (batch-first,
            # before advancing the checkpoint); a successful source therefore
            # already has its changes/<UTC>-<source>.json on disk. Error
            # receipts carry no batch and none is written for them.
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
