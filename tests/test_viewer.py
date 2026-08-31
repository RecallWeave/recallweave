from __future__ import annotations

import hashlib
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
from recallweave.viewer import (
    VIEWER_SCHEMA_VERSION,
    _dedupe_string_terms,
    _edge_evidence,
    _intersecting_tags,
    _nullable_timestamp,
    _valid_previous_viewer_nodes,
    build_viewer_document,
    export_viewer_graph,
)


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "examples" / "synthetic-vault"


class ViewerExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.database = Path(self.temporary.name) / "index.sqlite"
        build_index(
            VAULT,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
            minimum_candidate_score=0.08,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixture_uses_canonical_temp_path(self) -> None:
        root = Path(self.temporary.name)
        self.assertEqual(root, root.resolve())

    def test_document_separates_verified_and_candidate_edges(self) -> None:
        document = build_viewer_document(self.database)
        self.assertEqual(document["schema_version"], VIEWER_SCHEMA_VERSION)
        self.assertEqual(len(document["nodes"]), 6)
        self.assertTrue(any(edge["verified"] for edge in document["edges"]))
        self.assertTrue(any(not edge["verified"] for edge in document["edges"]))
        self.assertTrue(all(node["summary"] == "" for node in document["nodes"]))
        self.assertFalse(document["privacy"]["includes_excerpts"])
        self.assertFalse(document["privacy"]["includes_passage_text"])
        self.assertTrue(document["privacy"]["includes_note_derived_terms"])
        self.assertFalse(document["privacy"]["metadata_only"])
        self.assertEqual(
            document["privacy"]["export_profile"],
            "graph_metadata_and_note_derived_terms",
        )
        candidate = next(edge for edge in document["edges"] if not edge["verified"])
        self.assertIn("citation", candidate["evidence"]["source_evidence"])
        self.assertIn("citation", candidate["evidence"]["target_evidence"])
        self.assertNotIn("passage", candidate["evidence"]["source_evidence"])
        self.assertNotIn("passage", candidate["evidence"]["target_evidence"])

        verified = build_viewer_document(
            self.database,
            include_candidates=False,
            include_excerpts=True,
        )
        self.assertTrue(verified["edges"])
        self.assertTrue(all(edge["verified"] for edge in verified["edges"]))
        self.assertTrue(any(node["summary"] for node in verified["nodes"]))
        self.assertTrue(verified["privacy"]["includes_excerpts"])
        self.assertTrue(verified["privacy"]["includes_passage_text"])

        with_excerpts = build_viewer_document(self.database, include_excerpts=True)
        excerpt_candidate = next(
            edge for edge in with_excerpts["edges"] if not edge["verified"]
        )
        self.assertIn("passage", excerpt_candidate["evidence"]["source_evidence"])
        self.assertIn("passage", excerpt_candidate["evidence"]["target_evidence"])

    def test_viewer_v2_emits_node_hashes_history_and_signals(self) -> None:
        document = build_viewer_document(
            self.database, vault_name="synthetic-vault"
        )
        self.assertEqual(document["schema_version"], "recallweave.viewer.v2")
        self.assertEqual(document["vault_name"], "synthetic-vault")
        self.assertIn("export_history", document)
        history = document["export_history"]
        for key in (
            "export_id",
            "previous_content_hash",
            "node_content_hashes_changed",
            "node_content_hashes_unchanged",
            "nodes_added",
            "nodes_removed",
        ):
            self.assertIn(key, history)
        self.assertIsNone(history["previous_content_hash"])
        self.assertEqual(history["node_content_hashes_changed"], 0)
        self.assertEqual(history["node_content_hashes_unchanged"], 0)
        self.assertEqual(history["nodes_added"], len(document["nodes"]))
        for node in document["nodes"]:
            self.assertIn("created_at", node)
            self.assertIn("modified_at", node)
            self.assertIn("content_hash", node)
            self.assertTrue(node["content_hash"])
        candidate = next(edge for edge in document["edges"] if not edge["verified"])
        self.assertIn("signals", candidate["evidence"])
        self.assertEqual(
            candidate["evidence"]["signals"].get("lexical_terms"),
            candidate["evidence"].get("shared_terms"),
        )
        shared_tag_edge = next(
            edge
            for edge in document["edges"]
            if not edge["verified"]
            and edge["source"] == "Operations/Review Cadence.md"
            and edge["target"] == "Projects/Growth Atlas.md"
        )
        self.assertEqual(
            shared_tag_edge["evidence"]["signals"].get("shared_tags"),
            ["operating-system"],
        )
        source_tags = next(
            node["tags"]
            for node in document["nodes"]
            if node["id"] == "Operations/Review Cadence.md"
        )
        target_tags = next(
            node["tags"]
            for node in document["nodes"]
            if node["id"] == "Projects/Growth Atlas.md"
        )
        self.assertIn("operating-system", source_tags)
        self.assertIn("operating-system", target_tags)

        output = Path(self.temporary.name) / "v2-graph.json"
        export_viewer_graph(self.database, output, vault_name="synthetic-vault")
        again = export_viewer_graph(
            self.database, output, vault_name="synthetic-vault", force=True
        )
        self.assertEqual(again["schema_version"], VIEWER_SCHEMA_VERSION)
        second = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNotNone(second["export_history"]["previous_content_hash"])
        self.assertGreaterEqual(
            second["export_history"]["node_content_hashes_unchanged"], 1
        )

    def test_empty_computed_shared_tags_do_not_fall_back_to_claimed(self) -> None:
        evidence = _edge_evidence(
            json.dumps(
                {
                    "shared_terms": ["real"],
                    "shared_tags": ["fabricated"],
                    "explanation": "candidate",
                }
            ),
            source_path="a.md",
            target_path="b.md",
            include_excerpts=False,
            shared_tags=[],
        )
        signals = evidence.get("signals") or {}
        self.assertEqual(signals.get("lexical_terms"), ["real"])
        self.assertNotIn("shared_tags", signals)

    def test_computed_shared_tags_win_over_conflicting_claims(self) -> None:
        evidence = _edge_evidence(
            json.dumps({"shared_tags": ["fabricated", "also-fake"]}),
            source_path="a.md",
            target_path="b.md",
            include_excerpts=False,
            shared_tags=["alpha", "beta"],
        )
        self.assertEqual(
            evidence["signals"]["shared_tags"],
            ["alpha", "beta"],
        )

    def test_shared_tags_cap_dedupes_and_preserves_order(self) -> None:
        many = [f"tag-{index:02d}" for index in range(20)]
        many_with_dupes = many + ["tag-00", "TAG-01"]
        capped = _dedupe_string_terms(many_with_dupes, limit=12)
        self.assertEqual(capped, many[:12])
        evidence = _edge_evidence(
            "{}",
            source_path="a.md",
            target_path="b.md",
            include_excerpts=False,
            shared_tags=many_with_dupes,
        )
        self.assertEqual(evidence["signals"]["shared_tags"], many[:12])

    def test_intersecting_tags_are_deterministic(self) -> None:
        tags = {
            "a.md": ["zulu", "shared", "alpha"],
            "b.md": ["shared", "bravo", "alpha"],
        }
        self.assertEqual(
            _intersecting_tags(tags, "a.md", "b.md"),
            ["alpha", "shared"],
        )

    def test_malformed_prior_export_is_not_used_as_history(self) -> None:
        output = Path(self.temporary.name) / "malformed-prior.json"
        export_viewer_graph(self.database, output)
        history = {
            "export_id": "prior-export",
            "previous_content_hash": None,
            "node_content_hashes_changed": 0,
            "node_content_hashes_unchanged": 0,
            "nodes_added": 0,
            "nodes_removed": 0,
        }
        malformed = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {"title": "missing id"},
                {"id": "dup.md", "title": "Dup", "path": "dup.md", "content_hash": "a" * 64},
                {"id": "dup.md", "title": "Dup2", "path": "dup.md", "content_hash": "b" * 64},
            ],
            "edges": [],
            "export_history": history,
        }
        output.write_text(json.dumps(malformed), encoding="utf-8")
        export_viewer_graph(self.database, output, force=True)
        second = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(second["export_history"]["previous_content_hash"])
        self.assertIsNone(_valid_previous_viewer_nodes(malformed))
        emptyish = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [None, {"id": 12}],
            "edges": [],
            "export_history": history,
        }
        self.assertIsNone(_valid_previous_viewer_nodes(emptyish))
        missing_fields = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [{"id": "a.md"}],
            "edges": [],
            "export_history": history,
        }
        self.assertIsNone(_valid_previous_viewer_nodes(missing_fields))
        bad_hashes = [
            "not-a-sha256",
            "A" * 64,
            "ab",
            "",
            "g" * 64,
        ]
        for digest in bad_hashes:
            bad_hash = {
                "schema_version": VIEWER_SCHEMA_VERSION,
                "nodes": [
                    {
                        "id": "a.md",
                        "title": "A",
                        "path": "a.md",
                        "content_hash": digest,
                    }
                ],
                "edges": [],
                "export_history": history,
            }
            self.assertIsNone(
                _valid_previous_viewer_nodes(bad_hash),
                msg=f"expected reject for content_hash={digest!r}",
            )
        missing_hash_key = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [{"id": "a.md", "title": "A", "path": "a.md"}],
            "edges": [],
            "export_history": history,
        }
        self.assertIsNone(_valid_previous_viewer_nodes(missing_hash_key))
        missing_edges = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "a.md",
                    "title": "A",
                    "path": "a.md",
                    "content_hash": "a" * 64,
                }
            ],
            "export_history": history,
        }
        self.assertIsNone(_valid_previous_viewer_nodes(missing_edges))
        missing_history = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "a.md",
                    "title": "A",
                    "path": "a.md",
                    "content_hash": "a" * 64,
                }
            ],
            "edges": [],
        }
        self.assertIsNone(_valid_previous_viewer_nodes(missing_history))
        empty_missing_edges = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [],
            "export_history": history,
        }
        self.assertIsNone(_valid_previous_viewer_nodes(empty_missing_edges))
        for omitted in (
            "export_id",
            "previous_content_hash",
            "node_content_hashes_changed",
            "node_content_hashes_unchanged",
            "nodes_added",
            "nodes_removed",
        ):
            incomplete_history = dict(history)
            del incomplete_history[omitted]
            incomplete = {
                "schema_version": VIEWER_SCHEMA_VERSION,
                "nodes": [
                    {
                        "id": "a.md",
                        "title": "A",
                        "path": "a.md",
                        "content_hash": "a" * 64,
                    }
                ],
                "edges": [],
                "export_history": incomplete_history,
            }
            self.assertIsNone(
                _valid_previous_viewer_nodes(incomplete),
                msg=f"expected reject when export_history omits {omitted}",
            )
            output.write_text(json.dumps(incomplete), encoding="utf-8")
            export_viewer_graph(self.database, output, force=True)
            forced = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(
                forced["export_history"]["previous_content_hash"],
                msg=f"forced replace must ignore prior missing {omitted}",
            )
        contradictory_first = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "a.md",
                    "title": "A",
                    "path": "a.md",
                    "content_hash": "a" * 64,
                }
            ],
            "edges": [],
            "export_history": {
                "export_id": "contradictory-first",
                "previous_content_hash": None,
                "node_content_hashes_changed": 0,
                "node_content_hashes_unchanged": 0,
                "nodes_added": 0,
                "nodes_removed": 0,
            },
        }
        self.assertIsNone(_valid_previous_viewer_nodes(contradictory_first))
        output.write_text(json.dumps(contradictory_first), encoding="utf-8")
        export_viewer_graph(self.database, output, force=True)
        forced_contradictory = json.loads(output.read_text(encoding="utf-8"))
        self.assertIsNone(
            forced_contradictory["export_history"]["previous_content_hash"]
        )
        contradictory_subsequent = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "a.md",
                    "title": "A",
                    "path": "a.md",
                    "content_hash": "a" * 64,
                },
                {
                    "id": "b.md",
                    "title": "B",
                    "path": "b.md",
                    "content_hash": "b" * 64,
                },
            ],
            "edges": [],
            "export_history": {
                "export_id": "contradictory-subsequent",
                "previous_content_hash": "c" * 64,
                "node_content_hashes_changed": 0,
                "node_content_hashes_unchanged": 0,
                "nodes_added": 0,
                "nodes_removed": 0,
            },
        }
        self.assertIsNone(_valid_previous_viewer_nodes(contradictory_subsequent))

    def test_valid_empty_prior_export_is_used_as_history(self) -> None:
        empty_prior = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "title": "Empty predecessor",
            "generated_at": "2026-08-01T00:00:00+00:00",
            "nodes": [],
            "edges": [],
            "diagnostics": {"unresolved_links": 0},
            "privacy": {
                "export_profile": "empty_graph",
                "requested_profile": "without_passage_text",
                "metadata_only": True,
                "includes_excerpts": False,
                "includes_passage_text": False,
                "includes_note_derived_terms": False,
                "includes_paths_titles_tags": False,
                "generated_locally": True,
            },
            "export_history": {
                "export_id": "empty-prior",
                "previous_content_hash": None,
                "node_content_hashes_changed": 0,
                "node_content_hashes_unchanged": 0,
                "nodes_added": 0,
                "nodes_removed": 0,
            },
        }
        self.assertEqual(_valid_previous_viewer_nodes(empty_prior), [])
        output = Path(self.temporary.name) / "from-empty-prior.json"
        output.write_text(json.dumps(empty_prior), encoding="utf-8")
        export_viewer_graph(self.database, output, force=True)
        second = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            second["export_history"]["previous_content_hash"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertGreaterEqual(second["export_history"]["nodes_added"], 1)
        null_hash_prior = {
            "schema_version": VIEWER_SCHEMA_VERSION,
            "nodes": [
                {
                    "id": "legacy.md",
                    "title": "Legacy",
                    "path": "legacy.md",
                    "content_hash": None,
                }
            ],
            "edges": [],
            "export_history": {
                "export_id": "legacy-prior",
                "previous_content_hash": None,
                "node_content_hashes_changed": 0,
                "node_content_hashes_unchanged": 0,
                "nodes_added": 1,
                "nodes_removed": 0,
            },
        }
        self.assertIsNotNone(_valid_previous_viewer_nodes(null_hash_prior))

    def test_vault_name_rejects_path_like_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "vault label"):
            build_viewer_document(self.database, vault_name="/Users/alice/Vault")
        with self.assertRaisesRegex(ValueError, "vault label"):
            build_viewer_document(self.database, vault_name="../secrets")
        document = build_viewer_document(self.database, vault_name="Research Vault")
        self.assertEqual(document["vault_name"], "Research Vault")

    def test_export_refuses_overwrite_without_force(self) -> None:
        output = Path(self.temporary.name) / "graph.json"
        receipt = export_viewer_graph(self.database, output)
        self.assertEqual(receipt["notes"], 6)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], VIEWER_SCHEMA_VERSION)

        with self.assertRaisesRegex(ValueError, "already exists"):
            export_viewer_graph(self.database, output)

        replacement = export_viewer_graph(
            self.database,
            output,
            include_candidates=False,
            force=True,
        )
        self.assertFalse(replacement["candidate_edges_included"])
        self.assertFalse(replacement["candidate_edges_requested"])
        self.assertFalse(replacement["passage_text_included"])
        self.assertTrue(replacement["paths_titles_tags_included"])
        self.assertEqual(replacement["replacement_mode"], "two_phase_recoverable")
        retained_backup = Path(replacement["replacement_backup"])
        self.assertTrue(retained_backup.is_file())
        self.assertEqual(
            json.loads(retained_backup.read_text(encoding="utf-8"))["schema_version"],
            VIEWER_SCHEMA_VERSION,
        )

    def test_successful_force_replacement_never_auto_deletes_backup(self) -> None:
        output = Path(self.temporary.name) / "retained-force.json"
        output.write_text("approved old output", encoding="utf-8")

        receipt = export_viewer_graph(self.database, output, force=True)

        backup = Path(receipt["replacement_backup"])
        self.assertIn(".backup.", backup.parent.name)
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "approved old output")
        self.assertNotEqual(output.read_text(encoding="utf-8"), "approved old output")

    def test_export_cannot_replace_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            export_viewer_graph(self.database, self.database, force=True)

    def test_export_refuses_dangling_file_symlink(self) -> None:
        target = Path(self.temporary.name) / "elsewhere" / "graph.json"
        output = Path(self.temporary.name) / "dangling.json"
        try:
            output.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "symlink or junction"):
            export_viewer_graph(self.database, output)
        self.assertFalse(target.exists())
        self.assertTrue(output.is_symlink())

    def test_export_refuses_symlinked_parent(self) -> None:
        real_parent = Path(self.temporary.name) / "real"
        real_parent.mkdir()
        linked_parent = Path(self.temporary.name) / "linked"
        try:
            linked_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlink creation unavailable: {error}")

        with self.assertRaisesRegex(ValueError, "symlinked parent"):
            export_viewer_graph(self.database, linked_parent / "graph.json")
        self.assertFalse((real_parent / "graph.json").exists())

    def test_export_reports_regular_file_parent_consistently(self) -> None:
        parent = Path(self.temporary.name) / "not-a-directory"
        parent.write_text("ordinary file", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "parent is not a directory"):
            export_viewer_graph(self.database, parent / "graph.json")
        self.assertEqual(parent.read_text(encoding="utf-8"), "ordinary file")

    def test_export_refuses_database_hardlink(self) -> None:
        output = Path(self.temporary.name) / "database-alias.json"
        try:
            os.link(self.database, output)
        except OSError as error:
            self.skipTest(f"hardlink creation unavailable: {error}")
        before = self.database.read_bytes()

        with self.assertRaisesRegex(ValueError, "cannot replace"):
            export_viewer_graph(self.database, output, force=True)
        self.assertEqual(self.database.read_bytes(), before)

    def test_cli_index_requires_explicit_policy_choice(self) -> None:
        database = Path(self.temporary.name) / "cli-default.sqlite"
        error = StringIO()
        with redirect_stderr(error):
            exit_code = cli_main(
                ["index", str(VAULT), "--database", str(database)]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("explicit policy choice", error.getvalue())
        self.assertIn("--config", error.getvalue())
        self.assertIn("--no-policy", error.getvalue())
        self.assertFalse(database.exists())

    def test_cli_policy_acknowledgement_cannot_be_abbreviated(self) -> None:
        for abbreviation in ("--n", "--no", "--no-p"):
            with self.subTest(abbreviation=abbreviation):
                error = StringIO()
                with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                    cli_main(["index", str(VAULT), abbreviation])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments", error.getvalue())

    def test_cli_no_policy_is_an_explicit_opt_out(self) -> None:
        database = Path(self.temporary.name) / "cli-no-policy.sqlite"
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(
                [
                    "index",
                    str(VAULT),
                    "--database",
                    str(database),
                    "--no-policy",
                ]
            )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["notes_indexed"], 7)
        self.assertEqual(receipt["policy_mode"], "none")

    def test_cli_config_applies_policy(self) -> None:
        database = Path(self.temporary.name) / "cli-policy.sqlite"
        config = Path(self.temporary.name) / "policy.json"
        config.write_text(
            json.dumps({"deny_frontmatter": {"sensitivity": ["sealed"]}}),
            encoding="utf-8",
        )
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(
                [
                    "index",
                    str(VAULT),
                    "--database",
                    str(database),
                    "--config",
                    str(config),
                ]
            )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["notes_indexed"], 6)
        self.assertEqual(receipt["policy_mode"], "config")
        self.assertEqual(
            receipt["policy_config_sha256"],
            hashlib.sha256(config.read_bytes()).hexdigest(),
        )
        self.assertNotIn("policy_config", receipt)

    def test_cli_config_accepts_utf8_bom_and_hashes_exact_bytes(self) -> None:
        database = Path(self.temporary.name) / "cli-bom.sqlite"
        config = Path(self.temporary.name) / "policy-bom.json"
        config_bytes = (
            b"\xef\xbb\xbf"
            b'{"deny_frontmatter":{"sensitivity":["sealed"]}}'
        )
        config.write_bytes(config_bytes)
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(
                [
                    "index",
                    str(VAULT),
                    "--database",
                    str(database),
                    "--config",
                    str(config),
                ]
            )
        self.assertEqual(exit_code, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["notes_indexed"], 6)
        self.assertEqual(
            receipt["policy_config_sha256"],
            hashlib.sha256(config_bytes).hexdigest(),
        )

    def test_export_does_not_overwrite_file_created_during_build(self) -> None:
        output = Path(self.temporary.name) / "raced.json"
        document = build_viewer_document(self.database)

        def create_competing_output(*args: object, **kwargs: object) -> dict:
            output.write_text("competing writer", encoding="utf-8")
            return document

        with patch(
            "recallweave.viewer.build_viewer_document",
            side_effect=create_competing_output,
        ):
            with self.assertRaisesRegex(ValueError, "appeared during export"):
                export_viewer_graph(self.database, output)
        self.assertEqual(output.read_text(encoding="utf-8"), "competing writer")

    def test_export_refuses_parent_identity_change_during_build(self) -> None:
        output_parent = Path(self.temporary.name) / "exports"
        output_parent.mkdir()
        output = output_parent / self.database.name
        moved_parent = Path(self.temporary.name) / "exports-original"
        document = build_viewer_document(self.database)

        def replace_parent(*args: object, **kwargs: object) -> dict:
            output_parent.rename(moved_parent)
            output_parent.mkdir()
            return document

        with patch(
            "recallweave.viewer.build_viewer_document",
            side_effect=replace_parent,
        ):
            with self.assertRaisesRegex(ValueError, "parent changed during export"):
                export_viewer_graph(self.database, output, force=True)
        self.assertFalse(output.exists())
        self.assertTrue(self.database.exists())

    def test_empty_export_flags_actual_content_not_requested_mode(self) -> None:
        empty_vault = Path(self.temporary.name) / "empty-vault"
        empty_vault.mkdir()
        empty_database = Path(self.temporary.name) / "empty.sqlite"
        build_index(empty_vault, empty_database)

        document = build_viewer_document(empty_database, include_excerpts=True)
        privacy = document["privacy"]
        self.assertEqual(privacy["requested_profile"], "with_bounded_passage_text")
        self.assertEqual(privacy["export_profile"], "empty_graph")
        self.assertFalse(privacy["includes_excerpts"])
        self.assertFalse(privacy["includes_passage_text"])
        self.assertFalse(privacy["includes_note_derived_terms"])
        self.assertFalse(privacy["includes_paths_titles_tags"])
        self.assertTrue(privacy["metadata_only"])

        output = Path(self.temporary.name) / "empty.json"
        receipt = export_viewer_graph(
            empty_database,
            output,
            include_excerpts=True,
        )
        self.assertTrue(receipt["excerpts_requested"])
        self.assertFalse(receipt["excerpts_included"])
        self.assertFalse(receipt["passage_text_included"])
        self.assertFalse(receipt["note_derived_terms_included"])
        self.assertFalse(receipt["paths_titles_tags_included"])

    def test_force_replacement_detects_post_verify_victim_swap(self) -> None:
        output = Path(self.temporary.name) / "force-race.json"
        output.write_text("approved old output", encoding="utf-8")
        approved_elsewhere = Path(self.temporary.name) / "approved-elsewhere.json"
        victim = Path(self.temporary.name) / "victim.json"
        victim.write_text("late unapproved file", encoding="utf-8")
        original_rename = os.rename
        swapped = False

        def swap_before_rotation(source: object, destination: object) -> None:
            nonlocal swapped
            source_path = Path(source)
            if not swapped and source_path == output:
                swapped = True
                original_rename(output, approved_elsewhere)
                original_rename(victim, output)
            original_rename(source, destination)

        with patch("recallweave.viewer.os.rename", side_effect=swap_before_rotation):
            with self.assertRaisesRegex(
                ValueError,
                "rotated file was restored",
            ):
                export_viewer_graph(self.database, output, force=True)

        self.assertEqual(output.read_text(encoding="utf-8"), "late unapproved file")
        self.assertEqual(
            approved_elsewhere.read_text(encoding="utf-8"),
            "approved old output",
        )

    def test_force_replacement_rolls_back_if_install_fails(self) -> None:
        output = Path(self.temporary.name) / "force-rollback.json"
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
            with self.assertRaisesRegex(ValueError, "previous output was restored"):
                export_viewer_graph(self.database, output, force=True)
        self.assertEqual(output.read_text(encoding="utf-8"), "approved old output")

    def test_force_replacement_retains_backup_when_rollback_is_blocked(self) -> None:
        output = Path(self.temporary.name) / "force-retained.json"
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
            with self.assertRaisesRegex(ValueError, "Backup retained at:") as raised:
                export_viewer_graph(self.database, output, force=True)
        backup = Path(str(raised.exception).split("Backup retained at: ", 1)[1])
        self.assertEqual(output.read_text(encoding="utf-8"), "late competing file")
        self.assertEqual(backup.read_text(encoding="utf-8"), "approved old output")


