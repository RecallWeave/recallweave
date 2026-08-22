from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.contract import CONTRACT_SCHEMA_VERSION, build_contract_document
from recallweave.contract_spec import TaskSpec
from recallweave.contract_text import sanitize
from recallweave.index import build_index, connect
from recallweave.query import stats

_TRUNCATION_MARKER = "\u2026"


class ContractBoundsTest(unittest.TestCase):
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
            "Decision Log.md",
            "# Decision Log\n"
            "\n"
            "## Decision\n"
            "\n"
            "We chose option two for the zephyr system. This is the recorded decision.\n",
        )

    def _base_spec(self, **overrides) -> TaskSpec:
        payload = {
            "task_id": "bounds-task",
            "objective": "Summarize the alpha-beta relationship.",
            "retrieval": {
                "query": "zephyr quadrata",
                "limit": 8,
                "include_candidates": True,
                "max_characters": 8000,
            },
            "constraints": [
                {"text": "Do not invent relationships."},
                {"note": "Projects/Alpha.md", "heading": "Background"},
            ],
            "prior_decisions": [],
            "acceptance_criteria": ["All citations resolve to physical lines."],
            "exclusions": {},
        }
        payload.update(overrides)
        return TaskSpec.from_payload(payload)

    def _minimal_spec(self, max_characters: int) -> TaskSpec:
        return TaskSpec.from_payload(
            {
                "objective": "x",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": max_characters,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {},
            }
        )

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(list(args))
        return exit_code, stderr.getvalue()

    def _write_spec(self, payload) -> Path:
        path = self.root / "spec.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_characters_used_never_exceeds_budget_many_values(self) -> None:
        for budget in (1, 2, 5, 10, 30, 60, 100, 500, 2000, 8000):
            document = build_contract_document(self.database, self._minimal_spec(budget))
            self.assertLessEqual(
                document["budget"]["characters_used"],
                document["budget"]["character_budget"],
                f"budget {budget}",
            )

    def test_truncation_boundary_marks_item_and_budget(self) -> None:
        spec = TaskSpec.from_payload(
            {
                "objective": "x",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 12,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {},
            }
        )
        document = build_contract_document(self.database, spec)
        self.assertTrue(document["budget"]["truncated"])
        self.assertTrue(any(item["truncated"] for item in document["retrieved_context"]))
        self.assertLessEqual(
            document["budget"]["characters_used"],
            document["budget"]["character_budget"],
        )

    def test_operator_text_exceeding_budget_raises_actionable(self) -> None:
        spec = TaskSpec.from_payload(
            {
                "objective": "a" * 20,
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 5,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {},
            }
        )
        with self.assertRaisesRegex(ValueError, "exceeds the character budget"):
            build_contract_document(self.database, spec)

    def test_operator_text_exceeding_budget_exits_2_via_cli(self) -> None:
        spec_path = self._write_spec(
            {
                "objective": "a" * 20,
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 5,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {},
            }
        )
        exit_code, err = self.run_cli(
            "contract", str(spec_path), "--database", str(self.database)
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertIn("exceeds the character budget", error["message"])

    def test_two_builds_byte_identical_after_generated_at(self) -> None:
        spec = self._base_spec()
        first = build_contract_document(self.database, spec)
        second = build_contract_document(self.database, spec)
        first["provenance"].pop("generated_at")
        second["provenance"].pop("generated_at")
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_cli_two_exports_byte_identical_after_generated_at(self) -> None:
        spec_path = self._write_spec(
            {
                "task_id": "bounds-task",
                "objective": "Summarize the alpha-beta relationship.",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 8000,
                },
                "constraints": [
                    {"text": "Do not invent relationships."},
                    {"note": "Projects/Alpha.md", "heading": "Background"},
                ],
                "prior_decisions": [],
                "acceptance_criteria": ["Citations resolve."],
                "exclusions": {},
            }
        )
        first = self.root / "first.json"
        second = self.root / "second.json"
        exit_one, _ = self.run_cli(
            "contract", str(spec_path), "--database", str(self.database), "--output", str(first)
        )
        exit_two, _ = self.run_cli(
            "contract", str(spec_path), "--database", str(self.database), "--output", str(second)
        )
        self.assertEqual((exit_one, exit_two), (0, 0))
        first_doc = json.loads(first.read_text(encoding="utf-8"))
        second_doc = json.loads(second.read_text(encoding="utf-8"))
        first_doc["provenance"].pop("generated_at")
        second_doc["provenance"].pop("generated_at")
        self.assertEqual(json.dumps(first_doc), json.dumps(second_doc))

    def test_every_citation_resolves_to_physical_lines(self) -> None:
        spec = self._base_spec()
        document = build_contract_document(self.database, spec)
        cited_items = [
            item
            for item in document["constraints"] + document["prior_decisions"]
            if item["citation"] is not None
        ] + document["retrieved_context"]
        self.assertTrue(cited_items)
        for item in cited_items:
            path, line_range = item["citation"].rsplit(":", 1)
            start, end = (int(part) for part in line_range.split("-"))
            source = self.vault / path
            self.assertTrue(source.is_file(), item["citation"])
            physical_lines = source.read_text(encoding="utf-8").split("\n")
            self.assertGreaterEqual(start, 1)
            self.assertGreaterEqual(end, start)
            self.assertLessEqual(end, len(physical_lines))
            recomputed = sanitize("\n".join(physical_lines[start - 1 : end]))
            passage = item["passage"] or ""
            if item["truncated"] and passage.endswith(_TRUNCATION_MARKER):
                plain = passage[: -len(_TRUNCATION_MARKER)]
            else:
                plain = passage
            self.assertTrue(
                recomputed.startswith(plain),
                f"citation {item['citation']!r} does not resolve to its passage",
            )

    def test_provenance_index_matches_stats(self) -> None:
        document = build_contract_document(self.database, self._base_spec())
        index_prov = document["provenance"]["index"]
        stat = stats(self.database)
        self.assertEqual(index_prov["schema_version"], "2")
        self.assertEqual(index_prov["indexed_at"], stat["indexed_at"])
        self.assertEqual(index_prov["notes"], stat["notes"])
        self.assertEqual(index_prov["sections"], stat["sections"])

    def test_stale_index_reports_indexed_at_honestly(self) -> None:
        with connect(self.database, readonly=True) as connection:
            original_indexed_at = connection.execute(
                "SELECT value FROM meta WHERE key = 'indexed_at'"
            ).fetchone()["value"]
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
            "NEW STALE CONTENT NOT YET INDEXED.\n",
        )
        document = build_contract_document(self.database, self._base_spec())
        self.assertEqual(
            document["provenance"]["index"]["indexed_at"], original_indexed_at
        )

    def test_spec_abuse_exits_2_with_json_error(self) -> None:
        cases = {
            "oversized_constraints": {
                "objective": "x",
                "constraints": [{"text": "c"} for _ in range(51)],
            },
            "oversized_criteria": {
                "objective": "x",
                "acceptance_criteria": ["c"] * 51,
            },
            "oversized_exclusion_paths": {
                "objective": "x",
                "exclusions": {"paths": ["p"] * 201},
            },
            "deeply_nested_junk": {
                "objective": "x",
                "acceptance_criteria": [["nested"]],
            },
            "non_object_spec": [],
            "empty_objective": {"objective": ""},
            "unknown_keys": {"objective": "x", "bogus": 1},
            "both_text_and_note": {
                "objective": "x",
                "constraints": [{"text": "a", "note": "b"}],
            },
        }
        for name, payload in cases.items():
            with self.subTest(name):
                spec_path = self._write_spec(payload)
                exit_code, err = self.run_cli(
                    "contract", str(spec_path), "--database", str(self.database)
                )
                self.assertEqual(exit_code, 2, name)
                error = json.loads(err)
                self.assertEqual(error["operation"], "contract")
                self.assertIn("message", error)

    def test_vault_immutability_and_zero_side_effects(self) -> None:
        spec_path = self._write_spec(
            {
                "task_id": "bounds-task",
                "objective": "Summarize the alpha-beta relationship.",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 8000,
                },
                "constraints": [{"text": "Do not invent relationships."}],
                "prior_decisions": [],
                "acceptance_criteria": ["Citations resolve."],
                "exclusions": {},
            }
        )
        output = self.root / "out" / "contract.json"
        before = self._fingerprint(self.vault)
        exit_code, _ = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 0)
        after = self._fingerprint(self.vault)
        self.assertEqual(before, after)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["provenance"]["vault_writes"], 0)
        self.assertEqual(document["provenance"]["network_calls"], 0)

    def test_receipt_reports_zero_writes_and_calls(self) -> None:
        spec_path = self._write_spec(
            {
                "task_id": "bounds-task",
                "objective": "Summarize the alpha-beta relationship.",
                "retrieval": {
                    "query": "zephyr quadrata",
                    "limit": 8,
                    "max_characters": 8000,
                },
                "constraints": [{"text": "Do not invent relationships."}],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {},
            }
        )
        output = self.root / "receipt.json"
        exit_code, _ = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 0)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], CONTRACT_SCHEMA_VERSION)
        self.assertEqual(document["provenance"]["vault_writes"], 0)
        self.assertEqual(document["provenance"]["network_calls"], 0)
        self.assertTrue(document["provenance"]["generated_locally"])

    @staticmethod
    def _fingerprint(directory: Path) -> dict[str, tuple[int, int]]:
        result: dict[str, tuple[int, int]] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                stat = path.stat()
                result[str(path.relative_to(directory))] = (stat.st_mtime_ns, stat.st_size)
        return result


if __name__ == "__main__":
    unittest.main()
