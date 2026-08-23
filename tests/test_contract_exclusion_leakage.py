from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.contract import build_contract_document
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index

PATH_SENTINEL = "ZZQEXCLUDEDSENTINEL"
GLOB_SENTINEL = "ZZQGLOBSENTINEL"
TAG_SENTINEL = "ZZQTAGSENTINEL"


class ContractExclusionLeakageTest(unittest.TestCase):
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
        self.spec_path = self.root / "spec.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative_path: str, text: str) -> Path:
        path = self.vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _write_vault(self) -> None:
        self.write(
            "Included/Alpha.md",
            "---\n"
            "title: Alpha\n"
            "tags: [core]\n"
            "---\n"
            "# Alpha\n"
            "\n"
            "## Background\n"
            "\n"
            "Zephyr quadrata is the foundational core construct.\n",
        )
        self.write(
            "Included/Beta.md",
            "---\n"
            "title: Beta\n"
            "tags: [core]\n"
            "---\n"
            "# Beta\n"
            "\n"
            "## Notes\n"
            "\n"
            "Zephyr quadrata builds on Alpha. See [[Alpha]] and [[Secret]].\n",
        )
        self.write(
            "Restricted/Secret.md",
            "---\n"
            f"title: {PATH_SENTINEL}\n"
            "tags: [shadow]\n"
            "---\n"
            f"# {PATH_SENTINEL}\n"
            "\n"
            f"## {PATH_SENTINEL}\n"
            "\n"
            f"Zephyr quadrata {PATH_SENTINEL} secret body.\n",
        )
        self.write(
            "Private/Deep/Hidden.md",
            "---\n"
            f"title: {GLOB_SENTINEL}\n"
            "tags: [misc]\n"
            "---\n"
            f"# {GLOB_SENTINEL}\n"
            "\n"
            f"## {GLOB_SENTINEL}\n"
            "\n"
            f"Zephyr quadrata {GLOB_SENTINEL} hidden body.\n",
        )
        self.write(
            "Tagged/Private Note.md",
            "---\n"
            f"title: {TAG_SENTINEL}\n"
            "tags: [private]\n"
            "---\n"
            f"# {TAG_SENTINEL}\n"
            "\n"
            f"## {TAG_SENTINEL}\n"
            "\n"
            f"Zephyr quadrata {TAG_SENTINEL} tagged body.\n",
        )

    def _write_spec(self, exclusions: dict, *, constraints: list | None = None) -> Path:
        payload = {
            "task_id": "leak-test",
            "objective": "Summarize quadrata for a downstream agent.",
            "retrieval": {
                "query": "quadrata",
                "limit": 8,
                "max_characters": 5000,
            },
            "constraints": constraints
            if constraints is not None
            else [{"text": "Do not invent relationships."}],
            "prior_decisions": [],
            "acceptance_criteria": ["No excluded content leaks."],
            "exclusions": exclusions,
        }
        self.spec_path.write_text(json.dumps(payload), encoding="utf-8")
        return self.spec_path

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def export_both_formats(self, exclusions: dict, *, constraints: list | None = None):
        spec_path = self._write_spec(exclusions, constraints=constraints)
        json_out = self.root / "out.json"
        md_out = self.root / "out.md"
        json_exit, _, _ = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(json_out),
        )
        self.assertEqual(json_exit, 0)
        md_exit, _, _ = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(md_out),
            "--format",
            "markdown",
        )
        self.assertEqual(md_exit, 0)
        document = json.loads(json_out.read_text(encoding="utf-8"))
        markdown = md_out.read_bytes()
        return document, markdown, json_out

    def test_a_clean_selector_excludes_a_path_carrying_invisible_characters(self) -> None:
        # Matching is sanitized on BOTH sides. Without that, a note whose own
        # path carries a zero-width character could not be excluded by the
        # readable selector an operator would naturally write, and the note
        # would be emitted with its path shown clean -- looking exactly like
        # the path they thought they had excluded.
        import tempfile

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        vault = root / "vault"
        (vault / "Restricted").mkdir(parents=True)
        (vault / "Public.md").write_text(
            "---\ntitle: P\n---\n# P\n\n## S\n\nzephyr public body.\n",
            encoding="utf-8",
            newline="",
        )
        (vault / "Restricted" / "\u200bSecret.md").write_text(
            "---\ntitle: S\n---\n# S\n\n## S\n\nzephyr ZZSECRETBODY here.\n",
            encoding="utf-8",
            newline="",
        )
        database = root / "index.sqlite"
        build_index(vault, database, minimum_candidate_score=0.0)
        spec = TaskSpec.from_payload(
            {
                "objective": "invisible path",
                "retrieval": {
                    "query": "zephyr",
                    "limit": 8,
                    "include_candidates": True,
                    "max_characters": 9000,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {
                    "paths": ["Restricted/Secret.md"],
                    "globs": [],
                    "tags": [],
                    "directives": [],
                },
            }
        )
        document = build_contract_document(database, spec)
        blob = json.dumps(document)
        self.assertNotIn("ZZSECRETBODY", blob)
        self.assertNotIn("Secret.md", blob.replace("Restricted/Secret.md", ""))
        self.assertTrue(document["exclusions"]["enforced"])

    def test_path_exclusion_absent_from_both_formats(self) -> None:
        document, markdown, json_out = self.export_both_formats(
            {"paths": ["Restricted/Secret.md"]}
        )
        json_bytes = json_out.read_bytes()
        self.assertNotIn(PATH_SENTINEL.encode(), json_bytes)
        self.assertNotIn(PATH_SENTINEL.encode(), markdown)
        self.assertNotIn(
            "Restricted/Secret.md",
            [rc["relative_path"] for rc in document["retrieved_context"]],
        )
        self.assertNotIn(
            "Restricted/Secret.md", document["provenance"]["citations"]
        )
        for conn in document["connections"]:
            self.assertNotEqual(conn["source"], "Restricted/Secret.md")
            self.assertNotEqual(conn["target"], "Restricted/Secret.md")
        self.assertNotIn(
            "Restricted/Secret.md",
            [
                item["relative_path"]
                for item in document["constraints"] + document["prior_decisions"]
                if item["relative_path"] is not None
            ],
        )

    def test_glob_exclusion_absent_from_both_formats(self) -> None:
        document, markdown, json_out = self.export_both_formats(
            {"globs": ["Private/**"]}
        )
        json_bytes = json_out.read_bytes()
        self.assertNotIn(GLOB_SENTINEL.encode(), json_bytes)
        self.assertNotIn(GLOB_SENTINEL.encode(), markdown)
        self.assertNotIn(b"Private/Deep/Hidden.md", json_bytes)
        self.assertNotIn(b"Private/Deep/Hidden.md", markdown)
        self.assertNotIn(
            "Private/Deep/Hidden.md",
            [rc["relative_path"] for rc in document["retrieved_context"]],
        )
        self.assertNotIn(
            "Private/Deep/Hidden.md", document["provenance"]["citations"]
        )

    def test_tag_exclusion_absent_from_both_formats(self) -> None:
        document, markdown, json_out = self.export_both_formats(
            {"tags": ["private"]}
        )
        json_bytes = json_out.read_bytes()
        self.assertNotIn(TAG_SENTINEL.encode(), json_bytes)
        self.assertNotIn(TAG_SENTINEL.encode(), markdown)
        self.assertNotIn(b"Tagged/Private Note.md", json_bytes)
        self.assertNotIn(b"Tagged/Private Note.md", markdown)
        self.assertNotIn(
            "Tagged/Private Note.md",
            [rc["relative_path"] for rc in document["retrieved_context"]],
        )
        self.assertNotIn(
            "Tagged/Private Note.md", document["provenance"]["citations"]
        )
        self.assertNotIn("Tagged/Private Note.md", document["exclusions"]["paths"])
        self.assertIn("private", document["exclusions"]["tags"])

    def test_dropped_edge_is_suppressed_and_counted(self) -> None:
        document, markdown, _ = self.export_both_formats(
            {"paths": ["Restricted/Secret.md"]}
        )
        self.assertEqual(
            document["exclusions"]["suppressed"]["connections"], 1
        )
        for conn in document["connections"]:
            self.assertNotEqual(conn["source"], "Restricted/Secret.md")
            self.assertNotEqual(conn["target"], "Restricted/Secret.md")
        self.assertNotIn(PATH_SENTINEL.encode(), markdown)
        self.assertNotIn(
            "Restricted/Secret.md",
            [
                rc["citation"]
                for rc in document["retrieved_context"]
            ],
        )

    def test_suppressed_counts_are_exact(self) -> None:
        document, _, _ = self.export_both_formats(
            {
                "paths": ["Restricted/Secret.md"],
                "globs": ["Private/**"],
                "tags": ["private"],
            }
        )
        suppressed = document["exclusions"]["suppressed"]
        self.assertEqual(suppressed["retrieved_context"], 3)
        self.assertEqual(suppressed["notes"], 3)
        self.assertEqual(suppressed["connections"], 1)
        self.assertEqual(
            document["exclusions"]["suppressed"]["notes"],
            len(
                {
                    "Restricted/Secret.md",
                    "Private/Deep/Hidden.md",
                    "Tagged/Private Note.md",
                }
            ),
        )

    def test_selector_naming_excluded_note_fails_closed_leaves_no_artifact(self) -> None:
        spec_path = self._write_spec(
            {"paths": ["Restricted/Secret.md"]},
            constraints=[{"note": "Restricted/Secret.md"}],
        )
        output = self.root / "should-not-exist.json"
        exit_code, _, err = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertIn("Secret", error["message"])
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob("should-not-exist*")), [])


if __name__ == "__main__":
    unittest.main()
