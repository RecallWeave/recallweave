from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from typing import Any, Iterator


class TempVault:
    """Wrap a tempfile.TemporaryDirectory as a disposable fake vault tree."""

    def __init__(self, *, dir: Path | None = None) -> None:
        self._temporary = tempfile.TemporaryDirectory(dir=dir)
        self.root = Path(self._temporary.name)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def remove(self, rel: str) -> None:
        path = self.root / rel
        path.unlink()

    def move(self, old_rel: str, new_rel: str) -> None:
        old_path = self.root / old_rel
        new_path = self.root / new_rel
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)

    def touch_mtime(self, rel: str, offset_seconds: float) -> None:
        path = self.root / rel
        info = path.stat()
        atime = info.st_atime_ns
        mtime = info.st_mtime_ns + int(offset_seconds * 1_000_000_000)
        os.utime(path, ns=(atime, mtime))

    def snapshot(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for dirpath, _dirnames, filenames in os.walk(self.root, followlinks=False):
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(self.root).as_posix()
                info = full.lstat()
                if stat.S_ISLNK(info.st_mode):
                    continue
                if info.st_nlink > 1:
                    continue
                data = full.read_bytes()
                result[rel] = {
                    "content_hash": hashlib.sha256(data).hexdigest(),
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
        return result

    def cleanup(self) -> None:
        self._temporary.cleanup()

    def __enter__(self) -> "TempVault":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.cleanup()


def diff_snapshots(before: dict, after: dict) -> dict:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        path
        for path in set(before) & set(after)
        if before[path]["content_hash"] != after[path]["content_hash"]
    )
    return {"added": added, "removed": removed, "modified": modified}


class CrashInjector:
    """Patch module.attr so its Nth invocation raises, then restore on exit."""

    def __init__(
        self,
        module: Any,
        attr: str,
        fail_on_call: int = 1,
        exc: BaseException | None = None,
    ) -> None:
        self.module = module
        self.attr = attr
        self.fail_on_call = fail_on_call
        self.exc = exc if exc is not None else OSError("injected crash")
        self.calls = 0
        self._original: Any = None
        self._patcher: Any = None

    def __enter__(self) -> "CrashInjector":
        self._original = getattr(self.module, self.attr)
        self._patcher = patch.object(
            self.module, self.attr, side_effect=self._handle
        )
        self._patcher.start()
        return self

    def _handle(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise self.exc
        return self._original(*args, **kwargs)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self._patcher.stop()
        return False


@contextmanager
def hold_lock(path: Path) -> Iterator[Path]:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, b"test")
    finally:
        os.close(fd)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def make_symlink(target: Path, link: Path) -> bool:
    try:
        link.symlink_to(target)
        return True
    except OSError:
        return False


def make_hardlink(source: Path, link: Path) -> bool:
    try:
        os.link(source, link)
        return True
    except OSError:
        return False


def write_conflicted_copy(vault: TempVault, rel: str) -> Path:
    path = vault.root / rel
    conflicted = path.with_name(f"{path.stem} (conflicted copy){path.suffix}")
    conflicted.write_bytes(path.read_bytes())
    return conflicted
