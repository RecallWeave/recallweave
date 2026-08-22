from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.query import connections, context_packet, doctor, path_between, resurface, stats
from recallweave.viewer import VIEWER_SCHEMA_VERSION, export_viewer_graph


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "examples" / "synthetic-vault"

CORE_SCHEMA_VERSION = "2"

DOCUMENTED_KEYS = {
    "index": {
        "database",
        "notes_indexed",
        "sections_indexed",
        "note_tags",
        "verified_edges",
        "candidate_edges",
        "unresolved_links",
        "discovery",
        "skipped",
        "network_calls",
        "vault_writes",
    },
    "query": {
        "passages",
        "connections",
        "connections_total",
        "connections_returned",
        "connections_truncated",
        "characters_used",
        "character_budget",
        "citations",
    },
    "connections": {
        "source",
        "connections",
        "connections_total",
        "connections_returned",
        "connections_truncated",
    },
    "resurface": {"results"},
    "path": {"found", "steps"},
    "doctor": {"unresolved_total", "unresolved"},
    "stats": {
        "notes",
        "sections",
        "verified_edges",
        "candidate_edges",
        "note_tags",
        "unresolved_links",
        "indexed_at",
        "discovery",
    },
}

DOCUMENTED_OPERATIONS = {
    "index": "index",
    "query": "query",
    "connections": "connections",
    "resurface": "resurface",
    "path": "path",
    "doctor": "doctor",
    "stats": "stats",
}

EXPORT_VIEWER_RECEIPT_KEYS = {
    "schema_version",
    "operation",
    "output",
    "notes",
    "edges",
    "candidate_edges_requested",
    "candidate_edges_included",
    "replacement_mode",
    "replacement_backup",
    "export_profile",
    "requested_profile",
    "metadata_only",
    "excerpts_requested",
    "excerpts_included",
    "passage_text_included",
    "note_derived_terms_included",
    "paths_titles_tags_included",
}

CLI_SUBCOMMANDS = {
    "index",
    "query",
    "connections",
    "resurface",
    "path",
    "stats",
    "doctor",
    "export-viewer",
}


class PublicSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.database = Path(self.temporary.name) / "index.sqlite"
        self.index_receipt = build_index(
            VAULT,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
            minimum_candidate_score=0.08,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_documented_fields_present(self, receipt: dict, documented: set) -> None:
        self.assertTrue(documented <= set(receipt), set(documented) - set(receipt))

    def test_index_receipt_carries_schema_and_documented_fields(self) -> None:
        self.assertEqual(self.index_receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(self.index_receipt["operation"], "index")
        self._assert_documented_fields_present(self.index_receipt, DOCUMENTED_KEYS["index"])

    def test_query_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = context_packet(
            self.database,
            "binding constraint reversible experiments",
            max_characters=240,
        )
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "query")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["query"])

    def test_connections_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = connections(self.database, "Whiteboard Fragment")
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "connections")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["connections"])

    def test_resurface_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = resurface(
            self.database,
            "binding constraint reversible experiment weekly evidence",
            minimum_age_days=365,
        )
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "resurface")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["resurface"])

    def test_path_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = path_between(self.database, "Growth Atlas", "System Maps")
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "path")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["path"])

    def test_doctor_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = doctor(self.database, limit=100)
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "doctor")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["doctor"])

    def test_stats_receipt_carries_schema_and_documented_fields(self) -> None:
        receipt = stats(self.database)
        self.assertEqual(receipt["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "stats")
        self._assert_documented_fields_present(receipt, DOCUMENTED_KEYS["stats"])

    def test_every_core_operation_is_documented(self) -> None:
        self.assertEqual(
            set(DOCUMENTED_OPERATIONS),
            {
                "index",
                "query",
                "connections",
                "resurface",
                "path",
                "doctor",
                "stats",
            },
        )

    def test_export_viewer_receipt_uses_viewer_schema_and_documented_keys(self) -> None:
        output = Path(self.temporary.name) / "graph.json"
        receipt = export_viewer_graph(self.database, output)
        self.assertEqual(receipt["schema_version"], VIEWER_SCHEMA_VERSION)
        self.assertEqual(receipt["operation"], "export_viewer")
        self._assert_documented_fields_present(receipt, EXPORT_VIEWER_RECEIPT_KEYS)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], VIEWER_SCHEMA_VERSION)

    def test_cli_help_lists_every_public_subcommand(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                cli_main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        for subcommand in CLI_SUBCOMMANDS:
            self.assertIn(subcommand, output.getvalue())

    def test_missing_database_exits_two_with_json_error_on_stderr(self) -> None:
        missing = Path(self.temporary.name) / "missing.sqlite"
        error = StringIO()
        with redirect_stderr(error):
            exit_code = cli_main(["stats", "--database", str(missing)])
        self.assertEqual(exit_code, 2)
        payload = json.loads(error.getvalue())
        self.assertEqual(payload["schema_version"], CORE_SCHEMA_VERSION)
        self.assertEqual(payload["error"], "ValueError")
        self.assertEqual(payload["operation"], "stats")
        self.assertIn("RecallWeave database not found", payload["message"])


if __name__ == "__main__":
    unittest.main()
