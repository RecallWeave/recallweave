from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave.cli import _parser
from recallweave.steward_state import STEWARD_SCHEMA_VERSION
from recallweave.cli import main as cli_main
from recallweave.index import build_index
from recallweave.policy import IndexPolicy
from recallweave.steward_assess import assess_latest
from recallweave.steward_observe import observe_registry
from recallweave.steward_propose import propose_latest
from recallweave.steward_sources import SOURCES_SPEC_VERSION, SourceRegistry
from recallweave.steward_state import ensure_state_layout
from recallweave.steward_sweep import (
    REPORT_EVIDENCE_LIMIT,
    REPORT_KIND,
    STATUS_KIND,
    SWEEP_EXIT_CODES,
    SWEEP_RESULTS,
    _assemble_report,
    _atomic_write_text,
    status_report,
    sweep_registry,
)


class PruneAnchorTest(unittest.TestCase):
    def test_prune_open_refuses_swapped_dir_even_if_precheck_bypassed(self) -> None:
        # The prune must open the directory once O_NOFOLLOW and delete relative to
        # that descriptor, so a directory swapped for a symlink AFTER the
        # is_link_like precheck cannot make it enumerate and unlink files in the
        # symlink target (a vault). Forcing the precheck to pass proves the
        # descriptor-relative open still refuses the symlink. Regression for the
        # pathname iterdir/unlink race.
        import recallweave.steward_sweep as _sw

        if not _sw._DIR_FD_PRUNE:
            self.skipTest("descriptor-relative pruning unavailable")
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            external = base / "external"
            external.mkdir()
            victim = external / "old-victim.md"
            victim.write_text("x", encoding="utf-8")
            os.utime(victim, (0, 0))  # ancient mtime: would be prunable
            link = base / "reports"
            try:
                link.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with patch("recallweave.steward_sweep.is_link_like", return_value=False):
                with self.assertRaises(ValueError):
                    _sw._prune_dir(link, cutoff_epoch=1e18)
            self.assertTrue(victim.exists(), "prune deleted a file through a symlink")

    def test_prune_refuses_swapped_state_root_ancestor(self) -> None:
        # O_NOFOLLOW on the target directory alone protects only its final
        # component. If the STATE ROOT (the target's parent) is swapped for a
        # symlink to an external tree that itself contains a real reports/ dir,
        # pruning must still refuse -- the parent is opened O_NOFOLLOW and the
        # target reached relative to it. Regression for the ancestor-replacement
        # bypass.
        import recallweave.steward_sweep as _sw

        if not _sw._DIR_FD_PRUNE:
            self.skipTest("descriptor-relative pruning unavailable")
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            state_root = base / "state"
            (state_root / "reports").mkdir(parents=True)
            external = base / "external"
            (external / "reports").mkdir(parents=True)
            victim = external / "reports" / "old-victim.json"
            victim.write_text("{}", encoding="utf-8")
            os.utime(victim, (0, 0))  # ancient: would be prunable
            # Swap the state root for a symlink to the external tree. The target
            # `reports` is a real directory in the external tree, so only the
            # parent (state root) symlink can be caught.
            os.rename(state_root, base / "state-real")
            try:
                state_root.symlink_to(external, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaisesRegex(
                ValueError, "symlinked or missing state root"
            ):
                _sw._prune_dir(state_root / "reports", cutoff_epoch=1e18)
            self.assertTrue(victim.exists(), "prune deleted through a swapped state root")

    def test_prune_fails_closed_without_dir_fd_support(self) -> None:
        # On a platform without descriptor-relative deletion, pruning must refuse
        # (delete nothing) rather than fall back to a pathname race, since a prune
        # is irreversible. Regression for the fail-closed fallback.
        import recallweave.steward_sweep as _sw

        with tempfile.TemporaryDirectory() as name:
            directory = Path(name) / "reports"
            directory.mkdir()
            old = directory / "old.json"
            old.write_text("{}", encoding="utf-8")
            os.utime(old, (0, 0))  # ancient: would be prunable
            with patch("recallweave.steward_sweep._DIR_FD_PRUNE", False):
                with self.assertRaisesRegex(ValueError, "descriptor-relative deletion"):
                    _sw._prune_dir(directory, cutoff_epoch=1e18)
            self.assertTrue(old.exists(), "fail-closed prune still deleted a file")


class MarkdownReportAnchorTest(unittest.TestCase):
    def test_markdown_report_refuses_symlinked_state_root_ancestor(self) -> None:
        # The Markdown report projection must use the shared descriptor-relative
        # writer, so a state root swapped for a symlink cannot redirect the
        # report (temp + final file) outside the state tree -- e.g. into a vault.
        # Reaching `within` (the reports dir) through a symlinked state root must
        # be refused, not followed. Regression for the pathname-mkstemp writer.
        import recallweave.steward_state as _st

        if not _st._DIR_FD_STATE_WRITES:
            self.skipTest("descriptor-relative writes unavailable")
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            real_root = base / "state_real"
            (real_root / "reports").mkdir(parents=True)
            link_root = base / "state"
            try:
                link_root.symlink_to(real_root, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            within = link_root / "reports"  # reached through the symlinked root
            target = within / "20260101T000000000000Z-src-sweep.md"
            with self.assertRaisesRegex(ValueError, "symlinked or missing state root"):
                _atomic_write_text(target, "# report body", within=within)
            self.assertEqual(list((real_root / "reports").iterdir()), [])


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


class StewardSweepTest(unittest.TestCase):
    """Shared fixture: Alpha.md links to Beta.md and Gamma.md; Echo.md is
    standalone. Indexed once in setUp; individual tests mutate the on-disk
    vault and drive the sweep through it.

    Change/removal/rename detection is checkpoint-based (steward_observe
    compares against the *previous* observe run in the same state
    directory), so a test that wants to see a removal, a rename, or a
    modification-vs-addition distinction must first establish a baseline
    with one sweep over the untouched tree, then mutate, then sweep again.
    Sweep report/batch/assessment filenames carry second-precision UTC
    timestamps, so ``_settle`` pauses just past a second boundary between
    two sweeps in the same test to guarantee distinct filenames.
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
            "## Details\n\nSee also [[Gamma]] for more detail.\n",
        )
        self._write("Beta.md", "# Beta\n\nBeta body content.\n")
        self._write("Gamma.md", "# Gamma\n\nGamma content, standalone.\n")
        self._write("Echo.md", "# Echo\n\nEcho body unique text.\n")

        build_index(
            self.vault, self.database, policy=IndexPolicy(), minimum_candidate_score=0.0
        )

        self.state_root = self.root / "state"
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

    def _write(self, relative: str, text: str) -> Path:
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def _registry(self) -> SourceRegistry:
        return SourceRegistry.from_file(self.sources_path)

    def _sweep(self, **kwargs) -> dict:
        return sweep_registry(self._registry(), self.state_root, self.database, **kwargs)

    def _dirs(self) -> dict[str, Path]:
        return ensure_state_layout(self.state_root)

    def _settle(self) -> None:
        # Artifact filenames carry microsecond precision and assessments are
        # bound to the batch content digest, so back-to-back sweeps no longer
        # collide; kept as a no-op seam for the call sites below.
        pass

    def _baseline(self) -> dict:
        """Establish a checkpoint over the untouched tree; always no_change."""
        report = self._sweep()
        self.assertEqual(report["result"], "no_change")
        self._settle()
        return report


class NoChangeResultTest(StewardSweepTest):
    def test_unchanged_tree_sweep_is_no_change_exit0_and_writes_report(self) -> None:
        report = self._sweep()
        self.assertEqual(report["result"], "no_change")
        self.assertEqual(report["kind"], REPORT_KIND)
        self.assertEqual(report["proposals"]["created_this_sweep"], 0)
        self.assertEqual(report["proposals"]["pending_total"], 0)
        reports = list(self._dirs()["reports"].glob("*-sweep.json"))
        self.assertEqual(len(reports), 1)
        on_disk = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(on_disk, report)

    def test_cli_unchanged_tree_exits_zero(self) -> None:
        exit_code, stdout, stderr = _run_cli(
            "steward-sweep",
            str(self.sources_path),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_root),
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = _decode_single_json(stdout)
        self.assertEqual(payload["result"], "no_change")


class FindingsResultTest(StewardSweepTest):
    def test_modification_with_no_proposal_worthy_outcome_is_findings_exit3(self) -> None:
        self._baseline()

        # Append after Echo's existing content: content_hash changes (MODIFIED)
        # but the previously-indexed section's line range still matches the
        # unchanged prefix, so no CITATION_BROKEN fires, and Echo has no
        # inbound authored references or duplicate bytes -- MODIFIED is the
        # only relation this produces, and propose has nothing actionable for
        # a bare MODIFIED.
        self._write(
            "Echo.md",
            "# Echo\n\nEcho body unique text.\n\nExtra info now added.\n",
        )
        report = self._sweep()
        self.assertEqual(report["result"], "findings")
        self.assertGreaterEqual(report["assessments"]["MODIFIED"], 1)
        self.assertEqual(report["proposals"]["created_this_sweep"], 0)
        self.assertEqual(report["proposals"]["pending_total"], 0)
        self.assertEqual(report["integrity"]["broken_citations"], [])

    def test_cli_findings_exits_three(self) -> None:
        self._baseline()
        self._write(
            "Echo.md",
            "# Echo\n\nEcho body unique text.\n\nExtra info now added.\n",
        )
        exit_code, _stdout, stderr = _run_cli(
            "steward-sweep",
            str(self.sources_path),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_root),
        )
        self.assertEqual(exit_code, SWEEP_EXIT_CODES["findings"], stderr)


class RenameApprovalRequiredTest(StewardSweepTest):
    def test_rename_with_referrers_is_approval_required_and_counts_proposals(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        report = self._sweep()
        self.assertEqual(report["result"], "approval_required")
        self.assertGreaterEqual(report["proposals"]["created_this_sweep"], 1)
        self.assertEqual(
            report["proposals"]["pending_total"], report["proposals"]["created_this_sweep"]
        )
        self.assertIn("fix_links_after_rename", report["proposals"]["by_action"])

    def test_cli_rename_exits_four(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        exit_code, stdout, stderr = _run_cli(
            "steward-sweep",
            str(self.sources_path),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_root),
        )
        self.assertEqual(exit_code, SWEEP_EXIT_CODES["approval_required"], stderr)
        payload = _decode_single_json(stdout)
        self.assertEqual(payload["kind"], REPORT_KIND)
        self.assertEqual(payload["result"], "approval_required")


class ApplyDetailsBoundTest(StewardSweepTest):
    def test_apply_details_are_bounded_in_report(self) -> None:
        # sweep_auto_apply records one skipped entry per non-auto proposal and
        # per proposal beyond the apply cap; a large backlog must not embed an
        # unbounded list in the report. The apply detail lists get the same
        # count/char budget and truncation flag as the integrity arrays.
        from recallweave.steward_state import ensure_state_layout

        registry = self._registry()
        dirs = ensure_state_layout(self.state_root)
        apply_summary = {
            "applied": [],
            "skipped": [
                {"proposal": f"prp-{i:016d}", "reason": "not_auto_apply"}
                for i in range(REPORT_EVIDENCE_LIMIT + 5)
            ],
            "failures": [],
            "mutations": 0,
        }
        report = _assemble_report(
            registry,
            dirs,
            self.database,
            generated_at="2026-01-01T00:00:00+00:00",
            observe_receipt={"sources": []},
            proposals_created_this_sweep=0,
            apply_summary=apply_summary,
        )
        self.assertEqual(len(report["apply"]["skipped"]), REPORT_EVIDENCE_LIMIT)
        truncated = report["integrity"]["evidence_truncated"]
        self.assertIn("apply.skipped", truncated)
        self.assertEqual(truncated["apply.skipped"]["total"], REPORT_EVIDENCE_LIMIT + 5)
        self.assertEqual(truncated["apply.skipped"]["reported"], REPORT_EVIDENCE_LIMIT)

    def test_apply_details_bounded_by_character_budget(self) -> None:
        # The char budget (not only the element count) applies to apply details,
        # across all three arrays: a single oversized entry is omitted and flagged.
        from recallweave.steward_state import ensure_state_layout

        registry = self._registry()
        dirs = ensure_state_layout(self.state_root)
        oversized = {"proposal": "prp-huge", "note": "y" * 250_000}
        apply_summary = {
            "applied": [oversized, {"proposal": "prp-small", "mutations": 1}],
            "skipped": [],
            "failures": [{"proposal": "prp-f", "error": "X", "rolled_back": True}],
            "mutations": 1,
        }
        report = _assemble_report(
            registry,
            dirs,
            self.database,
            generated_at="2026-01-01T00:00:00+00:00",
            observe_receipt={"sources": []},
            proposals_created_this_sweep=0,
            apply_summary=apply_summary,
        )
        self.assertNotIn(oversized, report["apply"]["applied"])
        truncated = report["integrity"]["evidence_truncated"]
        self.assertIn("apply.applied", truncated)
        # The full summary still drove classification: the real rollback failure
        # is reflected in the result, not lost to display truncation.
        self.assertEqual(report["result"], "validation_failed_rolled_back")


class PendingCarryoverTest(StewardSweepTest):
    def test_pending_proposals_keep_approval_required_on_next_no_change_sweep(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        first = self._sweep()
        self.assertEqual(first["result"], "approval_required")
        self.assertGreaterEqual(first["proposals"]["created_this_sweep"], 1)

        self._settle()
        second = self._sweep()
        self.assertEqual(second["result"], "approval_required")
        self.assertEqual(second["proposals"]["created_this_sweep"], 0)
        self.assertEqual(
            second["proposals"]["pending_total"], first["proposals"]["pending_total"]
        )


class IntegritySectionTest(StewardSweepTest):
    def test_integrity_lists_broken_citations_and_dangling_references(self) -> None:
        self._baseline()
        # Delete Beta.md outright (no matching added path, so no rename
        # candidate): Alpha's wikilink to it becomes a dangling reference, and
        # Beta's own section citation is broken (reason note_deleted).
        (self.vault / "Beta.md").unlink()
        report = self._sweep()
        self.assertEqual(report["result"], "approval_required")
        self.assertIn("Beta.md", report["integrity"]["dangling_references"])
        self.assertTrue(report["integrity"]["broken_citations"])
        for citation in report["integrity"]["broken_citations"]:
            self.assertTrue(citation.startswith("Beta.md:"))


class ReportJsonShapeTest(StewardSweepTest):
    def test_report_is_json_serializable_with_sorted_deterministic_lists(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        report = self._sweep()
        reserialized = json.loads(json.dumps(report, ensure_ascii=True, sort_keys=True))
        self.assertEqual(reserialized, report)
        for key in (
            "broken_citations",
            "dangling_references",
            "duplicates",
            "sources_missing",
            "checkpoint_invalid",
        ):
            values = report["integrity"][key]
            self.assertEqual(values, sorted(values))
        self.assertEqual(
            list(report["proposals"]["by_action"]), sorted(report["proposals"]["by_action"])
        )
        self.assertEqual(list(report["changes"]), sorted(report["changes"]))


class MarkdownDefaultFormatTest(StewardSweepTest):
    def test_default_format_writes_only_json(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").unlink()
        self._sweep()
        dirs = self._dirs()
        self.assertEqual(len(list(dirs["reports"].glob("*-sweep.json"))), 2)
        self.assertEqual(len(list(dirs["reports"].glob("*-sweep.md"))), 0)


class MarkdownProjectionFencingTest(StewardSweepTest):
    def test_markdown_format_writes_md_and_fences_vault_derived_strings(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").unlink()
        report = self._sweep(report_format="markdown")
        dirs = self._dirs()
        md_files = list(dirs["reports"].glob("*-sweep.md"))
        self.assertEqual(len(md_files), 1)
        markdown = md_files[0].read_text(encoding="utf-8")

        self.assertTrue(report["integrity"]["broken_citations"])
        for citation in report["integrity"]["broken_citations"]:
            self.assertIn(f"```text\n{citation}\n```", markdown)
        self.assertIn("Beta.md", report["integrity"]["dangling_references"])
        for path in report["integrity"]["dangling_references"]:
            self.assertIn(f"```text\n{path}\n```", markdown)

        self.assertTrue(markdown.startswith("# Stewardship report"))
        self.assertLess(markdown.index("## Integrity"), markdown.index("## Changes"))


class CompositionPropertyTest(StewardSweepTest):
    def test_sweep_matches_manual_stage_composition(self) -> None:
        registry = self._registry()
        state_manual = self.root / "state-manual"
        state_sweep = self.root / "state-sweep"

        # Seed identical baseline checkpoints in both (separate) state dirs
        # before mutating the shared vault, exactly as StewardSweepTest's own
        # fixture requires: checkpoint-based rename/removal detection needs a
        # prior observe run in *that* state directory to diff against.
        observe_registry(registry, state_manual)
        sweep_registry(registry, state_sweep, self.database)
        self._settle()

        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")

        observe_receipt = observe_registry(registry, state_manual)
        assess_latest(registry, state_manual, self.database)
        propose_receipt = propose_latest(registry, state_manual, self.database)
        manual_report = _assemble_report(
            registry,
            ensure_state_layout(state_manual),
            self.database,
            generated_at="2026-01-01T00:00:00+00:00",
            observe_receipt=observe_receipt,
            proposals_created_this_sweep=propose_receipt["proposals_created"],
        )

        sweep_report = sweep_registry(registry, state_sweep, self.database)

        for key in ("integrity", "changes", "assessments", "proposals"):
            self.assertEqual(manual_report[key], sweep_report[key], key)


class ParserContractTest(unittest.TestCase):
    def test_no_scheduler_flags_anywhere_in_parser(self) -> None:
        forbidden = {"--daemon", "--serve", "--watch", "--interval"}
        parser = _parser()
        found: set[str] = set()

        def _walk(sub_parser) -> None:
            for action in sub_parser._actions:
                found.update(action.option_strings)
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    for child in choices.values():
                        _walk(child)

        _walk(parser)
        self.assertEqual(found & forbidden, set())

    def test_sweep_flags_are_subset_of_stage_flags_plus_format(self) -> None:
        parser = _parser()
        subparsers_action = next(
            action for action in parser._actions if action.dest == "command"
        )
        by_name = subparsers_action.choices

        def _flags(name: str) -> set[str]:
            return {
                option
                for action in by_name[name]._actions
                for option in action.option_strings
            }

        stage_union = (
            _flags("steward-observe")
            | _flags("steward-assess")
            | _flags("steward-propose")
            | _flags("steward-apply")
        )
        allowed = stage_union | {"--format", "--apply"}
        sweep_flags = _flags("steward-sweep")
        self.assertTrue(sweep_flags <= allowed, sweep_flags - allowed)


class StatusReportTest(StewardSweepTest):
    def test_status_counts_and_lock_state(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        self._sweep()
        status = status_report(self.state_root)
        self.assertEqual(status["kind"], STATUS_KIND)
        self.assertGreaterEqual(status["counts"]["change_batches"], 1)
        self.assertGreaterEqual(status["counts"]["assessments"], 1)
        self.assertGreaterEqual(status["counts"]["proposals_pending"], 1)
        self.assertGreaterEqual(status["counts"]["reports"], 1)
        self.assertIsNotNone(status["newest_report"])
        self.assertEqual(status["newest_report"]["result"], "approval_required")
        self.assertEqual(
            status["lock"], {"present": False, "pid": None, "acquired_at": None}
        )
        self.assertEqual(status["backups_total_bytes"], 0)
        self.assertTrue(all(status["subdirs"].values()))
        for key, value in status["subdirs"].items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, bool)

    def test_status_exit_zero_via_cli(self) -> None:
        exit_code, stdout, stderr = _run_cli(
            "steward-status", str(self.sources_path), "--state-dir", str(self.state_root)
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = _decode_single_json(stdout)
        self.assertEqual(payload["kind"], STATUS_KIND)

    def test_status_missing_registry_emits_error_envelope(self) -> None:
        missing = self.root / "does-not-exist.json"
        exit_code, stdout, stderr = _run_cli(
            "steward-status", str(missing), "--state-dir", str(self.state_root)
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        payload = _decode_single_json(stderr)
        self.assertEqual(payload["operation"], "steward-status")


class ReportBacklogAggregationTest(StewardSweepTest):
    def _write_assessment(self, name: str, records: list) -> None:
        counts: dict[str, int] = {}
        for r in records:
            counts[r["relation"]] = counts.get(r["relation"], 0) + 1
        (self._dirs()["assessments"] / name).write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "vault",
                    "registry_sha256": self._registry().registry_sha256,
                    "summary": counts,
                    "assessments": records,
                }
            ),
            encoding="utf-8",
        )

    def test_unresolved_finding_persists_across_unrelated_later_batch(self) -> None:
        # Assessments are incremental: a DELETED recorded in one batch, with a
        # later batch touching only an unrelated path, must still be reported.
        from recallweave.steward_sweep import _aggregate_assessments

        self._write_assessment(
            "20260101T000000Z-vault.json",
            [{"relation": "DELETED", "relative_path": "Gone.md"}],
        )
        self._write_assessment(
            "20260102T000000Z-vault.json",
            [{"relation": "MODIFIED", "relative_path": "Other.md"}],
        )
        summary, _b, _d = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["DELETED"], 1, "an unresolved deletion was dropped")
        self.assertEqual(summary["MODIFIED"], 1)

    def test_broken_citation_persists_until_path_reassessed(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        self._write_assessment(
            "20260101T000000Z-vault.json",
            [{
                "relation": "CITATION_BROKEN", "relative_path": "A.md",
                "inputs": {"broken_citations": [{"citation": "A.md:1-2"}]},
            }],
        )
        self._write_assessment(
            "20260102T000000Z-vault.json",
            [{"relation": "MODIFIED", "relative_path": "B.md"}],
        )
        summary, broken, _d = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["CITATION_BROKEN"], 1)
        self.assertIn("A.md:1-2", broken)

    def test_duplicate_persists_when_unrelated_note_changes(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        self._write_assessment(
            "20260101T000000Z-vault.json",
            [
                {"relation": "DUPLICATES_EXACT_BYTES", "relative_path": "A.md",
                 "inputs": {"duplicate_of": [], "duplicate_in_batch": ["B.md"]}},
                {"relation": "DUPLICATES_EXACT_BYTES", "relative_path": "B.md",
                 "inputs": {"duplicate_of": [], "duplicate_in_batch": ["A.md"]}},
            ],
        )
        self._write_assessment(
            "20260102T000000Z-vault.json",
            [{"relation": "MODIFIED", "relative_path": "C.md"}],
        )
        summary, _b, dupes = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["DUPLICATES_EXACT_BYTES"], 2)
        self.assertEqual(dupes, ["A.md", "B.md"])

    def test_duplicate_invalidated_when_a_participant_diverges(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        self._write_assessment(
            "20260101T000000Z-vault.json",
            [
                {"relation": "DUPLICATES_EXACT_BYTES", "relative_path": "A.md",
                 "inputs": {"duplicate_of": [], "duplicate_in_batch": ["B.md"]}},
                {"relation": "DUPLICATES_EXACT_BYTES", "relative_path": "B.md",
                 "inputs": {"duplicate_of": [], "duplicate_in_batch": ["A.md"]}},
            ],
        )
        # A later batch changes A.md to unique content (no longer a duplicate).
        self._write_assessment(
            "20260102T000000Z-vault.json",
            [{"relation": "MODIFIED", "relative_path": "A.md"}],
        )
        summary, _b, dupes = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(
            summary["DUPLICATES_EXACT_BYTES"], 0,
            "a stale duplicate finding survived a participant diverging",
        )
        self.assertEqual(dupes, [])

    def test_relationless_reassessment_clears_prior_finding(self) -> None:
        # A later batch reassesses A.md but emits no relation (e.g. restored
        # byte-for-byte): its prior CITATION_BROKEN must be cleared.
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()
        digest = self._registry().registry_sha256
        (dirs["assessments"] / "20260101T000000Z-vault.json").write_text(
            json.dumps({
                "schema_version": STEWARD_SCHEMA_VERSION, "kind": "assessment_batch",
                "source": "vault", "registry_sha256": digest,
                "summary": {"CITATION_BROKEN": 1},
                "assessments": [{
                    "relation": "CITATION_BROKEN", "relative_path": "A.md",
                    "inputs": {"broken_citations": [{"citation": "A.md:1-1"}]}}],
                "covered_paths": ["A.md"],
            }), encoding="utf-8")
        (dirs["assessments"] / "20260102T000000Z-vault.json").write_text(
            json.dumps({
                "schema_version": STEWARD_SCHEMA_VERSION, "kind": "assessment_batch",
                "source": "vault", "registry_sha256": digest,
                "summary": {"index_current": 1},
                "assessments": [],           # no relation this batch
                "covered_paths": ["A.md"],   # but A.md WAS reassessed
            }), encoding="utf-8")
        summary, broken, _d = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["CITATION_BROKEN"], 0)
        self.assertEqual(broken, [])

    def test_skipped_reassessment_does_not_clear_prior_finding(self) -> None:
        # A later batch that lists the path only as changed_during_observe (so it
        # was NOT assessed, covered_paths excludes it) must NOT erase the prior
        # CITATION_BROKEN finding.
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()
        digest = self._registry().registry_sha256
        (dirs["assessments"] / "20260101T000000Z-vault.json").write_text(
            json.dumps({
                "schema_version": STEWARD_SCHEMA_VERSION, "kind": "assessment_batch",
                "source": "vault", "registry_sha256": digest,
                "summary": {"CITATION_BROKEN": 1},
                "assessments": [{
                    "relation": "CITATION_BROKEN", "relative_path": "A.md",
                    "inputs": {"broken_citations": [{"citation": "A.md:1-1"}]}}],
                "covered_paths": ["A.md"],
            }), encoding="utf-8")
        (dirs["assessments"] / "20260102T000000Z-vault.json").write_text(
            json.dumps({
                "schema_version": STEWARD_SCHEMA_VERSION, "kind": "assessment_batch",
                "source": "vault", "registry_sha256": digest,
                "summary": {"skipped_changed_during_observe": 1},
                "assessments": [],
                "covered_paths": [],   # A.md was skipped, not assessed
            }), encoding="utf-8")
        summary, broken, _d = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["CITATION_BROKEN"], 1, "an unresolved finding was erased")
        self.assertIn("A.md:1-1", broken)

    def test_newest_report_filters_by_registry_digest(self) -> None:
        from recallweave.steward_sweep import _newest_report

        dirs = self._dirs()
        (dirs["reports"] / "20260101T000000Z-sweep.json").write_text(
            json.dumps({"registry_sha256": "foreign", "generated_at": "t1",
                        "result": "findings"}), encoding="utf-8")
        self.assertIsNone(_newest_report(dirs["reports"], "active"))
        (dirs["reports"] / "20260102T000000Z-sweep.json").write_text(
            json.dumps({"registry_sha256": "active", "generated_at": "t2",
                        "result": "no_change"}), encoding="utf-8")
        self.assertEqual(_newest_report(dirs["reports"], "active")["result"], "no_change")

    def test_foreign_registry_assessment_is_excluded_from_report(self) -> None:
        # An assessment recorded under a different registry (same source name)
        # must not leak its paths/citations/relations into this report.
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()
        (dirs["assessments"] / "20260101T000000Z-vault.json").write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "vault",
                    "registry_sha256": "some-foreign-digest",
                    "summary": {"CITATION_BROKEN": 1},
                    "assessments": [{
                        "relation": "CITATION_BROKEN", "relative_path": "Secret.md",
                        "inputs": {"broken_citations": [{"citation": "Secret.md:9-9"}]},
                    }],
                }
            ),
            encoding="utf-8",
        )
        summary, broken, _d = _aggregate_assessments(self._dirs(), self._registry())
        self.assertEqual(summary["CITATION_BROKEN"], 0)
        self.assertEqual(broken, [], "a foreign-registry finding leaked into the report")

    def test_same_path_in_two_sources_counts_as_two_findings(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        # A second source whose vault also contains Gone.md.
        vault_b = self.root / "vault-b"
        vault_b.mkdir()
        (vault_b / "x.md").write_text("# X\n", encoding="utf-8")
        registry = SourceRegistry.from_payload(
            {
                "spec_version": SOURCES_SPEC_VERSION,
                "sources": [
                    {"name": "vault", "type": "folder", "root": str(self.vault),
                     "mode": "read_only"},
                    {"name": "vaultb", "type": "folder", "root": str(vault_b),
                     "mode": "read_only"},
                ],
            }
        )
        dirs = self._dirs()
        for src in ("vault", "vaultb"):
            (dirs["assessments"] / f"20260101T000000Z-{src}.json").write_text(
                json.dumps(
                    {
                        "schema_version": STEWARD_SCHEMA_VERSION,
                        "kind": "assessment_batch",
                        "source": src,
                        "registry_sha256": registry.registry_sha256,
                        "summary": {"DELETED": 1},
                        "assessments": [{"relation": "DELETED", "relative_path": "Gone.md"}],
                    }
                ),
                encoding="utf-8",
            )
        summary, _b, _d = _aggregate_assessments(dirs, registry)
        self.assertEqual(
            summary["DELETED"], 2,
            "same-named findings in disjoint sources were merged into one",
        )

    def test_later_assessment_supersedes_earlier_finding(self) -> None:
        # DELETED in an earlier assessment, then NEW for the same path later:
        # the report must reflect the current state (NEW), not both.
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()

        def _assessment(name: str, relation: str) -> None:
            (dirs["assessments"] / name).write_text(
                json.dumps(
                    {
                        "schema_version": STEWARD_SCHEMA_VERSION,
                        "kind": "assessment_batch",
                        "source": "vault",
                        "registry_sha256": self._registry().registry_sha256,
                        "summary": {relation: 1},
                        "assessments": [{"relation": relation, "relative_path": "Gone.md"}],
                    }
                ),
                encoding="utf-8",
            )

        _assessment("20260101T000000Z-vault.json", "DELETED")
        _assessment("20260102T000000Z-vault.json", "NEW")
        summary, _b, _d = _aggregate_assessments(dirs, self._registry())
        self.assertEqual(summary["NEW"], 1)
        self.assertEqual(
            summary["DELETED"], 0,
            "a superseded DELETED finding was still reported as current",
        )

    def test_repaired_citation_does_not_persist(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()
        (dirs["assessments"] / "20260101T000000Z-vault.json").write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "vault",
                    "registry_sha256": self._registry().registry_sha256,
                    "summary": {"CITATION_BROKEN": 1},
                    "assessments": [
                        {
                            "relation": "CITATION_BROKEN",
                            "relative_path": "Note.md",
                            "inputs": {"broken_citations": [{"citation": "Note.md:1-2"}]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        # A later assessment shows the same note MODIFIED (citation repaired).
        (dirs["assessments"] / "20260102T000000Z-vault.json").write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "vault",
                    "registry_sha256": self._registry().registry_sha256,
                    "summary": {"MODIFIED": 1},
                    "assessments": [{"relation": "MODIFIED", "relative_path": "Note.md"}],
                }
            ),
            encoding="utf-8",
        )
        summary, broken, _d = _aggregate_assessments(dirs, self._registry())
        self.assertEqual(summary["CITATION_BROKEN"], 0)
        self.assertEqual(broken, [], "a repaired citation persisted in the report")

    def test_report_does_not_double_count_recurring_finding(self) -> None:
        from recallweave.steward_sweep import _aggregate_assessments

        dirs = self._dirs()

        def _assessment(name: str) -> None:
            (dirs["assessments"] / name).write_text(
                json.dumps(
                    {
                        "schema_version": STEWARD_SCHEMA_VERSION,
                        "kind": "assessment_batch",
                        "source": "vault",
                        "registry_sha256": self._registry().registry_sha256,
                        "summary": {"DELETED": 1},
                        "assessments": [
                            {"relation": "DELETED", "relative_path": "Gone.md"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

        # The SAME finding recorded in two retained assessments must count once.
        _assessment("20260101T000000Z-vault.json")
        _assessment("20260102T000000Z-vault.json")
        summary, _broken, _dupes = _aggregate_assessments(dirs, self._registry())
        self.assertEqual(summary["DELETED"], 1, "a recurring finding was double-counted")


class ForeignProposalPendingTest(StewardSweepTest):
    def test_foreign_registry_proposal_excluded_from_pending(self) -> None:
        from recallweave.steward_sweep import (
            _aggregate_proposals,
            _pending_proposal_count,
        )

        dirs = self._dirs()
        reg = self._registry()
        (dirs["proposals"] / "20260101T000000Z-vault-prp-foreign0000.json").write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "proposal",
                    "proposal_id": "prp-foreign0000",
                    "source": "vault",
                    "action": "review_dangling_references",
                    "registry_sha256": "0" * 64,  # foreign
                    "edits": [],
                    "evidence": {"deleted_path": "Gone.md"},
                }
            ),
            encoding="utf-8",
        )
        total, _by_action, _dangling = _aggregate_proposals(dirs, reg.registry_sha256)
        self.assertEqual(total, 0, "a foreign-registry proposal counted as pending")
        self.assertEqual(
            _pending_proposal_count(dirs["proposals"], reg.registry_sha256), 0
        )
        # Without a digest filter it still counts (back-compat).
        self.assertEqual(_aggregate_proposals(dirs)[0], 1)


class EvidenceBoundingTest(StewardSweepTest):
    def test_evidence_bounded_by_character_budget(self) -> None:
        import recallweave.steward_sweep as sw

        self._baseline()
        # Two entries whose combined length exceeds a tiny char budget, under the
        # element-count cap: the char budget must still truncate + flag.
        observe_receipt = {
            "sources": [
                {"source": "x" * 50, "error": "source_missing"},
                {"source": "y" * 50, "error": "source_missing"},
            ]
        }
        with patch.object(sw, "REPORT_EVIDENCE_LIMIT", 1000), \
                patch.object(sw, "REPORT_EVIDENCE_CHAR_BUDGET", 60):
            report = sw._assemble_report(
                self._registry(), self._dirs(), self.database,
                generated_at="2026-01-01T00:00:00+00:00",
                observe_receipt=observe_receipt, proposals_created_this_sweep=0,
            )
        integ = report["integrity"]
        self.assertLess(len(integ["sources_missing"]), 2)
        self.assertIn("sources_missing", integ["evidence_truncated"])

    def test_single_oversized_entry_is_omitted_and_flagged(self) -> None:
        import recallweave.steward_sweep as sw

        self._baseline()
        observe_receipt = {
            "sources": [{"source": "z" * 200, "error": "source_missing"}]
        }
        with patch.object(sw, "REPORT_EVIDENCE_CHAR_BUDGET", 20):
            report = sw._assemble_report(
                self._registry(), self._dirs(), self.database,
                generated_at="2026-01-01T00:00:00+00:00",
                observe_receipt=observe_receipt, proposals_created_this_sweep=0,
            )
        integ = report["integrity"]
        self.assertEqual(integ["sources_missing"], [])  # the lone huge entry omitted
        self.assertEqual(
            integ["evidence_truncated"]["sources_missing"], {"reported": 0, "total": 1}
        )

    def test_all_integrity_evidence_arrays_are_bounded(self) -> None:
        import recallweave.steward_sweep as sw

        self._baseline()
        observe_receipt = {
            "sources": [
                {"source": f"s{i}", "error": "source_missing"} for i in range(4)
            ]
        }
        with patch.object(sw, "REPORT_EVIDENCE_LIMIT", 2):
            report = sw._assemble_report(
                self._registry(),
                self._dirs(),
                self.database,
                generated_at="2026-01-01T00:00:00+00:00",
                observe_receipt=observe_receipt,
                proposals_created_this_sweep=0,
            )
        integ = report["integrity"]
        self.assertEqual(len(integ["sources_missing"]), 2)
        self.assertEqual(
            integ["evidence_truncated"]["sources_missing"],
            {"reported": 2, "total": 4},
        )


class ObserveErrorResultTest(StewardSweepTest):
    def test_missing_source_is_not_reported_as_no_change(self) -> None:
        import recallweave.steward_sweep as sw

        self._baseline()
        observe_receipt = {
            "sources": [{"source": "vault", "error": "source_missing"}]
        }
        report = sw._assemble_report(
            self._registry(),
            self._dirs(),
            self.database,
            generated_at="2026-01-01T00:00:00+00:00",
            observe_receipt=observe_receipt,
            proposals_created_this_sweep=0,
        )
        self.assertNotEqual(report["result"], "no_change")
        self.assertNotEqual(sw.SWEEP_EXIT_CODES[report["result"]], 0)


class PruneTest(StewardSweepTest):
    def test_prune_deletes_only_old_changes_assessments_reports(self) -> None:
        import recallweave.steward_sweep as _sw

        if not _sw._DIR_FD_PRUNE:
            self.skipTest("descriptor-relative pruning unavailable; prune fails closed")
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        self._sweep()
        dirs = self._dirs()

        (dirs["backups"] / "keepme.bin").write_bytes(b"x" * 10)
        (dirs["receipts"] / "keepme.json").write_text("{}", encoding="utf-8")

        old_epoch = time.time() - 40 * 86400
        for subdir in ("changes", "assessments", "proposals", "receipts", "reports", "backups"):
            for entry in dirs[subdir].iterdir():
                if entry.is_file():
                    os.utime(entry, (old_epoch, old_epoch))

        before_proposals = sum(1 for entry in dirs["proposals"].iterdir() if entry.is_file())
        before_receipts = sum(1 for entry in dirs["receipts"].iterdir() if entry.is_file())
        before_backups = sum(1 for entry in dirs["backups"].iterdir() if entry.is_file())
        self.assertGreaterEqual(before_proposals, 1)

        status = status_report(self.state_root, prune_older_than_days=30)

        self.assertEqual(status["counts"]["change_batches"], 0)
        self.assertEqual(status["counts"]["assessments"], 0)
        self.assertEqual(status["counts"]["reports"], 0)
        self.assertEqual(status["counts"]["proposals_pending"], before_proposals)
        self.assertEqual(
            sum(1 for entry in dirs["receipts"].iterdir() if entry.is_file()), before_receipts
        )
        self.assertEqual(
            sum(1 for entry in dirs["backups"].iterdir() if entry.is_file()), before_backups
        )
        self.assertGreater(status["pruned"]["changes"], 0)
        self.assertGreater(status["pruned"]["assessments"], 0)
        self.assertGreater(status["pruned"]["reports"], 0)
        self.assertEqual(
            status["pruned"]["total"],
            status["pruned"]["changes"]
            + status["pruned"]["assessments"]
            + status["pruned"]["reports"],
        )

    def test_status_prune_reports_unsupported_without_dir_fd(self) -> None:
        # status_report must gate destructive pruning on descriptor-relative
        # support: when unavailable it reports pruning unsupported and deletes
        # nothing, rather than pathname-prune through a possibly-swapped dir.
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        self._sweep()
        dirs = self._dirs()
        old_report = next(dirs["reports"].glob("*.json"), None)
        self.assertIsNotNone(old_report)
        os.utime(old_report, (0, 0))
        with patch("recallweave.steward_sweep._DIR_FD_PRUNE", False):
            status = status_report(self.state_root, prune_older_than_days=0)
        self.assertTrue(status["pruned"].get("unsupported_platform"))
        self.assertEqual(status["pruned"]["total"], 0)
        self.assertTrue(old_report.exists(), "fail-closed status pruned a file")

    def test_pruning_requires_valid_completion_marker(self) -> None:
        from recallweave.steward_sweep import _fully_processed_artifact_names

        dirs = self._dirs()
        name = "20260101T000000Z-vault.json"
        # An eligible (has a DELETED) assessment for source "vault".
        (dirs["assessments"] / name).write_text(
            json.dumps(
                {
                    "schema_version": STEWARD_SCHEMA_VERSION,
                    "kind": "assessment_batch",
                    "source": "vault",
                    "registry_sha256": "active",
                    "summary": {"DELETED": 1},
                    "assessments": [{"relation": "DELETED", "relative_path": "Gone.md"}],
                }
            ),
            encoding="utf-8",
        )

        def _marker(**over) -> None:
            doc = {
                "schema_version": STEWARD_SCHEMA_VERSION,
                "kind": "propose_marker",
                "assessment": name,
                "registry_sha256": "active",
            }
            doc.update(over)
            (dirs["proposed"] / name).write_text(json.dumps(doc), encoding="utf-8")

        # No marker -> not prunable even though a (partial) proposal may exist.
        (dirs["proposals"] / f"{name[:-5]}-prp-partial00.json").write_text(
            json.dumps({"kind": "proposal", "source": "vault",
                        "registry_sha256": "active",
                        "assessment_refs": [{"assessment_file": name}], "edits": []}),
            encoding="utf-8",
        )
        self.assertNotIn(name, _fully_processed_artifact_names(dirs, "active"))

        # Foreign-registry marker does not authorize.
        _marker(registry_sha256="stale")
        self.assertNotIn(name, _fully_processed_artifact_names(dirs, "active"))

        # A valid current-registry completion marker authorizes pruning.
        _marker()
        self.assertIn(name, _fully_processed_artifact_names(dirs, "active"))

    def test_prune_preserves_unassessed_change_batch(self) -> None:
        # observe advances the checkpoint; an unassessed batch holds the only
        # record of those changes and must never be pruned by age.
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaMoved.md")
        observe_registry(self._registry(), self.state_root)
        dirs = self._dirs()
        # The unassessed batch is the newest changes file with a real change.
        unassessed = sorted(dirs["changes"].glob("*.json"))[-1]
        old_epoch = time.time() - 40 * 86400
        for entry in dirs["changes"].iterdir():
            if entry.is_file():
                os.utime(entry, (old_epoch, old_epoch))

        status_report(self.state_root, prune_older_than_days=30)

        self.assertTrue(
            unassessed.exists(),
            "an unassessed change batch was pruned, losing its changes",
        )

    def test_no_prune_by_default(self) -> None:
        self._baseline()
        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        self._sweep()
        status = status_report(self.state_root)
        self.assertIsNone(status["pruned"])


class ResultConstantsTest(StewardSweepTest):
    def test_frozen_constants_shape(self) -> None:
        self.assertEqual(
            SWEEP_RESULTS,
            (
                "no_change",
                "findings",
                "approval_required",
                "applied",
                "validation_failed_rolled_back",
                "error",
            ),
        )
        self.assertEqual(
            SWEEP_EXIT_CODES,
            {
                "no_change": 0,
                "findings": 3,
                "approval_required": 4,
                "applied": 5,
                "validation_failed_rolled_back": 6,
                "error": 2,
            },
        )

    def test_v1_results_limited_to_three_across_fixtures(self) -> None:
        seen: set[str] = set()

        seen.add(self._sweep()["result"])
        self._settle()

        self._write(
            "Echo.md",
            "# Echo\n\nEcho body unique text.\n\nExtra info now added.\n",
        )
        seen.add(self._sweep()["result"])
        self._settle()

        (self.vault / "Beta.md").rename(self.vault / "BetaNew.md")
        seen.add(self._sweep()["result"])

        self.assertEqual(seen, {"no_change", "findings", "approval_required"})
        self.assertTrue(seen <= set(SWEEP_RESULTS))
        self.assertEqual(
            set(SWEEP_RESULTS) - seen,
            {"applied", "validation_failed_rolled_back", "error"},
        )


class CliErrorEnvelopeTest(StewardSweepTest):
    def test_cli_missing_registry_emits_error_envelope(self) -> None:
        missing = self.root / "does-not-exist.json"
        exit_code, stdout, stderr = _run_cli(
            "steward-sweep",
            str(missing),
            "--database",
            str(self.database),
            "--state-dir",
            str(self.state_root),
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        payload = _decode_single_json(stderr)
        self.assertEqual(payload["operation"], "steward-sweep")


if __name__ == "__main__":
    unittest.main()


class SweepApplyLegTest(StewardSweepTest):
    """The --apply leg executes only auto_apply-resolved proposals."""

    def setUp(self) -> None:
        super().setUp()
        # Writes require an appliable source with an explicit allowlist.
        self.sources_path.write_text(
            json.dumps(
                {
                    "spec_version": SOURCES_SPEC_VERSION,
                    "sources": [
                        {
                            "name": "vault",
                            "type": "folder",
                            "root": str(self.vault),
                            "mode": "appliable",
                            "policy": {
                                "include_paths": [
                                    "Alpha.md",
                                    "Beta.md",
                                    "Gamma.md",
                                    "Echo.md",
                                ]
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _seed_auto_proposal(self, policy_level_class: str = "append_at_eof"):
        import hashlib

        base = self.vault / "Alpha.md"
        data = base.read_bytes()
        appended = "\nPlain appended sentence.\n"
        proposal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": "prp-sweepapplytest0",
            "source": "vault",
            "action": "test",
            "policy_level": "propose_only",
            "edits": [
                {
                    "mutation_class": policy_level_class,
                    "relative_path": "Alpha.md",
                    "precondition_content_hash": hashlib.sha256(data).hexdigest(),
                    "replacement_text": appended,
                    "predicted_post_hash": hashlib.sha256(
                        data + appended.encode()
                    ).hexdigest(),
                }
            ],
            "conflicts_with": [],
            "registry_sha256": hashlib.sha256(
                self.sources_path.read_bytes()
            ).hexdigest(),
        }
        dirs = self._dirs()
        from recallweave.steward_state import atomic_write_json

        atomic_write_json(
            dirs["proposals"] / "20260101T000000000000Z-vault-prp-sweepapplytest0.json",
            proposal,
            within=dirs["proposals"],
        )
        return base, data, appended

    def _write_policy(self):
        import json as _json

        from recallweave.steward_policy import WritePolicy

        return WritePolicy.from_bytes(
            _json.dumps(
                {
                    "spec_version": "recallweave.steward.policy.v1",
                    "class_levels": {"append_at_eof": "auto_apply"},
                }
            ).encode()
        )

    def test_apply_requires_write_policy(self) -> None:
        self._baseline()
        with self.assertRaisesRegex(ValueError, "write-policy"):
            self._sweep(apply=True)

    def test_apply_leg_executes_auto_proposal_and_reports_applied(self) -> None:
        self._baseline()
        base, data, appended = self._seed_auto_proposal()
        report = self._sweep(apply=True, write_policy=self._write_policy())
        self.assertEqual(base.read_bytes(), data + appended.encode())
        self.assertEqual(report["result"], "applied")
        self.assertEqual(report["apply"]["mutations"], 1)
        self.assertEqual(len(report["apply"]["applied"]), 1)
        dirs = self._dirs()
        self.assertTrue(list(dirs["receipts"].glob("*.json")))

    def test_apply_leg_skips_non_auto_proposals(self) -> None:
        self._baseline()
        base, data, _appended = self._seed_auto_proposal(
            policy_level_class="fix_unresolved_link"
        )
        # fix_unresolved_link is not auto_apply under this policy.
        report = self._sweep(apply=True, write_policy=self._write_policy())
        self.assertEqual(base.read_bytes(), data)
        self.assertNotEqual(report["result"], "applied")
        skipped = {item["reason"] for item in report["apply"]["skipped"]}
        self.assertIn("not_auto_apply", skipped)

    def test_apply_leg_failure_reports_validation_failed(self) -> None:
        import hashlib

        self._baseline()
        # A proposal whose append introduces an unresolved link: L1 fails,
        # the apply rolls back, and the sweep reports code-6 semantics.
        base = self.vault / "Alpha.md"
        data = base.read_bytes()
        appended = "\nSee [[NoSuchNoteAnywhere]].\n"
        proposal = {
            "schema_version": STEWARD_SCHEMA_VERSION,
            "kind": "proposal",
            "proposal_id": "prp-sweepapplyfail0",
            "source": "vault",
            "action": "test",
            "policy_level": "propose_only",
            "edits": [
                {
                    "mutation_class": "append_at_eof",
                    "relative_path": "Alpha.md",
                    "precondition_content_hash": hashlib.sha256(data).hexdigest(),
                    "replacement_text": appended,
                    "predicted_post_hash": hashlib.sha256(
                        data + appended.encode()
                    ).hexdigest(),
                }
            ],
            "conflicts_with": [],
            "registry_sha256": hashlib.sha256(
                self.sources_path.read_bytes()
            ).hexdigest(),
        }
        dirs = self._dirs()
        from recallweave.steward_state import atomic_write_json

        atomic_write_json(
            dirs["proposals"] / "20260101T000000000000Z-vault-prp-sweepapplyfail0.json",
            proposal,
            within=dirs["proposals"],
        )
        report = self._sweep(apply=True, write_policy=self._write_policy())
        self.assertEqual(report["result"], "validation_failed_rolled_back")
        self.assertEqual(base.read_bytes(), data, "rollback did not restore")
        self.assertTrue(report["apply"]["failures"])
