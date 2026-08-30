from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    StateLock,
    atomic_write_json,
    ensure_state_layout,
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

    def test_root_fingerprint_casefolded(self) -> None:
        self.assertEqual(
            steward_state_root(Path("/Vault/Mixed")),
            steward_state_root(Path("/vault/mixed")),
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
                "receipts",
                "reports",
                "backups",
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

    def test_crash_in_replace_leaves_old_file(self) -> None:
        path = self.root / "data.json"
        atomic_write_json(path, {"value": "old"})
        original = path.read_bytes()
        with patch(
            "recallweave.steward_state.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"value": "new"})
        self.assertEqual(path.read_bytes(), original)

    def test_crash_in_replace_leaves_no_file_when_absent(self) -> None:
        path = self.root / "absent.json"
        with patch(
            "recallweave.steward_state.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            with self.assertRaises(OSError):
                atomic_write_json(path, {"value": "new"})
        self.assertFalse(path.exists())

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
