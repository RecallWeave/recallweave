from __future__ import annotations

"""Cross-platform regressions for root-identity and rename portability.

The root-identity proof must not depend on inode NUMBERS not being reused:
on ext4 (GitHub's Ubuntu runners) a directory deleted and recreated at the
same path reuses its inode number, and a removed file's number is reused by
the next created file. These tests pin the portable behavior:

- the registry holds an open descriptor to each root, which pins the inode so
  a same-path recreate is detected as an identity change on every platform;
- rename candidacy is content-hash based and carries no inode_match signal.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_apply import ApplyError, apply_proposal
from recallweave.steward_observe import observe_registry, observe_source
from recallweave.steward_policy import WritePolicy
from recallweave.steward_sources import (
    SOURCES_SPEC_VERSION,
    SourceRegistry,
    StewardSource,
    load_registry,
)
from recallweave.steward_state import STEWARD_SCHEMA_VERSION, ensure_state_layout

from steward_fixtures import TempVault


def _write_registry(path: Path, root: Path, *, mode: str = "read_only", policy=None):
    source: dict = {"name": "src", "type": "folder", "root": str(root), "mode": mode}
    if policy is not None:
        source["policy"] = policy
    path.write_text(
        json.dumps({"spec_version": SOURCES_SPEC_VERSION, "sources": [source]}),
        encoding="utf-8",
    )


class HeldRootDescriptorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "vault"
        self.root.mkdir()
        (self.root / "a.md").write_text("hello", encoding="utf-8")
        self.registry_path = self.base / "sources.json"
        _write_registry(self.registry_path, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_registry_holds_a_root_descriptor(self) -> None:
        registry = load_registry(self.registry_path)
        try:
            self.assertIsNotNone(registry.sources[0].root_fd)
            self.assertIsNotNone(registry.sources[0].root_dev)
            self.assertIsNotNone(registry.sources[0].root_ino)
        finally:
            registry.close()
        self.assertIsNone(registry.sources[0].root_fd)

    def test_context_manager_releases_descriptor(self) -> None:
        with load_registry(self.registry_path) as registry:
            self.assertIsNotNone(registry.sources[0].root_fd)
        self.assertIsNone(registry.sources[0].root_fd)

    def test_same_path_recreate_is_detected_as_identity_change(self) -> None:
        # This is the ext4 inode-reuse scenario: rmtree + mkdir at the same
        # path. The held descriptor pins the original inode number so the
        # recreated directory cannot present the pinned identity, on any
        # platform.
        database = self.base / "index.sqlite"
        build_index(self.root, database, policy=IndexPolicy())
        with load_registry(self.registry_path) as registry:
            shutil.rmtree(self.root)
            self.root.mkdir()
            (self.root / "planted.md").write_text("planted", encoding="utf-8")
            state_root = self.base / "state"
            receipt = observe_registry(registry, state_root)
            self.assertEqual(
                receipt["sources"][0].get("error"), "source_identity_changed"
            )
            # No batch or checkpoint should have been written for the swap.
            dirs = ensure_state_layout(state_root)
            self.assertEqual(list(dirs["changes"].glob("*.json")), [])

    def test_apply_refuses_same_path_recreate(self) -> None:
        database = self.base / "index.sqlite"
        _write_registry(
            self.registry_path,
            self.root,
            mode="appliable",
            policy={"include_paths": ["a.md", "new.md"]},
        )
        build_index(self.root, database, policy=IndexPolicy())
        with load_registry(self.registry_path) as registry:
            shutil.rmtree(self.root)
            self.root.mkdir()
            policy = WritePolicy.from_bytes(
                json.dumps(
                    {
                        "spec_version": "recallweave.steward.policy.v1",
                        "class_levels": {"create_new_file": "auto_apply"},
                    }
                ).encode()
            )
            proposal = {
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "proposal",
                "proposal_id": "prp-recreate000000",
                "source": "src",
                "action": "test",
                "policy_level": "propose_only",
                "edits": [
                    {
                        "mutation_class": "create_new_file",
                        "relative_path": "new.md",
                        "replacement_text": "x",
                        "predicted_post_hash": __import__("hashlib").sha256(
                            b"x"
                        ).hexdigest(),
                    }
                ],
                "conflicts_with": [],
                "registry_sha256": registry.registry_sha256,
            }
            dirs = ensure_state_layout(self.base / "state")
            with self.assertRaisesRegex(ApplyError, "identity changed"):
                apply_proposal(
                    proposal,
                    registry=registry,
                    state_dirs=dirs,
                    database=database,
                    policy=policy,
                    mode="per_item",
                    execute=True,
                )
            self.assertFalse((self.root / "new.md").exists())


class RenamePortabilityTest(unittest.TestCase):
    """Rename candidacy is content-hash based and inode-free."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.source = StewardSource(
            name="src",
            type="folder",
            root=self.vault.root,
            mode="read_only",
            policy=IndexPolicy(),
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _observe(self):
        return observe_source(self.source, self.dirs, registry_sha256=None)

    def test_rename_candidate_carries_no_inode_signal(self) -> None:
        self.vault.write("old.md", "same content")
        self._observe()
        self.vault.remove("old.md")
        self.vault.write("new.md", "same content")
        receipt = self._observe()
        self.assertEqual(len(receipt["rename_candidates"]), 1)
        candidate = receipt["rename_candidates"][0]
        self.assertEqual(candidate["removed_path"], "old.md")
        self.assertEqual(candidate["added_paths"], ["new.md"])
        self.assertNotIn("inode_match", candidate)

    def test_delete_then_create_identical_is_still_a_content_rename(self) -> None:
        # Whether or not the platform reuses the removed file's inode number
        # for the new file, the candidate is content-hash based and unique.
        self.vault.write("old.md", "shared bytes")
        self._observe()
        self.vault.remove("old.md")
        self.vault.write("fresh.md", "shared bytes")
        receipt = self._observe()
        self.assertEqual(len(receipt["rename_candidates"]), 1)
        self.assertEqual(receipt["rename_candidates"][0]["added_paths"], ["fresh.md"])
        self.assertNotIn("inode_match", receipt["rename_candidates"][0])


