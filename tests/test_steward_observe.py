from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave.cli import main as cli_main
from recallweave.policy import IndexPolicy
from recallweave.steward_checkpoint import (
    CHECKPOINT_KIND,
    CheckpointError,
    load_checkpoint,
)
from recallweave.steward_observe import (
    CHANGE_BATCH_KIND,
    _hash_file,
    observe_registry,
    observe_source,
)
from recallweave.steward_sources import (
    SOURCES_SPEC_VERSION,
    SourceRegistry,
    StewardSource,
)
from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    ensure_state_layout,
)

from steward_fixtures import (
    TempVault,
    hold_lock,
    make_hardlink,
    make_symlink,
)


def _source(name: str, root: Path, **extra) -> StewardSource:
    values = {
        "name": name,
        "type": "folder",
        "root": root,
        "mode": "read_only",
        "policy": IndexPolicy(),
    }
    values.update(extra)
    return StewardSource(**values)


class ObserveSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.vault = TempVault(dir=Path(self.temporary.name))
        self.source = _source("src", self.vault.root)

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def observe(self, now: str | None = None) -> dict:
        return observe_source(
            self.source, self.dirs, registry_sha256="reg", now=now
        )

    def checkpoint(self) -> dict | None:
        return load_checkpoint(self.dirs, "src")

    def test_first_run_all_added(self) -> None:
        self.vault.write("a.md", "hello")
        self.vault.write("b.md", "world")
        receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(receipt["kind"], CHANGE_BATCH_KIND)
        self.assertEqual(receipt["schema_version"], STEWARD_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "steward_observe")
        self.assertEqual(
            [c["change_type"] for c in receipt["changes"]], ["added", "added"]
        )
        self.assertEqual(receipt["change_summary"], {"added": 2, "modified": 0, "removed": 0})
        self.assertEqual(receipt["network_calls"], 0)
        self.assertEqual(receipt["vault_writes"], 0)
        for change in receipt["changes"]:
            self.assertIsNone(change["previous_content_hash"])
            self.assertIsNotNone(change["current_content_hash"])

    def test_second_run_no_changes(self) -> None:
        self.vault.write("a.md", "hello")
        self.observe(now="2026-01-01T00:00:00+00:00")
        receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(receipt["changes"], [])
        self.assertEqual(
            receipt["change_summary"], {"added": 0, "modified": 0, "removed": 0}
        )

    def test_one_added_modified_removed(self) -> None:
        self.vault.write("a.md", "one")
        self.vault.write("b.md", "two")
        self.observe()
        self.vault.write("a.md", "one-changed")
        self.vault.remove("b.md")
        self.vault.write("c.md", "three")
        receipt = self.observe()
        by_path = {c["relative_path"]: c for c in receipt["changes"]}
        self.assertEqual(by_path["a.md"]["change_type"], "modified")
        self.assertEqual(by_path["b.md"]["change_type"], "removed")
        self.assertEqual(by_path["c.md"]["change_type"], "added")
        self.assertIsNotNone(by_path["a.md"]["previous_content_hash"])
        self.assertIsNotNone(by_path["a.md"]["current_content_hash"])
        self.assertIsNotNone(by_path["b.md"]["previous_content_hash"])
        self.assertIsNone(by_path["b.md"]["current_content_hash"])
        self.assertIsNone(by_path["c.md"]["previous_content_hash"])
        self.assertEqual(
            receipt["change_summary"], {"added": 1, "modified": 1, "removed": 1}
        )

    def test_mtime_only_touch_no_modified(self) -> None:
        self.vault.write("a.md", "hello")
        self.observe()
        self.vault.touch_mtime("a.md", offset_seconds=30)
        receipt = self.observe()
        self.assertEqual(receipt["changes"], [])
        self.assertEqual(receipt["change_summary"]["modified"], 0)

    def test_rename_move_rename_candidate(self) -> None:
        self.vault.write("old.md", "same content")
        first = self.observe()
        old_hash = first["changes"][0]["current_content_hash"]
        self.vault.move("old.md", "new.md")
        receipt = self.observe()
        by_path = {c["relative_path"]: c for c in receipt["changes"]}
        self.assertEqual(by_path["old.md"]["change_type"], "removed")
        self.assertEqual(by_path["new.md"]["change_type"], "added")
        self.assertEqual(len(receipt["rename_candidates"]), 1)
        candidate = receipt["rename_candidates"][0]
        self.assertEqual(candidate["removed_path"], "old.md")
        self.assertEqual(candidate["added_paths"], ["new.md"])
        self.assertEqual(candidate["content_hash"], old_hash)
        self.assertNotIn("inode_match", candidate)

    def test_copy_duplicate_hash_no_rename_candidate(self) -> None:
        self.vault.write("a.md", "same content")
        first = self.observe()
        a_hash = first["changes"][0]["current_content_hash"]
        self.vault.write("b.md", "same content")
        receipt = self.observe()
        self.assertEqual(
            [c["relative_path"] for c in receipt["changes"]], ["b.md"]
        )
        self.assertEqual(receipt["changes"][0]["change_type"], "added")
        self.assertEqual(receipt["rename_candidates"], [])
        checkpoint = self.checkpoint()
        entries = {e["relative_path"]: e for e in checkpoint["entries"]}
        self.assertEqual(entries["a.md"]["content_hash"], a_hash)
        self.assertEqual(entries["b.md"]["content_hash"], a_hash)

    def test_ambiguous_rename_lists_both_added(self) -> None:
        self.vault.write("old.md", "same content")
        self.observe()
        self.vault.remove("old.md")
        self.vault.write("x.md", "same content")
        self.vault.write("y.md", "same content")
        receipt = self.observe()
        self.assertEqual(len(receipt["rename_candidates"]), 1)
        candidate = receipt["rename_candidates"][0]
        self.assertEqual(candidate["removed_path"], "old.md")
        self.assertEqual(candidate["added_paths"], ["x.md", "y.md"])
        self.assertNotIn("inode_match", candidate)

    def test_symlink_skipped(self) -> None:
        self.vault.write("target.md", "real content")
        if not make_symlink(self.vault.root / "target.md", self.vault.root / "link.md"):
            self.skipTest("symlink creation unavailable")
        receipt = self.observe()
        self.assertEqual(receipt["skipped"]["symlink"], 1)
        paths = [c["relative_path"] for c in receipt["changes"]]
        self.assertNotIn("link.md", paths)
        self.assertIn("target.md", paths)

    def test_hardlink_skipped(self) -> None:
        self.vault.write("source.md", "hard content")
        if not make_hardlink(self.vault.root / "source.md", self.vault.root / "hard.md"):
            self.skipTest("hardlink creation unavailable")
        receipt = self.observe()
        self.assertEqual(receipt["skipped"]["hardlink"], 2)
        self.assertEqual(receipt["changes"], [])

    def test_policy_excluded_not_hashed_not_in_checkpoint(self) -> None:
        self.source = _source(
            "src",
            self.vault.root,
            policy=IndexPolicy(deny_path_terms=["secret"]),
        )
        self.vault.write("ok.md", "fine")
        self.vault.write("secret-notes.md", "hidden")
        receipt = self.observe()
        self.assertEqual(receipt["skipped"]["denied_path_term"], 1)
        paths = [c["relative_path"] for c in receipt["changes"]]
        self.assertNotIn("secret-notes.md", paths)
        self.assertIn("ok.md", paths)
        checkpoint = self.checkpoint()
        entry_paths = [e["relative_path"] for e in checkpoint["entries"]]
        self.assertNotIn("secret-notes.md", entry_paths)

    def test_oversized_file_skipped(self) -> None:
        self.source = _source(
            "src", self.vault.root, policy=IndexPolicy(max_file_bytes=10)
        )
        self.vault.write("small.md", "tiny")
        self.vault.write("big.md", "x" * 100)
        receipt = self.observe()
        self.assertEqual(receipt["skipped"]["file_too_large"], 1)
        paths = [c["relative_path"] for c in receipt["changes"]]
        self.assertNotIn("big.md", paths)
        self.assertIn("small.md", paths)
        checkpoint = self.checkpoint()
        entry_paths = [e["relative_path"] for e in checkpoint["entries"]]
        self.assertNotIn("big.md", entry_paths)

    def test_torn_read_records_changed_during_observe(self) -> None:
        self.vault.write("a.md", "original")
        self.observe()
        original_hash = self.checkpoint()["entries"][0]["content_hash"]
        self.vault.write("a.md", "changed-during-read-content")
        real_hash = _hash_file

        def flaky_hash(path: Path) -> str:
            result = real_hash(path)
            self.vault.touch_mtime("a.md", offset_seconds=5)
            return result

        with patch(
            "recallweave.steward_observe._hash_file", side_effect=flaky_hash
        ):
            receipt = self.observe()
        self.assertIn("a.md", receipt["changed_during_observe"])
        self.assertEqual(receipt["changes"], [])
        self.assertEqual(receipt["change_summary"]["modified"], 0)
        checkpoint = self.checkpoint()
        self.assertEqual(checkpoint["entries"][0]["content_hash"], original_hash)

    def test_missing_root_source_missing_checkpoint_untouched(self) -> None:
        missing = Path(self.temporary.name) / "does-not-exist"
        self.source = _source("src", missing)
        receipt = self.observe()
        self.assertEqual(receipt["error"], "source_missing")
        self.assertEqual(receipt["changes"], [])
        self.assertEqual(receipt["change_summary"], {"added": 0, "modified": 0, "removed": 0})
        self.assertEqual(receipt["skipped"], {})
        self.assertIsNone(self.checkpoint())

    def test_corrupt_checkpoint_invalid_full_reobserve(self) -> None:
        self.vault.write("a.md", "hello")
        self.observe()
        path = self.dirs["checkpoints"] / "src.json"
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        path.write_bytes(bytes(raw))
        receipt = self.observe()
        self.assertTrue(receipt["checkpoint_invalid"])
        self.assertEqual(len(receipt["changes"]), 1)
        self.assertEqual(receipt["changes"][0]["change_type"], "added")
        fresh = self.checkpoint()
        self.assertEqual(fresh["kind"], CHECKPOINT_KIND)

    def test_byte_identical_except_generated_at(self) -> None:
        self.vault.write("a.md", "hello")
        self.observe(now="2026-01-01T00:00:00+00:00")
        first = self.observe(now="2026-01-02T00:00:00+00:00")
        second = self.observe(now="2026-01-03T00:00:00+00:00")
        self.assertEqual(first["changes"], [])
        self.assertEqual(second["changes"], [])
        first_without = {k: v for k, v in first.items() if k != "generated_at"}
        second_without = {k: v for k, v in second.items() if k != "generated_at"}
        self.assertEqual(first_without, second_without)


class ObserveRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary.name) / "state"
        self.vault = TempVault(dir=Path(self.temporary.name))

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _registry(self) -> SourceRegistry:
        source = _source("src", self.vault.root)
        return SourceRegistry(sources=[source], registry_sha256="reg")

    def test_writes_batch_and_checkpoint_and_receipt(self) -> None:
        self.vault.write("a.md", "hello")
        receipt = observe_registry(self._registry(), self.state_root)
        self.assertEqual(receipt["kind"], "observe_receipt")
        self.assertEqual(receipt["operation"], "steward_observe")
        self.assertEqual(len(receipt["sources"]), 1)
        self.assertEqual(receipt["network_calls"], 0)
        self.assertEqual(receipt["vault_writes"], 0)
        changes_dir = self.state_root / "changes"
        files = list(changes_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        batch = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(batch["kind"], CHANGE_BATCH_KIND)
        self.assertEqual(batch["source"], "src")
        self.assertEqual(batch["change_summary"]["added"], 1)
        checkpoint = load_checkpoint(
            ensure_state_layout(self.state_root), "src"
        )
        self.assertIsNotNone(checkpoint)

    def test_lock_held_raises_valueerror_naming_lock(self) -> None:
        ensure_state_layout(self.state_root)
        lock_path = self.state_root / "steward.lock"
        with hold_lock(lock_path):
            with self.assertRaises(ValueError) as raised:
                observe_registry(self._registry(), self.state_root)
            self.assertIn(str(lock_path), str(raised.exception))
            self.assertIn("Another steward run holds the lock", str(raised.exception))


class StewardObserveCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.state_dir = self.base / "state"

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _write_registry(self, root: Path | None = None) -> Path:
        sources_path = self.base / "sources.json"
        payload = {
            "spec_version": SOURCES_SPEC_VERSION,
            "sources": [
                {
                    "name": "src",
                    "type": "folder",
                    "root": str(root or self.vault.root),
                    "mode": "read_only",
                }
            ],
        }
        sources_path.write_text(json.dumps(payload), encoding="utf-8")
        return sources_path

    def test_end_to_end_single_json_object_exit0(self) -> None:
        self.vault.write("a.md", "hello")
        sources_path = self._write_registry()
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(
                ["steward-observe", str(sources_path), "--state-dir", str(self.state_dir)]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["kind"], "observe_receipt")
        self.assertEqual(payload["operation"], "steward_observe")
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["change_summary"]["added"], 1)
        changes_dir = self.state_dir / "changes"
        self.assertEqual(len(list(changes_dir.glob("*.json"))), 1)

    def test_missing_registry_file_exit2_error_envelope(self) -> None:
        missing = self.base / "absent.json"
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(
                ["steward-observe", str(missing), "--state-dir", str(self.state_dir)]
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertIn("error", error)
        self.assertEqual(error["operation"], "steward-observe")


if __name__ == "__main__":
    unittest.main()
