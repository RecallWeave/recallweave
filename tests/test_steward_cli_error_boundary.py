from __future__ import annotations

"""Confirmation-round remediation: malformed on-disk journal/proposal
artifacts, and any unexpected internal exception, must surface as the single
path-redacted Steward JSON error envelope (exit 2) — never a raw traceback
and never an absolute local path."""

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave import cli
from recallweave.cli import main as cli_main
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_sources import SOURCES_SPEC_VERSION
from recallweave.steward_state import ensure_state_layout

from steward_fixtures import TempVault


_MALFORMED = {
    "array": "[]",
    "null": "null",
    "string": '"just a string"',
    "number": "42",
}


class StewardCliErrorBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.vault = TempVault(dir=self.base)
        self.vault.write("a.md", "hello")
        self.database = self.base / "index.sqlite"
        build_index(self.vault.root, self.database, policy=IndexPolicy())
        self.registry_path = self.base / "sources.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "spec_version": SOURCES_SPEC_VERSION,
                    "sources": [
                        {
                            "name": "src",
                            "type": "folder",
                            "root": str(self.vault.root),
                            "mode": "appliable",
                            "policy": {"include_paths": ["a.md"]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.state_root = self.base / "state"
        self.dirs = ensure_state_layout(self.state_root)
        self.policy_path = self.base / "wp.json"
        self.policy_path.write_text(
            json.dumps({"spec_version": "recallweave.steward.policy.v1"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.vault.cleanup()
        self.temporary.cleanup()

    def _run_apply(self, *extra: str):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(
                [
                    "steward-apply",
                    str(self.registry_path),
                    "--database",
                    str(self.database),
                    "--state-dir",
                    str(self.state_root),
                    "--write-policy",
                    str(self.policy_path),
                    *extra,
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def _assert_clean_envelope(self, code: int, out: str, err: str) -> None:
        self.assertEqual(code, 2, f"expected exit 2; stderr={err!r}")
        combined = out + err
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("Error: Traceback", combined)
        # Exactly one JSON object on stderr; stdout empty.
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertEqual(payload["operation"], "steward-apply")
        self.assertIn("schema_version", payload)
        self.assertIn("error", payload)
        self.assertIn("message", payload)
        # No absolute local path anywhere in the envelope.
        self.assertNotIn(str(self.base), err)
        self.assertNotIn("/Users/", err)

    # --- malformed journal shapes on a normal apply invocation ---

    def test_malformed_journal_shapes_yield_clean_envelope(self) -> None:
        journal_path = self.dirs["journal"] / "20260101T000000000000Z-x.json"
        for shape, text in _MALFORMED.items():
            journal_path.write_text(text, encoding="utf-8")
            with self.subTest(journal=shape):
                code, out, err = self._run_apply(
                    "--proposal-id", "prp-doesnotexist0", "--execute"
                )
                self._assert_clean_envelope(code, out, err)

    # --- malformed proposal shapes on a normal apply invocation ---

    def test_malformed_proposal_shapes_yield_clean_envelope(self) -> None:
        proposal_path = self.dirs["proposals"] / "20260101T000000000000Z-src-x.json"
        for shape, text in _MALFORMED.items():
            proposal_path.write_text(text, encoding="utf-8")
            with self.subTest(proposal=shape):
                code, out, err = self._run_apply(
                    "--proposal-id", "prp-doesnotexist0", "--execute"
                )
                self._assert_clean_envelope(code, out, err)

    def test_malformed_proposal_shapes_under_approve_class(self) -> None:
        proposal_path = self.dirs["proposals"] / "20260101T000000000000Z-src-y.json"
        for shape, text in _MALFORMED.items():
            proposal_path.write_text(text, encoding="utf-8")
            with self.subTest(proposal=shape):
                code, out, err = self._run_apply(
                    "--approve-class", "append_at_eof", "--execute"
                )
                self._assert_clean_envelope(code, out, err)

    def test_approve_class_partial_progress_surfaced_for_non_apply_error(self) -> None:
        # A later proposal raising a NON-ApplyError during --approve-class carries
        # partial-progress annotations; the CLI's generic exception envelope must
        # surface them (not only the OSError/ValueError branch), so the operator
        # learns earlier proposals already mutated the vault.
        def boom(*_args, **_kwargs):
            error = TypeError("malformed artifact tripped a non-ApplyError")
            error.partial_applied = [
                {
                    "proposal_id": "prp-appliedearlier",
                    "journal_ref": "j1.json",
                    "steward_vault_mutations": 1,
                }
            ]
            error.failed_proposal_id = "prp-laterfailed000"
            raise error

        with patch("recallweave.steward_apply.apply_latest", side_effect=boom):
            code, out, err = self._run_apply(
                "--approve-class", "append_at_eof", "--execute"
            )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertEqual(payload["error"], "TypeError")
        self.assertIn("partial_applied", payload)
        self.assertEqual(
            payload["partial_applied"][0]["proposal_id"], "prp-appliedearlier"
        )
        self.assertEqual(payload["failed_proposal_id"], "prp-laterfailed000")

    # --- structural CLI catch-all for an unexpected internal exception ---

    def test_unexpected_exception_is_redacted_by_cli_catch_all(self) -> None:
        sensitive_path = "/Users/josh/hidden/vault/private.md"

        def boom(*_args, **_kwargs):
            raise RuntimeError(f"unexpected failure touching {sensitive_path}")

        with patch.object(cli, "load_registry", side_effect=boom):
            code, out, err = self._run_apply(
                "--proposal-id", "prp-anything00000", "--execute"
            )
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", out + err)
        self.assertEqual(out, "")
        payload = json.loads(err)
        self.assertEqual(payload["error"], "RuntimeError")
        self.assertEqual(payload["operation"], "steward-apply")
        self.assertNotIn(sensitive_path, err)
        self.assertNotIn("/Users/", err)

    def test_control_flow_exceptions_are_not_swallowed(self) -> None:
        # KeyboardInterrupt is BaseException, not Exception, so the catch-all
        # must let it propagate rather than converting it to an envelope.
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt()

        with patch.object(cli, "load_registry", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self._run_apply("--proposal-id", "prp-anything00000", "--execute")

    def test_non_steward_command_exceptions_still_propagate(self) -> None:
        # The catch-all is steward-scoped: a non-steward command must keep its
        # original behavior (unexpected exceptions propagate).
        def boom(*_args, **_kwargs):
            raise RuntimeError("engine boom")

        with patch.object(cli, "context_packet", side_effect=boom):
            with self.assertRaises(RuntimeError):
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    cli_main(["query", "anything", "--database", str(self.database)])


if __name__ == "__main__":
    unittest.main()