class NullableTimestampTest(unittest.TestCase):
    def test_rejects_timezone_less_and_date_only_values(self) -> None:
        self.assertIsNone(_nullable_timestamp("2026-01-01"))
        self.assertIsNone(_nullable_timestamp("2026-01-01T12:00:00"))
        self.assertIsNone(_nullable_timestamp("not-a-date"))
        self.assertIsNone(_nullable_timestamp(" 2026-01-01T12:00:00Z "))

    def test_accepts_explicit_utc_timestamps(self) -> None:
        self.assertEqual(
            _nullable_timestamp("2026-01-01T12:00:00Z"),
            "2026-01-01T12:00:00Z",
        )
        self.assertEqual(
            _nullable_timestamp("2026-01-01T12:00:00+00:00"),
            "2026-01-01T12:00:00Z",
        )

    def test_overflowing_timezone_conversion_is_unknown(self) -> None:
        self.assertIsNone(_nullable_timestamp("0001-01-01T00:00:00+23:59"))
        self.assertEqual(
            _nullable_timestamp("2026-06-15T00:00:00.001Z"),
            "2026-06-15T00:00:00.001000Z",
        )
        self.assertEqual(
            _nullable_timestamp("2026-01-01T01:00:00.123+01:00"),
            "2026-01-01T00:00:00.123000Z",
        )
        self.assertEqual(
            _nullable_timestamp("2026-01-01T00:00:00.999999+00:00"),
            "2026-01-01T00:00:00.999999Z",
        )


