from __future__ import annotations

import contextvars
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from .safe_write import is_link_like

if TYPE_CHECKING:
    from .steward_sources import StewardSource

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


def ensure_state_root_outside_sources(
    root: Path, sources: list["StewardSource"]
) -> None:
    """Refuse a state root that overlaps any registered source root.

    Steward state writes report ``vault_writes: 0``; that claim is only true
    when the state tree cannot sit inside (or contain) a registered source.
    """

    resolved_root = root.expanduser().resolve()
    for source in sources:
        resolved_source = Path(source.root).expanduser().resolve()
        # A file source's boundary is its CONTAINING directory, whether or not
        # the file still exists (a removed file must not narrow the boundary to
        # its own path); a folder source's boundary is itself -- including when
        # it is currently missing (a deleted root must not widen the boundary
        # to its parent). The type comes from the registered source, not a live
        # filesystem stat, so a disappeared file still contributes its parent.
        candidates = (
            resolved_source.parent if source.type == "file" else resolved_source
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


_LOCK_NAME = "steward.lock"


# The (st_dev, st_ino) of the state root the currently-held StateLock pinned,
# set by lock_state for the duration of a locked operation. State writes consult
# it to verify descriptor continuity before mutating (see
# _verify_locked_root_identity). A ContextVar keeps it bound to the running
# operation's call stack, so a direct, unlocked write (e.g. a unit test) sees
# None and skips the check.
_LOCKED_ROOT_IDENTITY: contextvars.ContextVar[tuple[int, int] | None] = (
    contextvars.ContextVar("steward_locked_root_identity", default=None)
)


def _verify_locked_root_identity(root_fd: int, anchor: Path) -> None:
    """Fail closed if the anchored state root is not the inode the held
    StateLock pinned.

    The anchored write descent reopens the state root by pathname (O_NOFOLLOW on
    the final component only). While a StateLock is held, ``lock_state`` records
    the locked root's ``(st_dev, st_ino)``; if a concurrent rename+replace
    swapped the state root (or an ancestor) for another real directory between
    lock acquisition and this write, the freshly opened root is a DIFFERENT
    inode. Refuse the write rather than mutate a rebound tree -- descriptor
    continuity against the pinned root (#31). No lock held (contextvar unset)
    means a direct, unlocked call: there is nothing to verify against."""

    pinned = _LOCKED_ROOT_IDENTITY.get()
    if pinned is None:
        return
    info = os.fstat(root_fd)
    if (info.st_dev, info.st_ino) != pinned:
        raise ValueError(
            f"Refusing to write: the state root {anchor} is no longer the "
            "directory the steward lock pinned (it was renamed or replaced)."
        )


class StateLock:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock_path = root / _LOCK_NAME
        self._held = False
        # A directory descriptor pinned to the state root inode for the lock's
        # whole lifetime (POSIX). Holding it means create, existence checks, and
        # release's unlink are all relative to the SAME inode: a state root
        # renamed-and-recreated after acquire cannot make release unlink a
        # replacement process's lock, nor let this process acquire in a
        # replacement tree while the original lock is still held.
        self._dir_fd: int | None = None

    @property
    def root_identity(self) -> tuple[int, int] | None:
        """``(st_dev, st_ino)`` of the pinned state-root descriptor, or None on
        the pathname fallback (no dir_fd) where there is nothing to pin."""
        if self._dir_fd is None:
            return None
        try:
            info = os.fstat(self._dir_fd)
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

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

        if _DIR_FD_STATE_WRITES:
            try:
                dir_fd = os.open(
                    str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            except OSError as error:
                raise ValueError(
                    f"Refusing a symlinked or missing steward state root: "
                    f"{root} ({type(error).__name__})"
                ) from error
            try:
                fd = os.open(
                    _LOCK_NAME,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o644,
                    dir_fd=dir_fd,
                )
            except FileExistsError as error:
                detail = self._existing_detail_fd(dir_fd)
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                raise ValueError(self._held_message(detail)) from error
            except BaseException:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                raise
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        payload, handle, ensure_ascii=True, sort_keys=True, indent=2
                    )
            except BaseException:
                try:
                    os.unlink(_LOCK_NAME, dir_fd=dir_fd)
                except OSError:
                    pass
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                raise
            self._dir_fd = dir_fd
            self._held = True
            return

        # Pathname fallback (e.g. Windows without dir_fd): a state-root swap
        # between this open and release cannot be fully excluded, matching the
        # module's other documented pathname fallbacks.
        try:
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError as error:
            raise ValueError(self._held_message(self._existing_detail())) from error
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

    def _held_message(self, detail: tuple[str, str] | None) -> str:
        return (
            f"Another steward run holds the lock: {self.lock_path}."
            + (
                f" It records pid={detail[0]} acquired_at={detail[1]}."
                if detail is not None
                else ""
            )
            + " If no steward process is running, remove the file to recover."
        )

    @staticmethod
    def _detail_from_text(text: str) -> tuple[str, str] | None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        pid = data.get("pid")
        acquired_at = data.get("acquired_at")
        if pid is None or acquired_at is None:
            return None
        return str(pid), str(acquired_at)

    def _existing_detail(self) -> tuple[str, str] | None:
        try:
            text = self.lock_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return self._detail_from_text(text)

    def _existing_detail_fd(self, dir_fd: int) -> tuple[str, str] | None:
        try:
            fd = os.open(_LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return None
        return self._detail_from_text(text)

    def release(self) -> None:
        if not self._held:
            return
        if self._dir_fd is not None:
            try:
                os.unlink(_LOCK_NAME, dir_fd=self._dir_fd)
            except OSError:
                pass
            try:
                os.close(self._dir_fd)
            except OSError:
                pass
            self._dir_fd = None
        else:
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
    # Publish the pinned root identity so every anchored state write performed
    # while this lock is held can verify descriptor continuity before mutating
    # (see _verify_locked_root_identity). Reset on release so a later unlocked
    # write in the same context is not checked against a stale identity (#31).
    identity = lock.root_identity
    token = (
        _LOCKED_ROOT_IDENTITY.set(identity) if identity is not None else None
    )
    try:
        yield lock
    finally:
        if token is not None:
            _LOCKED_ROOT_IDENTITY.reset(token)
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


def atomic_write_bytes(path: Path, data: bytes, *, within: Path | None = None) -> None:
    """Atomically write ``data`` to ``path``, descriptor-relative and anchored
    inside ``within`` where the platform supports it.

    Shared by JSON state writes and the sweep Markdown report projection so both
    get the same symlink-race-proof, within-anchored write: a parent (or the
    state root, or any directory between) swapped for a symlink after
    ``guard_within`` cannot redirect the temp file or the rename outside the
    state tree."""
    if within is not None:
        guard_within(path, within)
    if is_link_like(path):
        raise ValueError(f"Refusing to replace a symlink or junction: {path}")
    parent = path.parent
    # NB: do NOT pathname-mkdir the parent here. When anchored (below), missing
    # intermediate directories are created descriptor-relative (mkdirat) during
    # the O_NOFOLLOW descent, so a `backups/`-style directory swapped for a
    # symlink cannot make a pathname mkdir create a directory outside the state
    # tree. The non-anchored and pathname-fallback branches mkdir their own
    # parent where their weaker guarantee already applies.

    if _DIR_FD_STATE_WRITES:
        # Reach the destination parent by a descriptor-relative, component-by-
        # component O_DIRECTORY|O_NOFOLLOW descent anchored at the state root
        # (the parent of `within`). Opening only the final parent O_NOFOLLOW
        # would still FOLLOW a swapped intermediate ancestor; descending from the
        # anchor refuses a symlink at ANY component, so a race that swaps the
        # state root, the `within` subdir, or any dir between them cannot
        # redirect the temp file or the rename outside the state tree (and, for a
        # journal write, cannot strand the durable record where recovery will not
        # look). Without a `within` anchor there is nothing to descend from, so
        # that case takes the pathname fallback below.
        anchor = within.parent if within is not None else None
        if anchor is None:
            dir_fd = None
        else:
            try:
                rel_from_anchor = path.relative_to(anchor)
            except ValueError:
                dir_fd = None
            else:
                descend = rel_from_anchor.parts[:-1]
                try:
                    fds: list[int] = [
                        os.open(
                            str(anchor),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        )
                    ]
                except OSError as error:
                    raise ValueError(
                        f"Refusing to write through a symlinked or missing state "
                        f"root: {anchor} ({type(error).__name__})"
                    ) from error
                try:
                    # Descriptor continuity: the anchor is the state root; while a
                    # StateLock is held it must still be the inode the lock pinned
                    # (#31). Verified before the descent, so a rebound state tree
                    # is refused before any directory is created or file written.
                    _verify_locked_root_identity(fds[0], anchor)
                except ValueError:
                    for open_fd in fds:
                        try:
                            os.close(open_fd)
                        except OSError:
                            pass
                    raise
                try:
                    for part in descend:
                        try:
                            fds.append(
                                os.open(
                                    part,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fds[-1],
                                )
                            )
                        except FileNotFoundError:
                            # Create a missing intermediate directory ANCHORED
                            # (mkdirat), then open it O_NOFOLLOW -- so a pathname
                            # mkdir cannot create it outside the state tree through
                            # a swapped ancestor symlink. A symlinked (not missing)
                            # component fails the open with a different OSError and
                            # is refused by the outer handler below.
                            os.mkdir(part, 0o700, dir_fd=fds[-1])
                            fds.append(
                                os.open(
                                    part,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fds[-1],
                                )
                            )
                except OSError as error:
                    for open_fd in fds:
                        try:
                            os.close(open_fd)
                        except OSError:
                            pass
                    raise ValueError(
                        f"Refusing to write through a symlinked or missing state "
                        f"directory under {anchor} ({type(error).__name__})"
                    ) from error
                # The immediate parent fd is the last opened; close ancestors.
                dir_fd = fds[-1]
                for ancestor_fd in fds[:-1]:
                    try:
                        os.close(ancestor_fd)
                    except OSError:
                        pass

        if dir_fd is None:
            # No `within` anchor to descend from: create the parent by pathname
            # (the weaker, non-anchored guarantee for this case), then open it
            # O_NOFOLLOW directly.
            parent.mkdir(parents=True, exist_ok=True)
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
    parent.mkdir(parents=True, exist_ok=True)
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


def read_within_bytes(path: Path, *, within: Path) -> bytes:
    """Read ``path`` descriptor-relative, anchored inside ``within``.

    The mirror of ``atomic_write_bytes``: on POSIX it descends from the state
    root (``within.parent``) component-by-component O_DIRECTORY|O_NOFOLLOW and
    opens the leaf O_NOFOLLOW, so a directory (or the leaf) swapped for a symlink
    after ``guard_within`` cannot redirect the read outside the state tree -- used
    to verify a just-written backup through the same pinned path it was written
    to. Raises OSError/ValueError on any symlinked component or a missing leaf."""

    if _DIR_FD_STATE_WRITES:
        anchor = within.parent
        try:
            rel_from_anchor = path.relative_to(anchor)
        except ValueError:
            rel_from_anchor = None
        if rel_from_anchor is not None:
            descend = rel_from_anchor.parts[:-1]
            try:
                fds: list[int] = [
                    os.open(
                        str(anchor), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    )
                ]
            except OSError as error:
                raise ValueError(
                    f"Refusing to read through a symlinked or missing state root: "
                    f"{anchor} ({type(error).__name__})"
                ) from error
            try:
                for part in descend:
                    fds.append(
                        os.open(
                            part,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fds[-1],
                        )
                    )
                read_flags = (
                    os.O_RDONLY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_BINARY", 0)
                )
                fd = os.open(rel_from_anchor.parts[-1], read_flags, dir_fd=fds[-1])
                try:
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    return b"".join(chunks)
                finally:
                    os.close(fd)
            finally:
                for open_fd in fds:
                    try:
                        os.close(open_fd)
                    except OSError:
                        pass

    # Pathname fallback (e.g. Windows without dir_fd): reject a symlink leaf.
    if is_link_like(path):
        raise ValueError(f"Refusing to read a symlink or junction: {path}")
    return path.read_bytes()


def atomic_write_json(path: Path, payload: dict, *, within: Path | None = None) -> None:
    data = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, indent=2
    ).encode("utf-8")
    atomic_write_bytes(path, data, within=within)
