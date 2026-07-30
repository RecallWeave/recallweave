from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recallweave.index import build_index, connect
from recallweave.parser import parse_note
from recallweave.policy import IndexPolicy
from recallweave.query import connections, context_packet, path_between, resurface, stats


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "examples" / "synthetic-vault"


class RecallWeaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "index.sqlite"
        policy = IndexPolicy(
            deny_frontmatter={"sensitivity": ["sealed"]},
        )
        self.receipt = build_index(VAULT, self.database, policy=policy, minimum_candidate_score=0.08)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parser_preserves_evidence_locations(self) -> None:
        note = parse_note(VAULT / "Projects" / "Growth Atlas.md", VAULT)
        self.assertEqual(note.title, "Growth Atlas")
        self.assertEqual(note.status, "canonical")
        self.assertTrue(any(link.target == "Decision Memory" for link in note.links))
        self.assertTrue(all(section.line_start <= section.line_end for section in note.sections))

    def test_index_is_local_read_only_and_policy_aware(self) -> None:
        self.assertEqual(self.receipt["notes_indexed"], 6)
        self.assertEqual(self.receipt["vault_writes"], 0)
        self.assertEqual(self.receipt["network_calls"], 0)
        self.assertEqual(self.receipt["skipped"]["denied_frontmatter:sensitivity"], 1)
        self.assertEqual(stats(self.database)["notes"], 6)

    def test_exact_include_paths_fail_closed(self) -> None:
        database = Path(self.temporary.name) / "allowlisted.sqlite"
        receipt = build_index(
            VAULT,
            database,
            policy=IndexPolicy(include_paths=["Projects/Growth Atlas.md"]),
        )
        self.assertEqual(receipt["notes_indexed"], 1)
        self.assertEqual(receipt["skipped"]["not_allowlisted"], 6)

    def test_verified_and_candidate_edges_are_separate(self) -> None:
        with connect(self.database, readonly=True) as connection:
            verified = connection.execute(
                "SELECT COUNT(*) FROM edges WHERE is_verified = 1"
            ).fetchone()[0]
            candidates = connection.execute(
                "SELECT COUNT(*) FROM edges WHERE is_verified = 0"
            ).fetchone()[0]
        self.assertGreater(verified, 0)
        self.assertGreater(candidates, 0)
        result = connections(self.database, "Whiteboard Fragment")
        candidate = next(item for item in result["connections"] if not item["verified"])
        self.assertIn("shared_terms", candidate["evidence"])
        self.assertIn("citation", candidate["evidence"]["source_evidence"])
        self.assertIn("citation", candidate["evidence"]["target_evidence"])
        self.assertIn("Candidate only", candidate["evidence"]["explanation"])

    def test_query_is_bounded_and_cited(self) -> None:
        result = context_packet(
            self.database,
            "binding constraint reversible experiments",
            max_characters=240,
        )
        self.assertTrue(result["passages"])
        self.assertLessEqual(result["characters_used"], 240)
        self.assertEqual(result["citations"][0], result["passages"][0]["citation"])
        self.assertTrue(all(item["verified"] for item in result["connections"]))

    def test_resurface_finds_old_thinking(self) -> None:
        result = resurface(
            self.database,
            "binding constraint reversible experiment weekly evidence",
            minimum_age_days=365,
        )
        paths = {item["relative_path"] for item in result["results"]}
        self.assertIn("Archive/Whiteboard Fragment.md", paths)
        self.assertTrue(all("why" in item for item in result["results"]))

    def test_verified_path_does_not_need_candidates(self) -> None:
        result = path_between(self.database, "Growth Atlas", "System Maps")
        self.assertTrue(result["found"])
        self.assertTrue(all(step["verified"] for step in result["steps"]))


if __name__ == "__main__":
    unittest.main()