class ViewerNavigationExportInvariantsTest(ViewerExportTest):
    """Founder rulings for recallweave-fkd: Atlas Obsidian navigation is LOCAL
    presentation state. The export never carries an actionable navigation URI,
    never gains a navigation field, and cannot be parameterized by any
    viewer-side navigation configuration — so export bytes are identical
    regardless of how a viewer is (or is not) configured to open notes."""

    def test_export_carries_no_obsidian_uri_or_navigation_field(self) -> None:
        for kwargs in (
            {},
            {"vault_name": "Research Vault"},
            {"include_excerpts": True, "vault_name": "Research Vault"},
            {"include_candidates": False},
        ):
            document = build_viewer_document(self.database, **kwargs)
            serialized = json.dumps(document, ensure_ascii=True, sort_keys=True)
            # No actionable deep-link URI of any scheme is ever emitted.
            self.assertNotIn("obsidian://", serialized)
            self.assertNotIn("://", serialized)
            self.assertNotIn("obsidian_vault", document)
            self.assertNotIn("navigation", document)

    def test_vault_name_is_provenance_label_only(self) -> None:
        document = build_viewer_document(self.database, vault_name="Research Vault")
        self.assertEqual(document["vault_name"], "Research Vault")
        self.assertNotIn("://", document["vault_name"])
        self.assertNotIn("/", document["vault_name"])
        self.assertNotIn("\\", document["vault_name"])

    def test_export_builder_takes_no_navigation_parameter(self) -> None:
        # No Atlas navigation/vault-open setting can be threaded into the export;
        # navigation is purely a viewer-local concern, so nav configuration
        # cannot change export bytes.
        import inspect

        params = set(inspect.signature(build_viewer_document).parameters)
        for forbidden in (
            "obsidian_vault",
            "navigation",
            "open_vault",
            "vault_open",
            "obsidian",
        ):
            self.assertNotIn(forbidden, params)


if __name__ == "__main__":
    unittest.main()
