from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave import steward_git
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_apply import ApplyError, apply_latest
from recallweave.steward_assess import assess_latest
from recallweave.steward_git import (
    GitError,
    check_apply_preconditions,
    commit_applied,
    git_available,
    repo_status,
)
from recallweave.steward_observe import observe_registry
from recallweave.steward_policy import WritePolicy
from recallweave.steward_propose import propose_latest
from recallweave.steward_sources import SOURCES_SPEC_VERSION, load_registry
from recallweave.steward_state import ensure_state_layout

from steward_fixtures import TempVault


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {args} failed in {cwd}: {result.stderr}"
        )
    return result


def _init_repo(root: Path) -> None:
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test User"], root)


def _policy(payload: dict | None = None) -> WritePolicy:
    document = {"spec_version": "recallweave.steward.policy.v1"}
    document.update(payload or {})
    data = json.dumps(document).encode("utf-8")
    return WritePolicy.from_bytes(data)


@unittest.skipUnless(git_available(), "git binary not available")
class RepoStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_non_repo_returns_none(self) -> None:
        (self.root / "plain.md").write_text("hello", encoding="utf-8")
        self.assertIsNone(repo_status(self.root))

    def test_clean_repo_reports_head_and_branch(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)

        status = repo_status(self.root)
        self.assertIsNotNone(status)
        self.assertRegex(status["head"], r"^[0-9a-f]{7,64}$")
        self.assertIsInstance(status["branch"], str)
        self.assertTrue(status["branch"])
        self.assertFalse(status["detached"])
        self.assertEqual(status["dirty_paths"], [])
        self.assertFalse(status["has_content_rewriting_hooks"])
        self.assertEqual(Path(status["repo_root"]).resolve(), self.root.resolve())

    def test_dirty_file_is_listed(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)

        (self.root / "a.md").write_text("changed", encoding="utf-8")
        status = repo_status(self.root)
        self.assertIn("a.md", status["dirty_paths"])

    def test_detached_head_is_flagged(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        sha = _git(["rev-parse", "HEAD"], self.root).stdout.strip()
        _git(["checkout", "-q", sha], self.root)

        status = repo_status(self.root)
        self.assertTrue(status["detached"])
        self.assertIsNone(status["branch"])

    @unittest.skipIf(os.name == "nt", "executable bit is not meaningful on Windows")
    def test_executable_pre_commit_hook_is_flagged(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)

        hooks_dir = self.root / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)

        status = repo_status(self.root)
        self.assertTrue(status["has_content_rewriting_hooks"])

    @unittest.skipIf(os.name == "nt", "executable bit is not meaningful on Windows")
    def test_non_executable_pre_commit_hook_is_not_flagged(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)

        hooks_dir = self.root / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o644)

        status = repo_status(self.root)
        self.assertFalse(status["has_content_rewriting_hooks"])


