from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from recallweave.contract import (
    INDEX_CANDIDATE_EXPLANATION,
    INDEX_CANDIDATE_METHOD,
    _stream_connection_edges,
    build_contract_document,
)
from recallweave.contract_exclusions import ExclusionSet
from recallweave.contract_markdown import render_contract_markdown
from recallweave.contract_spec import TaskSpec
from recallweave.index import build_index, connect


class _FakeSpec:
    def __init__(
        self,
        exclusion_paths,
        exclusion_globs,
        exclusion_tags,
        exclusion_directives,
    ):
        self.exclusion_paths = exclusion_paths
        self.exclusion_globs = exclusion_globs
        self.exclusion_tags = exclusion_tags
        self.exclusion_directives = exclusion_directives


class FromSpecTest(unittest.TestCase):
    def test_copies_all_four_lists(self):
        spec = _FakeSpec(
            exclusion_paths=["Restricted/Sealed.md"],
            exclusion_globs=["Restricted/**"],
            exclusion_tags=["private"],
            exclusion_directives=["Do not infer client identity."],
        )
        ex = ExclusionSet.from_spec(spec)
        self.assertEqual(ex.paths, ["restricted/sealed.md"])
        self.assertEqual(ex.globs, ["restricted/**"])
        self.assertEqual(ex.tags, ["private"])
        self.assertEqual(ex.directives, ["Do not infer client identity."])
        self.assertFalse(ex.is_empty())

    def test_from_spec_empty_is_empty(self):
        spec = _FakeSpec([], [], [], [])
        self.assertTrue(ExclusionSet.from_spec(spec).is_empty())


class ExcludesPathTest(unittest.TestCase):
    def test_exact_path_exclusion(self):
        ex = ExclusionSet(paths=["Restricted/Sealed.md"])
        self.assertEqual(ex.excludes_path("Restricted/Sealed.md"), (True, "excluded_path"))

    def test_windows_style_path_matches(self):
        ex = ExclusionSet(paths=["Restricted/Sealed.md"])
        self.assertEqual(ex.excludes_path("Restricted\\Sealed.md"), (True, "excluded_path"))

    def test_case_variant_path_matches(self):
        ex = ExclusionSet(paths=["Restricted/Sealed.md"])
        self.assertEqual(ex.excludes_path("restricted/sealed.md"), (True, "excluded_path"))

    def test_prefix_does_not_exclude_sibling(self):
        ex = ExclusionSet(paths=["Restricted/Sealed.md"])
        self.assertEqual(ex.excludes_path("Restricted/SealedNotes.md"), (False, None))

    def test_glob_excludes_nested_note(self):
        ex = ExclusionSet(globs=["Restricted/**"])
        self.assertEqual(ex.excludes_path("Restricted/Private/Deep.md"), (True, "excluded_glob"))
        self.assertEqual(ex.excludes_path("Restricted/Sealed.md"), (True, "excluded_glob"))

    def test_glob_is_casefolded(self):
        ex = ExclusionSet(globs=["Restricted/**"])
        self.assertEqual(ex.excludes_path("RESTRICTED\\PRIVATE\\DEEP.md"), (True, "excluded_glob"))

    def test_no_match(self):
        ex = ExclusionSet(paths=["Restricted/Sealed.md"])
        self.assertEqual(ex.excludes_path("Other/Note.md"), (False, None))

    def test_empty_set_never_excludes(self):
        ex = ExclusionSet()
        self.assertEqual(ex.excludes_path("Anything/Note.md"), (False, None))


class ExcludesTagsTest(unittest.TestCase):
    def test_exact_tag(self):
        ex = ExclusionSet(tags=["private"])
        self.assertEqual(ex.excludes_tags(["private"]), (True, "excluded_tag"))

    def test_leading_hash_ignored(self):
        ex = ExclusionSet(tags=["private"])
        self.assertEqual(ex.excludes_tags(["#private"]), (True, "excluded_tag"))

    def test_case_ignored(self):
        ex = ExclusionSet(tags=["Private"])
        self.assertEqual(ex.excludes_tags(["private"]), (True, "excluded_tag"))
        self.assertEqual(ex.excludes_tags(["#PRIVATE"]), (True, "excluded_tag"))

    def test_no_match(self):
        ex = ExclusionSet(tags=["private"])
        self.assertEqual(ex.excludes_tags(["public"]), (False, None))

    def test_no_tags_probe(self):
        ex = ExclusionSet(tags=["private"])
        self.assertEqual(ex.excludes_tags([]), (False, None))

    def test_empty_set_never_excludes(self):
        ex = ExclusionSet()
        self.assertEqual(ex.excludes_tags(["private"]), (False, None))


