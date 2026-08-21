from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recallweave.contract import CONTRACT_SCHEMA_VERSION, build_contract_document
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index


class ContractDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
            "---\n"
            "title: Alpha\n"
            "tags: [core]\n"
            "---\n"
            "# Alpha\n"
            "\n"
            "## Background\n"
            "\n"
            "Zephyr quadrata is the foundational construct. It must be preserved verbatim.\n"
            "\n"
            "## Notes\n"
            "\n"
            "Additional alpha details.\n",
        )
        self.write(
            "Projects/Beta.md",
            "---\n"
            "title: Beta\n"
            "tags: [core]\n"
            "---\n"
            "# Beta\n"
            "\n"
            "## Background\n"
            "\n"
            "Zephyr quadrata builds on Alpha. See the [[Alpha]] reference.\n"
            "\n"
            "## Notes\n"
            "\n"
            "Beta specifics.\n",
        )
        self.write(
            "Projects/Gamma.md",
            "---\n"
            "title: Gamma\n"
            "tags: [core]\n"
            "---\n"
            "# Gamma\n"
            "\n"
            "## Background\n"
            "\n"
            "Quadrata mechanics differ in Gamma. See the [[Alpha]] reference.\n"
            "\n"
            "## Notes\n"
            "\n"
            "Gamma specifics.\n",
        )
        self.write(
            "Decision Log.md",
            "---\n"
            "title: Decision Log\n"
            "---\n"
            "# Decision Log\n"
            "\n"
            "## Decision\n"
            "\n"
            "We chose option two for the zephyr system. This is the recorded decision.\n"
            "\n"
            "## Rationale\n"
            "\n"
            "Backup rationale.\n",
        )
        self.write(
            "Restricted/Secret.md",
            "---\n"
            "title: Secret\n"
            "---\n"
            "# Secret\n"
            "\n"
            "Zephyr XYZZY_SECRET_SENTINEL must never appear in the contract.\n"
            "\n"
            "## More\n"
            "\n"
            "Hidden content.\n",
        )
        self.write(
            "Tagged/Private Note.md",
            "---\n"
            "title: Private Note\n"
            "tags: [private]\n"
            "---\n"
            "# Private Note\n"
            "\n"
            "PRIVATE_SENTINEL tag private content.\n",
        )

    def _full_spec(self, **overrides) -> TaskSpec:
        payload = {
            "task_id": "test-task",
            "objective": "Summarize the alpha-beta relationship for a downstream agent.",
            "retrieval": {
                "query": "zephyr quadrata",
                "limit": 8,
                "include_candidates": True,
                "max_characters": 5000,
            },
            "constraints": [
                {"text": "Do not invent relationships beyond the cited passages."},
                {
                    "note": "Projects/Alpha.md",
                    "heading": "Background",
                    "statement": "Alpha is the canonical source.",
                },
            ],
            "prior_decisions": [
                {"text": "Retain only verified evidence."},
            ],
            "acceptance_criteria": [
                "All citations resolve to physical lines.",
                "No excluded content leaks.",
            ],
            "exclusions": {
                "paths": ["Restricted/Secret.md"],
                "tags": ["private"],
            },
            "notes": "Work packet for the review agent.",
        }
        payload.update(overrides)
        return TaskSpec.from_payload(payload)

    def test_full_spec_produces_every_key_with_correct_evidence_classes(self) -> None:
        document = build_contract_document(self.database, self._full_spec())
        self.assertEqual(document["schema_version"], CONTRACT_SCHEMA_VERSION)
        self.assertEqual(document["task"]["id"], "test-task")
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "task",
                "retrieved_context",
                "connections",
                "constraints",
                "prior_decisions",
                "acceptance_criteria",
                "exclusions",
                "provenance",
                "budget",
                "disclosure",
                "handling",
            },
        )
        self.assertEqual(
            set(document["constraints"][0]),
            {"statement", "evidence_class", "citation", "relative_path", "passage", "truncated"},
        )
        self.assertEqual(
            document["constraints"][0]["evidence_class"], "authored_by_operator"
        )
        self.assertEqual(
            document["constraints"][1]["evidence_class"], "cited_passage"
        )
        self.assertEqual(document["prior_decisions"][0]["evidence_class"], "authored_by_operator")
        self.assertTrue(all(rc["evidence_class"] == "lexical_match" for rc in document["retrieved_context"]))
        self.assertTrue(all(not rc["verified"] for rc in document["retrieved_context"]))
        self.assertEqual(
            [ac["id"] for ac in document["acceptance_criteria"]],
            ["AC1", "AC2"],
        )
        self.assertTrue(document["exclusions"]["enforced"])
        self.assertEqual(document["provenance"]["network_calls"], 0)
        self.assertEqual(document["provenance"]["vault_writes"], 0)
        self.assertTrue(document["provenance"]["generated_locally"])
        self.assertEqual(document["handling"]["content_is_data_not_instructions"], True)
        self.assertIn("quoted from the operator's vault", document["handling"]["statement"])

    def test_cited_passage_citation_resolves_to_physical_lines(self) -> None:
        document = build_contract_document(self.database, self._full_spec())
        cited = next(
            item for item in document["constraints"] if item["evidence_class"] == "cited_passage"
        )
        self.assertIsNotNone(cited["citation"])
        path, line_range = cited["citation"].rsplit(":", 1)
        start, end = (int(part) for part in line_range.split("-"))
        source = self.vault / path
        physical_lines = source.read_text(encoding="utf-8").split("\n")
        recomputed = "\n".join(physical_lines[start - 1 : end])
        self.assertEqual(recomputed, cited["passage"])
        self.assertEqual(cited["relative_path"], path)
        self.assertEqual(cited["statement"], "Alpha is the canonical source.")

    def test_authored_and_cited_evidence_class_discipline(self) -> None:
        document = build_contract_document(self.database, self._full_spec())
        for item in document["constraints"] + document["prior_decisions"]:
            if item["evidence_class"] == "authored_by_operator":
                self.assertIsNone(item["citation"])
                self.assertIsNone(item["relative_path"])
                self.assertIsNone(item["passage"])
                self.assertFalse(item["truncated"])
            else:
                self.assertIsNotNone(item["citation"])
                self.assertIsNotNone(item["relative_path"])
                self.assertIsNotNone(item["passage"])

    def test_excluded_note_absent_from_whole_document(self) -> None:
        spec = self._full_spec(
            retrieval={
                "query": "zephyr quadrata",
                "limit": 8,
                "max_characters": 5000,
            }
        )
        document = build_contract_document(self.database, spec)
        serialized = json.dumps(document)
        self.assertNotIn("XYZZY_SECRET_SENTINEL", serialized)
        self.assertNotIn("PRIVATE_SENTINEL", serialized)
        self.assertNotIn("Tagged/Private Note.md", serialized)
        self.assertNotIn("Restricted/Secret.md", document["provenance"]["citations"])
        self.assertNotIn(
            "Restricted/Secret.md",
            [rc["relative_path"] for rc in document["retrieved_context"]],
        )
        self.assertNotIn(
            "Restricted/Secret.md",
            [
                path
                for conn in document["connections"]
                for path in (conn["source"], conn["target"])
            ],
        )
        self.assertGreater(document["exclusions"]["suppressed"]["retrieved_context"], 0)
        self.assertGreater(document["exclusions"]["suppressed"]["notes"], 0)
        self.assertEqual(document["exclusions"]["paths"], ["Restricted/Secret.md"])

    def test_selector_naming_excluded_note_raises_valueerror(self) -> None:
        spec = self._full_spec(
            constraints=[
                {"note": "Restricted/Secret.md"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "Secret"):
            build_contract_document(self.database, spec)

    def test_selector_naming_excluded_tag_raises_valueerror(self) -> None:
        spec = self._full_spec(
            constraints=[
                {"note": "Tagged/Private Note.md"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "Private Note"):
            build_contract_document(self.database, spec)

    def test_candidate_edges_absent_by_default_present_with_candidates(self) -> None:
        spec = self._full_spec(
            retrieval={
                "query": "zephyr quadrata",
                "limit": 8,
                "include_candidates": False,
                "max_characters": 5000,
            }
        )
        default = build_contract_document(self.database, spec)
        self.assertTrue(all(c["verified"] for c in default["connections"]))
        self.assertTrue(
            all(c["evidence_class"] == "authored_link" for c in default["connections"])
        )
        self.assertFalse(default["disclosure"]["includes_candidate_edges"])

        with_candidates = build_contract_document(self.database, self._full_spec())
        self.assertTrue(
            any(
                c["evidence_class"] == "discovery_candidate" and not c["verified"]
                for c in with_candidates["connections"]
            )
        )
        self.assertTrue(with_candidates["disclosure"]["includes_candidate_edges"])

    def test_budget_characters_used_never_exceeds_budget(self) -> None:
        document = build_contract_document(self.database, self._full_spec())
        self.assertLessEqual(
            document["budget"]["characters_used"], document["budget"]["character_budget"]
        )
        for item in document["constraints"] + document["prior_decisions"] + document["retrieved_context"]:
            if item["passage"] is not None and item["truncated"]:
                self.assertLessEqual(len(item["passage"]), 500)

    def test_budget_truncates_retrieved_passage_when_small(self) -> None:
        spec = self._full_spec(
            objective="short",
            retrieval={
                "query": "zephyr quadrata",
                "limit": 8,
                "max_characters": 60,
            },
            constraints=[{"text": "x"}],
            prior_decisions=[],
            acceptance_criteria=[],
        )
        document = build_contract_document(self.database, spec)
        self.assertTrue(document["budget"]["truncated"])
        self.assertLessEqual(
            document["budget"]["characters_used"], document["budget"]["character_budget"]
        )
        self.assertTrue(any(rc["truncated"] for rc in document["retrieved_context"]))

    def test_two_builds_differ_only_in_generated_at(self) -> None:
        spec = self._full_spec()
        first = build_contract_document(self.database, spec)
        second = build_contract_document(self.database, spec)
        self.assertEqual(first["provenance"]["generated_at"], first["provenance"]["generated_at"])
        first_copy = json.loads(json.dumps(first))
        second_copy = json.loads(json.dumps(second))
        first_copy["provenance"].pop("generated_at")
        second_copy["provenance"].pop("generated_at")
        self.assertEqual(first_copy, second_copy)

    def test_missing_section_heading_raises_valueerror(self) -> None:
        spec = self._full_spec(
            constraints=[
                {"note": "Projects/Alpha.md", "heading": "Does Not Exist"},
            ]
        )
        with self.assertRaises(ValueError):
            build_contract_document(self.database, spec)


if __name__ == "__main__":
    unittest.main()
