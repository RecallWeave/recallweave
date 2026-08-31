from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.policy import IndexPolicy
from recallweave.steward_sources import StewardSource
from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    StateLock,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    steward_state_root,
)


class StewardStateRootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.patcher = patch(
            "recallweave.steward_state._application_data_root", return_value=self.base
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_root_differs_for_different_registry_paths(self) -> None:
        first = steward_state_root(Path("/vault/a"))
        second = steward_state_root(Path("/vault/b"))
        self.assertNotEqual(first, second)

    def test_root_stable_for_same_registry_path(self) -> None:
        path = Path("/vault/stable")
        self.assertEqual(
            steward_state_root(path), steward_state_root(path)
        )

    def test_root_places_under_steward_and_base(self) -> None:
        root = steward_state_root(Path("/vault/x"))
        self.assertEqual(root.parent.parent, self.base)
        self.assertEqual(root.parent.name, "steward")

    def test_distinct_case_paths_get_distinct_state_roots(self) -> None:
        # On a case-sensitive filesystem these are two different registries;
        # they must never collapse onto one state tree (shared journals/locks).
        self.assertNotEqual(
            steward_state_root(Path("/Vault/Mixed")),
            steward_state_root(Path("/vault/mixed")),
        )

    def test_same_resolved_path_gets_stable_state_root(self) -> None:
        self.assertEqual(
            steward_state_root(Path("/vault/sources.json")),
            steward_state_root(Path("/vault/sources.json")),
        )


class EnsureStateRootOutsideSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _file_source(root: Path) -> StewardSource:
        return StewardSource(
            name="file-src",
            type="file",
            root=root,
            mode="read_only",
            policy=IndexPolicy(),
        )

    def test_missing_file_source_still_boundaries_to_parent(self) -> None:
        # A `type: file` source whose file has been removed from disk must
        # still treat its CONTAINING directory as the boundary. The state root
        # must be rejected when placed inside that directory even though the
        # file no longer exists.
        vault = self.base / "vault"
        vault.mkdir()
        missing_file = vault / "note.md"
        self.assertFalse(missing_file.exists())
        inside = vault / "StewardState"
        with self.assertRaisesRegex(ValueError, "overlaps a registered source"):
            ensure_state_root_outside_sources(
                inside, [self._file_source(missing_file)]
            )


class EnsureStateLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_all_subdirs(self) -> None:
        dirs = ensure_state_layout(self.root)
        self.assertEqual(
            set(dirs),
            {
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
            },
        )
        for name, subdir in dirs.items():
            self.assertEqual(subdir, self.root / name)
            self.assertTrue(subdir.is_dir())

    def test_idempotent(self) -> None:
        first = ensure_state_layout(self.root)
        second = ensure_state_layout(self.root)
        self.assertEqual(first, second)
        for name in first:
            self.assertTrue(first[name].is_dir())


class StateLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_second_acquirer_excluded_and_error_names_path(self) -> None:
        first = StateLock(self.root)
        first.acquire()
        try:
            with self.assertRaises(ValueError) as raised:
                StateLock(self.root).acquire()
            message = str(raised.exception)
            self.assertIn(str(first.lock_path), message)
            self.assertIn("Another steward run holds the lock", message)
            self.assertTrue(first.lock_path.exists())
        finally:
            first.release()

    def test_error_records_existing_pid_and_acquired_at(self) -> None:
        first = StateLock(self.root)
        first.acquire()
        try:
            with self.assertRaises(ValueError) as raised:
                StateLock(self.root).acquire()
            message = str(raised.exception)
            self.assertIn(f"pid={os.getpid()}", message)
            self.assertIn("acquired_at=", message)
        finally:
            first.release()

    def test_context_manager_releases(self) -> None:
        with StateLock(self.root) as lock:
            self.assertTrue(lock.lock_path.exists())
        self.assertFalse(lock.lock_path.exists())

    def test_release_unlinks_through_pinned_root_not_a_replacement(self) -> None:
        # If the state root is renamed-and-recreated after acquisition, release
        # must unlink the lock relative to the PINNED root inode (now moved
        # aside), never by pathname -- otherwise it would delete a replacement
        # process's freshly created lock. Regression for the unpinned lock race.
        import recallweave.steward_state as _st

        if not _st._DIR_FD_STATE_WRITES:
            self.skipTest("descriptor-relative locking unavailable")
        state_root = self.root / "state"
        state_root.mkdir()
        lock = StateLock(state_root)
        lock.acquire()
        self.assertIsNotNone(lock._dir_fd)
        stashed = self.root / "stashed_root"
        os.rename(state_root, stashed)  # original inode (with the lock) moves here
        state_root.mkdir()  # a brand-new root inode at the same pathname
        replacement_lock = state_root / "steward.lock"
        replacement_lock.write_text("another process's lock", encoding="utf-8")
        lock.release()
        self.assertTrue(
            replacement_lock.exists(),
            "release removed a replacement process's lock via pathname",
        )
        self.assertFalse(
            (stashed / "steward.lock").exists(),
            "release did not unlink the original lock through the pinned fd",
        )


class AtomicWriteJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_sorted_pretty_json(self) -> None:
        path = self.root / "data.json"
        atomic_write_json(path, {"b": 1, "a": 2})
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 2, "b": 1})

    def test_write_is_made_durable(self) -> None:
        # Durability (an fsync of the file and of the directory entry) is
        # attempted; implementation may fsync a handle or a dir fd.
        import recallweave.steward_state as _st
        path = self.root / "data.json"
        real_fsync = _st.os.fsync
        with patch.object(_st.os, "fsync", side_effect=real_fsync) as fsync:
            atomic_write_json(path, {"value": 1})
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1})

    def test_crash_in_rename_leaves_old_file(self) -> None:
        path = self.root / "data.json"
        atomic_write_json(path, {"value": "old"})
        original = path.read_bytes()
        with patch("recallweave.steward_state.os.replace",
                   side_effect=OSError("injected")), \
             patch("recallweave.steward_state.os.rename",
                   side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"value": "new"})
        self.assertEqual(path.read_bytes(), original)
        # No leftover temp files beside the target.
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name != "data.json"], []
        )

    def test_crash_in_rename_leaves_no_file_when_absent(self) -> None:
        path = self.root / "absent.json"
        with patch("recallweave.steward_state.os.replace",
                   side_effect=OSError("injected")), \
             patch("recallweave.steward_state.os.rename",
                   side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"value": "new"})
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_refuses_symlinked_parent_directory(self) -> None:
        # A state subdirectory swapped for a symlink must be refused.
        real = self.root / "real"
        real.mkdir()
        link = self.root / "sub"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        target = link / "data.json"
        import recallweave.steward_state as _st
        if not _st._DIR_FD_STATE_WRITES:
            self.skipTest("descriptor-relative writes unavailable")
        with self.assertRaisesRegex(ValueError, "symlinked or missing"):
            atomic_write_json(target, {"value": 1})

    def test_refuses_symlinked_state_root_ancestor(self) -> None:
        # Swap an INTERMEDIATE ancestor (the state root, parent of `within`) for
        # a symlink: the anchored descent must refuse it, not follow it.
        import recallweave.steward_state as _st
        if not _st._DIR_FD_STATE_WRITES:
            self.skipTest("descriptor-relative writes unavailable")
        real_root = self.root / "state_real"
        (real_root / "journal").mkdir(parents=True)
        link_root = self.root / "state"
        try:
            link_root.symlink_to(real_root, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        within = link_root / "journal"   # reached through the symlinked root
        target = within / "j.json"
        with self.assertRaisesRegex(ValueError, "symlinked or missing state root"):
            atomic_write_json(target, {"value": 1}, within=within)
        # Nothing was written into the real tree.
        self.assertEqual(list((real_root / "journal").iterdir()), [])

    def test_refuses_symlink_destination(self) -> None:
        target = self.root / "target.json"
        target.write_text("target", encoding="utf-8")
        link = self.root / "link.json"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            atomic_write_json(link, {"value": 1})
        self.assertEqual(target.read_text(encoding="utf-8"), "target")


if __name__ == "__main__":
    unittest.main()
