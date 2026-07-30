from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from recallweave.cli import main as cli_main
from recallweave.index import (
    _markdown_files,
    build_index,
    connect,
    default_database_for_vault,
)
from recallweave.parser import parse_note
from recallweave.policy import IndexPolicy
from recallweave.query import connections, context_packet, doctor, stats


class AdversarialRecallWeaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.database = self.root / "index.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative_path: str, text: str) -> Path:
        path = self.vault / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_citation_range_matches_exact_passage_lines(self) -> None:
        path = self.write("Citation.md", "# Heading\n\nfirst line\n\nlast line\n")
        note = parse_note(path, self.vault)
        section = note.sections[0]
        physical_lines = path.read_text(encoding="utf-8").split("\n")
        cited = "\n".join(physical_lines[section.line_start - 1 : section.line_end])
        self.assertEqual((section.line_start, section.line_end), (3, 5))
        self.assertEqual(cited, section.text)

    def test_unicode_line_separator_cannot_shift_physical_citations(self) -> None:
        path = self.write("Unicode.md", "# Heading\n\nfirst\u2028second\n")
        note = parse_note(path, self.vault)
        self.assertEqual(note.sections[0].line_start, 3)
        self.assertEqual(note.sections[0].line_end, 3)
        self.assertEqual(note.sections[0].text, "first\u2028second")

    def test_crlf_citations_use_physical_editor_lines(self) -> None:
        path = self.vault / "CRLF.md"
        path.write_bytes(b"# Heading\r\n\r\nfirst\r\nsecond\r\n")
        section = parse_note(path, self.vault).sections[0]
        self.assertEqual((section.line_start, section.line_end), (3, 4))
        self.assertEqual(section.text, "first\nsecond")

    def test_frontmatter_denials_handle_duplicates_commas_and_invalid_syntax(self) -> None:
        self.write(
            "Duplicate.md",
            "---\nsensitivity: sealed\nsensitivity: public\n---\n# Duplicate\nbody\n",
        )
        self.write(
            "Comma.md",
            "---\nsensitivity: sealed, internal\n---\n# Comma\nbody\n",
        )
        self.write(
            "Invalid.md",
            "---\nsensitivity sealed\n---\n# Invalid\nbody\n",
        )
        self.write(
            "Nested.md",
            "---\nprivacy:\n  sensitivity: sealed\n---\n# Nested\nbody\n",
        )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["denied_frontmatter:sensitivity"], 2)
        self.assertEqual(receipt["skipped"]["unparseable_frontmatter"], 2)

    def test_frontmatter_denials_cover_comments_and_unsupported_yaml_values(self) -> None:
        fixtures = {
            "Comment.md": "sensitivity: sealed # internal only",
            "Folded.md": "sensitivity: >-\n  sealed",
            "Literal.md": "sensitivity: |\n  sealed",
            "Tag.md": "sensitivity: !!str sealed",
            "Anchor.md": "sensitivity: &a sealed",
            "FlowMap.md": "sensitivity: {a: sealed}",
            "PlainContinuation.md": "sensitivity:\n  sealed",
            "VerbatimTag.md": "sensitivity: !<tag:yaml.org,2002:str> sealed",
            "BareTag.md": "sensitivity: ! sealed",
            "EscapedQuote.md": 'sensitivity: "\\x73ealed"',
            "LocalTag.md": "sensitivity: !str sealed",
        }
        for name, frontmatter in fixtures.items():
            self.write(
                name,
                f"---\n{frontmatter}\n---\n# {Path(name).stem}\nbody\n",
            )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["denied_frontmatter:sensitivity"], 1)
        self.assertEqual(receipt["skipped"]["unparseable_frontmatter"], 10)

    def test_quoted_hash_remains_data_under_frontmatter_policy(self) -> None:
        for name, value in {
            "DoubleQuoted.md": '"sealed # internal only"',
            "SingleQuoted.md": "'sealed # internal only'",
        }.items():
            self.write(
                name,
                f"---\nsensitivity: {value}\n---\n# {Path(name).stem}\nbody\n",
            )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 2)
        self.assertNotIn("denied_frontmatter:sensitivity", receipt["skipped"])
        self.assertNotIn("unparseable_frontmatter", receipt["skipped"])

    def test_unsupported_values_fail_closed_across_sequence_shapes(self) -> None:
        unsupported = {
            "CoreTag": "!!str sealed",
            "LocalTag": "!str sealed",
            "BareTag": "! sealed",
            "VerbatimTag": "!<tag:yaml.org,2002:str> sealed",
            "EscapedQuote": '"\\x73ealed"',
            "Anchor": "&a sealed",
            "FlowMap": "{a: sealed}",
        }
        wrappers = {
            "Top": lambda value: f"sensitivity: {value}",
            "Flow1": lambda value: f"sensitivity: [{value}]",
            "Flow2": lambda value: f"sensitivity: [[{value}]]",
            "Block1": lambda value: f"sensitivity:\n  - {value}",
            "Block2": lambda value: f"sensitivity:\n  - - {value}",
            "Block3": lambda value: f"sensitivity:\n  - - - {value}",
            "BlockFlow": lambda value: f"sensitivity:\n  - [{value}]",
        }
        for value_name, value in unsupported.items():
            for wrapper_name, wrapper in wrappers.items():
                name = f"{wrapper_name}-{value_name}.md"
                self.write(
                    name,
                    f"---\n{wrapper(value)}\n---\n# {Path(name).stem}\n"
                    f"sequence leak canary {value_name} {wrapper_name}\n",
                )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(
            receipt["skipped"]["unparseable_frontmatter"],
            len(unsupported) * len(wrappers),
        )
        self.assertNotIn(b"sequence leak canary", self.database.read_bytes())

    def test_nested_block_sequences_fail_closed_at_every_query_surface(self) -> None:
        nested_values = {
            "Plain2": "- sealed",
            "CoreTag2": "- !!str sealed",
            "BareTag2": "- ! sealed",
            "VerbatimTag2": "- !<tag:yaml.org,2002:str> sealed",
            "EscapedHex2": r'- "\x73ealed"',
            "EscapedUnicode2": r'- "\u0073ealed"',
            "Anchor2": "- &a sealed",
            "Plain3": "- - sealed",
            "CoreTag3": "- - !!str sealed",
            "BareTag3": "- - ! sealed",
            "VerbatimTag3": "- - !<tag:yaml.org,2002:str> sealed",
            "EscapedHex3": r'- - "\x73ealed"',
            "Flow3": "- [sealed]",
            "FlowTag3": "- [!!str sealed]",
            "Comment2": "- sealed # comment",
            "Indent4": "- sealed",
            "Mixed": "public\n  - - sealed",
            "NestedMap": "label: sealed",
        }
        for name, value in nested_values.items():
            indent = "    " if name == "Indent4" else "  "
            self.write(
                f"{name}.md",
                f"---\nsensitivity:\n{indent}- {value}\n---\n# {name}\n"
                f"zarquonium acquisition canary {name}\n",
            )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(
            receipt["skipped"]["unparseable_frontmatter"],
            len(nested_values),
        )
        with connect(self.database, readonly=True) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM sections").fetchone()[0],
                0,
            )
        self.assertNotIn(b"zarquonium", self.database.read_bytes())
        packet = context_packet(self.database, "zarquonium acquisition")
        self.assertEqual(packet["passages"], [])
        self.assertEqual(packet["citations"], [])

    def test_frontmatter_depth_limit_prevents_whole_vault_abort(self) -> None:
        nested = ("[" * 400) + "sealed" + ("]" * 400)
        self.write(
            "Bomb.md",
            f"---\nsensitivity: {nested}\n---\n# Bomb\ndepth bomb canary\n",
        )
        self.write("Good.md", "# Good\nhealthy note remains indexable\n")
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 1)
        self.assertEqual(receipt["skipped"]["unparseable_frontmatter"], 1)
        self.assertNotIn(b"depth bomb canary", self.database.read_bytes())
        self.assertEqual(
            context_packet(self.database, "depth bomb canary")["passages"],
            [],
        )

    def test_recursion_error_skips_only_the_malformed_note(self) -> None:
        bomb = self.write("Bomb.md", "# Bomb\nbody\n").resolve()
        self.write("Good.md", "# Good\nhealthy recursion fallback\n")
        real_parse_note = parse_note

        def guarded_parse(path: Path, vault: Path):
            if path == bomb:
                raise RecursionError("synthetic parser depth failure")
            return real_parse_note(path, vault)

        with patch("recallweave.index.parse_note", side_effect=guarded_parse):
            receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["notes_indexed"], 1)
        self.assertEqual(receipt["skipped"]["unparseable_frontmatter"], 1)
        self.assertEqual(stats(self.database)["notes"], 1)

    def test_nonempty_multiline_continuations_fail_closed(self) -> None:
        fixtures = {
            "TopSecret.md": "sensitivity: top\n  secret",
            "ClientConfidential.md": "sensitivity: client\n  confidential",
            "DoNotShare.md": "sensitivity: do\n  not\n  share",
            "InternalSealed.md": "sensitivity: internal,\n  sealed",
            "PublicInternalSealed.md": "sensitivity: public,\n  internal,\n  sealed",
        }
        for name, frontmatter in fixtures.items():
            self.write(
                name,
                f"---\n{frontmatter}\n---\n# {Path(name).stem}\ncontinuation leak canary\n",
            )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(
                deny_frontmatter={
                    "sensitivity": [
                        "top secret",
                        "client confidential",
                        "do not share",
                        "sealed",
                    ]
                }
            ),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["unparseable_frontmatter"], 5)
        self.assertNotIn(b"continuation leak canary", self.database.read_bytes())

    def test_supported_sequence_values_keep_policy_semantics(self) -> None:
        self.write(
            "FlowDenied.md",
            "---\nsensitivity: [sealed]\n---\n# Flow Denied\nbody\n",
        )
        self.write(
            "FlowQuotedDenied.md",
            '---\nsensitivity: ["sealed"]\n---\n# Flow Quoted Denied\nbody\n',
        )
        self.write(
            "BlockDenied.md",
            "---\nsensitivity:\n  - sealed\n---\n# Block Denied\nbody\n",
        )
        self.write(
            "FlowHashData.md",
            '---\nsensitivity: ["sealed # internal only"]\n---\n# Flow Hash Data\nbody\n',
        )
        self.write(
            "BlockUrlData.md",
            "---\nsensitivity:\n  - https://example.com/private\n"
            "---\n# Block URL Data\nbody\n",
        )
        self.write(
            "BlockQuotedColonData.md",
            '---\nsensitivity:\n  - "label: sealed"\n'
            "---\n# Block Quoted Colon Data\nbody\n",
        )
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_frontmatter={"sensitivity": ["sealed"]}),
        )
        self.assertEqual(receipt["notes_indexed"], 3)
        self.assertEqual(receipt["skipped"]["denied_frontmatter:sensitivity"], 3)
        self.assertNotIn("unparseable_frontmatter", receipt["skipped"])

    def test_symlink_file_is_never_indexed(self) -> None:
        secret = self.root / "secrets.md"
        secret.write_text("outside secret material", encoding="utf-8")
        link = self.vault / "Public Notes.md"
        try:
            os.symlink(secret, link)
        except OSError as error:
            self.skipTest(f"Symlink creation is unavailable: {error}")
        receipt = build_index(
            self.vault,
            self.database,
            policy=IndexPolicy(deny_path_terms=["secret"]),
        )
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["symlink"], 1)
        self.assertNotIn(b"outside secret material", self.database.read_bytes())

    def test_hardlink_alias_is_never_indexed(self) -> None:
        secret = self.root / "hardlinked-secret.md"
        secret.write_text("outside hardlinked secret material", encoding="utf-8")
        alias = self.vault / "Public Notes.md"
        try:
            os.link(secret, alias)
        except OSError as error:
            self.skipTest(f"Hardlink creation is unavailable: {error}")
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["hardlink"], 1)
        self.assertNotIn(b"outside hardlinked secret material", self.database.read_bytes())

    def test_duplicate_resolved_paths_are_indexed_once(self) -> None:
        original = self.write("Original.md", "# Original\nbody\n").resolve()
        alias = self.write("Alias.md", "# Alias\nbody\n").resolve()
        vault = self.vault.resolve()
        real_resolve = Path.resolve

        def simulated_junction_resolve(path: Path, strict: bool = False) -> Path:
            if path == alias:
                return real_resolve(original, strict=strict)
            return real_resolve(path, strict=strict)

        skipped: Counter[str] = Counter()
        with patch("recallweave.index.Path.resolve", new=simulated_junction_resolve):
            paths = _markdown_files(vault, skipped)
        self.assertEqual(len(paths), 1)
        self.assertEqual(skipped["duplicate_resolved_path"], 1)

    def test_existing_non_database_file_is_not_overwritten(self) -> None:
        self.write("Safe.md", "# Safe\nbody\n")
        victim = self.root / "IMPORTANT_NOTES.md"
        victim.write_text("do not destroy", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
            build_index(self.vault, victim)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not destroy")

    def test_folder_qualified_link_never_falls_back_to_wrong_basename(self) -> None:
        self.write("Source.md", "# Source\nSee [[Projects/Roadmap]].\n")
        self.write("Archive/Roadmap.md", "# Roadmap\narchived\n")
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["verified_edges"], 0)
        report = doctor(self.database)
        self.assertEqual(report["unresolved"][0]["reason"], "path_not_found")
        self.assertEqual(report["unresolved"][0]["target"], "Projects/Roadmap")

    def test_percent_encoded_markdown_link_resolves_exact_path(self) -> None:
        self.write("Projects/Source.md", "# Source\n[Road map](Road%20Map.md)\n")
        self.write("Projects/Road Map.md", "# Road Map\nbody\n")
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["verified_edges"], 1)
        self.assertEqual(receipt["unresolved_links"], 0)

    def test_duplicate_authored_link_is_one_edge_and_receipt_matches_database(self) -> None:
        self.write("Source.md", "# Source\n[[Target]] and again [[Target]].\n")
        self.write("Target.md", "# Target\nbody\n")
        receipt = build_index(self.vault, self.database)
        with connect(self.database, readonly=True) as connection:
            actual = int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
        self.assertEqual(receipt["verified_edges"], 1)
        self.assertEqual(receipt["verified_edges"] + receipt["candidate_edges"], actual)

    def test_database_inside_vault_requires_deliberate_override(self) -> None:
        self.write("Safe.md", "# Safe\nbody\n")
        inside = self.vault / "index.sqlite"
        with self.assertRaisesRegex(ValueError, "inside the vault"):
            build_index(self.vault, inside)
        self.assertFalse(inside.exists())
        receipt = build_index(self.vault, inside, allow_in_vault=True)
        self.assertEqual(receipt["vault_writes"], 1)

    def test_default_database_is_outside_vault(self) -> None:
        default = default_database_for_vault(self.vault)
        self.assertNotIn(self.vault.resolve(), default.resolve().parents)

    def test_readonly_uri_handles_reserved_uri_characters(self) -> None:
        self.write("Safe.md", "# Safe\nbody\n")
        database = self.root / "îndex space # percent%.sqlite"
        build_index(self.vault, database)
        self.assertEqual(stats(database)["notes"], 1)

    def test_shared_tags_do_not_materialize_quadratic_edges(self) -> None:
        for index in range(600):
            self.write(
                f"Note {index:04d}.md",
                f"# Note {index}\nA short entry. #daily #journal\n",
            )
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["note_tags"], 1_200)
        self.assertEqual(receipt["verified_edges"], 0)
        self.assertLess(self.database.stat().st_size, 10_000_000)
        result = connections(self.database, "Note 0000.md", limit=20)
        self.assertLessEqual(len(result["connections"]), 20)
        self.assertTrue(all(not item["verified"] for item in result["connections"]))

    def test_discovery_scales_past_the_old_absolute_df_cap(self) -> None:
        group_words = [
            "amber", "bronze", "cobalt", "denim", "ember",
            "fuchsia", "golden", "hazel", "indigo", "jade",
        ]
        for index in range(1_000):
            group = group_words[index // 100]
            self.write(
                f"Scale/Note {index:04d}.md",
                f"# Scale Note {index}\n{group} {group}mesh constellation synthesis pattern.\n",
            )
        receipt = build_index(
            self.vault,
            self.database,
            minimum_candidate_score=0.05,
            max_candidates_per_note=4,
        )
        self.assertGreater(receipt["candidate_edges"], 0)
        self.assertGreater(receipt["discovery"]["terms_usable"], 0)
        self.assertGreater(receipt["discovery"]["pairs_compared"], 0)

    def test_fenced_and_inline_code_do_not_create_graph_evidence(self) -> None:
        path = self.write(
            "Code.md",
            "# Real\n"
            "```md\n# Fake\n[[Secret]] #daily\n```\n"
            "~~~md\n## Also Fake\n[[Other Secret]] #journal\n~~~\n"
            "`[[Inline]] #inline` #include #abcdef [anchor](#anchor) #valid\n",
        )
        note = parse_note(path, self.vault)
        self.assertEqual([section.heading for section in note.sections], ["Real"])
        self.assertEqual(
            [(link.kind, link.target) for link in note.links],
            [("tag", "valid")],
        )

    def test_unknown_policy_keys_are_rejected(self) -> None:
        config = self.root / "policy.json"
        config.write_text(json.dumps({"deny_frontmater": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unknown policy"):
            IndexPolicy.from_file(config)

    def test_utf16_is_skipped_instead_of_indexed_as_mojibake(self) -> None:
        path = self.vault / "UTF16.md"
        path.write_text("# UTF16\nsecret\n", encoding="utf-16")
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertEqual(receipt["skipped"]["unsupported_encoding"], 1)

    def test_nested_reserved_directory_is_never_indexed(self) -> None:
        self.write("Team/.obsidian/Leaked.md", "# Leaked\nbody\n")
        self.write("Team/Safe.md", "# Safe\nbody\n")
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["notes_indexed"], 1)

    def test_truncated_passages_are_explicitly_marked(self) -> None:
        self.write("Long.md", "# Long\n\nsignal " + ("material " * 100) + "\n")
        build_index(self.vault, self.database)
        result = context_packet(self.database, "signal material", max_characters=80)
        self.assertTrue(result["passages"][0]["truncated"])
        self.assertLessEqual(result["characters_used"], 80)

    def test_connection_limits_report_total_and_truncation(self) -> None:
        links = " ".join(f"[[Target {index:03d}]]" for index in range(400))
        self.write("Hub.md", f"# Hub\nhubneedle\n{links}\n")
        for index in range(400):
            self.write(
                f"Targets/Target {index:03d}.md",
                f"# Target {index:03d}\nleaf\n",
            )
        build_index(self.vault, self.database)

        related = connections(
            self.database,
            "Hub",
            include_candidates=False,
            limit=100,
        )
        self.assertEqual(related["connections_total"], 400)
        self.assertEqual(related["connections_returned"], 100)
        self.assertTrue(related["connections_truncated"])

        packet = context_packet(
            self.database,
            "hubneedle",
            limit=1,
            include_candidates=False,
        )
        self.assertEqual(packet["connections_total"], 400)
        self.assertEqual(packet["connections_returned"], 200)
        self.assertTrue(packet["connections_truncated"])

    def test_missing_database_error_is_actionable(self) -> None:
        with self.assertRaisesRegex(ValueError, "Run 'recallweave index"):
            stats(self.root / "missing.sqlite")

    def test_cli_missing_and_corrupt_database_return_actionable_exit_two(self) -> None:
        missing_error = StringIO()
        with redirect_stderr(missing_error):
            missing_exit = cli_main(
                ["stats", "--database", str(self.root / "missing.sqlite")]
            )
        self.assertEqual(missing_exit, 2)
        self.assertIn("Run 'recallweave index", missing_error.getvalue())

        corrupt = self.root / "corrupt.sqlite"
        corrupt.write_text("not sqlite", encoding="utf-8")
        corrupt_error = StringIO()
        with redirect_stderr(corrupt_error):
            corrupt_exit = cli_main(["query", "anything", "--database", str(corrupt)])
        self.assertEqual(corrupt_exit, 2)
        self.assertIn("Not a RecallWeave database", corrupt_error.getvalue())

    def test_empty_vault_builds_an_actionable_empty_index(self) -> None:
        receipt = build_index(self.vault, self.database)
        self.assertEqual(receipt["notes_indexed"], 0)
        self.assertIn("Discovery needs at least two", receipt["discovery"]["warnings"][0])
        self.assertEqual(stats(self.database)["notes"], 0)


if __name__ == "__main__":
    unittest.main()
