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

    def test_two_removed_one_added_same_hash_no_rename_candidate(self) -> None:
        # Two removed notes with identical bytes and one added note with those
        # bytes: the rename mapping is ambiguous. No compilable candidate may be
        # emitted (each removal would otherwise be paired with the same addition
        # and both compiled as clean renames).
        self.vault.write("a.md", "same content")
        self.vault.write("b.md", "same content")
        self.observe()
        self.vault.remove("a.md")
        self.vault.remove("b.md")
        self.vault.write("c.md", "same content")
        receipt = self.observe()
        self.assertEqual(receipt["rename_candidates"], [])

    def test_two_removed_two_added_same_hash_no_rename_candidate(self) -> None:
        # Ambiguity on BOTH sides: two removed and two added notes share one
        # content hash. No compilable rename candidate may be emitted.
        self.vault.write("a.md", "same content")
        self.vault.write("b.md", "same content")
        self.observe()
        self.vault.remove("a.md")
        self.vault.remove("b.md")
        self.vault.write("c.md", "same content")
        self.vault.write("d.md", "same content")
        receipt = self.observe()
        self.assertEqual(receipt["rename_candidates"], [])

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

    def test_hardlink_planted_after_discovery_rejected_on_pathname_fallback(
        self,
    ) -> None:
        # A note admitted as a single-link regular file at discovery, then
        # turned into a hardlink just before it is opened, must be rejected by
        # the post-open st_nlink guard even on the pathname fallback (Windows).
        # The extra link is placed OUTSIDE the vault so discovery still admits
        # exactly one note; only the descriptor-level guard can catch the swap.
        # Reverting the fix (re-gating the guard to the dir_fd path) makes this
        # fail, so it genuinely covers the repaired race.
        self.vault.write("note.md", "single link at discovery")
        outside = Path(self.temporary.name) / "planted-link"
        import recallweave.steward_observe as _obs

        real_open_note_fd = _obs._open_note_fd

        def swap_then_open(source, resolved_root, base, relative):
            if not outside.exists():
                try:
                    os.link(self.vault.root / "note.md", outside)
                except OSError as error:  # pragma: no cover - platform guard
                    raise unittest.SkipTest(
                        f"hardlink creation unavailable: {error}"
                    )
            return real_open_note_fd(source, resolved_root, base, relative)

        try:
            with patch("recallweave.steward_observe._DIR_FD_OBSERVE", False), patch(
                "recallweave.steward_observe._open_note_fd",
                side_effect=swap_then_open,
            ):
                receipt = self.observe()
        except unittest.SkipTest as skip:
            self.skipTest(str(skip))
        self.assertEqual(receipt["skipped"]["hardlink"], 1)
        self.assertEqual(receipt["changes"], [])
        checkpoint = self.checkpoint()
        if checkpoint is not None:
            entry_paths = [e["relative_path"] for e in checkpoint["entries"]]
            self.assertNotIn("note.md", entry_paths)

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

        def flaky_read(fd: int, limit: int) -> bytes:
            result = real_read_fd(fd, limit)
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

    def test_hash_time_read_failure_counts_unreadable_and_retains_prior(
        self,
    ) -> None:
        # A read/post-read-stat failure at hash time counts as unreadable_path
        # (consistent with the open/stat branches) and retains the prior entry
        # instead of false-removing it.
        self.vault.write("a.md", "note a")
        self.vault.write("b.md", "note b")
        self.observe(now="2026-01-01T00:00:00+00:00")
        import recallweave.steward_observe as _obs

        real_read_fd = _obs._read_fd
        calls = {"n": 0}

        def flaky_read(fd, limit):
            calls["n"] += 1
            if calls["n"] == 1:  # a.md (sorted first): its hash-time read fails
                raise OSError("read failed at hash time")
            return real_read_fd(fd, limit)

        with patch("recallweave.steward_observe._read_fd", side_effect=flaky_read):
            receipt = self.observe(now="2026-01-01T00:00:01+00:00")
        self.assertGreaterEqual(receipt["skipped"].get("unreadable_path", 0), 1)
        self.assertIn("a.md", receipt["changed_during_observe"])
        removed = {
            c["relative_path"]
            for c in receipt["changes"]
            if c["change_type"] == "removed"
        }
        self.assertNotIn("a.md", removed)

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

    def test_failed_batch_retraction_raises(self) -> None:
        # If the out-of-scope batch cannot be retracted, fail closed (raise)
        # rather than leaving an assessable batch behind.
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
            if calls["n"] >= 4:  # fail the post-write check
                return (real_ident[0], real_ident[1] + 1)
            return real_ident

        with patch("recallweave.steward_observe.path_identity", side_effect=ident), \
                patch("recallweave.steward_observe.os.unlink", side_effect=OSError("cannot delete")), \
                patch("pathlib.Path.unlink", side_effect=OSError("cannot delete")):
            with self.assertRaises(OSError):
                self.observe(now="2026-01-01T00:00:00+00:00")

    def test_retract_change_batch_refuses_symlinked_changes_dir(self) -> None:
        # Batch retraction must be descriptor-relative to the pinned state root:
        # a changes/ directory swapped for a symlink must not let the unlink
        # delete a same-named file in the symlink target, and the retraction must
        # not be reported as successful.
        import recallweave.steward_observe as _obs

        if not _obs._DIR_FD_RETRACT:
            self.skipTest("descriptor-relative retraction unavailable")
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            state = base / "state"
            state.mkdir()
            external = base / "external"
            external.mkdir()
            victim = external / "batch.json"
            victim.write_text("{}", encoding="utf-8")
            changes_link = state / "changes"
            try:
                changes_link.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            self.assertFalse(_obs._retract_change_batch(changes_link, "batch.json"))
            self.assertTrue(victim.exists(), "retraction deleted through a symlink")

    def test_retract_change_batch_fallback_refuses_symlinked_changes_dir(self) -> None:
        # The pathname fallback (Windows, no dir_fd) must also refuse a link-like
        # changes/ directory rather than delete through it. Force the fallback and
        # verify the external same-named file is untouched and retraction fails.
        import recallweave.steward_observe as _obs

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            state = base / "state"
            state.mkdir()
            external = base / "external"
            external.mkdir()
            victim = external / "batch.json"
            victim.write_text("{}", encoding="utf-8")
            changes_link = state / "changes"
            try:
                changes_link.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with patch("recallweave.steward_observe._DIR_FD_RETRACT", False):
                self.assertFalse(
                    _obs._retract_change_batch(changes_link, "batch.json")
                )
            self.assertTrue(victim.exists(), "fallback retraction deleted through a symlink")

    def test_note_growing_during_read_is_not_committed(self) -> None:
        import recallweave.steward_observe as _obs
        self.vault.write("a.md", "small")

        def grown(fd, limit):  # simulate the file having grown past the check
            return b"x" * (limit + 500)

        with patch.object(_obs, "_read_fd", side_effect=grown):
            receipt = self.observe(now="2026-01-01T00:00:00+00:00")
        self.assertIn("a.md", receipt["changed_during_observe"])
        self.assertEqual(receipt["changes"], [])
        # a.md's grown bytes are never committed to the checkpoint.
        entries = {e["relative_path"] for e in (self.checkpoint() or {}).get("entries", [])}
        self.assertNotIn("a.md", entries)

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

        def flaky(fd, limit):
            calls["n"] += 1
            if calls["n"] == 1:  # first admitted file (a.md, sorted first)
                raise OSError("vanished mid-hash")
            return real_read_fd(fd, limit)

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

    def test_prior_note_failing_to_open_is_not_reported_as_removal(self) -> None:
        # A checkpointed note that fails to OPEN on a later run (unreadable,
        # vanished, or symlink-swapped between enumeration and open) must not be
        # emitted as a removal and dropped from the checkpoint: doing so makes a
        # subsequent stable run resurface it as newly added. Its prior entry is
        # retained and it is marked changed-during-observe. Regression for the
        # pre-open failure branch that only counted skipped["unreadable_path"].
        self.vault.write("a.md", "note a")
        self.vault.write("b.md", "note b")
        self.observe(now="2026-01-01T00:00:00+00:00")
        import recallweave.steward_observe as _obs

        real_open = _obs._open_note_fd

        def flaky_open(source, resolved_root, base, relative):
            if relative == "a.md":
                raise OSError("unreadable at open")
            return real_open(source, resolved_root, base, relative)

        with patch("recallweave.steward_observe._open_note_fd", side_effect=flaky_open):
            receipt = self.observe(now="2026-01-01T00:00:01+00:00")
        removed = {
            c["relative_path"]
            for c in receipt["changes"]
            if c["change_type"] == "removed"
        }
        self.assertNotIn("a.md", removed)
        self.assertIn("a.md", receipt["changed_during_observe"])
        entry_paths = {e["relative_path"] for e in self.checkpoint()["entries"]}
        self.assertIn("a.md", entry_paths)
        # A later run with a.md readable and unchanged must not resurface it as
        # newly added (the churn the false removal would have caused).
        receipt3 = self.observe(now="2026-01-01T00:00:02+00:00")
        added3 = {
            c["relative_path"]
            for c in receipt3["changes"]
            if c["change_type"] == "added"
        }
        self.assertNotIn("a.md", added3)

    def test_prior_note_failing_post_open_stat_is_not_reported_as_removal(
        self,
    ) -> None:
        # The false-removal retention must also cover a POST-OPEN _pinned_stat
        # failure (a swap/removal racing after the descriptor is opened), not
        # only a failed open. Regression for the stat-OSError branch dropping the
        # prior entry.
        self.vault.write("a.md", "note a")
        self.vault.write("b.md", "note b")
        self.observe(now="2026-01-01T00:00:00+00:00")
        import recallweave.steward_observe as _obs

        real_stat = _obs._pinned_stat
        calls = {"n": 0}

        def flaky_stat(fd):
            calls["n"] += 1
            if calls["n"] == 1:  # a.md (sorted first): its first stat fails
                raise OSError("stat failed after open")
            return real_stat(fd)

        with patch("recallweave.steward_observe._pinned_stat", side_effect=flaky_stat):
            receipt = self.observe(now="2026-01-01T00:00:01+00:00")
        removed = {
            c["relative_path"]
            for c in receipt["changes"]
            if c["change_type"] == "removed"
        }
        self.assertNotIn("a.md", removed)
        self.assertIn("a.md", receipt["changed_during_observe"])
        entry_paths = {e["relative_path"] for e in self.checkpoint()["entries"]}
        self.assertIn("a.md", entry_paths)

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
