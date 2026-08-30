from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave.cli import main as cli_main
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave import steward_apply
from recallweave.steward_apply import (
    ApplyError,
    RollbackError,
    apply_latest,
    apply_proposal,
    recover_journal,
)
from recallweave.steward_assess import assess_latest
from recallweave.steward_observe import observe_registry
from recallweave.steward_policy import WritePolicy
from recallweave.steward_propose import propose_latest
from recallweave.steward_sources import SOURCES_SPEC_VERSION, load_registry
from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
)

from steward_fixtures import TempVault


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy(payload: dict | None = None) -> WritePolicy:
    document = {"spec_version": "recallweave.steward.policy.v1"}
    document.update(payload or {})
    data = json.dumps(document).encode("utf-8")
    return WritePolicy.from_bytes(data)


class ApplyPipelineTest(unittest.TestCase):
    """End-to-end fixtures built through the real read-only pipeline."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("Alpha.md", "# Alpha\n\nSee [[Beta]] for detail.\n")
        self.vault.write("Beta.md", "# Beta\n\nBody.\n")
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
                                    "Alpha.md",
                                    "Beta.md",
                                    "Gamma.md",
                                    "Delta.md",
                                ]
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = load_registry(self.registry_path)
        self.state_root = self.base / "state"

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _pipeline_rename(self) -> dict:
        """Baseline sweep, rename Beta -> Gamma, run the pipeline, and return
        the compiled fix_links_after_rename proposal."""

        observe_registry(self.registry, self.state_root)
        self.vault.move("Beta.md", "Gamma.md")
        observe_registry(self.registry, self.state_root)
        assess_latest(self.registry, self.state_root, self.database)
        propose_latest(self.registry, self.state_root, self.database)
        dirs = ensure_state_layout(self.state_root)
        for path in sorted(dirs["proposals"].glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("action") == "fix_links_after_rename":
                return document
        raise AssertionError("no compiled rename proposal was produced")

    def test_dry_run_is_the_default_and_writes_nothing(self) -> None:
        proposal = self._pipeline_rename()
        before = (self.vault.root / "Alpha.md").read_bytes()
        receipt = apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=_policy({"class_levels": {"fix_unresolved_link": "require_approval"}}),
            proposal_id=proposal["proposal_id"],
        )
        self.assertTrue(receipt["dry_run"])
        self.assertEqual(receipt["steward_vault_mutations"], 0)
        self.assertEqual((self.vault.root / "Alpha.md").read_bytes(), before)
        dirs = ensure_state_layout(self.state_root)
        self.assertEqual(list(dirs["journal"].glob("*.json")), [])
        self.assertEqual(list(dirs["receipts"].glob("*.json")), [])

    def test_per_item_apply_end_to_end(self) -> None:
        proposal = self._pipeline_rename()
        receipt = apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=_policy({"class_levels": {"fix_unresolved_link": "require_approval"}}),
            proposal_id=proposal["proposal_id"],
            execute=True,
        )
        self.assertFalse(receipt["dry_run"])
        self.assertEqual(receipt["steward_vault_mutations"], 1)
        text = (self.vault.root / "Alpha.md").read_text(encoding="utf-8")
        self.assertIn("[[Gamma]]", text)
        self.assertNotIn("[[Beta]]", text)
        edit = proposal["edits"][0]
        self.assertEqual(
            _sha((self.vault.root / "Alpha.md").read_bytes()),
            edit["predicted_post_hash"],
        )
        dirs = ensure_state_layout(self.state_root)
        journals = list(dirs["journal"].glob("*.json"))
        self.assertEqual(len(journals), 1)
        journal = json.loads(journals[0].read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "applied")
        backups = list(dirs["backups"].rglob("*Alpha*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            _sha(backups[0].read_bytes()), edit["precondition_content_hash"]
        )
        self.assertEqual(len(list(dirs["receipts"].glob("*.json"))), 1)

    def test_double_apply_is_refused(self) -> None:
        proposal = self._pipeline_rename()
        policy = _policy({"class_levels": {"fix_unresolved_link": "require_approval"}})
        apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=policy,
            proposal_id=proposal["proposal_id"],
            execute=True,
        )
        with self.assertRaisesRegex(ApplyError, "No pending proposal"):
            apply_latest(
                self.registry,
                self.state_root,
                self.database,
                write_policy=policy,
                proposal_id=proposal["proposal_id"],
                execute=True,
            )

    def test_default_policy_refuses_the_apply(self) -> None:
        proposal = self._pipeline_rename()
        with self.assertRaisesRegex(ApplyError, "propose_only"):
            apply_latest(
                self.registry,
                self.state_root,
                self.database,
                write_policy=_policy(),
                proposal_id=proposal["proposal_id"],
                execute=True,
            )

    def test_precondition_drift_aborts_before_any_write(self) -> None:
        proposal = self._pipeline_rename()
        self.vault.write("Alpha.md", "# Alpha\n\nEdited since compile.\n")
        drifted = (self.vault.root / "Alpha.md").read_bytes()
        with self.assertRaisesRegex(ApplyError, "Precondition hash mismatch"):
            apply_latest(
                self.registry,
                self.state_root,
                self.database,
                write_policy=_policy(
                    {"class_levels": {"fix_unresolved_link": "require_approval"}}
                ),
                proposal_id=proposal["proposal_id"],
                execute=True,
            )
        self.assertEqual((self.vault.root / "Alpha.md").read_bytes(), drifted)
        dirs = ensure_state_layout(self.state_root)
        self.assertEqual(list(dirs["journal"].glob("*.json")), [])

    def test_crash_mid_batch_rolls_back_verified(self) -> None:
        # Two referrers so the proposal carries two edits; fail the second
        # replace and prove the first is restored byte-identically.
        self.vault.write("Delta.md", "# Delta\n\nAlso see [[Beta]].\n")
        build_index(self.vault.root, self.database, policy=IndexPolicy(), force=True)
        proposal = self._pipeline_rename()
        self.assertEqual(len(proposal["edits"]), 2)
        originals = {
            edit["relative_path"]: (self.vault.root / edit["relative_path"]).read_bytes()
            for edit in proposal["edits"]
        }
        real_rename = os.rename
        real_replace = os.replace
        calls = {"count": 0}

        def failing(src, dst, **kwargs):
            # The atomic install is os.rename (dir_fd path) or os.replace
            # (pathname fallback); the temp source carries the marker in both.
            if "steward-apply.tmp" in str(src):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected crash")
            fn = real_rename if kwargs else real_replace
            return fn(src, dst, **kwargs)

        with patch.object(steward_apply.os, "rename", side_effect=failing), \
                patch.object(steward_apply.os, "replace", side_effect=failing):
            with self.assertRaisesRegex(ApplyError, "injected crash|Post-write"):
                apply_latest(
                    self.registry,
                    self.state_root,
                    self.database,
                    write_policy=_policy(
                        {"class_levels": {"fix_unresolved_link": "require_approval"}}
                    ),
                    proposal_id=proposal["proposal_id"],
                    execute=True,
                )
        for relative_path, data in originals.items():
            self.assertEqual(
                (self.vault.root / relative_path).read_bytes(),
                data,
                f"{relative_path} was not restored",
            )
        dirs = ensure_state_layout(self.state_root)
        journal = json.loads(
            sorted(dirs["journal"].glob("*.json"))[-1].read_text(encoding="utf-8")
        )
        self.assertEqual(journal["status"], "rolled_back")
        self.assertEqual(journal["rollback_failures"], [])

    def test_failed_rollback_is_loud_and_retains_backups(self) -> None:
        proposal = self._pipeline_rename()
        real_rename = os.rename
        real_replace = os.replace
        state = {"applied": False}

        def install_then_fail_everything(src, dst, **kwargs):
            fn = real_rename if kwargs else real_replace
            if "steward-apply.tmp" in str(src):
                if not state["applied"]:
                    state["applied"] = True
                    fn(src, dst, **kwargs)
                    raise OSError("injected post-write crash")
                raise OSError("injected rollback failure")
            return fn(src, dst, **kwargs)

        with patch.object(
            steward_apply.os, "rename", side_effect=install_then_fail_everything
        ), patch.object(
            steward_apply.os, "replace", side_effect=install_then_fail_everything
        ):
            with self.assertRaisesRegex(RollbackError, "retained backups"):
                apply_latest(
                    self.registry,
                    self.state_root,
                    self.database,
                    write_policy=_policy(
                        {"class_levels": {"fix_unresolved_link": "require_approval"}}
                    ),
                    proposal_id=proposal["proposal_id"],
                    execute=True,
                )
        dirs = ensure_state_layout(self.state_root)
        journal = json.loads(
            sorted(dirs["journal"].glob("*.json"))[-1].read_text(encoding="utf-8")
        )
        self.assertEqual(journal["status"], "rollback_failed")
        self.assertTrue(journal["rollback_failures"])
        backup = dirs["backups"] / journal["backup_dir"] / journal["operations"][0]["backup_name"]
        self.assertTrue(backup.exists())

    def test_incomplete_journal_blocks_new_applies_and_recover_restores(self) -> None:
        proposal = self._pipeline_rename()
        edit = proposal["edits"][0]
        target = self.vault.root / edit["relative_path"]
        original = target.read_bytes()

        dirs = ensure_state_layout(self.state_root)
        backup_dir = dirs["backups"] / "crash-test"
        backup_dir.mkdir()
        backup_name = "0-" + edit["relative_path"].replace("/", "__")
        (backup_dir / backup_name).write_bytes(original)
        # Simulate a crash AFTER the (atomic) mutation landed: the target
        # holds exactly the journaled post-apply bytes.
        anchor = edit["anchor"]
        raw = original.decode("utf-8")
        mutated = raw.replace(anchor["old_text"], edit["replacement_text"], 1)
        target.write_bytes(mutated.encode("utf-8"))
        journal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "apply_journal",
            "proposal_id": proposal["proposal_id"],
            "source": "src",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": "intent",
            "backup_dir": "crash-test",
            "operations": [
                {
                    "relative_path": edit["relative_path"],
                    "mutation_class": edit["mutation_class"],
                    "content_hash_before": _sha(original),
                    "content_hash_after": _sha(mutated.encode("utf-8")),
                    "backup_name": backup_name,
                    "state": "done",
                }
            ],
            "rollback_failures": [],
            "registry_sha256": self.registry.registry_sha256,
        }
        journal_name = "20260101T000000000000Z-crash.json"
        atomic_write_json(
            dirs["journal"] / journal_name, journal, within=dirs["journal"]
        )

        policy = _policy({"class_levels": {"fix_unresolved_link": "require_approval"}})
        with self.assertRaisesRegex(ApplyError, "interrupted"):
            apply_latest(
                self.registry,
                self.state_root,
                self.database,
                write_policy=policy,
                proposal_id=proposal["proposal_id"],
                execute=True,
            )

        receipt = apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=policy,
            recover=journal_name,
            execute=True,
        )
        self.assertEqual(receipt["operations_rolled_back"], 1)
        self.assertEqual(target.read_bytes(), original)
        recovered = json.loads(
            (dirs["journal"] / journal_name).read_text(encoding="utf-8")
        )
        self.assertEqual(recovered["status"], "rolled_back")

    def test_cli_end_to_end_dry_run_then_execute(self) -> None:
        proposal = self._pipeline_rename()
        policy_path = self.base / "write-policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": {"fix_unresolved_link": "require_approval"},
                }
            ),
            encoding="utf-8",
        )

        def run(*extra: str) -> tuple[int, dict]:
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli_main(
                    [
                        "steward-apply",
                        str(self.registry_path),
                        "--database",
                        str(self.database),
                        "--state-dir",
                        str(self.state_root),
                        "--write-policy",
                        str(policy_path),
                        "--proposal-id",
                        proposal["proposal_id"],
                        *extra,
                    ]
                )
            payload = json.loads(out.getvalue()) if out.getvalue() else json.loads(err.getvalue())
            return code, payload

        code, payload = run()
        self.assertEqual(code, 0)
        self.assertTrue(payload["dry_run"])
        self.assertIn("[[Beta]]", (self.vault.root / "Alpha.md").read_text(encoding="utf-8"))

        code, payload = run("--execute")
        self.assertEqual(code, 0)
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["steward_vault_mutations"], 1)
        self.assertIn("[[Gamma]]", (self.vault.root / "Alpha.md").read_text(encoding="utf-8"))


class ApplyUnitTest(unittest.TestCase):
    """Hand-built proposals exercising the executors and refusals directly."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.database = self.base / "index.sqlite"
        self.vault.write("seed.md", "seed")
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
                                    "gone.md",
                                    "dup.md",
                                    "inbox/new.md",
                                    "bulk/0.md",
                                    "bulk/1.md",
                                    "bulk/2.md",
                                ]
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
            "proposal_id": "prp-testtesttesttest",
            "source": "src",
            "action": "test",
            "policy_level": "propose_only",
            "edits": edits,
            "conflicts_with": [],
            "registry_sha256": self.registry.registry_sha256,
        }
        document.update(overrides)
        return document

    def _apply(self, proposal: dict, policy: WritePolicy, *, mode="per_item", execute=True) -> dict:
        return apply_proposal(
            proposal,
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode=mode,
            execute=execute,
        )

    def test_create_new_file_and_append_at_eof(self) -> None:
        new_content = "# New\n\nMachine-attributed block.\n"
        base = self.vault.write("grow.md", "# Grow\n")
        base_bytes = base.read_bytes()
        appended = "\nAppended fenced block.\n"
        proposal = self._proposal(
            [
                {
                    "mutation_class": "create_new_file",
                    "relative_path": "inbox/new.md",
                    "replacement_text": new_content,
                    "predicted_post_hash": _sha(new_content.encode()),
                },
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "grow.md",
                    "precondition_content_hash": _sha(base_bytes),
                    "replacement_text": appended,
                    "predicted_post_hash": _sha(base_bytes + appended.encode()),
                },
            ]
        )
        policy = _policy(
            {
                "class_levels": {
                    "create_new_file": "auto_apply",
                    "append_at_eof": "auto_apply",
                }
            }
        )
        receipt = self._apply(proposal, policy)
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["steward_vault_mutations"], 2)
        self.assertEqual(
            (self.vault.root / "inbox/new.md").read_text(encoding="utf-8"),
            new_content,
        )
        self.assertEqual(
            (self.vault.root / "grow.md").read_bytes(), base_bytes + appended.encode()
        )

    def test_move_to_trash_copies_verified_then_removes(self) -> None:
        target = self.vault.write("gone.md", "bytes to preserve")
        data = target.read_bytes()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "move_to_trash",
                    "relative_path": "gone.md",
                    "precondition_content_hash": _sha(data),
                }
            ]
        )
        policy = _policy({"class_levels": {"move_to_trash": "require_approval"}})
        receipt = self._apply(proposal, policy)
        self.assertTrue(receipt["applied"])
        self.assertFalse(target.exists())
        trashed = list(self.dirs["trash"].rglob("*gone*"))
        self.assertEqual(len(trashed), 1)
        self.assertEqual(trashed[0].read_bytes(), data)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_trash_revert_restores_original_mode(self) -> None:
        target = self.vault.write("gone.md", "secret bytes")
        os.chmod(target, 0o600)
        data = target.read_bytes()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "move_to_trash",
                    "relative_path": "gone.md",
                    "precondition_content_hash": _sha(data),
                }
            ]
        )
        policy = _policy({"class_levels": {"move_to_trash": "require_approval"}})
        receipt = self._apply(proposal, policy)
        self.assertFalse(target.exists())
        apply_latest(
            self.registry, self.state_root, self.database,
            write_policy=policy, revert=receipt["journal_ref"], execute=True,
        )
        self.assertTrue(target.exists())
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_revert_refuses_when_deleted_path_is_recreated(self) -> None:
        target = self.vault.write("gone.md", "original")
        data = target.read_bytes()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "move_to_trash",
                    "relative_path": "gone.md",
                    "precondition_content_hash": _sha(data),
                }
            ]
        )
        policy = _policy({"class_levels": {"move_to_trash": "require_approval"}})
        receipt = self._apply(proposal, policy)
        # Another writer recreates the deleted path with unrelated content.
        self.vault.write("gone.md", "a different, unrelated file")
        with self.assertRaisesRegex(ApplyError, "Refusing to revert"):
            apply_latest(
                self.registry, self.state_root, self.database,
                write_policy=policy, revert=receipt["journal_ref"], execute=True,
            )
        self.assertEqual(
            (self.vault.root / "gone.md").read_text(encoding="utf-8"),
            "a different, unrelated file",
            "revert clobbered the unrelated recreated file",
        )

    def test_revert_of_create_removes_created_directory(self) -> None:
        content = "# New\n\nfresh note.\n"
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
        policy = _policy({"class_levels": {"create_new_file": "require_approval"}})
        receipt = self._apply(proposal, policy)
        self.assertTrue((self.vault.root / "inbox" / "new.md").exists())
        apply_latest(
            self.registry, self.state_root, self.database,
            write_policy=policy, revert=receipt["journal_ref"], execute=True,
        )
        self.assertFalse((self.vault.root / "inbox" / "new.md").exists())
        self.assertFalse(
            (self.vault.root / "inbox").exists(),
            "revert left the empty directory the create made",
        )

    def test_recover_and_revert_do_not_require_write_policy(self) -> None:
        # write_policy=None must not raise the policy-required error on a
        # restore path (recovery must work when the policy file is gone).
        with self.assertRaisesRegex(ApplyError, "No such journal"):
            apply_latest(
                self.registry, self.state_root, self.database,
                write_policy=None, revert="20260101T000000000000Z-none.json",
                execute=True,
            )
        # But proposal execution without a policy is refused.
        with self.assertRaisesRegex(ApplyError, "write policy is required"):
            apply_latest(
                self.registry, self.state_root, self.database,
                write_policy=None, proposal_id="prp-whatever0000000", execute=True,
            )

    def test_replace_whole_section_is_not_executable_yet(self) -> None:
        proposal = self._proposal(
            [
                {
                    "mutation_class": "replace_whole_section",
                    "relative_path": "seed.md",
                    "precondition_content_hash": "0" * 64,
                }
            ]
        )
        with self.assertRaisesRegex(ApplyError, "not executable"):
            self._apply(proposal, _policy(), execute=False)

    def test_predicted_hash_mismatch_refuses_unapproved_bytes(self) -> None:
        base = self.vault.write("grow.md", "# Grow\n")
        base_bytes = base.read_bytes()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "grow.md",
                    "precondition_content_hash": _sha(base_bytes),
                    "replacement_text": "\nX\n",
                    "predicted_post_hash": "f" * 64,
                }
            ]
        )
        policy = _policy({"class_levels": {"append_at_eof": "auto_apply"}})
        with self.assertRaisesRegex(ApplyError, "predicted hash"):
            self._apply(proposal, policy)
        self.assertEqual(base.read_bytes(), base_bytes)

    def test_conflicting_proposal_is_refused(self) -> None:
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "seed.md",
                    "precondition_content_hash": "0" * 64,
                    "replacement_text": "x",
                    "predicted_post_hash": "1" * 64,
                }
            ],
            conflicts_with=["prp-otherotherother1"],
        )
        with self.assertRaisesRegex(ApplyError, "conflicts with"):
            self._apply(proposal, _policy(), execute=False)

    def test_advisory_proposal_is_refused(self) -> None:
        proposal = self._proposal([])
        with self.assertRaisesRegex(ApplyError, "no executable edits"):
            self._apply(proposal, _policy(), execute=False)

    def test_registry_mismatch_is_refused(self) -> None:
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "seed.md",
                    "precondition_content_hash": "0" * 64,
                    "replacement_text": "x",
                    "predicted_post_hash": "1" * 64,
                }
            ],
            registry_sha256="differentdigest",
        )
        with self.assertRaisesRegex(ApplyError, "registry_sha256 mismatch"):
            self._apply(proposal, _policy(), execute=False)

    def test_reserved_dir_and_database_targets_are_refused(self) -> None:
        for hostile_path in (".obsidian/app.md", "../outside.md"):
            proposal = self._proposal(
                [
                    {
                        "mutation_class": "create_new_file",
                        "relative_path": hostile_path,
                        "replacement_text": "x",
                        "predicted_post_hash": _sha(b"x"),
                    }
                ]
            )
            with self.assertRaises(ApplyError):
                self._apply(proposal, _policy(), execute=False)

    def test_max_files_per_apply_cap(self) -> None:
        edits = [
            {
                "mutation_class": "create_new_file",
                "relative_path": f"bulk/{index}.md",
                "replacement_text": "x",
                "predicted_post_hash": _sha(b"x"),
            }
            for index in range(3)
        ]
        proposal = self._proposal(edits)
        policy = _policy({"max_files_per_apply": 2})
        with self.assertRaisesRegex(ApplyError, "caps an apply"):
            self._apply(proposal, policy, execute=False)

    def test_sync_root_refusal_and_override(self) -> None:
        marker = self.vault.root / ".stfolder"
        marker.mkdir()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "create_new_file",
                    "relative_path": "inbox/new.md",
                    "replacement_text": "x",
                    "predicted_post_hash": _sha(b"x"),
                }
            ]
        )
        policy = _policy({"class_levels": {"create_new_file": "auto_apply"}})
        with self.assertRaisesRegex(ApplyError, "sync service"):
            self._apply(proposal, policy, execute=False)
        receipt = apply_proposal(
            proposal,
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode="per_item",
            execute=False,
            allow_sync_root=True,
        )
        self.assertTrue(receipt["dry_run"])


    def test_protected_frontmatter_is_enforced_at_apply_time(self) -> None:
        target = self.vault.write(
            "grow.md", "---\nsensitivity: sealed\n---\n# Grow\n"
        )
        data = target.read_bytes()
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "grow.md",
                    "precondition_content_hash": _sha(data),
                    "replacement_text": "\nX\n",
                    "predicted_post_hash": _sha(data + b"\nX\n"),
                }
            ]
        )
        policy = _policy(
            {
                "class_levels": {"append_at_eof": "auto_apply"},
                "protected": {"frontmatter": {"sensitivity": ["sealed"]}},
            }
        )
        with self.assertRaisesRegex(ApplyError, "disabled"):
            self._apply(proposal, policy, execute=False)
        self.assertEqual(target.read_bytes(), data)

    def test_anchor_ambiguity_is_refused(self) -> None:
        target = self.vault.write("dup.md", "See [[Beta]] and [[Beta]].\n")
        data = target.read_bytes()
        added = self.vault.write("Gamma.md", "# Gamma\n")
        proposal = self._proposal(
            [
                {
                    "mutation_class": "fix_unresolved_link",
                    "relative_path": "dup.md",
                    "precondition_content_hash": _sha(data),
                    "anchor": {"line": 1, "old_text": "[[Beta]]"},
                    "replacement_text": "[[Gamma]]",
                    "predicted_post_hash": "0" * 64,
                }
            ],
            rename_preconditions={
                "removed_path": "Beta.md",
                "removed_absent": True,
                "added_path": "Gamma.md",
                "added_content_hash": _sha(added.read_bytes()),
            },
        )
        policy = _policy({"class_levels": {"fix_unresolved_link": "require_approval"}})
        with self.assertRaisesRegex(ApplyError, "exactly once"):
            self._apply(proposal, policy, execute=False)


if __name__ == "__main__":
    unittest.main()
