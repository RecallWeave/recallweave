from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.index import build_index, connect
from recallweave.policy import IndexPolicy
from recallweave.steward_assess import (
    ASSESS_ASSERTER,
    ASSESSMENT_KIND,
    DETERMINISTIC_RELATIONS,
    INTERPRETIVE_RELATIONS,
    STANDING_CAVEAT,
    assess_change_batch,
    assess_latest,
)
from recallweave.steward_sources import SOURCES_SPEC_VERSION, SourceRegistry
from recallweave.steward_state import ensure_state_layout

FROZEN_NOW = "2026-01-01T00:00:00+00:00"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _batch(source: str = "vault", changes=None, changed_during_observe=None, **overrides) -> dict:
    payload = {
        "schema_version": "recallweave.steward.v1",
        "kind": "change_batch",
        "operation": "steward_observe",
        "generated_at": FROZEN_NOW,
        "source": source,
        "registry_sha256": None,
        "changes": changes or [],
        "rename_candidates": [],
        "change_summary": {},
        "skipped": {},
        "changed_during_observe": changed_during_observe or [],
        "network_calls": 0,
        "vault_writes": 0,
    }
    payload.update(overrides)
    return payload


def _change(relative_path: str, change_type: str, *, previous=None, current=None) -> dict:
    return {
        "relative_path": relative_path,
        "change_type": change_type,
        "previous_content_hash": previous,
        "current_content_hash": current,
    }


