"""The public OSS tree must not track internal development/orchestration artifacts.

This is a boundary-regression guard: if a future change re-adds the Beads database,
the Codex review archive, agent tool configuration, the swarm runbook, the session
handoff, or the (now local-only) checkpoint marker, this test fails.
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_PREFIXES = (
    ".beads/",
    ".codex-reviews/",
    ".claude/",
    ".codex/",
    ".agents/",
    ".rw-supervisor/",
)
FORBIDDEN_EXACT = frozenset({
    "SWARM-RUNBOOK.md",
    "docs/SESSION-HANDOFF.md",
    "CHECKPOINT_NOT_APPROVED.md",
})


def _tracked_files():
    """Tracked paths, or None when this is not a git checkout (e.g. an sdist)."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


class PublicTrackedSurfaceTest(unittest.TestCase):
    def test_no_internal_orchestration_artifacts_are_tracked(self):
        tracked = _tracked_files()
        if tracked is None:
            self.skipTest("not a git checkout")
        leaked = [
            p
            for p in tracked
            if p in FORBIDDEN_EXACT
            or any(p.startswith(pre) for pre in FORBIDDEN_PREFIXES)
        ]
        self.assertEqual(
            leaked, [], f"internal artifacts must not be tracked in the public tree: {leaked}"
        )


if __name__ == "__main__":
    unittest.main()
