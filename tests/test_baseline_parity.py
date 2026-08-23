from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave.cli import main as cli_main
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.viewer import export_viewer_graph


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "examples" / "synthetic-vault"


def _decode_single_json(text: str) -> dict:
    """Assert text is exactly one JSON value (one emitted object) and return it."""
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    value, end = decoder.raw_decode(stripped)
    if stripped[end:].strip():
        raise AssertionError("more than one JSON value emitted on stdout")
    return value


def _run_cli(*args: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli_main(list(args))
    return exit_code, stdout.getvalue(), stderr.getvalue()


class ViewerMessageParityTest(unittest.TestCase):
    """Every exception reachable through export_viewer_graph is byte-identical to
    00d8fe7 (the pre-extraction viewer), and no route discloses an absolute path
    that 00d8fe7 did not. Reverting any one of the remediation message fixes
    makes this class fail."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.root = Path(self.temporary.name)
        self.database = self.root / "index.sqlite"
        build_index(
            VAULT,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
            minimum_candidate_score=0.08,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_samefile_database_message_unchanged(self) -> None:
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, self.database, force=True)
        self.assertEqual(
            str(raised.exception),
            "Viewer output cannot replace the RecallWeave database.",
        )

    def test_hardlink_database_message_unchanged(self) -> None:
        output = self.root / "database-alias.json"
        try:
            os.link(self.database, output)
        except OSError as error:
            self.skipTest(f"hardlink creation unavailable: {error}")
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, output, force=True)
        self.assertEqual(
            str(raised.exception),
            "Viewer output cannot replace the RecallWeave database.",
        )

    def test_symlink_target_message_unchanged(self) -> None:
        target = self.root / "elsewhere" / "graph.json"
        output = self.root / "symlink.json"
        try:
            output.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, output)
        self.assertEqual(
            str(raised.exception),
            f"Refusing to replace a symlink or junction: {output}",
        )

    def test_symlinked_parent_message_unchanged(self) -> None:
        real_parent = self.root / "real"
        real_parent.mkdir()
        linked_parent = self.root / "linked"
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlink creation unavailable: {error}")
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, linked_parent / "graph.json")
        self.assertEqual(
            str(raised.exception),
            f"Refusing viewer output through a symlinked parent: {linked_parent}",
        )

    def test_parent_not_directory_message_unchanged(self) -> None:
        parent = self.root / "not-a-directory"
        parent.write_text("ordinary file", encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, parent / "graph.json")
        self.assertEqual(
            str(raised.exception),
            f"Viewer output parent is not a directory: {parent}",
        )

    def test_exists_without_force_message_unchanged(self) -> None:
        output = self.root / "graph.json"
        output.write_text("existing", encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            export_viewer_graph(self.database, output)
        self.assertEqual(
            str(raised.exception),
            f"Viewer output already exists: {output}. Pass --force to replace it.",
        )

    def test_install_failure_restored_message_unchanged(self) -> None:
        output = self.root / "graph.json"
        output.write_text("approved old output", encoding="utf-8")
        original_install = __import__(
            "recallweave.viewer", fromlist=["_install_non_replacing"]
        )._install_non_replacing
        failed = False

        def fail_new_install(source: Path, destination: Path) -> None:
            nonlocal failed
            if not failed and source.suffix == ".tmp":
                failed = True
                raise OSError("injected installation failure")
            original_install(source, destination)

        with patch(
            "recallweave.viewer._install_non_replacing",
            side_effect=fail_new_install,
        ):
            with self.assertRaises(ValueError) as raised:
                export_viewer_graph(self.database, output, force=True)
        self.assertEqual(
            str(raised.exception),
            "Viewer export installation failed; the previous output was restored.",
        )

    def test_install_failure_retained_message_unchanged(self) -> None:
        output = self.root / "graph.json"
        output.write_text("approved old output", encoding="utf-8")
        original_install = __import__(
            "recallweave.viewer", fromlist=["_install_non_replacing"]
        )._install_non_replacing
        injected = False

        def block_install_and_restore(source: Path, destination: Path) -> None:
            nonlocal injected
            if not injected and source.suffix == ".tmp":
                injected = True
                destination.write_text("late competing file", encoding="utf-8")
                raise OSError("injected installation failure")
            original_install(source, destination)

        with patch(
            "recallweave.viewer._install_non_replacing",
            side_effect=block_install_and_restore,
        ):
            with self.assertRaises(ValueError) as raised:
                export_viewer_graph(self.database, output, force=True)
        message = str(raised.exception)
        self.assertTrue(
            message.startswith(
                "Viewer export installation failed and the previous output could not "
                "be restored without overwriting another file. Backup retained at: "
            ),
            message,
        )
        self.assertFalse(message.startswith("Viewer output installation failed"))


class CliEnvelopeParityTest(unittest.TestCase):
    """The CLI error envelope and per-route stdout object count are unchanged from
    00d8fe7, and the new contract command follows the same single-object contract
    (exactly one JSON object on stdout; errors on stderr with exit 2)."""

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
                    "task_id": "parity-test",
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

    def test_missing_database_error_envelope_unchanged(self) -> None:
        missing = self.root / "missing.sqlite"
        exit_code, out, err = _run_cli("stats", "--database", str(missing))
        self.assertEqual(exit_code, 2)
        self.assertEqual(out, "")
        envelope = json.loads(err)
        self.assertEqual(envelope["error"], "ValueError")
        self.assertEqual(envelope["operation"], "stats")
        self.assertIn("RecallWeave database not found", envelope["message"])

    def test_contract_success_emits_exactly_one_json_object(self) -> None:
        exit_code, out, err = _run_cli(
            "contract", str(self.spec_path), "--database", str(self.database)
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        receipt = _decode_single_json(out)
        self.assertEqual(receipt["operation"], "export_contract")

    def test_contract_success_to_file_emits_exactly_one_receipt_object(self) -> None:
        output = self.root / "contract.json"
        exit_code, out, err = _run_cli(
            "contract",
            str(self.spec_path),
            "--database",
            str(self.database),
            "--output",
            str(output),
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(err, "")
        receipt = _decode_single_json(out)
        self.assertEqual(receipt["operation"], "export_contract")
        self.assertTrue(output.exists())

    def test_contract_error_emits_nothing_on_stdout(self) -> None:
        missing = self.root / "missing.sqlite"
        exit_code, out, err = _run_cli(
            "contract", str(self.spec_path), "--database", str(missing)
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(out, "")
        envelope = json.loads(err)
        self.assertEqual(envelope["operation"], "contract")
        self.assertEqual(envelope["error"], "ValueError")


if __name__ == "__main__":
    unittest.main()
