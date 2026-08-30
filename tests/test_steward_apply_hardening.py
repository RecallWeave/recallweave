from __future__ import annotations

"""Adversarial regressions from the G2 independent review: forged recovery
journals, mutation-boundary symlink swaps, git filename quoting, and the
doc/CLI contract."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_apply import (
    ApplyError,
    RollbackError,
    _guarded_replace,
    _recheck_parent_chain,
    recover_journal,
    revert_journal,
)
from recallweave.steward_git import check_apply_preconditions, git_available
from recallweave.steward_sources import SOURCES_SPEC_VERSION, load_registry
from recallweave.steward_state import (
    STEWARD_SCHEMA_VERSION,
    atomic_write_json,
    ensure_state_layout,
)

from steward_fixtures import TempVault, make_symlink

ROOT = Path(__file__).resolve().parents[1]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ForgedJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
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
                            "policy": {"include_paths": ["a.md"]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = load_registry(self.registry_path)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.victim = self.base / "victim.txt"
        self.victim.write_text("precious unrelated bytes", encoding="utf-8")

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _write_journal(self, status: str, operations: list[dict], **extra) -> str:
        name = "20260101T000000000000Z-forged.json"
        journal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "apply_journal",
            "proposal_id": "prp-forgedforgedfor",
            "source": "src",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": status,
            "backup_dir": "forged-backups",
            "operations": operations,
            "rollback_failures": [],
        }
        journal.update(extra)
        atomic_write_json(
            self.dirs["journal"] / name, journal, within=self.dirs["journal"]
        )
        return name

    def _forged_op(self, **overrides) -> dict:
        op = {
            "relative_path": "../../victim.txt",
            "mutation_class": "create_new_file",
            "content_hash_before": None,
            "content_hash_after": _sha(b"precious unrelated bytes"),
            "backup_name": "0-x",
            "state": "done",
        }
        op.update(overrides)
        return op

    def test_traversal_relative_path_in_recovery_is_refused(self) -> None:
        name = self._write_journal("intent", [self._forged_op()])
        with self.assertRaisesRegex(ApplyError, "Invalid relative path"):
            recover_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertTrue(self.victim.exists())
        self.assertEqual(
            self.victim.read_text(encoding="utf-8"), "precious unrelated bytes"
        )

    def test_traversal_backup_dir_in_recovery_is_refused(self) -> None:
        name = self._write_journal(
            "intent",
            [self._forged_op(relative_path="a.md")],
            backup_dir="../../../somewhere",
        )
        with self.assertRaisesRegex(ApplyError, "invalid backup_dir"):
            recover_journal(name, registry=self.registry, state_dirs=self.dirs)

    def test_traversal_backup_name_in_recovery_is_refused(self) -> None:
        name = self._write_journal(
            "intent",
            [self._forged_op(relative_path="a.md", backup_name="../../escape")],
        )
        with self.assertRaisesRegex(ApplyError, "invalid backup name"):
            recover_journal(name, registry=self.registry, state_dirs=self.dirs)

    def test_forged_create_cannot_delete_unrelated_bytes(self) -> None:
        # In-source path, but the target holds bytes that do NOT match the
        # journal's planned post-state: recovery must leave them alone.
        target = self.vault.root / "a.md"
        name = self._write_journal(
            "intent",
            [
                self._forged_op(
                    relative_path="a.md",
                    state="in_progress",
                    content_hash_after="f" * 64,
                )
            ],
        )
        before = target.read_bytes()
        recover_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertEqual(target.read_bytes(), before)


    def test_recovery_refuses_when_crashed_target_was_edited(self) -> None:
        # A done op whose live bytes match neither the pre- nor post-apply
        # hash means someone edited the file after the crash: recovery must
        # refuse and leave the newer bytes alone.
        target = self.vault.root / "a.md"
        target.write_bytes(b"operator wrote this after the crash")
        newer = target.read_bytes()
        backup_dir = self.dirs["backups"] / "forged-backups"
        backup_dir.mkdir()
        (backup_dir / "0-a.md").write_bytes(b"pre-apply bytes")
        name = self._write_journal(
            "intent",
            [
                self._forged_op(
                    relative_path="a.md",
                    state="done",
                    content_hash_before=_sha(b"pre-apply bytes"),
                    content_hash_after=_sha(b"post-apply bytes"),
                    backup_name="0-a.md",
                )
            ],
        )
        with self.assertRaisesRegex(ApplyError, "newer work"):
            recover_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertEqual(target.read_bytes(), newer)

    def test_revert_validates_journal_paths_too(self) -> None:
        name = self._write_journal("applied", [self._forged_op()])
        with self.assertRaisesRegex(ApplyError, "Invalid relative path"):
            revert_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertTrue(self.victim.exists())

    def test_malformed_operation_state_is_refused(self) -> None:
        name = self._write_journal(
            "intent", [self._forged_op(relative_path="a.md", state="bogus")]
        )
        with self.assertRaisesRegex(ApplyError, "invalid state"):
            recover_journal(name, registry=self.registry, state_dirs=self.dirs)


class MutationBoundaryTest(unittest.TestCase):
    def test_guarded_replace_refuses_swapped_parent(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            boundary = base / "source"
            (boundary / "sub").mkdir(parents=True)
            target = boundary / "sub" / "note.md"
            target.write_text("original", encoding="utf-8")
            elsewhere = base / "elsewhere"
            elsewhere.mkdir()
            # Swap the checked directory for a symlink after "preflight".
            import os
            import shutil

            shutil.rmtree(boundary / "sub")
            if not make_symlink(elsewhere, boundary / "sub"):
                self.skipTest("symlinks unsupported")
            with self.assertRaisesRegex(ApplyError, "symlink"):
                _guarded_replace(target, b"attacker", boundary=boundary)
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_recheck_refuses_target_outside_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            boundary = base / "source"
            boundary.mkdir()
            with self.assertRaisesRegex(ApplyError, "escaped its boundary"):
                _recheck_parent_chain(base / "outside.md", boundary)


@unittest.skipUnless(git_available(), "git is not installed")
class GitQuotedFilenameTest(unittest.TestCase):
    def test_dirty_target_with_hostile_name_is_still_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )
            hostile = 'we"ird -> nöte.md'
            (root / hostile).write_text("committed", encoding="utf-8")
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-q",
                    "-m",
                    "seed",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / hostile).write_text("dirty now", encoding="utf-8")
            from recallweave.steward_git import GitError

            with self.assertRaises(GitError):
                check_apply_preconditions(root, [hostile], require_git=False)


class CliDocContractTest(unittest.TestCase):
    def test_every_steward_cli_command_is_documented(self) -> None:
        from recallweave.cli import _parser

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        parser = _parser()
        subparsers = next(
            action
            for action in parser._actions
            if hasattr(action, "choices") and action.choices
        )
        for command in subparsers.choices:
            if command.startswith("steward-"):
                self.assertIn(
                    f"`{command}`", readme,
                    f"{command} is exposed but undocumented in the README",
                )

    def test_no_doc_claims_steward_is_entirely_read_only(self) -> None:
        for rel in ("README.md", "ARCHITECTURE.md", "docs/steward.md"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn(
                "entirely read-only", text,
                f"{rel} still claims the write-capable steward is read-only",
            )




class Round2RegressionTest(unittest.TestCase):
    def test_redaction_swallows_colon_components(self) -> None:
        from recallweave.cli import _redact_local_paths

        redacted = _redact_local_paths(
            "unreadable /vault/customer:alpha/secret.md while observing"
        )
        self.assertNotIn("alpha", redacted)
        self.assertNotIn("secret", redacted)

    @unittest.skipUnless(git_available(), "git is not installed")
    def test_commit_excludes_previously_staged_unrelated_content(self) -> None:
        from recallweave.steward_git import commit_applied

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q")
            (root / "touched.md").write_text("v1", encoding="utf-8")
            git("add", "-A")
            git("commit", "-q", "-m", "seed")
            # Unrelated file staged BEFORE steward runs.
            (root / "unrelated-secret.md").write_text("sensitive", encoding="utf-8")
            git("add", "unrelated-secret.md")
            (root / "touched.md").write_text("v2", encoding="utf-8")
            result = commit_applied(
                root,
                ["touched.md"],
                proposal_id="prp-test",
                journal_ref="j.json",
            )
            shown = subprocess.run(
                ["git", "show", "--name-only", "--format=", result["commit"]],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("touched.md", shown)
            self.assertNotIn("unrelated-secret.md", shown)


class RevertDriftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("grow.md", "# Grow\n")
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
                            "policy": {"include_paths": ["grow.md"]},
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

    def test_revert_refuses_after_post_apply_edit(self) -> None:
        from recallweave.steward_apply import apply_proposal
        from recallweave.steward_policy import WritePolicy

        base = self.vault.root / "grow.md"
        data = base.read_bytes()
        appended = "\nAppended.\n"
        policy = WritePolicy.from_bytes(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": {"append_at_eof": "auto_apply"},
                }
            ).encode()
        )
        receipt = apply_proposal(
            {
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "proposal",
                "proposal_id": "prp-driftdriftdrift",
                "source": "src",
                "action": "test",
                "policy_level": "propose_only",
                "edits": [
                    {
                        "mutation_class": "append_at_eof",
                        "relative_path": "grow.md",
                        "precondition_content_hash": _sha(data),
                        "replacement_text": appended,
                        "predicted_post_hash": _sha(data + appended.encode()),
                    }
                ],
                "conflicts_with": [],
                "registry_sha256": self.registry.registry_sha256,
            },
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode="per_item",
            execute=True,
        )
        # Operator edits the note after the apply.
        base.write_bytes(base.read_bytes() + b"\nnewer operator work\n")
        newer = base.read_bytes()
        with self.assertRaisesRegex(ApplyError, "newer work"):
            revert_journal(
                receipt["journal_ref"], registry=self.registry, state_dirs=self.dirs
            )
        self.assertEqual(base.read_bytes(), newer)




class RootIdentityAtApplyTest(unittest.TestCase):
    def test_apply_refuses_swapped_source_root(self) -> None:
        import shutil

        from recallweave.steward_apply import apply_proposal
        from recallweave.steward_policy import WritePolicy

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "vault"
            root.mkdir()
            (root / "a.md").write_text("hello", encoding="utf-8")
            database = base / "index.sqlite"
            build_index(root, database, policy=IndexPolicy())
            registry_path = base / "sources.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "spec_version": SOURCES_SPEC_VERSION,
                        "sources": [
                            {
                                "name": "src",
                                "type": "folder",
                                "root": str(root),
                                "mode": "appliable",
                                "policy": {"include_paths": ["a.md", "new.md"]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(registry_path)
            state_root = base / "state"
            dirs = ensure_state_layout(state_root)

            # Swap the admitted root for a different directory tree.
            shutil.rmtree(root)
            root.mkdir()
            (root / "planted.md").write_text("planted", encoding="utf-8")

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
                "proposal_id": "prp-rootswaprootswa",
                "source": "src",
                "action": "test",
                "policy_level": "propose_only",
                "edits": [
                    {
                        "mutation_class": "create_new_file",
                        "relative_path": "new.md",
                        "replacement_text": "x",
                        "predicted_post_hash": _sha(b"x"),
                    }
                ],
                "conflicts_with": [],
                "registry_sha256": registry.registry_sha256,
            }
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
            self.assertFalse((root / "new.md").exists())

    def test_recover_and_revert_refuse_swapped_root(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "vault"
            root.mkdir()
            (root / "a.md").write_text("hello", encoding="utf-8")
            database = base / "index.sqlite"
            build_index(root, database, policy=IndexPolicy())
            registry_path = base / "sources.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "spec_version": SOURCES_SPEC_VERSION,
                        "sources": [
                            {
                                "name": "src",
                                "type": "folder",
                                "root": str(root),
                                "mode": "appliable",
                                "policy": {"include_paths": ["a.md"]},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(registry_path)
            state_root = base / "state"
            dirs = ensure_state_layout(state_root)
            journal = {
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "apply_journal",
                "proposal_id": "prp-swappedswapped",
                "source": "src",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "status": "intent",
                "backup_dir": "b",
                "operations": [],
                "rollback_failures": [],
            }
            journal_name = "20260101T000000000000Z-s.json"
            atomic_write_json(
                dirs["journal"] / journal_name, journal, within=dirs["journal"]
            )
            shutil.rmtree(root)
            root.mkdir()
            with self.assertRaisesRegex(ApplyError, "identity changed"):
                recover_journal(
                    journal_name, registry=registry, state_dirs=dirs
                )
            journal["status"] = "applied"
            atomic_write_json(
                dirs["journal"] / journal_name, journal, within=dirs["journal"]
            )
            with self.assertRaisesRegex(ApplyError, "identity changed"):
                revert_journal(
                    journal_name, registry=registry, state_dirs=dirs
                )


class TwoPassRecoveryTest(unittest.TestCase):
    def test_no_mutation_happens_when_any_candidate_drifted(self) -> None:
        from recallweave.steward_apply import recover_journal as _recover

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / "vault"
            root.mkdir()
            clean_create = root / "clean.md"
            clean_create.write_text("planned bytes", encoding="utf-8")
            edited = root / "edited.md"
            edited.write_text("newer operator work", encoding="utf-8")
            database = base / "index.sqlite"
            build_index(root, database, policy=IndexPolicy())
            registry_path = base / "sources.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "spec_version": SOURCES_SPEC_VERSION,
                        "sources": [
                            {
                                "name": "src",
                                "type": "folder",
                                "root": str(root),
                                "mode": "appliable",
                                "policy": {
                                    "include_paths": ["clean.md", "edited.md"]
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(registry_path)
            state_root = base / "state"
            dirs = ensure_state_layout(state_root)
            backup_dir = dirs["backups"] / "b"
            backup_dir.mkdir()
            (backup_dir / "1-edited.md").write_bytes(b"pre-apply")
            journal = {
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "apply_journal",
                "proposal_id": "prp-twopasstwopass",
                "source": "src",
                "generated_at": "2026-01-01T00:00:00+00:00",
                "status": "intent",
                "backup_dir": "b",
                "operations": [
                    {
                        "relative_path": "clean.md",
                        "mutation_class": "create_new_file",
                        "content_hash_before": None,
                        "content_hash_after": _sha(b"planned bytes"),
                        "backup_name": "0-clean.md",
                        "state": "done",
                    },
                    {
                        "relative_path": "edited.md",
                        "mutation_class": "append_at_eof",
                        "content_hash_before": _sha(b"pre-apply"),
                        "content_hash_after": _sha(b"post-apply"),
                        "backup_name": "1-edited.md",
                        "state": "done",
                    },
                ],
                "rollback_failures": [],
            }
            journal_name = "20260101T000000000000Z-t.json"
            atomic_write_json(
                dirs["journal"] / journal_name, journal, within=dirs["journal"]
            )
            with self.assertRaisesRegex(ApplyError, "newer work"):
                _recover(journal_name, registry=registry, state_dirs=dirs)
            self.assertTrue(
                clean_create.exists(),
                "recovery deleted a create before finishing its drift screen",
            )
            self.assertEqual(
                edited.read_text(encoding="utf-8"), "newer operator work"
            )




class Round2G3RegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("kept.md", "kept")
        self.vault.write("Restricted/hidden.md", "outside the allowlist")
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
                            "policy": {"include_paths": ["kept.md"]},
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

    def _proposal(self, edits, registry_sha=True):
        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": "prp-g3round2round2",
            "source": "src",
            "action": "test",
            "policy_level": "propose_only",
            "edits": edits,
            "conflicts_with": [],
            "registry_sha256": (
                self.registry.registry_sha256 if registry_sha else None
            ),
        }

    def _apply(self, proposal, class_levels):
        from recallweave.steward_apply import apply_proposal
        from recallweave.steward_policy import WritePolicy

        policy = WritePolicy.from_bytes(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": class_levels,
                }
            ).encode()
        )
        return apply_proposal(
            proposal,
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode="per_item",
            execute=True,
        )

    def test_trash_of_unadmitted_file_is_refused(self) -> None:
        hidden = self.vault.root / "Restricted/hidden.md"
        proposal = self._proposal(
            [
                {
                    "mutation_class": "move_to_trash",
                    "relative_path": "Restricted/hidden.md",
                    "precondition_content_hash": _sha(hidden.read_bytes()),
                }
            ]
        )
        with self.assertRaisesRegex(ApplyError, "not admitted"):
            self._apply(proposal, {"move_to_trash": "require_approval"})
        self.assertTrue(hidden.exists())
        self.assertEqual(list(self.dirs["journal"].glob("*.json")), [])

    def test_null_proposal_digest_fails_closed(self) -> None:
        kept = self.vault.root / "kept.md"
        proposal = self._proposal(
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "kept.md",
                    "precondition_content_hash": _sha(kept.read_bytes()),
                    "replacement_text": "\nx\n",
                    "predicted_post_hash": _sha(kept.read_bytes() + b"\nx\n"),
                }
            ],
            registry_sha=False,
        )
        with self.assertRaisesRegex(ApplyError, "registry"):
            self._apply(proposal, {"append_at_eof": "auto_apply"})

    def test_interrupted_trash_is_restored_and_counted(self) -> None:
        kept = self.vault.root / "kept.md"
        original = kept.read_bytes()
        backup_dir = self.dirs["backups"] / "b"
        backup_dir.mkdir()
        (backup_dir / "0-kept.md").write_bytes(original)
        kept.unlink()  # simulate crash right after the trash unlink
        journal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "apply_journal",
            "proposal_id": "prp-trashcrashtras",
            "source": "src",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": "intent",
            "backup_dir": "b",
            "operations": [
                {
                    "relative_path": "kept.md",
                    "mutation_class": "move_to_trash",
                    "content_hash_before": _sha(original),
                    "content_hash_after": None,
                    "backup_name": "0-kept.md",
                    "state": "in_progress",
                }
            ],
            "rollback_failures": [],
        }
        journal_name = "20260101T000000000000Z-tc.json"
        atomic_write_json(
            self.dirs["journal"] / journal_name,
            journal,
            within=self.dirs["journal"],
        )
        receipt = recover_journal(
            journal_name, registry=self.registry, state_dirs=self.dirs
        )
        self.assertEqual(kept.read_bytes(), original)
        self.assertEqual(receipt["operations_rolled_back"], 1)
        self.assertGreaterEqual(receipt["vault_writes"], 1)

    def test_every_destructive_unlink_routes_through_the_guarded_primitive(self) -> None:
        import ast

        source = (ROOT / "src" / "recallweave" / "steward_apply.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        # Every .unlink() call anywhere in the module must live inside either
        # _guarded_unlink (the dir_fd primitive + its documented fallback) or
        # _guarded_replace (temp-file cleanup on its own O_EXCL-created temp,
        # never a target). Any other unlink is an unguarded destructive path.
        allowed = {"_guarded_unlink", "_guarded_replace"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in allowed:
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "unlink"
                ):
                    offenders.append(f"{node.name}:{inner.lineno}")
        self.assertEqual(
            offenders, [],
            f"unguarded unlink() calls outside the guarded primitives: {offenders}",
        )




class Round3G3RegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("kept.md", "kept")
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
                                "include_paths": ["kept.md", "nested/deep/new.md"]
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

    def _apply(self, proposal, class_levels):
        from recallweave.steward_apply import apply_proposal
        from recallweave.steward_policy import WritePolicy

        policy = WritePolicy.from_bytes(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": class_levels,
                }
            ).encode()
        )
        return apply_proposal(
            proposal,
            registry=self.registry,
            state_dirs=self.dirs,
            database=self.database,
            policy=policy,
            mode="per_item",
            execute=True,
        )

    def _proposal(self, proposal_id, edits):
        return {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": proposal_id,
            "source": "src",
            "action": "test",
            "policy_level": "propose_only",
            "edits": edits,
            "conflicts_with": [],
            "registry_sha256": self.registry.registry_sha256,
        }

    def test_traversal_proposal_id_is_refused_before_any_write(self) -> None:
        kept = self.vault.root / "kept.md"
        proposal = self._proposal(
            "../../../../" + self.vault.root.name + "/pwned",
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "kept.md",
                    "precondition_content_hash": _sha(kept.read_bytes()),
                    "replacement_text": "\nx\n",
                    "predicted_post_hash": _sha(kept.read_bytes() + b"\nx\n"),
                }
            ],
        )
        with self.assertRaisesRegex(ApplyError, "valid identifier"):
            self._apply(proposal, {"append_at_eof": "auto_apply"})
        self.assertFalse((self.vault.root / "pwned.json").exists())

    def test_guard_within_rejects_traversal_to_missing_parent(self) -> None:
        from recallweave.steward_state import guard_within

        with self.assertRaisesRegex(ValueError, "traversal|outside"):
            guard_within(
                self.state_root / "journal" / ".." / ".." / "escape.json",
                self.dirs["journal"],
            )

    def test_preexisting_temp_file_is_not_destroyed(self) -> None:
        kept = self.vault.root / "kept.md"
        temp = self.vault.root / ".kept.md.steward-apply.tmp"
        temp.write_text("someone else's data", encoding="utf-8")
        proposal = self._proposal(
            "prp-tempcollision0",
            [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "kept.md",
                    "precondition_content_hash": _sha(kept.read_bytes()),
                    "replacement_text": "\nx\n",
                    "predicted_post_hash": _sha(kept.read_bytes() + b"\nx\n"),
                }
            ],
        )
        with self.assertRaisesRegex(ApplyError, "temporary path"):
            self._apply(proposal, {"append_at_eof": "auto_apply"})
        self.assertEqual(temp.read_text(encoding="utf-8"), "someone else's data")

    def test_rollback_removes_created_directories(self) -> None:
        # A create into a new nested dir plus a second edit that fails
        # validation: the whole proposal rolls back, including the new dirs.
        kept = self.vault.root / "kept.md"
        proposal = self._proposal(
            "prp-nesteddirroll0",
            [
                {
                    "mutation_class": "create_new_file",
                    "relative_path": "nested/deep/new.md",
                    "replacement_text": "# New\n",
                    "predicted_post_hash": _sha(b"# New\n"),
                },
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "kept.md",
                    "precondition_content_hash": _sha(kept.read_bytes()),
                    "replacement_text": "\nSee [[NoSuchNote]].\n",
                    "predicted_post_hash": _sha(
                        kept.read_bytes() + b"\nSee [[NoSuchNote]].\n"
                    ),
                },
            ],
        )
        with self.assertRaises(ApplyError):
            self._apply(
                proposal,
                {
                    "create_new_file": "auto_apply",
                    "append_at_eof": "auto_apply",
                },
            )
        self.assertFalse((self.vault.root / "nested").exists(),
                         "rollback left a created directory behind")




class WriteRaceHardeningTest(unittest.TestCase):
    """H1: a parent-directory swap in the validate->syscall window must not
    redirect the write (the dir_fd anchoring holds)."""

    def setUp(self) -> None:
        from recallweave import steward_apply

        if not steward_apply._DIR_FD_WRITES:
            self.skipTest("platform lacks dir_fd primitives (documented fallback)")
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        (self.vault.root / "sub").mkdir()
        self.vault.write("sub/note.md", "original")
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
                            "policy": {"include_paths": ["sub/note.md"]},
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

    def test_parent_swap_after_chain_open_does_not_escape(self) -> None:
        import shutil

        from recallweave import steward_apply

        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        real_chain = steward_apply._open_parent_chain
        state = {"swapped": False}

        def swapping_chain(boundary, target, *, create_dirs, root_identity=None):
            result = real_chain(
                boundary, target, create_dirs=create_dirs,
                root_identity=root_identity,
            )
            # After the parent fd is open, swap the on-disk `sub` directory for
            # a symlink to an outside tree, then let the write proceed relative
            # to the already-open fd.
            if not state["swapped"] and str(target).endswith("note.md"):
                state["swapped"] = True
                sub = self.vault.root / "sub"
                moved = self.vault.root / "sub__moved"
                sub.rename(moved)
                try:
                    sub.symlink_to(elsewhere)
                except OSError:
                    moved.rename(sub)
                    self.skipTest("symlinks unsupported")
            return result

        note = self.vault.root / "sub" / "note.md"
        original = note.resolve()
        data = original.read_bytes()
        proposal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": "prp-parentswaprace0",
            "source": "src",
            "action": "test",
            "policy_level": "propose_only",
            "edits": [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "sub/note.md",
                    "precondition_content_hash": _sha(data),
                    "replacement_text": "\nappended\n",
                    "predicted_post_hash": _sha(data + b"\nappended\n"),
                }
            ],
            "conflicts_with": [],
            "registry_sha256": self.registry.registry_sha256,
        }
        from recallweave.steward_apply import apply_proposal
        from recallweave.steward_policy import WritePolicy

        policy = WritePolicy.from_bytes(
            json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": {"append_at_eof": "auto_apply"},
                }
            ).encode()
        )
        with patch.object(steward_apply, "_open_parent_chain", swapping_chain):
            # The write is anchored to the real directory fd, so it either
            # lands in the original (now-detached) directory or fails; it must
            # NEVER traverse the planted symlink into `elsewhere`.
            try:
                apply_proposal(
                    proposal,
                    registry=self.registry,
                    state_dirs=self.dirs,
                    database=self.database,
                    policy=policy,
                    mode="per_item",
                    execute=True,
                )
            except ApplyError:
                pass
        self.assertTrue(state["swapped"], "the swap hook did not fire")
        self.assertEqual(
            list(elsewhere.iterdir()), [],
            "a write escaped through the swapped-in symlink parent",
        )


class RollbackDriftTest(unittest.TestCase):
    """H2: in-process rollback must not overwrite bytes changed after the
    apply wrote them."""

    def test_rollback_refuses_drifted_target(self) -> None:
        from recallweave.steward_apply import _rollback

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            state_root = base / "state"
            dirs = ensure_state_layout(state_root)
            source = base / "src"
            source.mkdir()
            target = source / "a.md"
            # The user's newer content is live now.
            target.write_text("newer operator work", encoding="utf-8")
            backup_dir = dirs["backups"] / "b"
            backup_dir.mkdir()
            backup = backup_dir / "0-a.md"
            backup.write_text("pre-apply bytes", encoding="utf-8")
            completed = [
                {
                    "relative_path": "a.md",
                    "target": str(target),
                    "had_file": True,
                    "backup_path": str(backup),
                    "content_hash_before": _sha(b"pre-apply bytes"),
                    "content_hash_after": _sha(b"stewards written bytes"),
                }
            ]
            journal = {"status": "applied", "rollback_failures": []}
            journal_path = dirs["journal"] / "j.json"
            atomic_write_json(journal_path, journal, within=dirs["journal"])
            with self.assertRaises(RollbackError):
                _rollback(completed, journal_path, journal, dirs["journal"], source)
            # The user's newer content must survive untouched.
            self.assertEqual(
                target.read_text(encoding="utf-8"), "newer operator work"
            )
            reloaded = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["status"], "rollback_failed")

    def test_rollback_restores_when_stewards_write_is_intact(self) -> None:
        from recallweave.steward_apply import _rollback

        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            state_root = base / "state"
            dirs = ensure_state_layout(state_root)
            source = base / "src"
            source.mkdir()
            target = source / "a.md"
            target.write_bytes(b"stewards written bytes")  # untouched since apply
            backup_dir = dirs["backups"] / "b"
            backup_dir.mkdir()
            backup = backup_dir / "0-a.md"
            backup.write_bytes(b"pre-apply bytes")
            completed = [
                {
                    "relative_path": "a.md",
                    "target": str(target),
                    "had_file": True,
                    "backup_path": str(backup),
                    "content_hash_before": _sha(b"pre-apply bytes"),
                    "content_hash_after": _sha(b"stewards written bytes"),
                }
            ]
            journal = {"status": "applied", "rollback_failures": []}
            journal_path = dirs["journal"] / "j.json"
            atomic_write_json(journal_path, journal, within=dirs["journal"])
            _rollback(completed, journal_path, journal, dirs["journal"], source)
            self.assertEqual(target.read_bytes(), b"pre-apply bytes")
            reloaded = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["status"], "rolled_back")




class RecoverRevertDriftWindowTest(unittest.TestCase):
    """Round 3: recovery and revert must not overwrite bytes changed in the
    window between their pre-screen and _rollback's restore."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "pre-apply bytes")
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
                            "policy": {"include_paths": ["a.md"]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = load_registry(self.registry_path)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.backup_dir = self.dirs["backups"] / "b"
        self.backup_dir.mkdir()
        (self.backup_dir / "0-a.md").write_bytes(b"pre-apply bytes")

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _journal(self, status: str) -> str:
        journal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "apply_journal",
            "proposal_id": "prp-driftwindow000",
            "source": "src",
            "generated_at": "2026-01-01T00:00:00+00:00",
            "status": status,
            "backup_dir": "b",
            "operations": [
                {
                    "relative_path": "a.md",
                    "mutation_class": "append_at_eof",
                    "content_hash_before": _sha(b"pre-apply bytes"),
                    "content_hash_after": _sha(b"stewards written bytes"),
                    "backup_name": "0-a.md",
                    "state": "done",
                }
            ],
            "rollback_failures": [],
        }
        name = f"20260101T000000000000Z-{status}.json"
        atomic_write_json(
            self.dirs["journal"] / name, journal, within=self.dirs["journal"]
        )
        return name

    def _mutate_before_rollback(self, newer: bytes):
        # Hook _rollback so that, immediately before it runs, a concurrent
        # writer replaces the target with newer content the pre-screen did
        # not see.
        from recallweave import steward_apply

        real_rollback = steward_apply._rollback
        target = self.vault.root / "a.md"

        def racing_rollback(*args, **kwargs):
            target.write_bytes(newer)
            return real_rollback(*args, **kwargs)

        return steward_apply, real_rollback, racing_rollback, target

    def test_recovery_refuses_edit_in_the_rollback_window(self) -> None:
        # Target holds Steward's post-apply bytes so the pre-screen accepts it.
        target = self.vault.root / "a.md"
        target.write_bytes(b"stewards written bytes")
        name = self._journal("intent")
        mod, real, racing, target = self._mutate_before_rollback(b"newer operator work")
        with patch.object(mod, "_rollback", racing):
            with self.assertRaises((ApplyError, RollbackError)):
                recover_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertEqual(target.read_bytes(), b"newer operator work")

    def test_revert_refuses_edit_in_the_rollback_window(self) -> None:
        target = self.vault.root / "a.md"
        target.write_bytes(b"stewards written bytes")
        name = self._journal("applied")
        mod, real, racing, target = self._mutate_before_rollback(b"newer operator work")
        with patch.object(mod, "_rollback", racing):
            with self.assertRaises((ApplyError, RollbackError)):
                revert_journal(name, registry=self.registry, state_dirs=self.dirs)
        self.assertEqual(target.read_bytes(), b"newer operator work")




