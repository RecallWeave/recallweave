from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recallweave.contract_provenance import index_provenance
from recallweave.index import build_index, connect
from recallweave.policy import IndexPolicy
from recallweave.query import stats


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "examples" / "synthetic-vault"


class ContractProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "index.sqlite"
        policy = IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]})
        self.receipt = build_index(
            VAULT, self.database, policy=policy, minimum_candidate_score=0.08
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_provenance_matches_stats(self) -> None:
        with connect(self.database, readonly=True) as connection:
            provenance = index_provenance(connection)
        expected = stats(self.database)
        self.assertEqual(
            provenance,
            {
                "schema_version": expected["schema_version"],
                "indexed_at": expected["indexed_at"],
                "notes": expected["notes"],
                "sections": expected["sections"],
            },
        )

    def test_provenance_has_exactly_four_keys_and_no_path(self) -> None:
        with connect(self.database, readonly=True) as connection:
            provenance = index_provenance(connection)
        self.assertEqual(set(provenance), {"schema_version", "indexed_at", "notes", "sections"})
        self.assertNotIn("path", provenance)
        self.assertNotIn("database", provenance)
        self.assertEqual(provenance["notes"], self.receipt["notes_indexed"])
        self.assertEqual(provenance["sections"], self.receipt["sections_indexed"])

    def test_missing_meta_row_raises_naming_the_key(self) -> None:
        with connect(self.database) as connection:
            connection.execute("DELETE FROM meta WHERE key = 'schema_version'")
            connection.commit()
        with connect(self.database, readonly=True) as connection:
            with self.assertRaises(ValueError) as ctx:
                index_provenance(connection)
        self.assertIn("schema_version", str(ctx.exception))
