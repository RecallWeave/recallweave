from __future__ import annotations

import os
import unittest
from pathlib import Path

from steward_fixtures import (
    CrashInjector,
    TempVault,
    diff_snapshots,
    hold_lock,
    make_hardlink,
    make_symlink,
    write_conflicted_copy,
)


class TempVaultTest(unittest.TestCase):
    def test_write_creates_parents_without_trailing_newline(self) -> None:
        with TempVault() as vault:
            path = vault.write("a/b/c.md", "body")
            self.assertEqual(path, vault.root / "a" / "b" / "c.md")
            self.assertEqual(path.read_bytes(), b"body")

    def test_remove_and_move(self) -> None:
        with TempVault() as vault:
            vault.write("old/x.md", "# x")
            vault.move("old/x.md", "new/x.md")
            self.assertTrue((vault.root / "new" / "x.md").exists())
            self.assertFalse((vault.root / "old" / "x.md").exists())
            vault.remove("new/x.md")
            self.assertFalse((vault.root / "new" / "x.md").exists())

    def test_snapshot_determinism(self) -> None:
        with TempVault() as vault:
            vault.write("a.md", "alpha")
            vault.write("nested/b.md", "beta")
            first = vault.snapshot()
            second = vault.snapshot()
            self.assertEqual(first, second)

    def test_context_manager_cleans_up(self) -> None:
        with TempVault() as vault:
            root = vault.root
            self.assertTrue(root.is_dir())
        self.assertFalse(root.exists())

    def test_snapshot_skips_symlinks(self) -> None:
        with TempVault() as vault:
            target = vault.write("real.md", "# real")
            link = vault.root / "sym.md"
            if not make_symlink(target, link):
                self.skipTest("symlink creation unavailable")
            snap = vault.snapshot()
            self.assertIn("real.md", snap)
            self.assertNotIn("sym.md", snap)

    def test_snapshot_skips_hardlinks(self) -> None:
        with TempVault() as vault:
            source = vault.write("src.md", "# data")
            link = vault.root / "hard.md"
            if not make_hardlink(source, link):
                self.skipTest("hardlink creation unavailable")
            snap = vault.snapshot()
            self.assertNotIn("src.md", snap)
            self.assertNotIn("hard.md", snap)


class DiffSnapshotsTest(unittest.TestCase):
    def test_diff_detects_added_removed_modified(self) -> None:
        with TempVault() as vault:
            vault.write("a.md", "alpha")
            vault.write("b.md", "beta")
            before = vault.snapshot()
            vault.write("a.md", "changed")
            vault.remove("b.md")
            vault.write("c.md", "gamma")
            after = vault.snapshot()
            diff = diff_snapshots(before, after)
            self.assertEqual(diff["added"], ["c.md"])
            self.assertEqual(diff["removed"], ["b.md"])
            self.assertEqual(diff["modified"], ["a.md"])

    def test_move_shows_removed_and_added_with_equal_hash(self) -> None:
        with TempVault() as vault:
            vault.write("old/note.md", "# content")
            before = vault.snapshot()
            vault.move("old/note.md", "new/note.md")
            after = vault.snapshot()
            diff = diff_snapshots(before, after)
            self.assertEqual(diff["removed"], ["old/note.md"])
            self.assertEqual(diff["added"], ["new/note.md"])
            self.assertEqual(diff["modified"], [])
            self.assertEqual(
                before["old/note.md"]["content_hash"],
                after["new/note.md"]["content_hash"],
            )

    def test_touch_mtime_changes_mtime_not_hash_and_no_diff(self) -> None:
        with TempVault() as vault:
            vault.write("note.md", "# content")
            before = vault.snapshot()
            vault.touch_mtime("note.md", offset_seconds=3600)
            after = vault.snapshot()
            self.assertEqual(
                before["note.md"]["content_hash"], after["note.md"]["content_hash"]
            )
            self.assertEqual(before["note.md"]["size"], after["note.md"]["size"])
            self.assertGreater(
                after["note.md"]["mtime_ns"], before["note.md"]["mtime_ns"]
            )
            diff = diff_snapshots(before, after)
            self.assertEqual(diff["added"], [])
            self.assertEqual(diff["removed"], [])
            self.assertEqual(diff["modified"], [])


class CrashInjectorTest(unittest.TestCase):
    def _module(self):
        return __import__(
            "recallweave.safe_write", fromlist=["_install_non_replacing"]
        )

    def test_crash_injector_fires_on_exact_nth_call(self) -> None:
        module = self._module()
        first_src = self._tmp("first.tmp")
        first_dst = self._tmp("first")
        second_src = self._tmp("second.tmp")
        second_dst = self._tmp("second")
        first_src.write_bytes(b"one")
        second_src.write_bytes(b"two")
        with CrashInjector(module, "_install_non_replacing", fail_on_call=2) as injector:
            module._install_non_replacing(first_src, first_dst)
            self.assertEqual(injector.calls, 1)
            with self.assertRaisesRegex(OSError, "injected crash"):
                module._install_non_replacing(second_src, second_dst)
            self.assertEqual(injector.calls, 2)
        self.assertTrue(first_dst.exists())
        self.assertFalse(second_dst.exists())
        self.assertTrue(second_src.exists())

    def test_crash_injector_restores_attribute(self) -> None:
        module = self._module()
        original = module._install_non_replacing
        src = self._tmp("a.tmp")
        dst = self._tmp("a")
        src.write_bytes(b"x")
        with CrashInjector(module, "_install_non_replacing", fail_on_call=1):
            with self.assertRaises(OSError):
                module._install_non_replacing(src, dst)
        self.assertIs(module._install_non_replacing, original)

    def _tmp(self, name: str) -> Path:
        if not hasattr(self, "_vault"):
            self._vault = TempVault()
            self.addCleanup(self._vault.cleanup)
        return self._vault.root / name


class HoldLockTest(unittest.TestCase):
    def test_hold_lock_excludes_second_holder(self) -> None:
        with TempVault() as vault:
            lock = vault.root / "lock"
            with hold_lock(lock):
                self.assertTrue(lock.exists())
                self.assertEqual(lock.read_bytes(), b"test")
                with self.assertRaises(FileExistsError):
                    with hold_lock(lock):
                        pass
            self.assertFalse(lock.exists())


class HelperTest(unittest.TestCase):
    def test_symlink_helper_returns_bool_or_skips(self) -> None:
        with TempVault() as vault:
            target = vault.write("target.md", "# T")
            link = vault.root / "link.md"
            if not make_symlink(target, link):
                self.skipTest("symlink creation unavailable")
            self.assertTrue(link.is_symlink())

    def test_hardlink_helper_returns_bool_or_skips(self) -> None:
        with TempVault() as vault:
            source = vault.write("src.txt", "data")
            link = vault.root / "hard.txt"
            if not make_hardlink(source, link):
                self.skipTest("hardlink creation unavailable")
            self.assertTrue(link.exists())
            self.assertTrue(os.path.samefile(source, link))

    def test_write_conflicted_copy(self) -> None:
        with TempVault() as vault:
            path = vault.write("note.md", "# body")
            conflicted = write_conflicted_copy(vault, "note.md")
            self.assertEqual(conflicted.name, "note (conflicted copy).md")
            self.assertEqual(conflicted.read_bytes(), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
