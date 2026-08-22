from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recallweave.contract import CONTRACT_SCHEMA_VERSION, build_contract_document
from recallweave.contract_spec import TaskSpec
from recallweave.contract_text import MAX_STATEMENT_CHARACTERS
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
        kept = getattr(self, "_kept_tmp", [])
        for tmp in kept:
            tmp.cleanup()

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

    def _build_vault_index(self) -> tuple[Path, Path]:
        if not hasattr(self, "_kept_tmp"):
            self._kept_tmp = []
        temp = tempfile.TemporaryDirectory()
        self._kept_tmp.append(temp)
        root = Path(temp.name)
        vault = root / "vault"
        vault.mkdir()

        def write(relative_path: str, text: str) -> None:
            path = vault / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")

        write(
            "Alpha.md",
            "---\ntitle: Alpha\n---\n# Alpha\n\n## S\n\n"
            "zephyr"
            "\u202e"
            "quadrata"
            "\u200b"
            " quadrata"
            "\u0007"
            " shared topic alpha.\n",
        )
        write(
            "Beta.md",
            "---\ntitle: Beta\n---\n# Beta\n\n## S\n\n"
            "zephyr quadrata zephyr quadrata shared topic beta.\n",
        )
        write(
            "Gamma.md",
            "---\ntitle: Gamma\n---\n# Gamma\n\n## S\n\n"
            "zephyr quadrata zephyr quadrata shared topic gamma.\n",
        )
        database = root / "index.sqlite"
        build_index(vault, database, minimum_candidate_score=0.0)
        return vault, database

    def _evidence_spec(self, **retrieval_overrides) -> TaskSpec:
        retrieval = {
            "query": "zephyr",
            "limit": 8,
            "include_candidates": True,
            "max_characters": 5000,
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

    def test_connection_evidence_strips_control_and_bidi_characters(self) -> None:
        _vault, database = self._build_vault_index()
        document = build_contract_document(database, self._evidence_spec())
        self.assertTrue(document["connections"])
        for conn in document["connections"]:
            evidence = conn["evidence"]
            for side_name in ("source_evidence", "target_evidence"):
                side = evidence.get(side_name, {})
                for field in ("citation", "heading", "passage"):
                    value = side.get(field)
                    if value is not None:
                        self.assertNotIn("\u202e", value)
                        self.assertNotIn("\u200b", value)
                        self.assertNotIn("\u0007", value)

    def test_connection_evidence_whitelists_keys(self) -> None:
        import sqlite3

        _vault, database = self._build_vault_index()
        with sqlite3.connect(str(database)) as connection:
            rows = connection.execute(
                "SELECT id, evidence_json FROM edges"
            ).fetchall()
            for edge_id, raw in rows:
                evidence = json.loads(raw)
                evidence["evil_top"] = "drop me"
                evidence["source_evidence"]["evil_nested"] = "drop me too"
                connection.execute(
                    "UPDATE edges SET evidence_json = ? WHERE id = ?",
                    (json.dumps(evidence), edge_id),
                )
        document = build_contract_document(database, self._evidence_spec())
        self.assertTrue(document["connections"])
        for conn in document["connections"]:
            self.assertNotIn("evil_top", conn["evidence"])
            source_evidence = conn["evidence"].get("source_evidence", {})
            self.assertNotIn("evil_nested", source_evidence)

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
            for side_name in ("source_evidence", "target_evidence"):
                side = conn["evidence"].get(side_name, {})
                if side.get("passage") is not None:
                    total += len(side["passage"])
                if side.get("heading") is not None:
                    total += len(side["heading"])
        return total

    def test_budget_characters_used_covers_all_vault_derived_text(self) -> None:
        _vault, database = self._build_vault_index()
        document = build_contract_document(database, self._evidence_spec())

        self.assertGreaterEqual(
            document["budget"]["characters_used"], self._emitted_vault_strings(document)
        )
        self.assertLessEqual(
            document["budget"]["characters_used"], document["budget"]["character_budget"]
        )

    def test_budget_too_small_for_connections_truncates(self) -> None:
        _vault, database = self._build_vault_index()
        document = build_contract_document(
            database, self._evidence_spec(max_characters=60)
        )
        self.assertTrue(document["budget"]["truncated"])
        self.assertLessEqual(
            document["budget"]["characters_used"], document["budget"]["character_budget"]
        )
        emitted = self._emitted_vault_strings(document)
        self.assertLessEqual(emitted, document["budget"]["character_budget"])

    def test_handling_scope_matches_v2_replacement(self) -> None:
        _vault, database = self._build_vault_index()
        document = build_contract_document(database, self._evidence_spec())
        scope = document["handling"]["scope"]
        self.assertEqual(
            scope,
            "This bundle contains the context the operator selected for this task. "
            "It is a scoped projection of an index, not an authorization decision, "
            "and it does not certify that anything outside it is forbidden or that "
            "everything inside it is permitted.",
        )
        self.assertNotIn("authorized", scope)
        self.assertNotIn("complete", scope)

    def test_statement_truncation_sets_truncated_flag(self) -> None:
        long_statement = "x" * (MAX_STATEMENT_CHARACTERS + 50)
        spec = self._full_spec(
            constraints=[{"text": long_statement}],
            prior_decisions=[],
            acceptance_criteria=[],
        )
        document = build_contract_document(self.database, spec)
        authored = next(
            item
            for item in document["constraints"]
            if item["evidence_class"] == "authored_by_operator"
        )
        self.assertTrue(authored["truncated"])

    def test_exclusion_heavy_retrieval_returns_up_to_limit(self) -> None:
        if not hasattr(self, "_kept_tmp"):
            self._kept_tmp = []
        temp = tempfile.TemporaryDirectory()
        self._kept_tmp.append(temp)
        root = Path(temp.name)
        vault = root / "vault"
        vault.mkdir()

        def write(relative_path: str, text: str) -> None:
            path = vault / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="")

        for index in range(1, 5):
            body = " ".join(["zephyr quadrata"] * 10)
            write(
                f"Restricted/Ex{index}.md",
                f"---\ntitle: Ex{index}\ntags: [private]\n---\n# Ex{index}\n\n## S\n\n{body}\n",
            )
        write(
            "Keep1.md",
            "---\ntitle: Keep1\n---\n# Keep1\n\n## S\n\nzephyr quadrata here.\n",
        )
        write(
            "Keep2.md",
            "---\ntitle: Keep2\n---\n# Keep2\n\n## S\n\nzephyr quadrata there.\n",
        )
        database = root / "index.sqlite"
        build_index(vault, database, minimum_candidate_score=0.0)

        spec = TaskSpec.from_payload(
            {
                "objective": "test",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 2,
                    "include_candidates": False,
                    "max_characters": 2000,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {"paths": [], "globs": [], "tags": ["private"], "directives": []},
            }
        )
        document = build_contract_document(database, spec)
        kept_paths = [rc["relative_path"] for rc in document["retrieved_context"]]
        self.assertEqual(len(kept_paths), 2)
        self.assertEqual(sorted(kept_paths), ["Keep1.md", "Keep2.md"])


if __name__ == "__main__":
    unittest.main()
