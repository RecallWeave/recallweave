from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_apply import ApplyError, apply_proposal, revert_journal
from recallweave.steward_policy import WritePolicy
from recallweave.steward_sources import SOURCES_SPEC_VERSION, load_registry
from recallweave.steward_state import STEWARD_SCHEMA_VERSION, ensure_state_layout
from recallweave.steward_validate import (
    ValidationError,
    validate_l1,
    validate_l3,
)

from steward_fixtures import TempVault


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy(payload: dict | None = None) -> WritePolicy:
    document = {"spec_version": "recallweave.steward.policy.v1"}
    document.update(payload or {})
    return WritePolicy.from_bytes(json.dumps(document).encode("utf-8"))


class ValidationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("seed.md", "# Seed\n\nBody.\n")
        self.database = self.base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.registry_path = self.base / "sources.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "spec_version": SOURCES_SPEC_VERSION,
                    "sources": [
                        {
                            "name": "src",
                            "type": "folder",
                            "root": str(self.vault.root),
                            "mode": "appliable",
                            "policy": {
                                "include_paths": [
                                    "seed.md",
                                    "grow.md",
                                    "linky.md",
                                    "inbox/new.md",
                                ],
                                "deny_frontmatter": {
                                    "sensitivity": ["sealed"]
                                },
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = load_registry(self.registry_path)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _proposal(self, edits: list[dict], **overrides) -> dict:
        document = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": "prp-validationtest0",
            "source": "src",
            "action": "test",
            "policy_level": "propose_only",
            "edits": edits,
            "conflicts_with": [],
            "registry_sha256": self.registry.registry_sha256,
        }
        document.update(overrides)
        return document

    def _apply(self, proposal: dict, policy: WritePolicy) -> dict:
        return apply_proposal(
            proposal,
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode="per_item",
            execute=True,
        )

    def test_successful_apply_reports_validation_and_deltas(self) -> None:
        base = self.vault.write("grow.md", "# Grow\n\nBody.\n")
        data = base.read_bytes()
        appended = "\nPlain appended sentence.\n"
        receipt = self._apply(
            self._proposal(
                [
                    {
                        "mutation_class": "append_at_eof",
                        "relative_path": "grow.md",
                        "precondition_content_hash": _sha(data),
                        "replacement_text": appended,
                        "predicted_post_hash": _sha(data + appended.encode()),
                    }
                ]
            ),
            _policy({"class_levels": {"append_at_eof": "auto_apply"}}),
        )
        self.assertTrue(receipt["applied"])
        self.assertTrue(receipt["validation"]["passed"])
        self.assertEqual(
            receipt["validation"]["index_deltas"]["notes_indexed"], 0
        )

    def test_l0_denied_frontmatter_creation_rolls_back(self) -> None:
        content = "---\nsensitivity: sealed\n---\n# Smuggled\n"
        proposal = self._proposal(
            [
                {
                    "mutation_class": "create_new_file",
                    "relative_path": "inbox/new.md",
                    "replacement_text": content,
                    "predicted_post_hash": _sha(content.encode()),
                }
            ]
        )
        with self.assertRaisesRegex(ApplyError, "L0.*inadmissible"):
            self._apply(
                proposal,
                _policy({"class_levels": {"create_new_file": "auto_apply"}}),
            )
        self.assertFalse(
            (self.vault.root / "inbox/new.md").exists(),
            "the inadmissible creation survived rollback",
        )
        journal = json.loads(
            sorted(self.dirs["journal"].glob("*.json"))[-1].read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["status"], "rolled_back")

    def test_l1_unresolved_link_growth_rolls_back(self) -> None:
        base = self.vault.write("grow.md", "# Grow\n\nBody.\n")
        data = base.read_bytes()
        appended = "\nSee [[NoSuchNote]] someday.\n"
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "grow.md",
                    "precondition_content_hash": _sha(data),
                    "replacement_text": appended,
                    "predicted_post_hash": _sha(data + appended.encode()),
                }
            ]
        )
        with self.assertRaisesRegex(ApplyError, "L1.*unresolved_links"):
            self._apply(
                proposal,
                _policy({"class_levels": {"append_at_eof": "auto_apply"}}),
            )
        self.assertEqual(base.read_bytes(), data, "rollback did not restore")

    def test_l2_structure_shift_in_link_fix_rolls_back(self) -> None:
        target = self.vault.write("linky.md", "# Linky\n\nSee [[seed]] here.\n")
        data = target.read_bytes()
        # replacement smuggles a heading marker into the line; predicted hash
        # matches the smuggled bytes, so only the structure gate can catch it.
        bad_line = "See [[seed]] here.".replace("[[seed]]", "[[seed]]\n# Injected")
        post = data.decode().replace("See [[seed]] here.", bad_line).encode()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "fix_unresolved_link",
                    "relative_path": "linky.md",
                    "precondition_content_hash": _sha(data),
                    "anchor": {"line": 3, "old_text": "[[seed]]"},
                    "replacement_text": "[[seed]]\n# Injected",
                    "predicted_post_hash": _sha(post),
                }
            ],
            rename_preconditions={
                "removed_path": "OldSeed.md",
                "removed_absent": True,
                "added_path": "seed.md",
                "added_content_hash": _sha(
                    (self.vault.root / "seed.md").read_bytes()
                ),
            },
        )
        with self.assertRaisesRegex(ApplyError, "L2|L1|structure"):
            self._apply(
                proposal,
                _policy(
                    {"class_levels": {"fix_unresolved_link": "require_approval"}}
                ),
            )
        self.assertEqual(target.read_bytes(), data)

    def test_l3_rogue_write_is_detected(self) -> None:
        before = {"a.md": "1" * 64, "b.md": "2" * 64}
        after = {"a.md": "1" * 64, "b.md": "f" * 64}
        plans = [
            {"edit": {"relative_path": "a.md", "mutation_class": "append_at_eof"}}
        ]
        with self.assertRaisesRegex(ValidationError, "never named.*b.md"):
            validate_l3(before, after, plans)

    def test_l1_bounds_reject_unexplained_note_loss(self) -> None:
        receipt_before = {"notes_indexed": 5, "unresolved_links": 0, "verified_edges": 2, "skipped": {}}
        receipt_after = {"notes_indexed": 4, "unresolved_links": 0, "verified_edges": 2, "skipped": {}}
        plans = [
            {"edit": {"relative_path": "a.md", "mutation_class": "append_at_eof"}}
        ]
        with self.assertRaisesRegex(ValidationError, "notes_indexed"):
            validate_l1(receipt_before, receipt_after, plans)

    def test_revert_restores_an_applied_journal(self) -> None:
        base = self.vault.write("grow.md", "# Grow\n\nBody.\n")
        data = base.read_bytes()
        appended = "\nPlain appended sentence.\n"
        receipt = self._apply(
            self._proposal(
                [
                    {
                        "mutation_class": "append_at_eof",
                        "relative_path": "grow.md",
                        "precondition_content_hash": _sha(data),
                        "replacement_text": appended,
                        "predicted_post_hash": _sha(data + appended.encode()),
                    }
                ]
            ),
            _policy({"class_levels": {"append_at_eof": "auto_apply"}}),
        )
        self.assertTrue(receipt["applied"])
        self.assertNotEqual(base.read_bytes(), data)
        revert = revert_journal(
            receipt["journal_ref"], registry=self.registry, state_dirs=self.dirs
        )
        self.assertEqual(revert["operations_reverted"], 1)
        self.assertEqual(base.read_bytes(), data)
        journal = json.loads(
            (self.dirs["journal"] / receipt["journal_ref"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(journal["status"], "reverted")
        with self.assertRaisesRegex(ApplyError, "only an applied journal"):
            revert_journal(
                receipt["journal_ref"], registry=self.registry, state_dirs=self.dirs
            )


if __name__ == "__main__":
    unittest.main()
