from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from contextlib import closing, redirect_stderr, redirect_stdout
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
        # The CLI's error contract is that stderr carries the JSON receipt and
        # nothing else, so these tests parse the whole stream. A ResourceWarning
        # emitted by an unrelated object being finalized mid-call would land in
        # the captured stream and break that parse, turning someone else's
        # resource leak into a failure here. Suppress warnings for the capture
        # window only: everything the CLI itself writes is still asserted.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
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

    def test_cli_malformed_persisted_evidence_exits_2_and_writes_nothing(self) -> None:
        # The fail-closed builder gate (recallweave-4su) must surface through
        # the CLI as the ordinary error contract: exit 2, a structured error on
        # stderr naming the offending connection, NOTHING on stdout, and no
        # artifact written. An operator must be able to see which edge is bad
        # and act on it, rather than receive a contract whose evidence this
        # project's own validator rejects.
        import sqlite3

        # Corrupt only the CANDIDATE edges. Writing candidate-shaped evidence
        # onto a verified edge would trip the edge-envelope gate first and this
        # test would stop exercising the evidence gate it is named for.
        with closing(sqlite3.connect(str(self.database))) as connection, connection:
            connection.execute(
                "UPDATE edges SET evidence_json = ? WHERE is_verified = 0",
                (
                    json.dumps(
                        {
                            "source_evidence": {"citation": "Projects/Alpha.md:1-2"},
                            "shared_terms": ["zephyr"],
                        }
                    ),
                ),
            )
        # Candidates must be in scope, or the corrupted edges are never
        # examined and the export succeeds for the wrong reason.
        spec_path = self.root / "malformed-spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "task_id": "cli-test",
                    "objective": "Explain the alpha-beta relationship.",
                    "retrieval": {
                        "query": "zephyr quadrata",
                        "limit": 8,
                        "include_candidates": True,
                        "max_characters": 5000,
                    },
                    "constraints": [],
                    "prior_decisions": [],
                    "acceptance_criteria": ["Citations resolve."],
                    "exclusions": {"paths": []},
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "contract-malformed.json"
        exit_code, out, err = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(out, "")
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertEqual(error["error"], "ValueError")
        self.assertIn("malformed connection evidence", error["message"])
        self.assertFalse(output.exists(), "no artifact may be written on failure")

    def test_cli_malformed_evidence_diagnostic_leaks_no_vault_paths(self) -> None:
        # FAIL-FIRST (recallweave-w3k). The fail-closed diagnostic added for
        # recallweave-4su named the offending connection by its vault-relative
        # endpoint PATHS, and the CLI serializes the message verbatim into the
        # structured stderr receipt. Vault-relative paths are vault-derived
        # metadata that can disclose people, health information, legal matters
        # and organizational structure — PRIVACY.md already treats them as
        # sensitive bundle content. Leaking them on the FAILURE path is worse
        # than on the success path: no bundle is produced at all, so the
        # operator consented to no disclosure whatsoever.
        #
        # The edge must still be identifiable, so the message carries the
        # edges table's primary key, which is not content-bearing.
        import sqlite3

        sentinel_source = "ZZSENSITIVESOURCE"
        sentinel_target = "ZZSENSITIVETARGET"
        self.write(
            f"People/{sentinel_source}.md",
            "# S\n\n## Background\n\nZephyr quadrata shared topic one.\n",
        )
        self.write(
            f"Legal/{sentinel_target}.md",
            "# T\n\n## Background\n\nZephyr quadrata shared topic two.\n",
        )
        database = self.root / "sentinel.sqlite"
        build_index(self.vault, database, minimum_candidate_score=0.0)
        # Corrupt ONLY the edges touching the sentinel notes, so the edge that
        # trips the gate is provably one whose endpoints carry the sentinels.
        # Corrupting every edge would let an unrelated edge fail first and make
        # this leak test vacuous.
        with closing(sqlite3.connect(str(database))) as connection, connection:
            corrupted = connection.execute(
                """
                UPDATE edges SET evidence_json = ?
                WHERE source_note_id IN (
                        SELECT id FROM notes WHERE relative_path LIKE ?
                      )
                   OR target_note_id IN (
                        SELECT id FROM notes WHERE relative_path LIKE ?
                      )
                """,
                (
                    json.dumps(
                        {
                            "source_evidence": {"citation": "x"},
                            "shared_terms": ["zephyr"],
                        }
                    ),
                    f"%{sentinel_source}%",
                    f"%{sentinel_target}%",
                ),
            ).rowcount
            self.assertGreater(
                corrupted, 0, "no edge touches the sentinel notes"
            )
        # The sentinel edges are candidates, so the spec must include them or
        # they never reach the validator and this test proves nothing.
        spec_path = self.root / "sentinel-spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "task_id": "sentinel",
                    "objective": "Explain the shared topic.",
                    "retrieval": {
                        "query": "zephyr quadrata",
                        "limit": 8,
                        "include_candidates": True,
                        "max_characters": 5000,
                    },
                    "constraints": [],
                    "prior_decisions": [],
                    "acceptance_criteria": ["Citations resolve."],
                    "exclusions": {"paths": []},
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "sentinel-contract.json"
        exit_code, out, err = self.run_cli(
            "contract",
            str(spec_path),
            "--database",
            str(database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        self.assertFalse(output.exists())
        for stream_name, stream in (("stdout", out), ("stderr", err)):
            for sentinel in (sentinel_source, sentinel_target):
                self.assertNotIn(
                    sentinel,
                    stream,
                    f"vault note path leaked into {stream_name}",
                )
        error = json.loads(err)
        self.assertIn("malformed connection evidence", error["message"])
        # Still actionable: the message names the edge by its database id.
        self.assertRegex(error["message"], r"edge \d+")

    def test_cli_refuses_a_destination_inside_the_vault(self) -> None:
        # Writing the artifact into the vault IS a write to the vault, yet both
        # the receipt and the embedded document assert `vault_writes: 0`. The
        # document is serialized before it is written, so it cannot describe its
        # own destination -- reporting 1 would leave the artifact carrying a
        # false claim about itself. Refusing keeps every existing claim true,
        # and matches how `index` already treats an in-vault database.
        output = self.vault / "contract.json"
        exit_code, out, err = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--vault",
            str(self.vault),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(out, "")
        error = json.loads(err)
        self.assertEqual(error["operation"], "contract")
        self.assertIn("inside the vault", error["message"])
        self.assertIn("vault_writes", error["message"])
        self.assertFalse(
            output.exists(), "nothing may be written to the refused destination"
        )

    def test_cli_allows_a_destination_outside_the_vault(self) -> None:
        # The refusal must be about the VAULT, not about writing at all.
        output = self.root / "outside-contract.json"
        exit_code, out, _ = self.run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--vault",
            str(self.vault),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(out)["vault_writes"], 0)

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
