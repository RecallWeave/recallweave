from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.contract import build_contract_document
from recallweave.contract_exclusions import ExclusionSet
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


if __name__ == "__main__":
    unittest.main()