class FailClosedTest(unittest.TestCase):
    def test_empty_string_path_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(paths=[""])
        with self.assertRaises(ValueError):
            ExclusionSet(paths=["a", ""])

    def test_empty_string_glob_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(globs=[""])

    def test_empty_string_tag_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(tags=[""])

    def test_empty_string_directive_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(directives=[""])

    def test_non_string_path_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(paths=[1])

    def test_non_string_glob_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(globs=[None])

    def test_non_string_tag_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(tags=[42])

    def test_non_string_directive_rejected(self):
        with self.assertRaises(ValueError):
            ExclusionSet(directives=[object()])

    def test_from_spec_rejects_bad_entry(self):
        spec = _FakeSpec(["Restricted/Sealed.md"], [""], [], [])
        with self.assertRaises(ValueError):
            ExclusionSet.from_spec(spec)


class IsEmptyTest(unittest.TestCase):
    def test_all_empty(self):
        self.assertTrue(ExclusionSet().is_empty())
        self.assertTrue(ExclusionSet(paths=[], globs=[], tags=[], directives=[]).is_empty())

    def test_any_nonempty_means_not_empty(self):
        self.assertFalse(ExclusionSet(paths=["a"]).is_empty())
        self.assertFalse(ExclusionSet(globs=["a"]).is_empty())
        self.assertFalse(ExclusionSet(tags=["a"]).is_empty())
        self.assertFalse(ExclusionSet(directives=["a"]).is_empty())
        self.assertTrue(ExclusionSet(paths=["a"]).has_enforceable_selectors())
        self.assertTrue(ExclusionSet(globs=["a*"]).has_enforceable_selectors())
        self.assertTrue(ExclusionSet(tags=["a"]).has_enforceable_selectors())
        self.assertFalse(ExclusionSet(directives=["a"]).has_enforceable_selectors())
        self.assertFalse(ExclusionSet().has_enforceable_selectors())


