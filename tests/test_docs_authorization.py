from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCS = {
    "task_contracts": ROOT / "docs" / "task-contracts.md",
    "privacy": ROOT / "PRIVACY.md",
    "security": ROOT / "SECURITY.md",
    "architecture": ROOT / "ARCHITECTURE.md",
    "readme": ROOT / "README.md",
    "changelog": ROOT / "CHANGELOG.md",
}

FALSE_PHRASES = [
    "complete authorized context",
    "It is the complete authorized context",
    "This bundle is the complete authorized context for this task",
]

V2_HANDLING_SCOPE = (
    "This bundle contains the context the operator selected for this task. "
    "It is a scoped projection of an index, not an authorization decision, "
    "and it does not certify that anything outside it is forbidden or that "
    "everything inside it is permitted."
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _whitespace_normalized(text: str) -> str:
    return " ".join(text.split())


class DocsAuthorizationLanguageTest(unittest.TestCase):
    def test_no_doc_describes_a_bundle_as_complete_authorized_context(self) -> None:
        for name, path in DOCS.items():
            text = _text(path)
            for phrase in FALSE_PHRASES:
                self.assertNotIn(
                    phrase,
                    text,
                    f"{name}: {path.name} still contains false authorization phrase: {phrase!r}",
                )

    def test_task_contracts_documents_v2_handling_scope(self) -> None:
        text = _text(DOCS["task_contracts"])
        self.assertIn(V2_HANDLING_SCOPE, text)

    def test_task_contracts_documents_v2_budget_definition(self) -> None:
        text = _text(DOCS["task_contracts"])
        lowered = text.casefold()
        self.assertIn("connection evidence", lowered)
        self.assertIn("structural metadata", lowered)
        self.assertIn("connections", lowered)
        self.assertNotIn("whole-document budget", lowered)

    def test_task_contracts_drops_spec_notes(self) -> None:
        text = _text(DOCS["task_contracts"])
        spec_schema = text.split("The spec is a JSON object.", 1)[1].split("Field rules:", 1)[0]
        self.assertNotIn('"notes"', spec_schema)
        self.assertNotIn("`notes`: optional", text)

    def test_task_contracts_drops_suppressed_total_from_receipt(self) -> None:
        text = _text(DOCS["task_contracts"])
        self.assertNotIn('"suppressed_total"', text)

    def test_privacy_still_warns_bundle_not_safe_to_forward(self) -> None:
        text = _text(DOCS["privacy"])
        self.assertIn("not anonymous", text)
        self.assertIn("not automatically safe to forward", text)
        self.assertIn("passage text", text)

    def test_exclusions_not_described_as_authorization_boundary(self) -> None:
        for name in ("task_contracts", "security", "architecture"):
            normalized = _whitespace_normalized(_text(DOCS[name]))
            self.assertIn("authorization boundary", normalized)
            self.assertNotIn("is an authorization boundary", normalized)
            self.assertNotIn("are an authorization boundary", normalized)


if __name__ == "__main__":
    unittest.main()
