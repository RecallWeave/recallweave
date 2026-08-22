from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.split())


class DocsMarkdownProjectionTest(unittest.TestCase):
    def test_json_documented_as_canonical_and_markdown_as_projection(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("canonical", text)
        self.assertIn("projection", text)
        self.assertIn("human-readable projection", text)

    def test_documents_fenced_block_mechanism(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("fenced code block", text)
        self.assertIn("can never be interpreted as Markdown syntax", text)
        self.assertIn("Markdown syntax", text)

    def test_documents_separate_renderer_for_richer_presentation(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("separate renderer", text)
        self.assertIn("never a relaxation", text)

    def test_documents_appearance_changes(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertIn("# Task contract", text)
        self.assertIn("Passage", text)
        self.assertIn("table", text)
        self.assertIn("blockquote", text)
        self.assertIn("eight numbered sections", text)

    def test_no_claim_of_anonymity_authorization_boundary(self) -> None:
        text = _norm(_text("docs/task-contracts.md"))
        self.assertNotIn("complete authorized context", text)
        self.assertNotIn("is an authorization boundary", text)

    def test_changelog_documents_uniform_inert_markdown(self) -> None:
        text = _norm(_text("CHANGELOG.md"))
        self.assertIn("inert", text)
        self.assertIn("fenced", text)


if __name__ == "__main__":
    unittest.main()