@unittest.skipUnless(git_available(), "git binary not available")
class CheckApplyPreconditionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_non_repo_require_git_false_returns_not_used(self) -> None:
        info = check_apply_preconditions(self.root, ["a.md"], require_git=False)
        self.assertEqual(info, {"git_used": False, "head": None, "branch": None})

    def test_non_repo_require_git_true_raises(self) -> None:
        with self.assertRaises(GitError):
            check_apply_preconditions(self.root, ["a.md"], require_git=True)

    def test_overlap_with_dirty_touched_path_raises(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        (self.root / "b.md").write_text("b", encoding="utf-8")
        _git(["add", "a.md", "b.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        (self.root / "a.md").write_text("changed", encoding="utf-8")

        with self.assertRaises(GitError):
            check_apply_preconditions(self.root, ["a.md"], require_git=False)

    def test_touched_note_in_wholly_untracked_dir_is_refused(self) -> None:
        # git status --porcelain collapses a WHOLLY-untracked directory to a
        # single "notes/" entry, never "notes/draft.md". The overlap check must
        # still recognize a touched note under that prefix as dirty, or steward
        # would edit and commit an operator's untracked note. Regression for the
        # directory-prefix overlap gap.
        _init_repo(self.root)
        (self.root / "seed.md").write_text("seed", encoding="utf-8")
        _git(["add", "seed.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        (self.root / "notes").mkdir()
        (self.root / "notes" / "draft.md").write_text(
            "operator draft", encoding="utf-8"
        )
        with self.assertRaises(GitError):
            check_apply_preconditions(
                self.root, ["notes/draft.md"], require_git=False
            )

    def test_dirty_unrelated_path_is_allowed(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        (self.root / "b.md").write_text("b", encoding="utf-8")
        _git(["add", "a.md", "b.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        (self.root / "a.md").write_text("changed", encoding="utf-8")

        info = check_apply_preconditions(self.root, ["b.md"], require_git=False)
        self.assertTrue(info["git_used"])
        self.assertIsNotNone(info["head"])

    def test_detached_head_raises(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        sha = _git(["rev-parse", "HEAD"], self.root).stdout.strip()
        _git(["checkout", "-q", sha], self.root)

        with self.assertRaises(GitError):
            check_apply_preconditions(self.root, ["a.md"], require_git=False)

    @unittest.skipIf(os.name == "nt", "executable bit is not meaningful on Windows")
    def test_content_rewriting_hook_raises(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        hooks_dir = self.root / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)

        with self.assertRaises(GitError):
            check_apply_preconditions(self.root, ["a.md"], require_git=False)


@unittest.skipUnless(git_available(), "git binary not available")
class CommitAppliedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_commit_succeeds_when_user_name_is_missing(self) -> None:
        # user.email configured, user.name absent, user.useConfigOnly=true so
        # git will not synthesize a name: the apply commit must still land, with
        # the fallback name and the operator's real email. Regression for the
        # identity fallback that only checked user.email.
        _git(["init", "-q"], self.root)
        _git(["config", "user.email", "real@operator.test"], self.root)
        _git(["config", "user.useConfigOnly", "true"], self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        result = commit_applied(
            self.root,
            ["a.md"],
            proposal_id="prp-identitytesttest0",
            journal_ref="20260101T000000000000Z-prp-identitytesttest0.json",
        )
        self.assertTrue(result["committed"])
        author = _git(["log", "-1", "--pretty=%an <%ae>"], self.root).stdout.strip()
        self.assertEqual(author, "RecallWeave Steward <real@operator.test>")

    def test_commit_preserves_configured_name_when_only_email_missing(self) -> None:
        # The mirror case: a configured name must NOT be overwritten by the
        # fallback when only the email is missing -- the fallback is per-field.
        _git(["init", "-q"], self.root)
        _git(["config", "user.name", "Real Operator"], self.root)
        _git(["config", "user.useConfigOnly", "true"], self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        result = commit_applied(
            self.root,
            ["a.md"],
            proposal_id="prp-identitytesttest1",
            journal_ref="20260101T000000000000Z-prp-identitytesttest1.json",
        )
        self.assertTrue(result["committed"])
        author = _git(["log", "-1", "--pretty=%an <%ae>"], self.root).stdout.strip()
        self.assertEqual(author, "Real Operator <steward@localhost>")

    def test_commits_exactly_the_touched_paths(self) -> None:
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        (self.root / "b.md").write_text("b", encoding="utf-8")
        _git(["add", "a.md", "b.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)

        (self.root / "a.md").write_text("a-changed", encoding="utf-8")
        (self.root / "b.md").write_text("b-changed", encoding="utf-8")

        result = commit_applied(
            self.root,
            ["a.md"],
            proposal_id="prp-testtesttesttest",
            journal_ref="20260101T000000000000Z-prp-testtesttesttest.json",
        )
        self.assertTrue(result["committed"])
        self.assertRegex(result["commit"], r"^[0-9a-f]{7,64}$")

        status = _git(["status", "--porcelain"], self.root).stdout
        self.assertIn("b.md", status)
        self.assertNotIn(" a.md", status)

        message = _git(["log", "-1", "--pretty=%B"], self.root).stdout
        self.assertIn("prp-testtesttesttest", message)
        self.assertIn(
            "20260101T000000000000Z-prp-testtesttesttest.json", message
        )

        head = _git(["rev-parse", "HEAD"], self.root).stdout.strip()
        self.assertEqual(head, result["commit"])

    def test_post_commit_hook_does_not_run_during_steward_commit(self) -> None:
        # --no-verify does not suppress post-commit; the steward commit must run
        # with hooks disabled so no hook can mutate the validated notes.
        _init_repo(self.root)
        (self.root / "a.md").write_text("validated", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        (self.root / "a.md").write_text("validated-v2", encoding="utf-8")

        hooks = self.root / ".git" / "hooks"
        marker = self.root / "post-commit-ran.txt"
        hook = hooks / "post-commit"
        hook.write_text(
            "#!/bin/sh\n"
            f"echo tampered > '{self.root / 'a.md'}'\n"
            f"touch '{marker}'\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)

        result = commit_applied(
            self.root,
            ["a.md"],
            proposal_id="prp-hookhookhookhook",
            journal_ref="j.json",
        )
        self.assertTrue(result["committed"])
        self.assertFalse(
            marker.exists(), "post-commit hook ran despite hooks being disabled"
        )
        self.assertEqual(
            (self.root / "a.md").read_text(encoding="utf-8"),
            "validated-v2",
            "a hook mutated the validated note after apply",
        )

    def test_failed_commit_unstages_touched_paths(self) -> None:
        # git add succeeds, then the commit fails (an unsatisfiable gpg-sign
        # requirement): the touched path must not remain staged in the operator's
        # index, or it would ride along in their next commit.
        _init_repo(self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")
        _git(["add", "a.md"], self.root)
        _git(["commit", "-m", "initial"], self.root)
        (self.root / "a.md").write_text("a-changed", encoding="utf-8")
        # Force commit failure without a usable signing key.
        _git(["config", "commit.gpgsign", "true"], self.root)
        _git(["config", "gpg.program", "/nonexistent-gpg-binary"], self.root)

        with self.assertRaises(GitError):
            commit_applied(
                self.root,
                ["a.md"],
                proposal_id="prp-failfailfailfail",
                journal_ref="j.json",
            )

        staged = _git(["diff", "--cached", "--name-only"], self.root).stdout
        self.assertNotIn("a.md", staged, "failed commit left a.md staged")

    def test_works_when_repo_has_no_user_identity_configured(self) -> None:
        _git(["init", "-q"], self.root)
        (self.root / "a.md").write_text("a", encoding="utf-8")

        temp_home = tempfile.mkdtemp()
        env_overrides = {
            "HOME": temp_home,
            "XDG_CONFIG_HOME": os.path.join(temp_home, ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        try:
            with patch.dict(os.environ, env_overrides, clear=False):
                for key in (
                    "GIT_AUTHOR_NAME",
                    "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL",
                ):
                    os.environ.pop(key, None)
                email_check = subprocess.run(
                    ["git", "config", "user.email"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    env=os.environ.copy(),
                )
                self.assertNotEqual(email_check.returncode, 0)

                result = commit_applied(
                    self.root,
                    ["a.md"],
                    proposal_id="prp-noidentity0000000",
                    journal_ref="j.json",
                )
        finally:
            import shutil as _shutil

            _shutil.rmtree(temp_home, ignore_errors=True)

        self.assertTrue(result["committed"])
        author = _git(["log", "-1", "--pretty=%an <%ae>"], self.root).stdout.strip()
        self.assertEqual(author, "RecallWeave Steward <steward@localhost>")


class ModulePostureTest(unittest.TestCase):
    def test_module_has_no_network_git_subcommands(self) -> None:
        source = Path(steward_git.__file__).read_text(encoding="utf-8")
        forbidden = ("fetch", "pull", "push", "clone", " remote")
        offenders = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            for word in forbidden:
                if word in line:
                    offenders.append(f"{lineno}:{word}:{line.strip()}")
        self.assertEqual(offenders, [], f"forbidden git subcommand mentions: {offenders}")

    def test_public_api_present(self) -> None:
        self.assertTrue(steward_git.git_available.__call__)
        self.assertEqual(steward_git.GIT_TIMEOUT_SECONDS, 30)
        self.assertTrue(issubclass(steward_git.GitError, ValueError))


@unittest.skipUnless(git_available(), "git binary not available")
class ApplyPipelineGitIntegrationTest(unittest.TestCase):
    """End-to-end: the real steward_apply pipeline, with and without git."""

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
                                "include_paths": ["Alpha.md", "Beta.md", "Gamma.md"]
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

    def test_apply_inside_git_repo_commits_and_receipt_carries_sha(self) -> None:
        _init_repo(self.vault.root)
        _git(["add", "Alpha.md", "Beta.md"], self.vault.root)
        _git(["commit", "-m", "initial vault"], self.vault.root)

        proposal = self._pipeline_rename()
        receipt = apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=_policy(
                {"class_levels": {"fix_unresolved_link": "require_approval"}}
            ),
            proposal_id=proposal["proposal_id"],
            execute=True,
        )
        proposal_receipt = receipt["proposals"][0]
        self.assertTrue(proposal_receipt["applied"])
        git_receipt = proposal_receipt["git"]
        self.assertTrue(git_receipt["used"])
        self.assertIsNone(git_receipt.get("commit_error"))
        self.assertRegex(git_receipt["commit"], r"^[0-9a-f]{7,64}$")

        named = _git(
            ["log", "-1", "--name-only", "--pretty=format:"], self.vault.root
        ).stdout
        self.assertIn("Alpha.md", named.splitlines())

    def test_apply_on_plain_folder_reports_git_unused(self) -> None:
        proposal = self._pipeline_rename()
        receipt = apply_latest(
            self.registry,
            self.state_root,
            self.database,
            write_policy=_policy(
                {"class_levels": {"fix_unresolved_link": "require_approval"}}
            ),
            proposal_id=proposal["proposal_id"],
            execute=True,
        )
        proposal_receipt = receipt["proposals"][0]
        self.assertTrue(proposal_receipt["applied"])
        self.assertEqual(proposal_receipt["git"], {"used": False})

    def test_require_git_true_on_plain_folder_refuses_before_journal(self) -> None:
        proposal = self._pipeline_rename()
        dirs = ensure_state_layout(self.state_root)
        self.assertEqual(list(dirs["journal"].glob("*.json")), [])

        with self.assertRaises(GitError):
            apply_latest(
                self.registry,
                self.state_root,
                self.database,
                write_policy=_policy(
                    {
                        "class_levels": {"fix_unresolved_link": "require_approval"},
                        "require_git": True,
                    }
                ),
                proposal_id=proposal["proposal_id"],
                execute=True,
            )

        self.assertEqual(list(dirs["journal"].glob("*.json")), [])
        self.assertEqual(list(dirs["receipts"].glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
