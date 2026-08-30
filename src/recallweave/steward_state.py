from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .safe_write import is_link_like

STEWARD_SCHEMA_VERSION = "recallweave.steward.v1"

STEWARD_SUBDIRS = (
    "checkpoints",
    "changes",
    "assessments",
    "proposals",
    "proposed",
    "receipts",
    "reports",
    "backups",
    "journal",
    "trash",
)


def _application_data_root() -> Path:
    if sys.platform == "win32":
        return Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        ) / "RecallWeave"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RecallWeave"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "recallweave"


def steward_state_root(registry_path: Path) -> Path:
    # Key the state directory on the resolved registry path. We must NOT
    # case-fold unconditionally: on a case-sensitive filesystem `Sources.json`
    # and `sources.json` are two distinct registries, and folding would collapse
    # them onto one state tree (shared checkpoints/journals/locks -> corruption
    # or overwrite). Distinct paths therefore always yield distinct fingerprints.
    # On a case-insensitive filesystem the two spellings denote the same file,
    # and ``resolve()`` canonicalises to its on-disk casing, so they converge on
    # a single state tree without any folding here.
    resolved = registry_path.expanduser().resolve()
    fingerprint = hashlib.sha256(
        resolved.as_posix().encode("utf-8")
    ).hexdigest()
    return _application_data_root() / "steward" / fingerprint


def ensure_state_root_outside_sources(root: Path, source_roots: list[Path]) -> None:
    """Refuse a state root that overlaps any registered source root.

    Steward state writes report ``vault_writes: 0``; that claim is only true
    when the state tree cannot sit inside (or contain) a registered source.
    """

    resolved_root = root.expanduser().resolve()
    for source_root in source_roots:
        resolved_source = Path(source_root).expanduser().resolve()
        # A file source's boundary is its containing directory; a folder
        # source's boundary is itself -- including when it is currently
        # missing (a deleted root must not widen the boundary to its parent).
        candidates = (
            resolved_source.parent if resolved_source.is_file() else resolved_source
        )
        if (
            resolved_root == candidates
            or candidates in resolved_root.parents
            or resolved_root in resolved_source.parents
            or resolved_root == resolved_source
        ):
            raise ValueError(
                "Refusing a steward state directory that overlaps a registered "
                f"source: state root {resolved_root} vs source {resolved_source}. "
                "Choose a --state-dir outside every registered source."
            )


def guard_within(path: Path, within: Path) -> None:
    """Refuse a state write whose destination escapes ``within``.

    Every path component strictly below ``within`` must not be a symlink or
    junction, and the destination parent must resolve inside ``within``.
    ``within`` itself is expected to be a directory Steward created.
    """

    if is_link_like(within):
        raise ValueError(
            f"Refusing a state write through a symlinked directory: {within}"
        )
    resolved_within = within.resolve()
    try:
        relative = path.absolute().relative_to(within.absolute())
    except ValueError:
        raise ValueError(
            f"Refusing a state write outside its state directory: {path}"
        ) from None
    # Reject traversal components outright: a ".." in the relative path can
    # escape `within` even though relative_to accepted the lexical prefix,
    # and the resolved-parent check below only fires once the parent exists.
    if any(part == ".." for part in relative.parts):
        raise ValueError(
            f"Refusing a state write with a traversal component: {path}"
        )
    current = within
    for part in relative.parts[:-1]:
        current = current / part
        if is_link_like(current):
            raise ValueError(
                f"Refusing a state write through a symlinked directory: {current}"
            )
    if is_link_like(path):
        raise ValueError(f"Refusing to replace a symlink or junction: {path}")
    # Containment is verified against the nearest existing ancestor, so a
    # not-yet-created parent cannot skip the check (mkdir would create it).
    probe = path.parent
    while not probe.exists():
        probe = probe.parent
    resolved_probe = probe.resolve()
    if not (
        resolved_probe == resolved_within
        or resolved_within in resolved_probe.parents
    ):
        raise ValueError(
            f"Refusing a state write outside its state directory: {path}"
        )


