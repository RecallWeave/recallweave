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

    def test_docs_do_not_claim_whole_index_validation(self) -> None:
        # The fail-closed gate validates every connection the export RETURNS,
        # not every eligible edge in the index: the loop stops at the budget and
        # later edges are never examined. Documentation must not claim the
        # stronger property. Cycle 17 caught the CHANGELOG doing exactly that.
        for relative in ("CHANGELOG.md", "docs/task-contracts.md"):
            with self.subTest(document=relative):
                text = _norm(_text(relative))
                self.assertNotIn("on every connection before the budget", text)
                self.assertNotIn("validates every connection before the budget", text)
        contract_docs = _norm(_text("docs/task-contracts.md"))
        self.assertIn("not a whole-index scan", contract_docs)
        self.assertIn("never examined", contract_docs)

    def test_docs_scope_attribution_to_the_indexed_snapshot(self) -> None:
        # The exporter reads the INDEX, never the vault, so evidence is
        # attributed to the snapshot the index recorded, not to the vault's
        # current bytes. Documentation must not claim export-time verification
        # against physical vault lines, which would be a promise the code does
        # not keep (cycle 18).
        for relative in ("ARCHITECTURE.md", "docs/task-contracts.md"):
            with self.subTest(document=relative):
                text = _norm(_text(relative))
                self.assertNotIn("resolves to physical vault lines", text)
                self.assertIn("indexed snapshot", text)
        contract_docs = _norm(_text("docs/task-contracts.md"))
        self.assertIn("reads the index, never the vault", contract_docs)
        # And the stronger property the code DOES keep: content, not just
        # coordinates, is compared.
        self.assertIn("Checking the coordinates alone is", contract_docs)

    def test_changelog_documents_per_field_projection(self) -> None:
        text = _norm(_text("CHANGELOG.md"))
        self.assertIn("per field", text)
        self.assertIn("evidence boundary", text)


if __name__ == "__main__":
    unittest.main()