class MalformedJournalBoundaryTest(unittest.TestCase):
    """Round 4: a corrupt/edited journal must surface as the structured,
    path-redacted JSON error envelope, never a raw traceback."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
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
                            "policy": {"include_paths": ["a.md"]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.registry = load_registry(self.registry_path)
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.policy_path = self.base / "wp.json"
        self.policy_path.write_text(
            json.dumps({"spec_version": "recallweave.steward.policy.v1"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _write_journal_text(self, text: str) -> str:
        name = "20260101T000000000000Z-malformed.json"
        (self.dirs["journal"] / name).write_text(text, encoding="utf-8")
        return name

    def _run(self, selector: str, value: str):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from recallweave.cli import main as cli_main

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
                    str(self.policy_path),
                    selector,
                    value,
                    "--execute",
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def _assert_clean_envelope(self, code, out, err):
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)
        payload = json.loads(err.getvalue()) if err.strip() else json.loads(out)
        self.assertNotIn(str(self.base), payload["message"])
        self.assertNotIn("/Users/", payload["message"])

    def test_operations_null_is_a_clean_error(self) -> None:
        name = self._write_journal_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "apply_journal",
                    "source": "src",
                    "status": "intent",
                    "backup_dir": "b",
                    "operations": None,
                }
            )
        )
        code, out, err = self._run("--recover", name)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)
        payload = json.loads(err)
        self.assertNotIn(str(self.base), payload["message"])

    def test_non_object_journal_is_a_clean_error(self) -> None:
        name = self._write_journal_text("[1, 2, 3]")
        code, out, err = self._run("--recover", name)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)
        payload = json.loads(err)
        self.assertNotIn(str(self.base), payload["message"])

    def test_revert_operations_wrong_type_is_a_clean_error(self) -> None:
        name = self._write_journal_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "apply_journal",
                    "source": "src",
                    "status": "applied",
                    "backup_dir": "b",
                    "operations": "not-a-list",
                }
            )
        )
        code, out, err = self._run("--revert", name)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)
        payload = json.loads(err)
        self.assertNotIn(str(self.base), payload["message"])


if __name__ == "__main__":
    unittest.main()