class ProposeRenameWithoutInodeTest(unittest.TestCase):
    """A unique content-hash rename compiles an edit with no inode signal."""

    def test_unique_content_rename_compiles_without_inode_match(self) -> None:
        from recallweave.steward_propose import propose_from_assessment

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            vault = TempVault(dir=base)
            try:
                vault.write("Alpha.md", "# Alpha\n\nSee [[Beta]].\n")
                vault.write("Beta.md", "# Beta\n\nBody.\n")
                database = base / "index.sqlite"
                build_index(vault.root, database, policy=IndexPolicy())
                # Beta renamed to Gamma; batch carries a content-hash rename
                # candidate with NO inode_match field at all.
                batch = {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "change_batch",
                    "operation": "steward_observe",
                    "generated_at": "2026-01-01T00:00:00+00:00",
                    "source": "src",
                    "registry_sha256": None,
                    "changes": [
                        {
                            "relative_path": "Beta.md",
                            "change_type": "removed",
                            "previous_content_hash": __import__("hashlib").sha256(
                                b"# Beta\n\nBody.\n"
                            ).hexdigest(),
                            "current_content_hash": None,
                        },
                    ],
                    "rename_candidates": [
                        {
                            "removed_path": "Beta.md",
                            "added_paths": ["Gamma.md"],
                            "content_hash": __import__("hashlib").sha256(
                                b"# Beta\n\nBody.\n"
                            ).hexdigest(),
                        }
                    ],
                    "change_summary": {},
                    "skipped": {},
                    "changed_during_observe": [],
                    "network_calls": 0,
                    "vault_writes": 0,
                }
                assessment = {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "src",
                    "registry_sha256": None,
                    "change_batch_ref": "b.json",
                    "assessments": [
                        {
                            "relation": "DELETED",
                            "relative_path": "Beta.md",
                            "inputs": {},
                        }
                    ],
                }
                proposals = propose_from_assessment(
                    assessment, batch, database, vault.root,
                    now="2026-01-01T00:00:00+00:00",
                )
                actions = {p["action"] for p in proposals}
                self.assertIn("fix_links_after_rename", actions)
                rename = next(
                    p for p in proposals if p["action"] == "fix_links_after_rename"
                )
                self.assertTrue(rename["edits"])
                self.assertEqual(rename["edits"][0]["relative_path"], "Alpha.md")
            finally:
                vault.cleanup()


if __name__ == "__main__":
    unittest.main()
