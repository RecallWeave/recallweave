from __future__ import annotations

import json
import sqlite3
import tempfile
import unicodedata
import unittest
from contextlib import closing
from pathlib import Path

from recallweave.contract import build_contract_document
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index

# Control and bidi characters that must never survive into the document,
# including into connection evidence (v2 section B).
_BAD_CHARS = ("\u202e", "\u200b", "\u0007")

_SIDE_NAMES = ("source_evidence", "target_evidence")


class ContractEvidenceBoundsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmps: list[tempfile.TemporaryDirectory] = []
        self.tmp = tempfile.TemporaryDirectory()
        self._tmps.append(self.tmp)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = self.root / "index.sqlite"
        self._write_vault()
        build_index(self.vault, self.database, minimum_candidate_score=0.05)

    def tearDown(self) -> None:
        for tmp in self._tmps:
            tmp.cleanup()

    def write(self, relative_path: str, text: str) -> Path:
        path = self.vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _write_vault(self) -> None:
        # Near-duplicate notes sharing distinctive vocabulary so discovery
        # produces candidate edges. The control/bidi characters sit at the START
        # of the section so they land inside the 500-character evidence excerpt
        # window rather than being buried beyond it.
        bad = "".join(_BAD_CHARS) + "Zephyr quadrata is the shared construct alpha engine."
        self.write(
            "Alpha.md",
            "---\ntitle: Alpha\n---\n# Alpha\n\n## Overview\n\n" + bad + "\n",
        )
        self.write(
            "Beta.md",
            "---\ntitle: Beta\n---\n# Beta\n\n## Overview\n\n"
            "Zephyr quadrata is the shared construct beta engine.\n",
        )

    def _spec(self, **retrieval_overrides) -> TaskSpec:
        retrieval = {
            "query": "zephyr quadrata",
            "limit": 8,
            "include_candidates": True,
            "max_characters": 2000,
        }
        retrieval.update(retrieval_overrides)
        return TaskSpec.from_payload(
            {
                "objective": "test objective",
                "retrieval": retrieval,
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {"paths": [], "globs": [], "tags": [], "directives": []},
            }
        )

    def _emitted_vault_strings(self, document: dict) -> int:
        total = len(document["task"]["objective"])
        for item in document["acceptance_criteria"]:
            total += len(item["statement"])
        for item in document["constraints"] + document["prior_decisions"]:
            if item["statement"] is not None:
                total += len(item["statement"])
            if item["passage"] is not None:
                total += len(item["passage"])
        for item in document["retrieved_context"]:
            total += len(item["passage"])
        for directive in document["exclusions"]["directives"]:
            total += len(directive)
        for conn in document["connections"]:
            for side_name in _SIDE_NAMES:
                side = conn["evidence"].get(side_name, {})
                if side.get("passage") is not None:
                    total += len(side["passage"])
                if side.get("heading") is not None:
                    total += len(side["heading"])
        return total

    def test_no_control_or_format_characters_anywhere_in_document(self) -> None:
        document = build_contract_document(self.database, self._spec())
        self.assertTrue(document["connections"])
        serialized = json.dumps(document, ensure_ascii=False)
        offending = [
            ch
            for ch in serialized
            if ch not in "\n\t" and unicodedata.category(ch) in {"Cc", "Cf"}
        ]
        self.assertEqual(offending, [])

    def test_characters_used_covers_connection_evidence_and_stays_in_budget(self) -> None:
        document = build_contract_document(self.database, self._spec())
        self.assertTrue(document["connections"])
        emitted = self._emitted_vault_strings(document)
        self.assertGreaterEqual(document["budget"]["characters_used"], emitted)
        self.assertLessEqual(
            document["budget"]["characters_used"], document["budget"]["character_budget"]
        )

    def test_unknown_evidence_key_is_dropped_not_forwarded(self) -> None:
        with closing(sqlite3.connect(str(self.database))) as connection, connection:
            rows = connection.execute(
                "SELECT id, evidence_json FROM edges"
            ).fetchall()
            for edge_id, raw in rows:
                evidence = json.loads(raw)
                evidence["sneaky_top"] = "forward me"
                evidence.setdefault("source_evidence", {})["sneaky_nested"] = "forward me too"
                connection.execute(
                    "UPDATE edges SET evidence_json = ? WHERE id = ?",
                    (json.dumps(evidence), edge_id),
                )
        document = build_contract_document(self.database, self._spec())
        self.assertTrue(document["connections"])
        for conn in document["connections"]:
            self.assertNotIn("sneaky_top", conn["evidence"])
            source_evidence = conn["evidence"].get("source_evidence", {})
            self.assertNotIn("sneaky_nested", source_evidence)

    def test_budget_too_small_for_connections_truncates(self) -> None:
        document = build_contract_document(
            self.database, self._spec(max_characters=80)
        )
        self.assertTrue(document["budget"]["truncated"])
        emitted = self._emitted_vault_strings(document)
        self.assertLessEqual(emitted, document["budget"]["character_budget"])


if __name__ == "__main__":
    unittest.main()
