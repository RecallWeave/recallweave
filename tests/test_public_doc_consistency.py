"""Public documentation must not leak internal orchestration doctrine, and its
product-boundary claims must stay internally consistent.

Guards two regressions surfaced by the boundary-cleanup review:
  1. no tracked public file hard-codes the private integration-branch name or the
     removed tracked checkpoint marker;
  2. the ARCHITECTURE product boundary and the extension-points section do not
     contradict each other about hosted, operator-run providers.
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_TEXT_SUFFIXES = (".md", ".sh", ".py", ".toml", ".yml", ".yaml", ".json", ".txt", ".cfg")
_INTERNAL_TOKENS = ("foundry/steward", "CHECKPOINT_NOT_APPROVED")


def _tracked_text_files():
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.splitlines() if p.endswith(_TEXT_SUFFIXES)]


class PublicDocConsistencyTest(unittest.TestCase):
    def test_no_public_file_leaks_internal_branch_or_marker(self):
        files = _tracked_text_files()
        if files is None:
            self.skipTest("not a git checkout")
        offenders = []
        for rel in files:
            # The tests directory legitimately names these tokens as the very
            # thing it forbids, so it is exempt from the content scan.
            if rel.startswith("tests/"):
                continue
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            if any(tok in text for tok in _INTERNAL_TOKENS):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            f"public files must not hard-code the internal branch/marker: {offenders}",
        )

    def _boundary_section(self, arch: str) -> str:
        start = arch.index("## Product boundary")
        rest = arch[start + len("## Product boundary"):]
        end = rest.find("\n## ")
        return rest if end == -1 else rest[:end]

    def test_boundary_and_extension_points_are_consistent(self):
        arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("## Product boundary", arch)
        # If the extension points advertise a hosted capability, the document must
        # also make clear it is an operator-run client using the operator's own
        # credentials — otherwise the boundary ("no hosted/managed infrastructure")
        # and the extension points contradict each other.
        if "hosted embedding" in arch.lower():
            self.assertIn(
                "operator's own credentials", arch,
                "a hosted-capability extension point must be reconciled with the "
                "local, single-user product boundary",
            )

    def test_boundary_distinguishes_static_shell_from_hosted_data_processing(self):
        # The repo already supports serving the inert Atlas application shell
        # (viewer/README.md). A categorical "no server" boundary would contradict
        # that, so if the boundary uses "no server" language it must also carve out
        # the static, data-less application shell.
        arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        boundary = self._boundary_section(arch)
        if "no server" in boundary.lower():
            self.assertIn(
                "application shell", boundary,
                "the boundary's 'no server' wording must carve out the inert, "
                "data-less Atlas application shell that viewer/README.md documents",
            )


class StewardDocConsistencyTest(unittest.TestCase):
    """Steward doctrine tripwires from the approved architecture.

    1. The steward doc must state the load-bearing rulings: interpretive
       relations are reserved for an opt-in provider (whose fallback is the
       absence of the claim), and technical determinism never implies
       authorization.
    2. No tracked public doc may present an interpretive relation as shipped
       behavior: any doc that names CONTRADICTS must also carry the
       reservation language.
    """

    def test_steward_doc_states_the_rulings(self):
        doc = (ROOT / "docs" / "steward.md").read_text(encoding="utf-8")
        self.assertIn("InterpretationProvider", doc)
        self.assertIn("absence", doc)
        self.assertIn("propose_only", doc)
        self.assertIn("determinism never implies", doc)
        for fragment in ("no_change", "findings", "approval_required"):
            self.assertIn(fragment, doc)

    def test_extension_points_require_absent_not_stub_fallback(self):
        arch = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("absence", arch)

    def test_no_doc_presents_interpretive_relations_as_shipped(self):
        files = _tracked_text_files()
        if files is None:
            self.skipTest("not a git checkout")
        offenders = []
        for rel in files:
            if not rel.endswith(".md") or rel.startswith("tests/"):
                continue
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
            if "CONTRADICTS" in text and "reserved" not in text:
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            "docs naming CONTRADICTS must carry the reservation language: "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
