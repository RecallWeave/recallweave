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
        import recallweave.steward_observe as _obs
        real_read_fd = _obs._read_fd

        def flaky_read(fd: int) -> bytes:
            result = real_read_fd(fd)
            self.vault.touch_mtime("a.md", offset_seconds=5)
            return result

        with patch(
            "recallweave.steward_observe._read_fd", side_effect=flaky_read
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

    def test_batch_is_written_before_checkpoint_advances(self) -> None:
        # observe_source must persist the change batch itself, so a change is
        # never lost if the process dies before the checkpoint's batch reaches
        # disk. The batch file must exist right after observe_source returns.
        self.vault.write("a.md", "hello")
        self.observe(now="2026-01-01T00:00:00+00:00")
        batches = list(self.dirs["changes"].glob("*-src.json"))
        self.assertEqual(len(batches), 1)
        batch = json.loads(batches[0].read_text(encoding="utf-8"))
        self.assertEqual(batch["kind"], CHANGE_BATCH_KIND)
        self.assertEqual(batch["changes"][0]["change_type"], "added")

    def test_root_swap_before_commit_writes_nothing(self) -> None:
        # A root swapped after enumeration but before the batch/checkpoint write
        # must fail closed: no batch, no checkpoint advance.
        from recallweave.safe_write import path_identity
        real_ident = path_identity(self.vault.root)
        self.source = _source(
            "src", self.vault.root,
            root_dev=real_ident[0], root_ino=real_ident[1],
        )
        self.vault.write("a.md", "hello")
        calls = {"n": 0}

        def ident(_p):
            calls["n"] += 1
            # Pass the initial + post-enumeration checks; fail the commit check.
            if calls["n"] >= 3:
                return (real_ident[0], real_ident[1] + 1)  # different inode
            return real_ident

        with patch("recallweave.steward_observe.path_identity", side_effect=ident):
            receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(receipt.get("error"), "source_identity_changed")
        self.assertEqual(list(self.dirs["changes"].glob("*.json")), [])
        self.assertIsNone(self.checkpoint())

    def test_root_swap_during_batch_write_retracts_batch(self) -> None:
        # A swap detected only AFTER the batch is written must retract the batch
        # and not advance the checkpoint.
        from recallweave.safe_write import path_identity
        real_ident = path_identity(self.vault.root)
        self.source = _source(
            "src", self.vault.root,
            root_dev=real_ident[0], root_ino=real_ident[1],
        )
        self.vault.write("a.md", "hello")
        calls = {"n": 0}

        def ident(_p):
            calls["n"] += 1
            # Pass initial + post-enum + pre-write; fail only the POST-write check.
            if calls["n"] >= 4:
                return (real_ident[0], real_ident[1] + 1)
            return real_ident

        with patch("recallweave.steward_observe.path_identity", side_effect=ident):
            receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(receipt.get("error"), "source_identity_changed")
        self.assertEqual(list(self.dirs["changes"].glob("*.json")), [],
                         "the batch was not retracted after the root swap")
        self.assertIsNone(self.checkpoint())

    def test_open_note_fd_refuses_symlinked_ancestor(self) -> None:
        import os as _os
        from recallweave.steward_observe import _open_note_fd

        external = Path(self.temporary.name) / "ext"
        external.mkdir()
        (external / "note.md").write_text("outside", encoding="utf-8")
        try:
            _os.symlink(external, self.vault.root / "sub", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported")
        with self.assertRaises(OSError):
            _open_note_fd(self.source, self.vault.root, self.vault.root, "sub/note.md")

    def test_same_timestamp_run_does_not_overwrite_an_earlier_batch(self) -> None:
        # A second observation sharing the first's timestamp (clock rollback or
        # injected `now`) must NOT overwrite the first's not-yet-assessed batch.
        self.vault.write("a.md", "hello")
        first = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(
            [c["change_type"] for c in first["changes"]], ["added"]
        )
        first_batch = self.dirs["changes"] / "20260101T000000000000Z-src.json"
        self.assertTrue(first_batch.is_file())
        # Second run, same timestamp, no tree change -> empty batch. It must land
        # under a distinct name, leaving the first batch's change intact.
        second = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertEqual(second["changes"], [])
        saved = json.loads(first_batch.read_text(encoding="utf-8"))
        self.assertEqual(
            [c["change_type"] for c in saved["changes"]], ["added"],
            "the earlier batch's recorded change was overwritten",
        )
        batches = list(self.dirs["changes"].glob("*-src.json"))
        self.assertEqual(len(batches), 2, "the second batch reused the first's name")

    def test_checkpoint_from_a_different_registry_is_rebaselined(self) -> None:
        self.vault.write("a.md", "hello")
        observe_source(
            self.source, self.dirs, registry_sha256="reg-A",
            now="2026-01-01T00:00:00+00:00",
        )
        # A later run under a different registry digest must not diff against
        # the old baseline; it rebaselines instead of emitting a false removal.
        self.vault.write("b.md", "world")
        receipt = observe_source(
            self.source, self.dirs, registry_sha256="reg-B",
            now="2026-01-02T00:00:00+00:00",
        )
        self.assertTrue(receipt["checkpoint_invalid"])
        self.assertEqual(
            {c["change_type"] for c in receipt["changes"]}, {"added"}
        )
        self.assertEqual(len(receipt["changes"]), 2)  # both a.md and b.md as added
        fresh = self.checkpoint()
        self.assertEqual(fresh["registry_sha256"], "reg-B")

    def test_file_unreadable_during_hash_does_not_abort_observation(self) -> None:
        self.vault.write("a.md", "hello")
        self.vault.write("b.md", "world")
        import recallweave.steward_observe as _obs
        real_read_fd = _obs._read_fd
        calls = {"n": 0}

        def flaky(fd):
            calls["n"] += 1
            if calls["n"] == 1:  # first admitted file (a.md, sorted first)
                raise OSError("vanished mid-hash")
            return real_read_fd(fd)

        with patch("recallweave.steward_observe._read_fd", side_effect=flaky):
            receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        # b.md still observed; a.md recorded as changed-during-observe, not fatal.
        self.assertIn("a.md", receipt["changed_during_observe"])
        added = {c["relative_path"] for c in receipt["changes"] if c["change_type"] == "added"}
        self.assertIn("b.md", added)

    def test_frontmatter_denial_uses_hashed_bytes(self) -> None:
        # Frontmatter admission is computed from the same bytes that are hashed
        # (read from the pinned fd), so an undecodable note under a deny rule is
        # skipped as unsupported_encoding and never committed, while a normal
        # note in the same run is still observed.
        self.source = _source(
            "src", self.vault.root,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        (self.vault.root / "a.md").write_bytes(b"\xff\xfe\x00b\x00a\x00d")  # UTF-16 BOM
        self.vault.write("b.md", "---\ntitle: B\n---\nbody\n")
        receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertGreaterEqual(receipt["skipped"].get("unsupported_encoding", 0), 1)
        added = {c["relative_path"] for c in receipt["changes"] if c["change_type"] == "added"}
        self.assertIn("b.md", added)
        self.assertNotIn("a.md", added)

    def test_unreadable_subtree_is_not_reported_as_deletions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX directory permissions")
        # Baseline: a note inside a subdirectory is observed and checkpointed.
        self.vault.write("sub/keep.md", "kept note")
        self.vault.write("top.md", "top note")
        self.observe(now="2026-01-01T00:00:00+00:00")
        subdir = self.vault.root / "sub"
        os.chmod(subdir, 0o000)
        try:
            receipt = self.observe(now="2026-01-02T00:00:00+00:00")
        finally:
            os.chmod(subdir, 0o755)
        removed = {c["relative_path"] for c in receipt["changes"] if c["change_type"] == "removed"}
        self.assertNotIn(
            "sub/keep.md", removed,
            "an unreadable subtree was reported as deletions",
        )
        # The prior entry is retained in the checkpoint (not dropped).
        cp = self.checkpoint()
        self.assertIn("sub/keep.md", {e["relative_path"] for e in cp["entries"]})

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
