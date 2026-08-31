"""The local review helper must resolve a comparison base generically.

`scripts/codex-review.sh --print-base [DIR]` must derive the base from the
checked-out branch's upstream, then the remote default branch, then a local
default branch — never a hard-coded internal integration-branch name that will
not exist in a fresh clone or a contributor fork.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-review.sh"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _init(root, branch):
    repo = root / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "f").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "i")
    return repo


def _print_base(repo):
    return subprocess.run(
        ["zsh", str(SCRIPT), "--print-base", str(repo)],
        capture_output=True, text=True,
    )


@unittest.skipUnless(shutil.which("zsh"), "zsh not available")
@unittest.skipUnless(SCRIPT.is_file(), "review helper missing")
class ReviewBaseFallbackTest(unittest.TestCase):
    def test_prefers_branch_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            upstream = _init(root, branch="main")
            clone = root / "clone"
            _git(root, "clone", "-q", str(upstream), str(clone))
            _git(clone, "checkout", "-q", "-b", "feature", "--track", "origin/main")
            r = _print_base(clone)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "origin/main")

    def test_falls_back_to_origin_head_without_upstream(self):
        # A branch with no upstream, in a clone that has origin/HEAD, must fall
        # back to the remote's default branch rather than failing.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            upstream = _init(root, branch="main")
            clone = root / "clone"
            _git(root, "clone", "-q", str(upstream), str(clone))
            _git(clone, "checkout", "-q", "-b", "feature")  # no --track: no upstream
            r = _print_base(clone)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "origin/main")

    def test_falls_back_to_local_default_branch(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _init(Path(d), branch="feature")
            _git(repo, "branch", "main")  # no upstream, no origin -> local default
            r = _print_base(repo)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "main")

    def test_precise_error_when_no_base(self):
        with tempfile.TemporaryDirectory() as d:
            repo = _init(Path(d), branch="solo")  # no upstream, no origin, no main
            r = _print_base(repo)
            self.assertEqual(r.returncode, 3)
            self.assertIn("no comparison base", r.stderr.lower())


if __name__ == "__main__":
    unittest.main()