def ensure_state_layout(root: Path) -> dict[str, Path]:
    # The state tree is the write boundary: a symlinked root or subdirectory
    # would redirect state writes elsewhere (including into a source) while
    # receipts still claim zero vault writes. Refuse link-like entries before
    # anything is created or written.
    if is_link_like(root):
        raise ValueError(
            f"Refusing a symlinked steward state root: {root}"
        )
    result: dict[str, Path] = {}
    for name in STEWARD_SUBDIRS:
        subdir = root / name
        if is_link_like(subdir):
            raise ValueError(
                f"Refusing a symlinked steward state directory: {subdir}"
            )
        subdir.mkdir(parents=True, exist_ok=True)
        result[name] = subdir
    return result


class StateLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock_path = root / "steward.lock"
        self._held = False

    def acquire(self) -> None:
        root = self.root
        if is_link_like(root):
            raise ValueError(
                f"Refusing a symlinked steward state root: {root}"
            )
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError as error:
            detail = self._existing_detail()
            raise ValueError(
                f"Another steward run holds the lock: {self.lock_path}."
                + (
                    f" It records pid={detail[0]} acquired_at={detail[1]}."
                    if detail is not None
                    else ""
                )
                + " If no steward process is running, remove the file to recover."
            ) from error
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
        except BaseException:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            raise
        self._held = True

    def _existing_detail(self) -> tuple[str, str] | None:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        pid = data.get("pid")
        acquired_at = data.get("acquired_at")
        if pid is None or acquired_at is None:
            return None
        return str(pid), str(acquired_at)

    def release(self) -> None:
        if self._held:
            try:
                self.lock_path.unlink()
            except OSError:
                pass
            self._held = False

    def __enter__(self) -> "StateLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@contextmanager
def lock_state(root: Path) -> Iterator[StateLock]:
    lock = StateLock(root)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


def _fsync_parent_dir(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename into it is durable.

    POSIX only; on platforms/filesystems where a directory cannot be opened for
    fsync (notably Windows) this is a no-op, matching the platform's weaker but
    still-atomic rename guarantees."""

    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


_DIR_FD_STATE_WRITES = (
    os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def atomic_write_json(path: Path, payload: dict, *, within: Path | None = None) -> None:
    if within is not None:
        guard_within(path, within)
    if is_link_like(path):
        raise ValueError(f"Refusing to replace a symlink or junction: {path}")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, indent=2
    ).encode("utf-8")

    if _DIR_FD_STATE_WRITES:
        # Anchor the write to the parent directory opened O_NOFOLLOW, so a state
        # subdirectory swapped for a symlink after guard_within cannot redirect
        # the temp file or the rename outside the state tree (and, for a journal
        # write, cannot leave the durable record where recovery will not look).
        try:
            dir_fd = os.open(
                str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise ValueError(
                f"Refusing to write into a symlinked or missing state "
                f"directory: {parent} ({type(error).__name__})"
            ) from error
        temp_name = f".{path.name}.steward-state.tmp"
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
            try:
                fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
            except FileExistsError as error:
                raise ValueError(
                    f"A stale temp file occupies the state path beside "
                    f"{path.name}; remove it and retry."
                ) from error
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(
                    temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
                )
                # Durability: flush the directory entry of the rename.
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
            except BaseException:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except OSError:
                    pass
                raise
        finally:
            os.close(dir_fd)
        return

    # Pathname fallback (e.g. Windows without dir_fd): re-check the leaf is not a
    # symlink immediately before the replace; the parent-swap window cannot be
    # fully excluded here, matching the apply module's documented fallback.
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if is_link_like(path):
            raise ValueError(f"Refusing to replace a symlink or junction: {path}")
        os.replace(temp_name, path)
        _fsync_parent_dir(parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