class ConnectionExclusionVariableLimitTest(unittest.TestCase):
    """Regression: recallweave-ur0. Pushing the excluded-note set into SQL as
    placeholders explodes past SQLITE_LIMIT_VARIABLE_NUMBER when exclusions
    cover many notes (~125k in the wild, but reproduced here with a small
    limit). The fix streams the edges and applies exclusion in Python, so the
    export succeeds under a small variable limit and the allowed lower-ranked
    edge still appears, with exact suppression counts."""

    def test_export_succeeds_under_small_variable_limit(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        vault = root / "vault"
        vault.mkdir()
        database = root / "index.sqlite"
        # Enough excluded notes that 2*len(excluded) placeholders would exceed
        # the small limit (had the excluded set been pushed into SQL), AND more
        # than the 200-row cap so the suppression count is proven exact beyond
        # the cap.
        n_excluded = 250
        for i in range(n_excluded):
            private = vault / "Private"
            private.mkdir(exist_ok=True)
            (private / f"P{i:03d}.md").write_text(
                f"---\ntags: [private]\n---\n# P{i}\n\n## S\n\nprivate term P{i}.\n",
                encoding="utf-8",
                newline="",
            )
        (vault / "Allowed.md").write_text(
            "# Allowed\n\n## S\n\nallowed term.\n", encoding="utf-8", newline=""
        )
        links = "".join(f"[[Private/P{i:03d}]] " for i in range(n_excluded)) + "[[Allowed]]"
        (vault / "Hub.md").write_text(
            f"# Hub\n\n## S\n\nzzhubanchor. Links: {links}\n",
            encoding="utf-8",
            newline="",
        )
        build_index(vault, database, minimum_candidate_score=0.0)
        spec = TaskSpec.from_payload(
            {
                "objective": "obj",
                "retrieval": {
                    "query": "zzhubanchor",
                    "limit": 8,
                    "max_characters": 100000,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {"tags": ["private"]},
            }
        )

        def limited_connect(database: Path, readonly: bool = False):
            connection = connect(database, readonly=readonly)
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 300)
            return connection

        with patch("recallweave.contract.connect", side_effect=limited_connect):
            document = build_contract_document(database, spec)

        allowed = [
            c
            for c in document["connections"]
            if "Allowed" in c["source"] or "Allowed" in c["target"]
        ]
        self.assertEqual(
            len(allowed),
            1,
            "the allowed lower-ranked connection must be exported even under a "
            "small SQLite variable limit",
        )
        # Suppression counts remain exact even though the excluded edges exceed
        # the 200-row cap.
        self.assertEqual(
            document["exclusions"]["suppressed"]["connections"],
            n_excluded,
        )


class TagPrefetchCandidateFilterTest(unittest.TestCase):
    """Regression: when include_candidates=false, both tag-prefetch UNION
    branches must apply the same verified-only filter as the edge cursor, or
    candidate-only endpoints fan the note_tags load without ever exporting."""

    def _seeded_index(self) -> tuple[Path, int, int, int]:
        temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        vault = root / "vault"
        vault.mkdir()
        (vault / "Hub.md").write_text(
            "# Hub\n\n## S\n\nzzhubanchor. See [[Verified]].\n",
            encoding="utf-8",
            newline="",
        )
        (vault / "Verified.md").write_text(
            "---\ntags: [keep]\n---\n# Verified\n\n## S\n\nverified neighbor.\n",
            encoding="utf-8",
            newline="",
        )
        (vault / "CandidateOnly.md").write_text(
            "---\ntags: [candonly]\n---\n# CandidateOnly\n\n## S\n\n"
            "candidate only neighbor never linked.\n",
            encoding="utf-8",
            newline="",
        )
        database = root / "index.sqlite"
        build_index(vault, database, minimum_candidate_score=0.0)
        with closing(connect(database)) as connection, connection:
            ids = {
                row["relative_path"]: int(row["id"])
                for row in connection.execute(
                    "SELECT id, relative_path FROM notes"
                )
            }
            hub_id = ids["Hub.md"]
            verified_id = ids["Verified.md"]
            candidate_id = ids["CandidateOnly.md"]
            evidence = json.dumps(
                {
                    "method": INDEX_CANDIDATE_METHOD,
                    "explanation": INDEX_CANDIDATE_EXPLANATION,
                    "shared_terms": ["zzhubanchor", "candidate"],
                    "source_evidence": {"citation": "Hub.md"},
                    "target_evidence": {"citation": "CandidateOnly.md"},
                }
            )
            connection.execute(
                """
                INSERT INTO edges(
                    source_note_id, target_note_id, kind, is_verified, score,
                    evidence_json
                ) VALUES (?, ?, 'discovery_candidate', 0, 0.5, ?)
                """,
                (hub_id, candidate_id, evidence),
            )
        return database, hub_id, verified_id, candidate_id

    def test_both_prefetch_branches_require_verified_when_candidates_omitted(
        self,
    ) -> None:
        database, hub_id, verified_id, candidate_id = self._seeded_index()
        exclusions = ExclusionSet(tags=["candonly"])
        tag_sql: list[str] = []
        with closing(connect(database, readonly=True)) as connection:
            real_execute = connection.execute

            def tracing_execute(sql, parameters=()):
                text = str(sql)
                if "FROM note_tags" in text or "from note_tags" in text:
                    tag_sql.append(text)
                return real_execute(sql, parameters)

            connection.execute = tracing_execute  # type: ignore[method-assign]
            _stream_connection_edges(
                connection,
                [hub_id],
                exclusions,
                include_candidates=False,
            )
            self.assertEqual(len(tag_sql), 1)
            self.assertEqual(
                tag_sql[0].count("is_verified = 1"),
                2,
                "both UNION branches must filter candidate edges",
            )
            loaded = {
                int(row["note_id"])
                for row in real_execute(
                    """
                    SELECT nt.note_id, nt.tag
                    FROM note_tags nt
                    WHERE nt.note_id IN (
                        SELECT e.source_note_id FROM edges e
                        WHERE (e.source_note_id IN (?) OR e.target_note_id IN (?))
                        AND e.is_verified = 1
                        UNION
                        SELECT e.target_note_id FROM edges e
                        WHERE (e.source_note_id IN (?) OR e.target_note_id IN (?))
                        AND e.is_verified = 1
                    )
                    """,
                    (hub_id, hub_id, hub_id, hub_id),
                )
            }
            self.assertIn(verified_id, loaded)
            self.assertNotIn(candidate_id, loaded)

            tag_sql.clear()
            _stream_connection_edges(
                connection,
                [hub_id],
                exclusions,
                include_candidates=True,
            )
            self.assertEqual(len(tag_sql), 1)
            self.assertEqual(
                tag_sql[0].count("is_verified = 1"),
                0,
                "candidate-inclusive prefetch must not inject the verified filter",
            )


class TagPrefetchMaxSeedVariableLimitTest(unittest.TestCase):
    """Cycle-23 / recallweave-cxn: at the retrieval.limit ceiling (50 seeds),
    tag-prefetch parameter count must stay seed-bounded under a tight
    SQLITE_LIMIT_VARIABLE_NUMBER — never scale with excluded-note count."""

    def test_fifty_seeds_tag_prefetch_under_reduced_variable_limit(self) -> None:
        n_seeds = 50
        temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        vault = root / "vault"
        private = vault / "Private"
        private.mkdir(parents=True)
        for i in range(n_seeds):
            (private / f"P{i:02d}.md").write_text(
                f"---\ntags: [private]\n---\n# P{i}\n\n## S\n\nprivate term {i}.\n",
                encoding="utf-8",
                newline="",
            )
            (vault / f"Seed{i:02d}.md").write_text(
                f"# Seed{i}\n\n## S\n\nzzseedanchor{i:02d}. See [[Private/P{i:02d}]].\n",
                encoding="utf-8",
                newline="",
            )
        database = root / "index.sqlite"
        build_index(vault, database, minimum_candidate_score=0.0)
        with closing(connect(database, readonly=True)) as connection:
            seed_ids = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM notes WHERE relative_path LIKE 'Seed%' "
                    "ORDER BY relative_path"
                )
            ]
            self.assertEqual(len(seed_ids), n_seeds)
            # Tag prefetch binds seed_ids four times (200 placeholders at the
            # ceiling). Cap just above that so a seed-bounded query succeeds
            # while any excluded-note placeholder expansion would still fail.
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 220)
            max_params = 0
            real_execute = connection.execute

            def tracing_execute(sql, parameters=()):
                nonlocal max_params
                count = len(parameters) if parameters is not None else 0
                max_params = max(max_params, count)
                return real_execute(sql, parameters)

            connection.execute = tracing_execute  # type: ignore[method-assign]
            allowed, suppressed, dropped = _stream_connection_edges(
                connection,
                seed_ids,
                ExclusionSet(tags=["private"]),
                include_candidates=False,
            )
            self.assertEqual(allowed, [])
            self.assertEqual(suppressed, n_seeds)
            self.assertEqual(len(dropped), n_seeds)
            self.assertLessEqual(
                max_params,
                200,
                "tag prefetch + edge cursor must bind only seed placeholders "
                f"(4×{n_seeds}=200), not the exclusion set",
            )
            self.assertGreaterEqual(
                max_params,
                200,
                "expected the 50-seed tag prefetch to exercise the 200-parameter bound",
            )