class StewardAssessTest(unittest.TestCase):
    """Shared fixture: a small vault with authored links, indexed once.

    Alpha.md links to Beta.md (an authored wikilink edge). Gamma.md and
    Echo.md are unrelated standalone notes; Echo.md exists to give a
    duplicate-bytes baseline already present in the index.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = self.root / "index.sqlite"

        self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background.\n\n"
            "## Details\n\nAlpha detail line one.\nAlpha detail line two.\n",
        )
        self._write("Beta.md", "# Beta\n\nBeta body content.\n")
        self._write("Gamma.md", "# Gamma\n\nGamma content, standalone.\n")
        self._write("Echo.md", "# Echo\n\nEcho body unique text.\n")

        build_index(self.vault, self.database, policy=IndexPolicy(), minimum_candidate_score=0.0)

    def _write(self, relative: str, text: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _sections(self, relative_path: str) -> list[dict]:
        with connect(self.database, readonly=True) as connection:
            note_id = connection.execute(
                "SELECT id FROM notes WHERE relative_path = ?", (relative_path,)
            ).fetchone()["id"]
            rows = connection.execute(
                "SELECT heading, line_start, line_end, text FROM sections "
                "WHERE note_id = ? ORDER BY line_start",
                (note_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _index_hash(self, relative_path: str) -> str:
        with connect(self.database, readonly=True) as connection:
            row = connection.execute(
                "SELECT content_hash FROM notes WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            return str(row["content_hash"])

    def _by_relation(self, document: dict, relation: str) -> list[dict]:
        return [item for item in document["assessments"] if item["relation"] == relation]


class NewModifiedDeletedTest(StewardAssessTest):
    def test_new_relation_for_unindexed_added_file(self) -> None:
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        batch = _batch(changes=[_change("Newcomer.md", "added", current=_hash(path))])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "NEW")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relative_path"], "Newcomer.md")
        self.assertIsNone(records[0]["inputs"]["index_content_hash"])
        self.assertEqual(document["summary"]["NEW"], 1)

    def test_modified_relation_for_changed_indexed_file(self) -> None:
        before_hash = self._index_hash("Beta.md")
        path = self._write("Beta.md", "# Beta\n\nBeta body content, edited.\n")
        batch = _batch(
            changes=[
                _change("Beta.md", "modified", previous=before_hash, current=_hash(path))
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "MODIFIED")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["inputs"]["index_content_hash"], before_hash)
        self.assertEqual(document["summary"]["MODIFIED"], 1)
        self.assertEqual(document["baseline_divergence"], [])

    def test_deleted_relation_for_removed_indexed_file(self) -> None:
        before_hash = self._index_hash("Gamma.md")
        (self.vault / "Gamma.md").unlink()
        batch = _batch(changes=[_change("Gamma.md", "removed", previous=before_hash)])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "DELETED")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["inputs"]["index_content_hash"], before_hash)
        self.assertEqual(document["summary"]["DELETED"], 1)

    def test_index_current_no_relation_when_hash_matches(self) -> None:
        current_hash = self._index_hash("Gamma.md")
        batch = _batch(
            changes=[_change("Gamma.md", "modified", previous=current_hash, current=current_hash)]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["assessments"], [])
        self.assertEqual(document["summary"]["index_current"], 1)

    def test_never_indexed_no_relation_for_removed_unindexed_path(self) -> None:
        batch = _batch(changes=[_change("Ghost.md", "removed", previous="deadbeef")])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["assessments"], [])
        self.assertEqual(document["summary"]["never_indexed"], 1)

    def test_skipped_changed_during_observe_emits_no_relation(self) -> None:
        path = self._write("Gamma.md", "# Gamma\n\nGamma content, edited mid-run.\n")
        batch = _batch(
            changes=[
                _change(
                    "Gamma.md",
                    "modified",
                    previous=self._index_hash("Gamma.md"),
                    current=_hash(path),
                )
            ],
            changed_during_observe=["Gamma.md"],
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["assessments"], [])
        self.assertEqual(document["summary"]["skipped_changed_during_observe"], 1)
        # A skipped path was NOT assessed: it must be excluded from covered_paths
        # so the report never treats it as a resolution of a prior finding.
        self.assertEqual(document["covered_paths"], [])

    def test_covered_paths_excludes_symlinked_ancestor(self) -> None:
        # A parent swapped for a symlink to an external dir (whose file matches
        # the observed hash) must NOT be accepted as covered -- the verifier must
        # refuse to follow the symlink out of the source.
        import os as _os
        external = self.root / "external"
        external.mkdir()
        (external / "note.md").write_text("outside content\n", encoding="utf-8")
        ext_hash = _hash(external / "note.md")
        try:
            _os.symlink(external, self.vault / "folder", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unsupported")
        batch = _batch(
            changes=[_change("folder/note.md", "modified",
                             previous="deadbeef", current=ext_hash)]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(
            document["covered_paths"], [],
            "a symlinked ancestor was followed out of the source to clear a path",
        )

    def test_covered_paths_excludes_reappeared_removed_path(self) -> None:
        # A removed change whose file has reappeared on disk before assessment
        # must NOT be treated as covered (it is not genuinely resolved).
        self._write("Back.md", "# Back\n\nreappeared.\n")  # exists again on disk
        batch = _batch(changes=[_change("Back.md", "removed", previous="deadbeef")])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["covered_paths"], [])

    def test_covered_paths_excludes_drifted_present_path(self) -> None:
        # A modified change whose file no longer matches the observed hash
        # (changed again before assessment) must NOT be covered.
        self._write("Drift.md", "# Drift\n\nNEWER content than observed.\n")
        batch = _batch(
            changes=[_change("Drift.md", "modified",
                             previous=self._index_hash("Gamma.md"),
                             current="0" * 64)]  # observed hash != live hash
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["covered_paths"], [])

    def test_covered_paths_lists_only_verified_relationless_paths(self) -> None:
        # Gamma.md modified back to its indexed bytes -> index_current, no
        # relation, and the live file matches -> covered (verified). Skipped.md
        # is changed_during_observe -> not assessed -> excluded.
        gamma_hash = self._index_hash("Gamma.md")  # file on disk still matches
        batch = _batch(
            changes=[
                _change("Gamma.md", "modified", previous=gamma_hash, current=gamma_hash),
                _change("Skipped.md", "added", current="deadbeef"),
            ],
            changed_during_observe=["Skipped.md"],
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["assessments"], [])  # index_current, no relation
        self.assertEqual(document["covered_paths"], ["Gamma.md"])


class DuplicatesTest(StewardAssessTest):
    def test_duplicates_exact_bytes_against_index_path(self) -> None:
        echo_hash = self._index_hash("Echo.md")
        path = self._write("Zeta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        self.assertEqual(_hash(path), echo_hash)
        batch = _batch(changes=[_change("Zeta.md", "added", current=echo_hash)])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "DUPLICATES_EXACT_BYTES")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relative_path"], "Zeta.md")
        self.assertEqual(records[0]["inputs"]["duplicate_of"], ["Echo.md"])
        self.assertEqual(records[0]["inputs"]["duplicate_in_batch"], [])
        # It is also a NEW file -- both relations stand for the same path.
        self.assertEqual(len(self._by_relation(document, "NEW")), 1)

    def test_duplicates_exact_bytes_within_batch(self) -> None:
        shared_text = "# Theta\n\nShared bytes across two new files.\n"
        theta1 = self._write("Theta1.md", shared_text)
        theta2 = self._write("Theta2.md", shared_text)
        shared_hash = _hash(theta1)
        self.assertEqual(shared_hash, _hash(theta2))
        batch = _batch(
            changes=[
                _change("Theta1.md", "added", current=shared_hash),
                _change("Theta2.md", "added", current=shared_hash),
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = {
            item["relative_path"]: item
            for item in self._by_relation(document, "DUPLICATES_EXACT_BYTES")
        }
        self.assertEqual(set(records), {"Theta1.md", "Theta2.md"})
        self.assertEqual(records["Theta1.md"]["inputs"]["duplicate_in_batch"], ["Theta2.md"])
        self.assertEqual(records["Theta2.md"]["inputs"]["duplicate_in_batch"], ["Theta1.md"])
        self.assertEqual(records["Theta1.md"]["inputs"]["duplicate_of"], [])


    def test_duplicates_exact_bytes_within_batch_for_modified_notes(self) -> None:
        # Two already-indexed notes modified in the same batch to identical new
        # bytes (neither new hash in the index) must both be flagged; keying the
        # same-batch map on "added" alone would drop this finding.
        beta_before = self._index_hash("Beta.md")
        gamma_before = self._index_hash("Gamma.md")
        shared_text = "# Merged\n\nBeta and Gamma now hold identical bytes.\n"
        beta = self._write("Beta.md", shared_text)
        gamma = self._write("Gamma.md", shared_text)
        shared_hash = _hash(beta)
        self.assertEqual(shared_hash, _hash(gamma))
        batch = _batch(
            changes=[
                _change("Beta.md", "modified", previous=beta_before, current=shared_hash),
                _change("Gamma.md", "modified", previous=gamma_before, current=shared_hash),
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = {
            item["relative_path"]: item
            for item in self._by_relation(document, "DUPLICATES_EXACT_BYTES")
        }
        self.assertEqual(set(records), {"Beta.md", "Gamma.md"})
        self.assertEqual(records["Beta.md"]["inputs"]["duplicate_in_batch"], ["Gamma.md"])
        self.assertEqual(records["Gamma.md"]["inputs"]["duplicate_in_batch"], ["Beta.md"])


class AuthoredReferenceTouchedTest(StewardAssessTest):
    def test_outbound_edge_when_source_of_link_is_touched(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        path = self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background, edited.\n\n"
            "## Details\n\nAlpha detail line one.\nAlpha detail line two.\n",
        )
        batch = _batch(
            changes=[_change("Alpha.md", "modified", previous=before_hash, current=_hash(path))]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "AUTHORED_REFERENCE_TOUCHED")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relative_path"], "Alpha.md")
        edges = records[0]["inputs"]["authored_edges"]
        self.assertEqual(edges, [{"other_path": "Beta.md", "direction": "outbound", "kind": "wikilink"}])

    def test_inbound_edge_when_target_of_link_is_touched(self) -> None:
        before_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").unlink()
        batch = _batch(changes=[_change("Beta.md", "removed", previous=before_hash)])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "AUTHORED_REFERENCE_TOUCHED")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["relative_path"], "Beta.md")
        edges = records[0]["inputs"]["authored_edges"]
        self.assertEqual(edges, [{"other_path": "Alpha.md", "direction": "inbound", "kind": "wikilink"}])

    def test_no_authored_reference_touched_for_unlinked_note(self) -> None:
        before_hash = self._index_hash("Gamma.md")
        (self.vault / "Gamma.md").unlink()
        batch = _batch(changes=[_change("Gamma.md", "removed", previous=before_hash)])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(self._by_relation(document, "AUTHORED_REFERENCE_TOUCHED"), [])


class CitationBrokenTest(StewardAssessTest):
    def test_note_deleted_lists_all_sections(self) -> None:
        sections = self._sections("Alpha.md")
        self.assertEqual(len(sections), 2)
        before_hash = self._index_hash("Alpha.md")
        (self.vault / "Alpha.md").unlink()
        batch = _batch(changes=[_change("Alpha.md", "removed", previous=before_hash)])
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "CITATION_BROKEN")
        self.assertEqual(len(records), 1)
        broken = records[0]["inputs"]["broken_citations"]
        self.assertEqual(len(broken), len(sections))
        for entry, section in zip(broken, sections):
            self.assertEqual(entry["reason"], "note_deleted")
            self.assertEqual(
                entry["citation"], f"Alpha.md:{section['line_start']}-{section['line_end']}"
            )

    def test_range_mismatch_only_affects_edited_section(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        # Edit the *first* section's text; leave the "Details" section intact.
        path = self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background -- rewritten.\n\n"
            "## Details\n\nAlpha detail line one.\nAlpha detail line two.\n",
        )
        batch = _batch(
            changes=[_change("Alpha.md", "modified", previous=before_hash, current=_hash(path))]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "CITATION_BROKEN")
        self.assertEqual(len(records), 1)
        broken = records[0]["inputs"]["broken_citations"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["reason"], "range_mismatch")
        self.assertEqual(broken[0]["heading"], "Alpha")
        self.assertEqual(broken[0]["citation"], "Alpha.md:3-3")

    def test_citation_intact_when_appending_after_last_section(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        original = (self.vault / "Alpha.md").read_text(encoding="utf-8")
        path = self._write("Alpha.md", original + "\n## Appendix\n\nAppended later.\n")
        batch = _batch(
            changes=[_change("Alpha.md", "modified", previous=before_hash, current=_hash(path))]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(self._by_relation(document, "CITATION_BROKEN"), [])
        # It is still a genuine MODIFIED change.
        self.assertEqual(len(self._by_relation(document, "MODIFIED")), 1)

    def test_content_drifted_skips_citation_check(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background -- rewritten again.\n\n"
            "## Details\n\nAlpha detail line one.\nAlpha detail line two.\n",
        )
        # The batch claims a current_content_hash that does not match what is
        # actually on disk right now (simulating drift between observe and
        # assess).
        batch = _batch(
            changes=[
                _change(
                    "Alpha.md",
                    "modified",
                    previous=before_hash,
                    current="0" * 64,
                )
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["content_drifted"], ["Alpha.md"])
        self.assertEqual(self._by_relation(document, "CITATION_BROKEN"), [])
        # MODIFIED is unaffected by the drift check (it only compares the
        # batch's claimed hash to the index, not to the live file).
        self.assertEqual(len(self._by_relation(document, "MODIFIED")), 1)

    def test_unreadable_file_reason_and_error_type(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        # The batch reports Alpha.md as modified, but the file is gone by the
        # time assess runs (e.g. a race with another process).
        (self.vault / "Alpha.md").unlink()
        batch = _batch(
            changes=[
                _change("Alpha.md", "modified", previous=before_hash, current="1" * 64)
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        records = self._by_relation(document, "CITATION_BROKEN")
        self.assertEqual(len(records), 1)
        broken = records[0]["inputs"]["broken_citations"]
        self.assertEqual(len(broken), 2)
        for entry in broken:
            self.assertEqual(entry["reason"], "unreadable")
            self.assertEqual(entry["error_type"], "FileNotFoundError")


class BaselineDivergenceTest(StewardAssessTest):
    def test_baseline_divergence_recorded_but_relation_still_computed(self) -> None:
        path = self._write("Beta.md", "# Beta\n\nBeta body content, edited again.\n")
        batch = _batch(
            changes=[
                _change(
                    "Beta.md",
                    "modified",
                    previous="stale-baseline-hash",
                    current=_hash(path),
                )
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        self.assertEqual(document["baseline_divergence"], ["Beta.md"])
        self.assertEqual(len(self._by_relation(document, "MODIFIED")), 1)


class DeterminismAndRelationSetTest(StewardAssessTest):
    def test_determinism_byte_identical_with_frozen_now(self) -> None:
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        before_hash = self._index_hash("Beta.md")
        beta_path = self._write("Beta.md", "# Beta\n\nBeta body content, edited.\n")
        batch = _batch(
            changes=[
                _change("Newcomer.md", "added", current=_hash(path)),
                _change(
                    "Beta.md", "modified", previous=before_hash, current=_hash(beta_path)
                ),
            ]
        )
        first = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        second = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        first_bytes = json.dumps(first, sort_keys=True, ensure_ascii=True).encode("utf-8")
        second_bytes = json.dumps(second, sort_keys=True, ensure_ascii=True).encode("utf-8")
        self.assertEqual(first_bytes, second_bytes)

    def test_no_interpretive_relation_constructible(self) -> None:
        self.assertEqual(INTERPRETIVE_RELATIONS & DETERMINISTIC_RELATIONS, frozenset())

        # A rich batch exercising every deterministic relation this module
        # can emit.
        new_path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        alpha_before = self._index_hash("Alpha.md")
        alpha_path = self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background -- rewritten.\n\n"
            "## Details\n\nAlpha detail line one.\nAlpha detail line two.\n",
        )
        gamma_before = self._index_hash("Gamma.md")
        (self.vault / "Gamma.md").unlink()
        echo_hash = self._index_hash("Echo.md")
        dup_path = self._write("EchoTwin.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))

        batch = _batch(
            changes=[
                _change("Newcomer.md", "added", current=_hash(new_path)),
                _change("Alpha.md", "modified", previous=alpha_before, current=_hash(alpha_path)),
                _change("Gamma.md", "removed", previous=gamma_before),
                _change("EchoTwin.md", "added", current=echo_hash),
            ]
        )
        document = assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)
        relations = {item["relation"] for item in document["assessments"]}
        self.assertTrue(relations.issubset(DETERMINISTIC_RELATIONS))
        self.assertEqual(relations & INTERPRETIVE_RELATIONS, set())
        self.assertGreaterEqual(len(relations), 4)
        for item in document["assessments"]:
            self.assertEqual(item["asserter"], ASSESS_ASSERTER)
            self.assertEqual(item["decidability"], "deterministic")
            self.assertTrue(item["reproducible"])
            self.assertEqual(item["standing_caveat"], STANDING_CAVEAT)
        self.assertEqual(document["kind"], ASSESSMENT_KIND)


class ValidationTest(StewardAssessTest):
    def test_wrong_batch_schema_rejected(self) -> None:
        batch = _batch(schema_version="not-the-real-schema")
        with self.assertRaises(ValueError):
            assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)

    def test_wrong_batch_kind_rejected(self) -> None:
        batch = _batch(kind="not_change_batch")
        with self.assertRaises(ValueError):
            assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)

    def test_missing_required_key_rejected(self) -> None:
        batch = _batch()
        del batch["network_calls"]
        with self.assertRaises(ValueError):
            assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)

    def test_wrong_index_schema_rejected(self) -> None:
        with connect(self.database) as connection:
            connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", ("99",)
            )
            connection.commit()
        batch = _batch(changes=[])
        with self.assertRaisesRegex(ValueError, "re-index"):
            assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)


class AssessLatestTest(StewardAssessTest):
    def setUp(self) -> None:
        super().setUp()
        self.state_root = self.root / "state"
        self.dirs = ensure_state_layout(self.state_root)
        registry_payload = {
            "spec_version": SOURCES_SPEC_VERSION,
            "sources": [
                {
                    "name": "vault",
                    "type": "folder",
                    "root": str(self.vault),
                    "mode": "read_only",
                }
            ],
        }
        self.registry = SourceRegistry.from_payload(registry_payload)

    def _write_batch_file(self, timestamp: str, changes: list[dict]) -> Path:
        path = self.dirs["changes"] / f"{timestamp}-vault.json"
        path.write_text(json.dumps(_batch(changes=changes)), encoding="utf-8")
        return path

    def test_end_to_end_and_idempotent_on_second_run(self) -> None:
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        batch_file = self._write_batch_file(
            "20260101T000000Z", [_change("Newcomer.md", "added", current=_hash(path))]
        )
        receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(len(receipt["assessed"]), 1)
        assessment_path = self.dirs["assessments"] / batch_file.name
        self.assertTrue(assessment_path.is_file())
        saved = json.loads(assessment_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["change_batch_ref"], batch_file.name)
        self.assertEqual(len(saved["assessments"]), 1)

        second = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(second["assessed"], [])
        self.assertEqual(
            second["skipped_sources"], [{"source": "vault", "reason": "already_assessed"}]
        )

    def test_source_identity_change_skips_assessment(self) -> None:
        # A root renamed and replaced between observe and assess changes its
        # (dev, ino); assessment must refuse to read the replacement tree -- it
        # records the source as identity-changed and persists nothing, rather
        # than assessing the wrong tree or reporting a false no_change.
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        self._write_batch_file(
            "20260101T000000Z", [_change("Newcomer.md", "added", current=_hash(path))]
        )
        source = self.registry.sources[0]
        if source.root_dev is None or source.root_ino is None:
            self.skipTest("source identity is not pinned on this platform")
        with patch(
            "recallweave.steward_assess.path_identity",
            return_value=(source.root_dev + 1, source.root_ino + 1),
        ):
            receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["assessed"], [])
        self.assertIn(
            {"source": "vault", "reason": "source_identity_changed"},
            receipt["skipped_sources"],
        )
        self.assertEqual(list(self.dirs["assessments"].glob("*.json")), [])

    def test_symlinked_root_resolving_to_pinned_inode_is_refused(self) -> None:
        # A root RENAMED aside and REPLACED by a symlink back to the original
        # resolves, through the symlink, to the pinned (dev, ino) -- so an
        # identity-only check would accept it. is_link_like must refuse the
        # symlinked root before the identity comparison.
        source = self.registry.sources[0]
        if source.root_dev is None or source.root_ino is None:
            self.skipTest("source identity is not pinned on this platform")
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        self._write_batch_file(
            "20260101T000000Z", [_change("Newcomer.md", "added", current=_hash(path))]
        )
        renamed = self.root / "vault-original"
        os.rename(self.vault, renamed)
        try:
            self.vault.symlink_to(renamed, target_is_directory=True)
        except OSError as error:
            os.rename(renamed, self.vault)  # restore for teardown
            self.skipTest(f"symlink creation unavailable: {error}")
        receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["assessed"], [])
        self.assertIn(
            {"source": "vault", "reason": "source_identity_changed"},
            receipt["skipped_sources"],
        )
        self.assertEqual(list(self.dirs["assessments"].glob("*.json")), [])

    def test_no_change_batch_is_reported_and_skipped(self) -> None:
        receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["assessed"], [])
        self.assertEqual(
            receipt["skipped_sources"], [{"source": "vault", "reason": "no_change_batch"}]
        )

    def test_processes_every_unassessed_batch_not_just_newest(self) -> None:
        # An earlier batch must not be skipped because a later batch exists.
        p1 = self._write("One.md", "# One\n\nfirst.\n")
        b1 = self._write_batch_file(
            "20260101T000000Z", [_change("One.md", "added", current=_hash(p1))]
        )
        p2 = self._write("Two.md", "# Two\n\nsecond.\n")
        b2 = self._write_batch_file(
            "20260102T000000Z", [_change("Two.md", "added", current=_hash(p2))]
        )
        receipt = assess_latest(self.registry, self.state_root, self.database)
        refs = {item["change_batch_ref"] for item in receipt["assessed"]}
        self.assertEqual(refs, {b1.name, b2.name})
        self.assertTrue((self.dirs["assessments"] / b1.name).is_file())
        self.assertTrue((self.dirs["assessments"] / b2.name).is_file())

    def test_suffix_named_source_batch_does_not_collide(self) -> None:
        # A stray batch for source "x-vault" must not be pulled into "vault"
        # (glob '*-vault.json' would match it) and block the run.
        stray = self.dirs["changes"] / "20260101T000000Z-x-vault.json"
        stray.write_text(
            json.dumps(_batch(source="x-vault", changes=[])), encoding="utf-8"
        )
        receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["assessed"], [])
        self.assertEqual(
            receipt["skipped_sources"],
            [{"source": "vault", "reason": "no_change_batch"}],
        )

    def test_multi_source_registry_is_refused(self) -> None:
        second = self.root / "vault-b"
        second.mkdir()
        (second / "b.md").write_text("# B\n\nbody.\n", encoding="utf-8")
        registry = SourceRegistry.from_payload(
            {
                "spec_version": SOURCES_SPEC_VERSION,
                "sources": [
                    {"name": "vault", "type": "folder", "root": str(self.vault),
                     "mode": "read_only"},
                    {"name": "vaultb", "type": "folder", "root": str(second),
                     "mode": "read_only"},
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "more than one source"):
            assess_latest(registry, self.state_root, self.database)


class AssessmentEvidenceCompletenessTest(StewardAssessTest):
    def test_assessment_document_retains_all_records_for_propose(self) -> None:
        # The persisted assessment is an internal artifact propose reads in
        # full; it must never be truncated (that would drop proposals).
        changes = []
        for i in range(5):
            p = self._write(f"New{i}.md", f"# New{i}\n\nunique {i}.\n")
            changes.append(_change(f"New{i}.md", "added", current=_hash(p)))
        document = assess_change_batch(
            _batch(changes=changes), self.database, self.vault, now=FROZEN_NOW
        )
        self.assertEqual(len(document["assessments"]), 5)
        self.assertNotIn("assessments_truncated", document)


class SingleFileSourceAssessTest(unittest.TestCase):
    def test_file_source_resolves_from_parent_directory(self) -> None:
        # A type:"file" source: source_root is the file, the observed relative
        # path is its filename. Citation reads must hit <parent>/<filename>,
        # not <file>/<filename>, so a real section is not misreported unreadable.
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            vault = base / "vault"
            vault.mkdir()
            note = vault / "one.md"
            note.write_text(
                "# One\n\nSee [[one]] here.\n\n## Body\n\nline a\nline b\n",
                encoding="utf-8",
                newline="",
            )
            database = base / "index.sqlite"
            build_index(vault, database, policy=IndexPolicy(), minimum_candidate_score=0.0)
            before = _hash(note)
            note.write_text(
                "# One\n\nSee [[one]] here.\n\n## Body\n\nline a\nline b changed\n",
                encoding="utf-8",
                newline="",
            )
            batch = _batch(
                source="single",
                changes=[_change("one.md", "modified", previous=before, current=_hash(note))],
            )
            # With the fix, this assesses cleanly (no path-escape error, no
            # spurious unreadable-citation) because path_base is the parent dir.
            document = assess_change_batch(
                batch, database, note, now=FROZEN_NOW, source_is_file=True
            )
            self.assertEqual(document["summary"].get("MODIFIED"), 1)


def _run_cli(*args: str) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli_main(list(args))
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _decode_single_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    stripped = text.lstrip()
    value, end = decoder.raw_decode(stripped)
    if stripped[end:].strip():
        raise AssertionError("more than one JSON value emitted on stdout")
    return value


class StewardAssessCliTest(StewardAssessTest):
    def setUp(self) -> None:
        super().setUp()
        self.state_dir = self.root / "cli-state"
        self.dirs = ensure_state_layout(self.state_dir)
        self.sources_path = self.root / "sources.json"
        self.sources_path.write_text(
            json.dumps(
                {
                    "spec_version": SOURCES_SPEC_VERSION,
                    "sources": [
                        {
                            "name": "vault",
                            "type": "folder",
                            "root": str(self.vault),
                            "mode": "read_only",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_cli_end_to_end_emits_single_json_object(self) -> None:
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        batch_path = self.dirs["changes"] / "20260101T000000Z-vault.json"
        batch = _batch(changes=[_change("Newcomer.md", "added", current=_hash(path))])
        # The CLI loads the registry from disk, so the digest binding is
        # strict: the batch must carry the real registry digest.
        batch["registry_sha256"] = hashlib.sha256(
            self.sources_path.read_bytes()
        ).hexdigest()
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        exit_code, stdout, stderr = _run_cli(
            "steward-assess",
            str(self.sources_path),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_dir),
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = _decode_single_json(stdout)
        self.assertEqual(payload["kind"], "steward_assess_receipt")
        self.assertEqual(len(payload["assessed"]), 1)
        self.assertTrue((self.dirs["assessments"] / batch_path.name).is_file())

    def test_cli_missing_database_emits_error_envelope(self) -> None:
        path = self._write("Newcomer.md", "# Newcomer\n\nBrand new note.\n")
        batch_path = self.dirs["changes"] / "20260101T000000Z-vault.json"
        batch_path.write_text(
            json.dumps(_batch(changes=[_change("Newcomer.md", "added", current=_hash(path))])),
            encoding="utf-8",
        )
        missing_database = self.root / "does-not-exist.sqlite"
        exit_code, stdout, stderr = _run_cli(
            "steward-assess",
            str(self.sources_path),
            "--database",
            str(missing_database),
            "--state-dir",
            str(self.state_dir),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        payload = _decode_single_json(stderr)
        self.assertEqual(payload["error"], "ValueError")
        self.assertEqual(payload["operation"], "steward-assess")


if __name__ == "__main__":
    unittest.main()
