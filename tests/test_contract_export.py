from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recallweave.contract_export import export_contract
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index


class ContractExportReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = self.root / "index.sqlite"
        self._write_vault()
        build_index(self.vault, self.database, minimum_candidate_score=0.08)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative_path: str, text: str) -> Path:
        path = self.vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _write_vault(self) -> None:
        self.write(
            "Projects/Alpha.md",
            "# Alpha\n\n## Background\n\nZephyr quadrata foundational construct.\n",
        )
        self.write(
            "Projects/Beta.md",
            "# Beta\n\n## Background\n\nZephyr quadrata builds on Alpha. [[Alpha]]\n",
        )
        self.write(
            "Restricted/Secret.md",
            "# Secret\n\nZephyr XYZZY_SECRET_SENTINEL hidden.\n",
        )

    def _spec(self) -> TaskSpec:
        return TaskSpec.from_payload(
            {
                "task_id": "export-test",
                "objective": "Explain the alpha-beta relationship.",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 5000,
                },
                "constraints": [{"text": "Do not invent relationships."}],
                "prior_decisions": [],
                "acceptance_criteria": ["Citations resolve."],
                "exclusions": {"paths": ["Restricted/Secret.md"]},
            }
        )

    def test_receipt_has_no_suppressed_total_aggregate(self) -> None:
        receipt = export_contract(self.database, self._spec(), None)
        self.assertNotIn("suppressed_total", receipt)

    def test_document_keeps_per_category_suppressed_counts(self) -> None:
        receipt = export_contract(self.database, self._spec(), None)
        document = receipt["contract"]
        suppressed = document["exclusions"]["suppressed"]
        for key in ("retrieved_context", "connections", "notes"):
            self.assertIn(key, suppressed)
            self.assertIsInstance(suppressed[key], int)
            self.assertGreaterEqual(suppressed[key], 0)


if __name__ == "__main__":
    unittest.main()
