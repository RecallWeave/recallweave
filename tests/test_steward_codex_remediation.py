"""Adversarial regressions from the G1 independent review.

Each test here demonstrates a defect the review identified and pins its fix:
policy projection of index-derived paths, state-root/source overlap refusal,
symlink-safe state writes and pruning, digest-bound re-assessment, byte-level
integrity under restored timestamps, cross-source binding, and machine-local
persisted artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_assess import assess_change_batch, assess_latest
from recallweave.steward_observe import observe_registry, observe_source
from recallweave.steward_propose import propose_latest
from recallweave.steward_sources import SourceRegistry, StewardSource
from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    guard_within,
)
from recallweave.steward_sweep import _prune_dir, status_report, sweep_registry

from steward_fixtures import TempVault, hold_lock, make_symlink


def _source(name: str, root: Path, policy: IndexPolicy | None = None) -> StewardSource:
    return StewardSource(
        name=name,
        type="folder",
        root=root,
        mode="read_only",
        policy=policy if policy is not None else IndexPolicy(),
    )


def _batch(source: str, changes: list[dict], *, registry_sha256: str | None = "reg") -> dict:
    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": "change_batch",
        "operation": "steward_observe",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "source": source,
        "registry_sha256": registry_sha256,
        "changes": changes,
        "rename_candidates": [],
        "change_summary": {},
        "skipped": {},
        "changed_during_observe": [],
        "network_calls": 0,
        "vault_writes": 0,
    }


def _walk_strings(value, out):
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_strings(key, out)
            _walk_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _walk_strings(item, out)
    elif isinstance(value, str):
        out.append(value)


class PolicyProjectionTest(unittest.TestCase):
    """Index-derived paths must pass the active source policy before emission."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.database = base / "index.sqlite"

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_duplicate_of_redacts_paths_outside_source_policy(self) -> None:
        shared_bytes = "the very same bytes"
        self.vault.write("Public/New.md", shared_bytes)
        self.vault.write("Restricted/Patient.md", shared_bytes)
        build_index(self.vault.root, self.database, policy=IndexPolicy())

        narrow = IndexPolicy.from_payload({"include_paths": ["Public/New.md"]})
        current_hash = hashlib.sha256(shared_bytes.encode()).hexdigest()
        batch = _batch(
            "src",
            [
                {
                    "relative_path": "Public/New.md",
                    "change_type": "modified",
                    "previous_content_hash": "0" * 64,
                    "current_content_hash": current_hash,
                }
            ],
        )
        result = assess_change_batch(
            batch, self.database, self.vault.root, policy=narrow
        )
        strings: list[str] = []
        _walk_strings(result, strings)
        self.assertFalse(
            any("Restricted" in item for item in strings),
            "an excluded path leaked into the assessment",
        )
        self.assertGreaterEqual(result["summary"]["redacted_out_of_policy"], 1)

    def test_authored_edges_redact_paths_outside_source_policy(self) -> None:
        self.vault.write("Public/Note.md", "# Note\n\nSee [[Secret]].\n")
        self.vault.write("Restricted/Secret.md", "# Secret\n\nBody.\n")
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        narrow = IndexPolicy.from_payload({"include_paths": ["Public/Note.md"]})
        batch = _batch(
            "src",
            [
                {
                    "relative_path": "Public/Note.md",
                    "change_type": "modified",
                    "previous_content_hash": "0" * 64,
                    "current_content_hash": "1" * 64,
                }
            ],
        )
        result = assess_change_batch(
            batch, self.database, self.vault.root, policy=narrow
        )
        strings: list[str] = []
        _walk_strings(result, strings)
        self.assertFalse(any("Restricted" in item for item in strings))


