from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from recallweave.cli import main as cli_main
from recallweave.index import build_index, connect
from recallweave.policy import IndexPolicy
from recallweave.steward_assess import assess_change_batch, assess_latest
from recallweave.steward_propose import (
    ACTIONS,
    POLICY_LEVEL,
    PROPOSAL_KIND,
    PROPOSE_ASSERTER,
    propose_from_assessment,
    propose_latest,
)
from recallweave.steward_sources import SOURCES_SPEC_VERSION, SourceRegistry
from recallweave.steward_state import STEWARD_SCHEMA_VERSION, ensure_state_layout

FROZEN_NOW = "2026-01-01T00:00:00+00:00"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _batch(source: str = "vault", changes=None, rename_candidates=None, **overrides) -> dict:
    payload = {
        "schema_version": "recallweave.steward.v1",
        "kind": "change_batch",
        "operation": "steward_observe",
        "generated_at": FROZEN_NOW,
        "source": source,
        "registry_sha256": None,
        "changes": changes or [],
        "rename_candidates": rename_candidates or [],
        "change_summary": {},
        "skipped": {},
        "changed_during_observe": [],
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


def _all_string_leaves(value, parent_key=None):
    """Yield (key, string_value) for every string leaf in a JSON document."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _all_string_leaves(item, parent_key=key)
    elif isinstance(value, list):
        for item in value:
            yield from _all_string_leaves(item, parent_key=parent_key)
    elif isinstance(value, str):
        yield (parent_key, value)


class StewardProposeTest(unittest.TestCase):
    """Shared fixture: Alpha.md links to Beta.md and Gamma.md; Echo.md gives
    a duplicate-bytes baseline. Indexed once in setUp; individual tests then
    mutate the on-disk vault and hand-build a change_batch the way
    steward-observe would have produced one."""

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
            "## Details\n\nSee also [[Gamma]] for more detail.\n",
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

    def _index_hash(self, relative_path: str) -> str:
        with connect(self.database, readonly=True) as connection:
            row = connection.execute(
                "SELECT content_hash FROM notes WHERE relative_path = ?", (relative_path,)
            ).fetchone()
            return str(row["content_hash"])

    def _assess(self, batch: dict) -> dict:
        return assess_change_batch(batch, self.database, self.vault, now=FROZEN_NOW)

    def _propose(self, assessment: dict, batch: dict | None, **kwargs) -> list[dict]:
        return propose_from_assessment(
            assessment, batch, self.database, self.vault, now=FROZEN_NOW, **kwargs
        )

    def _by_action(self, proposals: list[dict], action: str) -> list[dict]:
        return [p for p in proposals if p["action"] == action]


class RenameFixLinksTest(StewardProposeTest):
    def _rename_beta(self) -> tuple[dict, dict]:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        return batch, self._assess(batch)

    def test_unique_inode_matched_rename_produces_compiled_edit(self) -> None:
        alpha_before = (self.vault / "Alpha.md").read_bytes()
        alpha_hash = hashlib.sha256(alpha_before).hexdigest()
        batch, assessment = self._rename_beta()
        proposals = self._propose(assessment, batch)
        fixes = self._by_action(proposals, "fix_links_after_rename")
        self.assertEqual(len(fixes), 1)
        proposal = fixes[0]
        self.assertEqual(proposal["policy_level"], "propose_only")
        self.assertEqual(len(proposal["edits"]), 1)
        edit = proposal["edits"][0]
        self.assertEqual(edit["mutation_class"], "fix_unresolved_link")
        self.assertEqual(edit["relative_path"], "Alpha.md")
        self.assertEqual(edit["precondition_content_hash"], alpha_hash)
        self.assertEqual(edit["anchor"]["line"], 3)
        self.assertEqual(edit["anchor"]["old_text"], "[[Beta]]")
        self.assertEqual(edit["replacement_text"], "[[BetaNew]]")

        # Verify predicted_post_hash by actually applying the edit.
        lines = alpha_before.decode("utf-8").splitlines(keepends=True)
        lines[edit["anchor"]["line"] - 1] = lines[edit["anchor"]["line"] - 1].replace(
            edit["anchor"]["old_text"], edit["replacement_text"]
        )
        rebuilt = "".join(lines).encode("utf-8")
        self.assertEqual(edit["predicted_post_hash"], hashlib.sha256(rebuilt).hexdigest())

        self.assertEqual(proposal["blast_radius"]["predicted_citation_shifts"], [])
        self.assertEqual(proposal["blast_radius"]["files_edited"], 1)
        self.assertEqual(proposal["blast_radius"]["notes_affected"], ["Alpha.md"])

    def test_ambiguous_new_stem_produces_qualified_wikilink(self) -> None:
        # Beta.md -> sub/BetaNew.md while an unrelated other/BetaNew.md exists:
        # the new basename "BetaNew" is not unique, so a bare [[BetaNew]] would be
        # ambiguous. The compiled rewrite must emit the path-qualified target.
        # Add a same-stem note and rebuild the index while Beta.md still exists,
        # so the index (which reflects the pre-rename vault) knows both Beta.md
        # and the colliding other/BetaNew.md. Then perform the on-disk rename.
        self._write("other/BetaNew.md", "# Other\n\nUnrelated BetaNew note.\n")
        build_index(
            self.vault, self.database, policy=IndexPolicy(), minimum_candidate_score=0.0
        )
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "sub").mkdir()
        (self.vault / "Beta.md").rename(self.vault / "sub" / "BetaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("sub/BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["sub/BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        fixes = self._by_action(proposals, "fix_links_after_rename")
        self.assertEqual(len(fixes), 1)
        edit = fixes[0]["edits"][0]
        self.assertEqual(edit["relative_path"], "Alpha.md")
        self.assertEqual(edit["replacement_text"], "[[sub/BetaNew]]")

    def test_referrer_excluded_by_source_policy_is_not_compiled(self) -> None:
        # The index was built with a broad policy, but a source policy whose
        # include_paths allowlist excludes the referrer must NOT read or compile
        # an edit for it -- the whole proposal would otherwise be rejected at
        # apply and the excluded path leaked. It falls back to an advisory.
        batch, assessment = self._rename_beta()
        restrictive = IndexPolicy(
            include_paths=["Beta.md", "BetaNew.md", "Gamma.md", "Echo.md"]
        )
        proposals = self._propose(assessment, batch, policy=restrictive)
        self.assertEqual(self._by_action(proposals, "fix_links_after_rename"), [])
        advisories = self._by_action(proposals, "review_dangling_references")
        self.assertEqual(len(advisories), 1)

    def test_referrer_hash_drift_skips_edit_and_falls_back_to_advisory(self) -> None:
        batch, assessment = self._rename_beta()
        # Alpha.md changes on disk after indexing but before propose runs.
        self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background, edited after indexing.\n\n"
            "## Details\n\nSee also [[Gamma]] for more detail.\n",
        )
        skipped: list[str] = []
        proposals = self._propose(assessment, batch, skipped_drifted=skipped)
        self.assertEqual(skipped, ["Alpha.md"])
        self.assertEqual(self._by_action(proposals, "fix_links_after_rename"), [])
        advisories = self._by_action(proposals, "review_dangling_references")
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0]["evidence"]["deleted_path"], "Beta.md")

    def test_ambiguous_rename_two_added_paths_produces_advisory_not_edits(self) -> None:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        self._write("BetaNew2.md", (self.vault / "BetaNew.md").read_text(encoding="utf-8"))
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
                _change("BetaNew2.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md", "BetaNew2.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        self.assertEqual(self._by_action(proposals, "fix_links_after_rename"), [])
        advisories = self._by_action(proposals, "review_dangling_references")
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0]["edits"], [])

    def test_rename_preconditions_pin_both_sides(self) -> None:
        beta_hash = self._index_hash("Beta.md")
        batch, assessment = self._rename_beta()
        proposals = self._propose(assessment, batch)
        fixes = self._by_action(proposals, "fix_links_after_rename")
        self.assertEqual(len(fixes), 1)
        pre = fixes[0]["rename_preconditions"]
        self.assertEqual(pre["removed_path"], "Beta.md")
        self.assertTrue(pre["removed_absent"])
        self.assertEqual(pre["added_path"], "BetaNew.md")
        self.assertEqual(pre["added_content_hash"], beta_hash)

    def test_referrer_with_multiple_occurrences_is_not_partially_rewritten(self) -> None:
        # A referrer that links to the renamed note more than once must not get
        # a partial edit (which would leave the other link dangling).
        self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] and again [[Beta]] here.\n",
        )
        build_index(
            self.vault, self.database, policy=IndexPolicy(),
            minimum_candidate_score=0.0, force=True,
        )
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                }
            ],
        )
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        for proposal in self._by_action(proposals, "fix_links_after_rename"):
            for edit in proposal["edits"]:
                self.assertNotEqual(edit["relative_path"], "Alpha.md")

    def test_conflicts_with_populated_when_two_renames_touch_one_referrer(self) -> None:
        beta_hash = self._index_hash("Beta.md")
        gamma_hash = self._index_hash("Gamma.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        (self.vault / "Gamma.md").rename(self.vault / "GammaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
                _change("Gamma.md", "removed", previous=gamma_hash),
                _change("GammaNew.md", "added", current=gamma_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                },
                {
                    "removed_path": "Gamma.md",
                    "added_paths": ["GammaNew.md"],
                    "content_hash": gamma_hash,
                    "inode_match": True,
                },
            ],
        )
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        fixes = self._by_action(proposals, "fix_links_after_rename")
        self.assertEqual(len(fixes), 2)
        ids = {proposal["proposal_id"] for proposal in fixes}
        for proposal in fixes:
            others = ids - {proposal["proposal_id"]}
            self.assertEqual(set(proposal["conflicts_with"]), others)


class AssignConflictsScopingTest(unittest.TestCase):
    def test_conflicts_are_scoped_by_source_not_path_alone(self) -> None:
        from recallweave.steward_propose import _assign_conflicts

        proposals = [
            {"proposal_id": "prp-a", "source": "vaultA",
             "edits": [{"relative_path": "Alpha.md"}]},
            {"proposal_id": "prp-b", "source": "vaultB",
             "edits": [{"relative_path": "Alpha.md"}]},
        ]
        _assign_conflicts(proposals)
        # Same path, different sources -> disjoint targets -> no conflict.
        self.assertEqual(proposals[0]["conflicts_with"], [])
        self.assertEqual(proposals[1]["conflicts_with"], [])

    def test_same_source_same_path_conflicts(self) -> None:
        from recallweave.steward_propose import _assign_conflicts

        proposals = [
            {"proposal_id": "prp-a", "source": "vault",
             "edits": [{"relative_path": "Alpha.md"}]},
            {"proposal_id": "prp-b", "source": "vault",
             "edits": [{"relative_path": "Alpha.md"}]},
        ]
        _assign_conflicts(proposals)
        self.assertEqual(proposals[0]["conflicts_with"], ["prp-b"])
        self.assertEqual(proposals[1]["conflicts_with"], ["prp-a"])


class DanglingReferencesTest(StewardProposeTest):
    def test_deleted_note_with_inbound_refs_is_advisory(self) -> None:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").unlink()
        batch = _batch(changes=[_change("Beta.md", "removed", previous=beta_hash)])
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        advisories = self._by_action(proposals, "review_dangling_references")
        self.assertEqual(len(advisories), 1)
        proposal = advisories[0]
        self.assertEqual(proposal["edits"], [])
        self.assertEqual(proposal["policy_level"], "propose_only")
        self.assertIn(
            "no automatic link rewrite: target resolution is not unique/decidable",
            proposal["non_actions"],
        )
        referrers = proposal["evidence"]["referrers"]
        self.assertEqual(referrers, [{"other_path": "Alpha.md", "kind": "wikilink"}])

    def test_deleted_note_with_no_inbound_refs_yields_no_dangling_advisory(self) -> None:
        # Echo.md is not linked from anywhere in this fixture, so deleting it
        # produces no DELETED-with-inbound-refs advisory. It still produces a
        # separate, independent review_broken_citations advisory: deleting a
        # note always invalidates that note's own section citations, which
        # is its own deterministic CITATION_BROKEN relation, unrelated to
        # whether anything links to it.
        echo_hash = self._index_hash("Echo.md")
        (self.vault / "Echo.md").unlink()
        batch = _batch(changes=[_change("Echo.md", "removed", previous=echo_hash)])
        assessment = self._assess(batch)
        proposals = self._propose(assessment, batch)
        self.assertEqual(self._by_action(proposals, "review_dangling_references"), [])
        broken = self._by_action(proposals, "review_broken_citations")
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["evidence"]["relative_path"], "Echo.md")


class BrokenCitationsTest(StewardProposeTest):
    def test_range_mismatch_produces_review_broken_citations(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        path = self._write(
            "Alpha.md",
            "# Alpha\n\nSee [[Beta]] for background -- rewritten.\n\n"
            "## Details\n\nSee also [[Gamma]] for more detail.\n",
        )
        batch = _batch(
            changes=[_change("Alpha.md", "modified", previous=before_hash, current=_hash(path))]
        )
        assessment = self._assess(batch)
        proposals = self._propose(assessment, None)
        advisories = self._by_action(proposals, "review_broken_citations")
        self.assertEqual(len(advisories), 1)
        proposal = advisories[0]
        self.assertEqual(proposal["edits"], [])
        citations = proposal["evidence"]["citations"]
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["reason"], "range_mismatch")

    def test_note_deleted_reason_produces_review_broken_citations(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        (self.vault / "Alpha.md").unlink()
        batch = _batch(changes=[_change("Alpha.md", "removed", previous=before_hash)])
        assessment = self._assess(batch)
        proposals = self._propose(assessment, None)
        advisories = self._by_action(proposals, "review_broken_citations")
        self.assertEqual(len(advisories), 1)
        for citation in advisories[0]["evidence"]["citations"]:
            self.assertEqual(citation["reason"], "note_deleted")

    def test_unreadable_reason_is_not_decidable_and_is_excluded(self) -> None:
        before_hash = self._index_hash("Alpha.md")
        (self.vault / "Alpha.md").unlink()
        batch = _batch(
            changes=[
                _change("Alpha.md", "modified", previous=before_hash, current="1" * 64)
            ]
        )
        assessment = self._assess(batch)
        broken = [a for a in assessment["assessments"] if a["relation"] == "CITATION_BROKEN"]
        self.assertEqual(len(broken), 1)
        self.assertTrue(
            all(c["reason"] == "unreadable" for c in broken[0]["inputs"]["broken_citations"])
        )
        proposals = self._propose(assessment, None)
        self.assertEqual(proposals, [])


class DuplicatesTest(StewardProposeTest):
    def test_duplicates_exact_bytes_is_advisory(self) -> None:
        echo_hash = self._index_hash("Echo.md")
        path = self._write("Zeta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        self.assertEqual(_hash(path), echo_hash)
        batch = _batch(changes=[_change("Zeta.md", "added", current=echo_hash)])
        assessment = self._assess(batch)
        proposals = self._propose(assessment, None)
        advisories = self._by_action(proposals, "review_duplicates")
        self.assertEqual(len(advisories), 1)
        proposal = advisories[0]
        self.assertEqual(proposal["edits"], [])
        self.assertEqual(proposal["evidence"]["duplicate_of"], ["Echo.md"])
        self.assertEqual(proposal["evidence"]["duplicate_in_batch"], [])


class DocumentInvariantsTest(StewardProposeTest):
    def _all_proposals(self) -> list[dict]:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        rename_batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        rename_assessment = self._assess(rename_batch)
        proposals = self._propose(rename_assessment, rename_batch)

        echo_hash = self._index_hash("Echo.md")
        self._write("Zeta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        dup_batch = _batch(changes=[_change("Zeta.md", "added", current=echo_hash)])
        dup_assessment = self._assess(dup_batch)
        proposals += self._propose(dup_assessment, None)
        return proposals

    def test_no_absolute_source_paths_anywhere(self) -> None:
        for proposal in self._all_proposals():
            for key, value in _all_string_leaves(proposal):
                if key == "database":
                    continue
                self.assertFalse(
                    Path(value).is_absolute(),
                    f"absolute path leaked under key {key!r}: {value!r}",
                )
                self.assertNotIn("://", value)

    def test_policy_level_always_propose_only(self) -> None:
        for proposal in self._all_proposals():
            self.assertEqual(proposal["policy_level"], POLICY_LEVEL)
            self.assertEqual(proposal["kind"], PROPOSAL_KIND)
            self.assertIn(proposal["action"], ACTIONS)

    def test_no_identity_keys_present(self) -> None:
        blocked = {"approver", "assignee", "role", "user", "account", "submitted_by"}

        def _walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn(key, blocked)
                    _walk(item)
            elif isinstance(value, list):
                for item in value:
                    _walk(item)

        for proposal in self._all_proposals():
            _walk(proposal)


class ProposalIdDeterminismTest(StewardProposeTest):
    def test_same_inputs_produce_byte_identical_proposals(self) -> None:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        assessment = self._assess(batch)
        first = self._propose(assessment, batch)
        second = self._propose(assessment, batch)
        self.assertEqual(
            json.dumps(first, sort_keys=True, ensure_ascii=True),
            json.dumps(second, sort_keys=True, ensure_ascii=True),
        )

    def test_id_changes_when_inputs_change(self) -> None:
        echo_hash = self._index_hash("Echo.md")
        self._write("Zeta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        batch1 = _batch(changes=[_change("Zeta.md", "added", current=echo_hash)])
        assessment1 = self._assess(batch1)
        proposals1 = self._propose(assessment1, None)

        self._write("Yotta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        batch2 = _batch(
            changes=[
                _change("Zeta.md", "added", current=echo_hash),
                _change("Yotta.md", "added", current=echo_hash),
            ]
        )
        assessment2 = self._assess(batch2)
        proposals2 = self._propose(assessment2, None)

        ids1 = {p["proposal_id"] for p in proposals1}
        ids2 = {p["proposal_id"] for p in proposals2}
        self.assertNotEqual(ids1, ids2)


class AssertionsAboutAsserterTest(StewardProposeTest):
    def test_asserter_constant_is_versioned(self) -> None:
        self.assertEqual(PROPOSE_ASSERTER, "recallweave.steward.propose.v1")


class ProposeLatestTest(StewardProposeTest):
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

    def _write_assessment_and_batch(self, timestamp: str) -> None:
        beta_hash = self._index_hash("Beta.md")
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        batch = _batch(
            changes=[
                _change("Beta.md", "removed", previous=beta_hash),
                _change("BetaNew.md", "added", current=beta_hash),
            ],
            rename_candidates=[
                {
                    "removed_path": "Beta.md",
                    "added_paths": ["BetaNew.md"],
                    "content_hash": beta_hash,
                    "inode_match": True,
                }
            ],
        )
        batch_path = self.dirs["changes"] / f"{timestamp}-vault.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        assessment = self._assess(batch)
        assessment["change_batch_ref"] = batch_path.name
        assessment_path = self.dirs["assessments"] / batch_path.name
        assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

    def test_end_to_end_and_idempotent_on_second_run(self) -> None:
        self._write_assessment_and_batch("20260101T000000Z")
        receipt = propose_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["kind"], "steward_propose_receipt")
        # A same-content rename yields three independent, correct advisories
        # from the underlying deterministic relations: the compiled link fix
        # (DELETED matched to a clean rename), a review_broken_citations for
        # the old path's now-stale section citations (CITATION_BROKEN fires
        # on any deleted note regardless of rename), and a review_duplicates
        # for the transient byte-identical old/new path pair the batch
        # briefly holds together (DUPLICATES_EXACT_BYTES compares the
        # renamed-in path against every path still on record, including the
        # one about to be removed).
        self.assertEqual(receipt["proposals_created"], 3)
        written = list(self.dirs["proposals"].glob("20260101T000000Z-vault-*.json"))
        self.assertEqual(len(written), 3)
        actions = {
            json.loads(path.read_text(encoding="utf-8"))["action"] for path in written
        }
        self.assertEqual(
            actions,
            {"fix_links_after_rename", "review_broken_citations", "review_duplicates"},
        )

        second = propose_latest(self.registry, self.state_root, self.database)
        self.assertEqual(second["proposals_created"], 0)
        self.assertEqual(
            second["per_source"], [{"source": "vault", "reason": "already_proposed"}]
        )

    def _rename_assessment(self, ts: str, removed: str, added: str) -> None:
        removed_hash = self._index_hash(removed)
        (self.vault / removed).rename(self.vault / added)
        batch = _batch(
            changes=[
                _change(removed, "removed", previous=removed_hash),
                _change(added, "added", current=removed_hash),
            ],
            rename_candidates=[
                {"removed_path": removed, "added_paths": [added],
                 "content_hash": removed_hash}
            ],
        )
        batch_path = self.dirs["changes"] / f"{ts}-vault.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        assessment = self._assess(batch)
        assessment["change_batch_ref"] = batch_path.name
        (self.dirs["assessments"] / batch_path.name).write_text(
            json.dumps(assessment), encoding="utf-8"
        )

    def test_reemitted_applied_proposal_does_not_poison_new_conflict(self) -> None:
        # P applied, P re-emitted under a new assessment filename, Q touches the
        # same referrer: Q must NOT carry the terminal P id in conflicts_with.
        self._rename_assessment("20260101T000000Z", "Beta.md", "BetaNew.md")
        propose_latest(self.registry, self.state_root, self.database)
        beta_fix = [
            p for p in self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0]
        marked = json.loads(beta_fix.read_text())
        marked["status"] = "applied"
        beta_fix.write_text(json.dumps(marked), encoding="utf-8")
        beta_id = marked["proposal_id"]

        # Re-emit P's assessment (same id, applied) + a fresh Q on the same referrer.
        for sub in ("changes", "assessments"):
            src = self.dirs[sub] / "20260101T000000Z-vault.json"
            dst = self.dirs[sub] / "20260102T000000Z-vault.json"
            doc = json.loads(src.read_text())
            if sub == "assessments":
                doc["change_batch_ref"] = "20260102T000000Z-vault.json"
            dst.write_text(json.dumps(doc), encoding="utf-8")
        self._rename_assessment("20260103T000000Z", "Gamma.md", "GammaNew.md")
        propose_latest(self.registry, self.state_root, self.database)

        gamma_fix = json.loads([
            p for p in self.dirs["proposals"].glob("20260103T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0].read_text())
        self.assertNotIn(
            beta_id, gamma_fix["conflicts_with"],
            "a new proposal conflicts with a re-emitted APPLIED proposal",
        )

    def test_reemitted_duplicate_syncs_conflict_onto_existing(self) -> None:
        # A re-emitted proposal P (id already on disk) that newly conflicts with
        # a fresh proposal Q must sync the conflict onto the on-disk P too, so P
        # is not left conflict-free-and-applyable.
        self._rename_assessment("20260101T000000Z", "Beta.md", "BetaNew.md")
        propose_latest(self.registry, self.state_root, self.database)
        beta_fix = [
            p for p in self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0]
        beta_id = json.loads(beta_fix.read_text())["proposal_id"]
        self.assertEqual(json.loads(beta_fix.read_text())["conflicts_with"], [])

        # Re-emit Beta's batch+assessment under a new timestamp (same id P), and
        # add a Gamma rename (Q) touching the same referrer (Alpha).
        import shutil
        for sub in ("changes", "assessments"):
            src = self.dirs[sub] / "20260101T000000Z-vault.json"
            dst = self.dirs[sub] / "20260102T000000Z-vault.json"
            doc = json.loads(src.read_text())
            if sub == "assessments":
                doc["change_batch_ref"] = "20260102T000000Z-vault.json"
            dst.write_text(json.dumps(doc), encoding="utf-8")
        self._rename_assessment("20260103T000000Z", "Gamma.md", "GammaNew.md")
        propose_latest(self.registry, self.state_root, self.database)

        # On-disk P now declares the conflict with Q (not left conflict-free).
        beta_doc = json.loads(beta_fix.read_text())
        gamma_fix = json.loads([
            p for p in self.dirs["proposals"].glob("20260103T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0].read_text())
        self.assertIn(gamma_fix["proposal_id"], beta_doc["conflicts_with"])
        self.assertIn(beta_id, gamma_fix["conflicts_with"])

    def test_applied_proposal_excluded_from_new_conflict_set(self) -> None:
        # An APPLIED proposal for a path must not appear in a later proposal's
        # conflicts_with (which would make _validate_proposal reject the new work
        # against a terminal counterpart).
        self._rename_assessment("20260101T000000Z", "Beta.md", "BetaNew.md")
        propose_latest(self.registry, self.state_root, self.database)
        beta_fix = [
            p for p in self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0]
        marked = json.loads(beta_fix.read_text())
        marked["status"] = "applied"
        beta_fix.write_text(json.dumps(marked), encoding="utf-8")
        beta_id = marked["proposal_id"]

        self._rename_assessment("20260102T000000Z", "Gamma.md", "GammaNew.md")
        propose_latest(self.registry, self.state_root, self.database)
        gamma_fix = json.loads([
            p for p in self.dirs["proposals"].glob("20260102T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ][0].read_text())
        self.assertNotIn(
            beta_id, gamma_fix["conflicts_with"],
            "a new proposal conflicts with an already-applied one",
        )

    def test_conflict_is_synced_onto_existing_pending_proposal(self) -> None:
        # Two separate assessments each rewrite the same referrer (Alpha.md,
        # which links to both Beta and Gamma). The second run must record the
        # conflict on BOTH the new AND the already-written proposal.
        self._rename_assessment("20260101T000000Z", "Beta.md", "BetaNew.md")
        first = propose_latest(self.registry, self.state_root, self.database)
        beta_fix = [
            p for p in self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ]
        self.assertEqual(len(beta_fix), 1)
        self.assertEqual(
            json.loads(beta_fix[0].read_text())["conflicts_with"], []
        )

        self._rename_assessment("20260102T000000Z", "Gamma.md", "GammaNew.md")
        propose_latest(self.registry, self.state_root, self.database)

        beta_doc = json.loads(beta_fix[0].read_text())
        gamma_fix = [
            p for p in self.dirs["proposals"].glob("20260102T000000Z-vault-*.json")
            if json.loads(p.read_text())["action"] == "fix_links_after_rename"
        ]
        self.assertEqual(len(gamma_fix), 1)
        gamma_doc = json.loads(gamma_fix[0].read_text())
        # Both proposals now declare the conflict with each other.
        self.assertIn(gamma_doc["proposal_id"], beta_doc["conflicts_with"])
        self.assertIn(beta_doc["proposal_id"], gamma_doc["conflicts_with"])

    def test_reemitted_batch_does_not_create_duplicate_proposal_id(self) -> None:
        # A re-emitted batch (same changes under a new timestamp) must not
        # produce a second proposal file with the same deterministic id.
        self._write_assessment_and_batch("20260101T000000Z")
        propose_latest(self.registry, self.state_root, self.database)
        first = list(self.dirs["proposals"].glob("*.json"))
        ids = {json.loads(p.read_text())["proposal_id"] for p in first}
        # Duplicate the batch+assessment under a NEW timestamp (re-emit).
        import shutil
        for sub in ("changes", "assessments"):
            src = self.dirs[sub] / "20260101T000000Z-vault.json"
            dst = self.dirs[sub] / "20260102T000000Z-vault.json"
            doc = json.loads(src.read_text())
            if sub == "assessments":
                doc["change_batch_ref"] = "20260102T000000Z-vault.json"
            dst.write_text(json.dumps(doc), encoding="utf-8")
        propose_latest(self.registry, self.state_root, self.database)
        # No proposal id appears in more than one file.
        seen: dict[str, int] = {}
        for p in self.dirs["proposals"].glob("*.json"):
            pid = json.loads(p.read_text())["proposal_id"]
            seen[pid] = seen.get(pid, 0) + 1
        self.assertTrue(all(n == 1 for n in seen.values()), f"duplicate ids: {seen}")
        self.assertEqual(set(seen), ids)

    def test_foreign_assessment_is_skipped_not_fatal(self) -> None:
        # A stale assessment from a prior registry revision must be SKIPPED, not
        # raise -- otherwise it would wedge every propose/sweep until removed.
        self._write_assessment_and_batch("20260102T000000Z")  # valid current one
        (self.dirs["assessments"] / "20260101T000000Z-vault.json").write_text(
            json.dumps({
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "assessment_batch",
                "source": "vault",
                "registry_sha256": "some-foreign-digest",
                "change_batch_ref": "20260101T000000Z-vault.json",
                "summary": {}, "assessments": [], "covered_paths": [],
            }),
            encoding="utf-8",
        )
        # Must not raise; the current assessment still yields proposals.
        receipt = propose_latest(self.registry, self.state_root, self.database)
        self.assertGreaterEqual(receipt["proposals_created"], 1)

    def test_no_assessment_is_reported_and_skipped(self) -> None:
        receipt = propose_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["proposals_created"], 0)
        self.assertEqual(
            receipt["per_source"], [{"source": "vault", "reason": "no_assessment"}]
        )

    def test_every_assessment_is_proposed_not_just_newest(self) -> None:
        # Two assessments recorded before proposing: an earlier deletion must
        # still produce its proposal even though a later assessment exists.
        gamma_hash = self._index_hash("Gamma.md")
        (self.vault / "Gamma.md").unlink()
        early_batch = _batch(
            changes=[_change("Gamma.md", "removed", previous=gamma_hash)]
        )
        early_batch_path = self.dirs["changes"] / "20260101T000000Z-vault.json"
        early_batch_path.write_text(json.dumps(early_batch), encoding="utf-8")
        early = self._assess(early_batch)
        early["change_batch_ref"] = early_batch_path.name
        (self.dirs["assessments"] / early_batch_path.name).write_text(
            json.dumps(early), encoding="utf-8"
        )
        # A later, empty assessment.
        later_batch = _batch(changes=[])
        later_batch_path = self.dirs["changes"] / "20260102T000000Z-vault.json"
        later_batch_path.write_text(json.dumps(later_batch), encoding="utf-8")
        later = self._assess(later_batch)
        later["change_batch_ref"] = later_batch_path.name
        (self.dirs["assessments"] / later_batch_path.name).write_text(
            json.dumps(later), encoding="utf-8"
        )

        receipt = propose_latest(self.registry, self.state_root, self.database)
        # Gamma's deletion (from the earlier assessment) produced a proposal.
        written = list(
            self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
        )
        self.assertTrue(written, "earlier assessment produced no proposal")
        self.assertGreaterEqual(receipt["proposals_created"], 1)

    def test_assess_then_propose_two_batches_end_to_end(self) -> None:
        # Full stage pairing: two change batches (earlier deletion, later empty)
        # observed before assessment. assess_latest must assess both, and
        # propose_latest must then emit a proposal for the earlier deletion.
        gamma_hash = self._index_hash("Gamma.md")
        (self.vault / "Gamma.md").unlink()
        early = _batch(changes=[_change("Gamma.md", "removed", previous=gamma_hash)])
        (self.dirs["changes"] / "20260101T000000Z-vault.json").write_text(
            json.dumps(early), encoding="utf-8"
        )
        later = _batch(changes=[])
        (self.dirs["changes"] / "20260102T000000Z-vault.json").write_text(
            json.dumps(later), encoding="utf-8"
        )
        assess_receipt = assess_latest(self.registry, self.state_root, self.database)
        self.assertEqual(len(assess_receipt["assessed"]), 2)
        propose_latest(self.registry, self.state_root, self.database)
        self.assertTrue(
            list(self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")),
            "earlier deletion produced no proposal after full assess+propose",
        )

    def test_partial_write_is_completed_on_rerun(self) -> None:
        # Simulate a crash that wrote only ONE of the assessment's proposals:
        # the next run must create the missing proposals, not treat the single
        # existing file as a completion marker and skip the rest.
        self._write_assessment_and_batch("20260101T000000Z")
        all_three = propose_latest(self.registry, self.state_root, self.database)
        self.assertEqual(all_three["proposals_created"], 3)
        written = sorted(
            self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
        )
        self.assertEqual(len(written), 3)
        # Delete two of the three to mimic a crash after the first write.
        for path in written[1:]:
            path.unlink()
        receipt = propose_latest(self.registry, self.state_root, self.database)
        self.assertEqual(receipt["proposals_created"], 2)
        self.assertEqual(
            len(list(self.dirs["proposals"].glob("20260101T000000Z-vault-*.json"))),
            3,
        )

    def test_applied_proposal_is_not_overwritten_on_rerun(self) -> None:
        self._write_assessment_and_batch("20260101T000000Z")
        propose_latest(self.registry, self.state_root, self.database)
        written = sorted(
            self.dirs["proposals"].glob("20260101T000000Z-vault-*.json")
        )
        # Mark one proposal applied, as steward-apply would.
        marked = json.loads(written[0].read_text(encoding="utf-8"))
        marked["status"] = "applied"
        written[0].write_text(json.dumps(marked), encoding="utf-8")
        propose_latest(self.registry, self.state_root, self.database)
        after = json.loads(written[0].read_text(encoding="utf-8"))
        self.assertEqual(after["status"], "applied")


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


class StewardProposeCliTest(StewardProposeTest):
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
        echo_hash = self._index_hash("Echo.md")
        self._write("Zeta.md", (self.vault / "Echo.md").read_text(encoding="utf-8"))
        batch = _batch(changes=[_change("Zeta.md", "added", current=echo_hash)])
        batch_path = self.dirs["changes"] / "20260101T000000Z-vault.json"
        batch_path.write_text(json.dumps(batch), encoding="utf-8")
        assessment = self._assess(batch)
        assessment["change_batch_ref"] = batch_path.name
        # The CLI loads the registry from disk, so the digest binding is
        # strict: the assessment must carry the real registry digest.
        assessment["registry_sha256"] = hashlib.sha256(
            self.sources_path.read_bytes()
        ).hexdigest()
        (self.dirs["assessments"] / batch_path.name).write_text(
            json.dumps(assessment), encoding="utf-8"
        )

        exit_code, stdout, stderr = _run_cli(
            "steward-propose",
            str(self.sources_path),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_dir),
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = _decode_single_json(stdout)
        self.assertEqual(payload["kind"], "steward_propose_receipt")
        self.assertEqual(payload["proposals_created"], 1)
        written = list(self.dirs["proposals"].glob("20260101T000000Z-vault-*.json"))
        self.assertEqual(len(written), 1)

    def test_cli_missing_registry_emits_error_envelope(self) -> None:
        missing_sources = self.root / "does-not-exist.json"
        exit_code, stdout, stderr = _run_cli(
            "steward-propose",
            str(missing_sources),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_dir),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        payload = _decode_single_json(stderr)
        self.assertEqual(payload["operation"], "steward-propose")


if __name__ == "__main__":
    unittest.main()
