from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.contract_export import export_contract
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index


class ContractCliTest(unittest.TestCase):
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
        self.spec_path.write_text(
            json.dumps(
                {
                    "task_id": "cli-test",
                    "objective": "Explain the alpha-beta relationship.",
                    "retrieval": {
                        "query": "zephyr quadrata",
                        "limit": 8,
                        "max_characters": 5000,
                    },
                    "constraints": [
                        {"text": "Do not invent relationships."},
                        {"note": "Projects/Alpha.md"},
                    ],
                    "prior_decisions": [],
                    "acceptance_criteria": ["Citations resolve."],
                    "exclusions": {"paths": ["Restricted/Secret.md"]},
                }
            ),
            encoding="utf-8",
        )

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

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_cli_no_output_emits_document(self) -> None:
        exit_code, out, _ = self.run_cli(
            "contract", str(self.spec_path), "--database", str(self.database)
        )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["operation"], "export_contract")
        self.assertIsNone(receipt["output"])
        self.assertIsNone(receipt["replacement_mode"])
        self.assertEqual(
            receipt["contract"]["schema_version"], "recallweave.contract.v1"
        )
        self.assertEqual(receipt["contract"]["task"]["id"], "cli-test")

    def test_cli_markdown_no_output_returns_rendered_text(self) -> None:
        exit_code, out, _ = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--format",
            "markdown",
        )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(out)
        self.assertIn("markdown", receipt)
        self.assertTrue(receipt["markdown"].startswith("# Task contract"))
        self.assertIn("## 1. Objective", receipt["markdown"])

    def test_cli_output_writes_file_non_replacing(self) -> None:
        output = self.root / "out" / "contract.json"
        exit_code, out, _ = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["output"], str(output.resolve()))
        self.assertEqual(receipt["replacement_mode"], "non_replacing")
        self.assertIsNone(receipt["replacement_backup"])
        self.assertNotIn("contract", receipt)
        written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(written["schema_version"], "recallweave.contract.v1")

    def test_cli_existing_output_without_force_exits_2(self) -> None:
        output = self.root / "contract.json"
        output.write_text("old", encoding="utf-8")
        exit_code, _, err = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertIn("already exists", error["message"])
        self.assertIn(str(output), error["message"])
        self.assertEqual(output.read_text(encoding="utf-8"), "old")

    def test_cli_force_two_phase_replacement_retains_backup(self) -> None:
        output = self.root / "contract.json"
        output.write_text("approved old output", encoding="utf-8")
        exit_code, out, _ = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
            "--force",
        )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(out)
        self.assertEqual(receipt["replacement_mode"], "two_phase_recoverable")
        backup = Path(receipt["replacement_backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "approved old output")
        self.assertNotEqual(output.read_text(encoding="utf-8"), "approved old output")

    def test_cli_refuses_symlinked_destination(self) -> None:
        target = self.root / "elsewhere" / "contract.json"
        output = self.root / "dangling.json"
        try:
            output.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        exit_code, _, err = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertIn("symlink or junction", error["message"])

    def test_cli_refuses_database_as_destination(self) -> None:
        exit_code, _, err = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(self.database),
            "--force",
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertIn("cannot replace", error["message"])

    def test_cli_invalid_spec_exits_2_and_writes_no_file(self) -> None:
        output = self.root / "should-not-exist.json"
        bad_spec = self.root / "bad.json"
        bad_spec.write_text(json.dumps({"objective": 42}), encoding="utf-8")
        exit_code, _, err = self.run_cli(
            "contract",
            str(bad_spec),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertIn("objective", error["message"])
        self.assertFalse(output.exists())

    def test_cli_missing_spec_file_exits_2(self) -> None:
        missing = self.root / "missing.json"
        exit_code, _, err = self.run_cli(
            "contract", str(missing), "--database", str(self.database)
        )
        self.assertEqual(exit_code, 2)
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")

    def test_export_contract_direct_api(self) -> None:
        spec = TaskSpec.from_file(self.spec_path)
        output = self.root / "api.json"
        receipt = export_contract(self.database, spec, output)
        self.assertEqual(receipt["replacement_mode"], "non_replacing")
        self.assertEqual(receipt["output"], str(output.resolve()))
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "recallweave.contract.v1")

    def test_export_contract_markdown_direct_api(self) -> None:
        spec = TaskSpec.from_file(self.spec_path)
        output = self.root / "api.md"
        receipt = export_contract(
            self.database, spec, output, output_format="markdown"
        )
        self.assertEqual(receipt["format"], "markdown")
        self.assertEqual(receipt["replacement_mode"], "non_replacing")
        self.assertTrue(output.read_text(encoding="utf-8").startswith("# Task contract"))

    def test_export_contract_no_output_carries_document(self) -> None:
        spec = TaskSpec.from_file(self.spec_path)
        receipt = export_contract(self.database, spec, None)
        self.assertIsNone(receipt["output"])
        self.assertIsNone(receipt["replacement_mode"])
        self.assertIn("contract", receipt)
        self.assertEqual(receipt["contract"]["schema_version"], "recallweave.contract.v1")


if __name__ == "__main__":
    unittest.main()
