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

from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_apply import (
    ApplyError,
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
            with self.assertRaisesRegex(ApplyError, "symlinked directory"):
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


if __name__ == "__main__":
    unittest.main()