class StateRootOverlapTest(unittest.TestCase):
    """A state root inside (or containing) a registered source is refused."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
        self.database = self.base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.registry = SourceRegistry(
            sources=[_source("src", self.vault.root)], registry_sha256="reg"
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_every_entry_point_refuses_in_source_state_root(self) -> None:
        inside = self.vault.root / "StewardState"
        entry_points = [
            lambda: observe_registry(self.registry, inside),
            lambda: assess_latest(self.registry, inside, self.database),
            lambda: propose_latest(self.registry, inside, self.database),
            lambda: sweep_registry(self.registry, inside, self.database),
            lambda: status_report(
                inside, sources=list(self.registry.sources)
            ),
        ]
        for call in entry_points:
            with self.assertRaisesRegex(ValueError, "overlaps a registered source"):
                call()

    def test_state_root_containing_a_source_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "overlaps a registered source"):
            ensure_state_root_outside_sources(
                self.base, [_source("vault", self.vault.root)]
            )


class SymlinkedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_atomic_write_refuses_symlinked_state_subdir(self) -> None:
        state_root = self.base / "state"
        dirs = ensure_state_layout(state_root)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        os.rmdir(dirs["changes"])
        if not make_symlink(elsewhere, dirs["changes"]):
            self.skipTest("symlinks unsupported")
        with self.assertRaisesRegex(ValueError, "symlink"):
            atomic_write_json(
                dirs["changes"] / "x.json", {"a": 1}, within=state_root
            )

    def test_guard_within_refuses_escaping_destination(self) -> None:
        state_root = self.base / "state"
        ensure_state_layout(state_root)
        with self.assertRaisesRegex(ValueError, "outside its state directory"):
            guard_within(self.base / "outside.json", state_root)

    def test_prune_refuses_symlinked_directory(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (real / "victim.txt").write_text("data", encoding="utf-8")
        link = self.base / "link"
        if not make_symlink(real, link):
            self.skipTest("symlinks unsupported")
        with self.assertRaisesRegex(ValueError, "symlinked"):
            _prune_dir(link, cutoff_epoch=10**12)
        self.assertTrue((real / "victim.txt").exists())


class DigestBoundAssessmentTest(unittest.TestCase):
    """A rewritten batch under a colliding filename must be re-assessed."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.vault.write("a.md", "hello")
        self.database = base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.state_root = base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.registry = SourceRegistry(
            sources=[_source("src", self.vault.root)], registry_sha256=None
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _write_batch(self, changes: list[dict]) -> Path:
        path = self.dirs["changes"] / "20260101T000000000000Z-src.json"
        atomic_write_json(
            path, _batch("src", changes, registry_sha256=None),
            within=self.dirs["changes"],
        )
        return path

    def test_rewritten_batch_same_filename_is_reassessed(self) -> None:
        self._write_batch(
            [
                {
                    "relative_path": "new1.md",
                    "change_type": "added",
                    "previous_content_hash": None,
                    "current_content_hash": "a" * 64,
                }
            ]
        )
        first = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(len(first["assessed"]), 1)

        second = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(second["skipped_sources"][0]["reason"], "already_assessed")

        self._write_batch(
            [
                {
                    "relative_path": "new2.md",
                    "change_type": "added",
                    "previous_content_hash": None,
                    "current_content_hash": "b" * 64,
                }
            ]
        )
        third = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(len(third["assessed"]), 1, "rewritten batch was skipped")
        assessment = json.loads(
            (self.dirs["assessments"] / "20260101T000000000000Z-src.json").read_text(
                encoding="utf-8"
            )
        )
        paths = [item["relative_path"] for item in assessment["assessments"]]
        self.assertIn("new2.md", paths)


class RestoredMtimeIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.state_root = base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.source = _source("src", self.vault.root)

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_equal_length_change_with_restored_mtime_is_detected(self) -> None:
        path = self.vault.write("a.md", "aaaa")
        observe_source(self.source, self.dirs, registry_sha256=None)
        stat = path.stat()
        path.write_bytes(b"bbbb")  # same length
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        receipt = observe_source(self.source, self.dirs, registry_sha256=None)
        modified = [
            change["relative_path"]
            for change in receipt["changes"]
            if change["change_type"] == "modified"
        ]
        self.assertEqual(modified, ["a.md"])


class CrossSourceBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.vault.write("a.md", "hello")
        self.database = base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_wrong_source_batch_is_refused(self) -> None:
        batch = _batch("other", [])
        with self.assertRaisesRegex(ValueError, "cross-source"):
            assess_change_batch(
                batch, self.database, self.vault.root, expected_source="src"
            )

    def test_registry_sha_mismatch_is_refused(self) -> None:
        batch = _batch("src", [], registry_sha256="stale")
        with self.assertRaisesRegex(ValueError, "registry_sha256 mismatch"):
            assess_change_batch(
                batch,
                self.database,
                self.vault.root,
                expected_source="src",
                expected_registry_sha256="current",
            )


class MachineLocalArtifactsTest(unittest.TestCase):
    """Persisted assessments and proposals carry no absolute paths."""

    def test_persisted_state_artifacts_have_no_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            vault = TempVault(dir=base)
            try:
                vault.write("a.md", "# A\n\nSee [[B]].\n")
                vault.write("b.md", "# B\n\nBody.\n")
                database = base / "index.sqlite"
                build_index(vault.root, database, policy=IndexPolicy())
                state_root = base / "state"
                registry = SourceRegistry(
                    sources=[_source("src", vault.root)], registry_sha256=None
                )
                sweep_registry(registry, state_root, database)
                vault.move("b.md", "c.md")
                sweep_registry(registry, state_root, database)

                offenders: list[str] = []
                for subdir in ("changes", "assessments", "proposals", "reports"):
                    for artifact in (state_root / subdir).glob("*.json"):
                        document = json.loads(
                            artifact.read_text(encoding="utf-8")
                        )
                        strings: list[str] = []
                        _walk_strings(document, strings)
                        for item in strings:
                            if str(base) in item:
                                offenders.append(f"{artifact.name}: {item}")
                self.assertEqual(offenders, [])
            finally:
                vault.cleanup()




class FrontmatterDenialTest(unittest.TestCase):
    """The full IndexPolicy applies during observation, deny_frontmatter included."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.state_root = base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        policy = IndexPolicy.from_payload(
            {"deny_frontmatter": {"sensitivity": ["sealed"]}}
        )
        self.source = _source("src", self.vault.root, policy=policy)

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_sealed_note_is_absent_from_all_steward_state(self) -> None:
        self.vault.write("open.md", "# Open\n\nBody.\n")
        self.vault.write(
            "sealed.md", "---\nsensitivity: sealed\n---\n# Secret\n"
        )
        receipt = observe_source(self.source, self.dirs, registry_sha256=None)
        strings: list[str] = []
        _walk_strings(receipt, strings)
        self.assertFalse(any("sealed.md" in item for item in strings))
        self.assertEqual(receipt["skipped"].get("denied_frontmatter:sensitivity"), 1)
        checkpoint = json.loads(
            (self.dirs["checkpoints"] / "src.json").read_text(encoding="utf-8")
        )
        paths = [entry["relative_path"] for entry in checkpoint["entries"]]
        self.assertEqual(paths, ["open.md"])

    def test_malformed_frontmatter_fails_closed_with_deny_rules(self) -> None:
        self.vault.write("open.md", "# Open\n")
        bad = self.vault.root / "bad.md"
        bad.write_bytes(b"\xff\xfe\x00b\x00r\x00o\x00k\x00e\x00n")
        receipt = observe_source(self.source, self.dirs, registry_sha256=None)
        strings: list[str] = []
        _walk_strings(receipt, strings)
        self.assertFalse(any("bad.md" in item for item in strings))
        self.assertEqual(receipt["skipped"].get("unsupported_encoding"), 1)


class ProductionShapedSymlinkTest(unittest.TestCase):
    """Symlinked state subdirs are refused in the exact production call shape."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
        self.database = self.base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.registry = SourceRegistry(
            sources=[_source("src", self.vault.root)], registry_sha256=None
        )
        self.state_root = self.base / "state"

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _link_subdir(self, name: str) -> bool:
        ensure_state_layout(self.state_root)
        target = self.vault.root / "Injected"
        target.mkdir(exist_ok=True)
        subdir = self.state_root / name
        os.rmdir(subdir)
        return make_symlink(target, subdir)

    def test_observe_refuses_symlinked_changes_dir_and_writes_nothing(self) -> None:
        if not self._link_subdir("changes"):
            self.skipTest("symlinks unsupported")
        with self.assertRaisesRegex(ValueError, "symlinked steward state"):
            observe_registry(self.registry, self.state_root)
        self.assertEqual(
            list((self.vault.root / "Injected").iterdir()), [],
            "a state write escaped into the source",
        )

    def test_sweep_refuses_symlinked_reports_dir(self) -> None:
        if not self._link_subdir("reports"):
            self.skipTest("symlinks unsupported")
        with self.assertRaisesRegex(ValueError, "symlinked steward state"):
            sweep_registry(self.registry, self.state_root, self.database)
        self.assertEqual(list((self.vault.root / "Injected").iterdir()), [])

    def test_layout_refuses_symlinked_state_root(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        link_root = self.base / "link-root"
        if not make_symlink(elsewhere, link_root):
            self.skipTest("symlinks unsupported")
        with self.assertRaisesRegex(ValueError, "symlinked steward state root"):
            ensure_state_layout(link_root)


class BatchPathHygieneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.vault.write("a.md", "hello")
        self.database = base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_traversal_and_absolute_paths_in_batches_are_refused(self) -> None:
        for hostile in ("../secret.md", "/etc/passwd", "a/../../b.md", "C:evil.md"):
            batch = _batch(
                "src",
                [
                    {
                        "relative_path": hostile,
                        "change_type": "modified",
                        "previous_content_hash": "0" * 64,
                        "current_content_hash": "1" * 64,
                    }
                ],
            )
            with self.assertRaisesRegex(ValueError, "Invalid relative path"):
                assess_change_batch(batch, self.database, self.vault.root)




class FullAdmissionProjectionTest(unittest.TestCase):
    """Index-neighbor projection applies the FULL policy, not just path rules."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.database = base / "index.sqlite"

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _modified_batch(self, path: str, current_hash: str) -> dict:
        return _batch(
            "src",
            [
                {
                    "relative_path": path,
                    "change_type": "modified",
                    "previous_content_hash": "0" * 64,
                    "current_content_hash": current_hash,
                }
            ],
        )

    def test_frontmatter_denied_neighbor_is_redacted(self) -> None:
        body = "# Same\n\nIdentical body bytes.\n"
        sealed = "---\nsensitivity: sealed\n---\n" + body
        self.vault.write("Public/Note.md", sealed)
        self.vault.write("Restricted/Patient.md", sealed)
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        policy = IndexPolicy.from_payload(
            {"deny_frontmatter": {"sensitivity": ["sealed"]}}
        )
        current = hashlib.sha256(sealed.encode()).hexdigest()
        result = assess_change_batch(
            self._modified_batch("Public/Note.md", current),
            self.database,
            self.vault.root,
            policy=policy,
        )
        strings: list[str] = []
        _walk_strings(result, strings)
        self.assertFalse(any("Patient" in item for item in strings))

    def test_oversized_neighbor_is_redacted(self) -> None:
        body = "x" * 512
        self.vault.write("Public/Note.md", body)
        self.vault.write("Restricted/Big.md", body)
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        policy = IndexPolicy.from_payload({"max_file_bytes": 100})
        current = hashlib.sha256(body.encode()).hexdigest()
        result = assess_change_batch(
            self._modified_batch("Public/Note.md", current),
            self.database,
            self.vault.root,
            policy=policy,
        )
        strings: list[str] = []
        _walk_strings(result, strings)
        self.assertFalse(any("Big" in item for item in strings))
        self.assertGreaterEqual(result["summary"]["redacted_out_of_policy"], 1)


class NullDigestBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.vault.write("a.md", "hello")
        self.database = base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_null_batch_digest_fails_closed_when_registry_has_one(self) -> None:
        batch = _batch("src", [], registry_sha256=None)
        with self.assertRaisesRegex(ValueError, "registry_sha256 mismatch"):
            assess_change_batch(
                batch,
                self.database,
                self.vault.root,
                expected_source="src",
                expected_registry_sha256="currentdigest",
            )


class PruneLockingTest(unittest.TestCase):
    def test_prune_refuses_while_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            state_root = Path(name) / "state"
            dirs = ensure_state_layout(state_root)
            victim = dirs["changes"] / "old.json"
            victim.write_text("{}", encoding="utf-8")
            os.utime(victim, times=(0, 0))
            with hold_lock(state_root / "steward.lock"):
                with self.assertRaisesRegex(ValueError, "holds the lock"):
                    status_report(state_root, prune_older_than_days=1)
            self.assertTrue(victim.exists(), "prune deleted despite held lock")




class EvidenceIntegrityTest(unittest.TestCase):
    """Checkpoint digests bind every field; proposals verify pinned batches."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_every_checkpoint_entry_field_affects_the_digest(self) -> None:
        from recallweave.steward_checkpoint import CheckpointEntry, manifest_digest

        base = CheckpointEntry("a.md", "a" * 64, 10, 20, 1, 100)
        digest = manifest_digest("src", [base])
        variants = [
            CheckpointEntry("b.md", "a" * 64, 10, 20, 1, 100),
            CheckpointEntry("a.md", "b" * 64, 10, 20, 1, 100),
            CheckpointEntry("a.md", "a" * 64, 11, 20, 1, 100),
            CheckpointEntry("a.md", "a" * 64, 10, 21, 1, 100),
            CheckpointEntry("a.md", "a" * 64, 10, 20, 2, 100),
            CheckpointEntry("a.md", "a" * 64, 10, 20, 1, 101),
        ]
        for variant in variants:
            self.assertNotEqual(
                digest, manifest_digest("src", [variant]),
                f"field change not digest-bound: {variant}",
            )

    def test_inode_only_checkpoint_tamper_is_rejected(self) -> None:
        from recallweave.steward_checkpoint import (
            CheckpointEntry,
            CheckpointError,
            load_checkpoint,
            save_checkpoint,
        )

        entry = CheckpointEntry("a.md", "a" * 64, 10, 20, 1, 100)
        path = save_checkpoint(
            self.dirs, "src", [entry],
            generated_at="2026-01-01T00:00:00+00:00", registry_sha256=None,
        )
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entries"][0]["file_ino"] = 999
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            load_checkpoint(self.dirs, "src")


class ProposalBatchPinningTest(unittest.TestCase):
    """A batch rewritten after assessment must never drive compiled edits."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.vault = TempVault(dir=base)
        self.vault.write("a.md", "hello")
        self.database = base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.state_root = base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.registry = SourceRegistry(
            sources=[_source("src", self.vault.root)], registry_sha256=None
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_tampered_rename_candidates_refuse_at_propose(self) -> None:
        batch_path = self.dirs["changes"] / "20260101T000000000000Z-src.json"
        atomic_write_json(
            batch_path,
            _batch("src", [], registry_sha256=None),
            within=self.dirs["changes"],
        )
        assess_latest(self.registry, self.state_root, self.database)

        tampered = json.loads(batch_path.read_text(encoding="utf-8"))
        tampered["rename_candidates"] = [
            {
                "removed_path": "old.md",
                "added_paths": ["unrelated.md"],
                "content_hash": "c" * 64,
                "inode_match": True,
            }
        ]
        batch_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no longer matches the digest"):
            propose_latest(self.registry, self.state_root, self.database)

    def test_invalid_change_batch_ref_is_refused(self) -> None:
        batch_path = self.dirs["changes"] / "20260101T000000000000Z-src.json"
        atomic_write_json(
            batch_path,
            _batch("src", [], registry_sha256=None),
            within=self.dirs["changes"],
        )
        assess_latest(self.registry, self.state_root, self.database)
        assessment_path = self.dirs["assessments"] / batch_path.name
        document = json.loads(assessment_path.read_text(encoding="utf-8"))
        document["change_batch_ref"] = "../escape.json"
        assessment_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid"):
            propose_latest(self.registry, self.state_root, self.database)




class RootRebindingTest(unittest.TestCase):
    """A source root replaced after registry load must not rebind the boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_root_swapped_for_symlink_is_refused_without_observation(self) -> None:
        real_root = self.base / "vault"
        real_root.mkdir()
        (real_root / "a.md").write_text("hello", encoding="utf-8")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret bytes", encoding="utf-8")

        registry_path = self.base / "sources.json"
        registry_path.write_text(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.sources.v1",
                    "sources": [
                        {
                            "name": "src",
                            "type": "folder",
                            "root": str(real_root),
                            "mode": "read_only",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        from recallweave.steward_sources import load_registry

        registry = load_registry(registry_path)
        self.assertIsNotNone(registry.sources[0].root_dev)

        import shutil

        shutil.rmtree(real_root)
        if not make_symlink(outside, real_root):
            self.skipTest("symlinks unsupported")

        state_root = self.base / "state"
        receipt = observe_registry(registry, state_root)
        source_receipt = receipt["sources"][0]
        self.assertIn(
            source_receipt.get("error"),
            ("source_root_symlinked", "source_identity_changed"),
        )
        strings: list[str] = []
        _walk_strings(receipt, strings)
        self.assertFalse(any("secret" in item for item in strings))
        self.assertEqual(list((state_root / "changes").glob("*.json")), [])
        self.assertEqual(list((state_root / "checkpoints").glob("*.json")), [])

    def test_root_replaced_by_new_directory_is_refused(self) -> None:
        real_root = self.base / "vault"
        real_root.mkdir()
        registry_path = self.base / "sources.json"
        registry_path.write_text(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.sources.v1",
                    "sources": [
                        {
                            "name": "src",
                            "type": "folder",
                            "root": str(real_root),
                            "mode": "read_only",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        from recallweave.steward_sources import load_registry

        registry = load_registry(registry_path)
        import shutil

        shutil.rmtree(real_root)
        real_root.mkdir()
        (real_root / "planted.md").write_text("planted", encoding="utf-8")
        receipt = observe_registry(registry, self.base / "state")
        self.assertEqual(
            receipt["sources"][0].get("error"), "source_identity_changed"
        )


class SymlinkedStateRootLockTest(unittest.TestCase):
    """assess/propose must not create a lock through a symlinked state root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
        self.database = self.base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.registry = SourceRegistry(
            sources=[_source("src", self.vault.root)], registry_sha256=None
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def test_assess_and_propose_refuse_symlinked_state_root(self) -> None:
        target = self.base / "target"
        target.mkdir()
        link_root = self.base / "link-state"
        if not make_symlink(target, link_root):
            self.skipTest("symlinks unsupported")
        for call in (
            lambda: assess_latest(self.registry, link_root, self.database),
            lambda: propose_latest(self.registry, link_root, self.database),
        ):
            with self.assertRaisesRegex(ValueError, "symlinked steward state root"):
                call()
            self.assertFalse(
                (target / "steward.lock").exists(),
                "a lock write escaped through the symlinked state root",
            )


class ErrorEnvelopeRedactionTest(unittest.TestCase):
    def test_steward_cli_errors_redact_absolute_paths(self) -> None:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from recallweave.cli import main as cli_main

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            missing = base / "does-not-exist" / "sources.json"
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = cli_main(["steward-observe", str(missing)])
            self.assertEqual(exit_code, 2)
            payload = json.loads(err.getvalue())
            self.assertNotIn(str(base), payload["message"])
            self.assertNotIn(str(base), out.getvalue())


class MissingSourceStalenessTest(unittest.TestCase):
    def test_missing_source_contributes_no_historical_data(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "vault"
            root.mkdir()
            (root / "a.md").write_text("# A\n\nBody.\n", encoding="utf-8")
            database = base / "index.sqlite"
            build_index(root, database, policy=IndexPolicy())
            registry = SourceRegistry(
                sources=[_source("src", root)], registry_sha256=None
            )
            state_root = base / "state"
            first = sweep_registry(registry, state_root, database)
            self.assertGreaterEqual(first["changes"]["src"]["added"], 1)

            shutil.rmtree(root)
            second = sweep_registry(registry, state_root, database)
            self.assertIn("src", second["integrity"]["sources_missing"])
            self.assertEqual(
                second["changes"]["src"],
                {"added": 0, "modified": 0, "removed": 0},
                "historical batch data masqueraded as current-run data",
            )
            total_relations = sum(
                second["assessments"].get(rel, 0)
                for rel in (
                    "NEW",
                    "DELETED",
                    "MODIFIED",
                    "DUPLICATES_EXACT_BYTES",
                    "AUTHORED_REFERENCE_TOUCHED",
                    "CITATION_BROKEN",
                )
            )
            self.assertEqual(total_relations, 0)




class RedactionCompletenessTest(unittest.TestCase):
    def test_paths_with_spaces_redact_every_component(self) -> None:
        from recallweave.cli import _redact_local_paths

        cases = [
            "Source root does not exist: /Users/josh/Secret Vault/Payroll.md",
            "failed C:\\Users\\Josh Smith\\Secret Vault\\x.md",
            "state root /a/Secret Docs vs source /b/Hidden Notes. Choose another.",
        ]
        for message in cases:
            redacted = _redact_local_paths(message)
            for component in ("Secret", "Vault", "Payroll", "Smith", "Hidden"):
                self.assertNotIn(
                    component, redacted,
                    f"component {component!r} leaked in {redacted!r}",
                )


class AutoApplyDefaultBanTest(unittest.TestCase):
    def test_default_level_auto_apply_is_refused_at_load(self) -> None:
        from recallweave.steward_policy import WritePolicy

        with self.assertRaisesRegex(ValueError, "default_level may not be auto_apply"):
            WritePolicy.from_payload(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "default_level": "auto_apply",
                }
            )

    def test_resolution_clamps_auto_apply_for_destructive_classes(self) -> None:
        from recallweave.steward_policy import WritePolicy, resolve_level

        policy = WritePolicy()
        policy.default_level = "auto_apply"  # only reachable by direct mutation
        level, reason = resolve_level(
            policy,
            mutation_class="move_to_trash",
            source_name=None,
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("require_approval", "auto_apply_clamped"))


if __name__ == "__main__":
    unittest.main()