class TiedScoreExclusionDeterminismTest(unittest.TestCase):
    """Cycle-23 / recallweave-dle: mixed allowed/excluded edges with tied
    verified scores must export byte-identical contracts across builds."""

    def _write_tied_vault(self, vault: Path) -> None:
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "KeepA.md").write_text(
            "# KeepA\n\n## S\n\nkeep a neighbor.\n", encoding="utf-8", newline=""
        )
        (vault / "Drop.md").write_text(
            "---\ntags: [private]\n---\n# Drop\n\n## S\n\ndrop neighbor.\n",
            encoding="utf-8",
            newline="",
        )
        (vault / "KeepB.md").write_text(
            "# KeepB\n\n## S\n\nkeep b neighbor.\n", encoding="utf-8", newline=""
        )
        (vault / "Hub.md").write_text(
            "# Hub\n\n## S\n\nzzhubanchor. "
            "[[KeepA]] [[Drop]] [[KeepB]]\n",
            encoding="utf-8",
            newline="",
        )

    def test_tied_scores_with_mixed_exclusions_are_byte_identical(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        spec = TaskSpec.from_payload(
            {
                "objective": "obj",
                "retrieval": {
                    "query": "zzhubanchor",
                    "limit": 8,
                    "max_characters": 100000,
                },
                "constraints": [],
                "prior_decisions": [],
                "acceptance_criteria": [],
                "exclusions": {"tags": ["private"]},
            }
        )

        def build_artifacts(label: str, *, reverse_edge_insert: bool) -> tuple[bytes, bytes]:
            vault = root / label / "vault"
            database = root / label / "index.sqlite"
            self._write_tied_vault(vault)
            build_index(vault, database, minimum_candidate_score=0.0)
            with closing(connect(database)) as connection, connection:
                rows = connection.execute(
                    """
                    SELECT e.id, e.source_note_id, e.target_note_id, e.kind,
                           e.is_verified, e.score, e.evidence_json,
                           sn.relative_path, tn.relative_path
                    FROM edges e
                    JOIN notes sn ON sn.id = e.source_note_id
                    JOIN notes tn ON tn.id = e.target_note_id
                    WHERE sn.relative_path = 'Hub.md' OR tn.relative_path = 'Hub.md'
                    ORDER BY e.id
                    """
                ).fetchall()
                self.assertGreaterEqual(len(rows), 3)

                def neighbor(row) -> str:
                    return (
                        str(row[8])
                        if str(row[7]) == "Hub.md"
                        else str(row[7])
                    )

                by_neighbor = {neighbor(row): row for row in rows}
                order = ["KeepA.md", "Drop.md", "KeepB.md"]
                self.assertEqual(set(by_neighbor), set(order))
                # Delete and reinsert with explicit ids. Reverse physical insert
                # order on one build so row order diverges while id order
                # (KeepA < Drop < KeepB) stays shared — the id tie-breaker must
                # decide export order among tied scores.
                for row in rows:
                    connection.execute("DELETE FROM edges WHERE id = ?", (row[0],))
                insert_order = list(reversed(order)) if reverse_edge_insert else order
                id_for = {name: index for index, name in enumerate(order, start=1)}
                for name in insert_order:
                    row = by_neighbor[name]
                    connection.execute(
                        """
                        INSERT INTO edges(
                            id, source_note_id, target_note_id, kind,
                            is_verified, score, evidence_json
                        ) VALUES (?, ?, ?, ?, 1, 1.0, ?)
                        """,
                        (
                            id_for[name],
                            int(row[1]),
                            int(row[2]),
                            str(row[3]),
                            str(row[6]),
                        ),
                    )
            document = build_contract_document(database, spec)
            document["provenance"].pop("generated_at")
            document["provenance"]["index"].pop("indexed_at", None)
            self.assertEqual(document["exclusions"]["suppressed"]["connections"], 1)
            ordered = [(c["source"], c["target"]) for c in document["connections"]]
            self.assertTrue(any("KeepA" in s or "KeepA" in t for s, t in ordered))
            self.assertTrue(any("KeepB" in s or "KeepB" in t for s, t in ordered))
            self.assertFalse(any("Drop" in s or "Drop" in t for s, t in ordered))
            json_bytes = json.dumps(
                {"ordered": ordered, "doc": document},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            markdown = render_contract_markdown(document)
            return json_bytes, markdown.encode("utf-8")

        first_json, first_md = build_artifacts("a", reverse_edge_insert=False)
        second_json, second_md = build_artifacts("b", reverse_edge_insert=True)
        self.assertEqual(first_json, second_json)
        self.assertEqual(first_md, second_md)


if __name__ == "__main__":
    unittest.main()
