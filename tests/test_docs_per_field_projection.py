from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.split())


class DocsPerFieldProjectionTest(unittest.TestCase):
    def test_documents_one_fenced_block_per_field(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("one fenced block per field", text)
        self.assertIn("per field", text)

    def test_documents_absence_distinguishable_from_emptiness(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("absence", text)
        self.assertIn("emptiness", text)
        self.assertIn("None recorded", text)

    def test_documents_evidence_boundary_rationale(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("evidence boundary", text)
        self.assertIn("first-order defect", text)
        self.assertIn("operator asserted", text)

    def test_documents_injectivity_property(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("injectivity", text)
        self.assertIn("render identically", text)
        self.assertIn("contract", text)

    def test_does_not_document_merged_block_shape(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertNotIn("one fenced block carrying the statement and, on its own line, the citation", text)

    def test_changelog_documents_per_field_projection(self) -> None:
        text = _norm(_text("CHANGELOG.md"))
        self.assertIn("per field", text)
        self.assertIn("evidence boundary", text)


if __name__ == "__main__":
    unittest.main()
