from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recallweave.contract_spec import (
    CONTRACT_SPEC_VERSION,
    SourceRef,
    TaskSpec,
)


def valid_payload() -> dict:
    return {
        "spec_version": CONTRACT_SPEC_VERSION,
        "task_id": "growth-atlas-refresh",
        "objective": "Refresh the growth atlas with current notes.",
        "retrieval": {
            "query": "growth strategy",
            "limit": 8,
            "include_candidates": False,
            "max_characters": 8000,
        },
        "constraints": [
            {"text": "Do not infer client identity."},
            {
                "note": "Projects/Decision Memory.md",
                "heading": "Decision",
                "statement": "Previous decision on scoping.",
            },
        ],
        "prior_decisions": [{"text": "Use the existing index."}],
        "acceptance_criteria": [
            "All cited items resolve to physical vault lines.",
            "Excluded content is absent from every field.",
        ],
        "exclusions": {
            "paths": ["Restricted/Sealed Note.md"],
            "globs": ["Restricted/**"],
            "tags": ["private"],
            "directives": ["Do not infer client identity."],
        },
    }


class TaskSpecParsingTest(unittest.TestCase):
    def test_valid_full_spec_round_trips(self) -> None:
        spec = TaskSpec.from_payload(valid_payload())
        self.assertEqual(spec.objective, "Refresh the growth atlas with current notes.")
        self.assertEqual(spec.task_id, "growth-atlas-refresh")
        self.assertEqual(spec.query, "growth strategy")
        self.assertEqual(spec.limit, 8)
        self.assertFalse(spec.include_candidates)
        self.assertEqual(spec.max_characters, 8000)
        self.assertEqual(len(spec.constraints), 2)
        self.assertIsInstance(spec.constraints[0], SourceRef)
        self.assertEqual(spec.constraints[0].text, "Do not infer client identity.")
        self.assertIsNone(spec.constraints[0].note)
        self.assertEqual(spec.constraints[1].note, "Projects/Decision Memory.md")
        self.assertEqual(spec.constraints[1].heading, "Decision")
        self.assertEqual(
            spec.constraints[1].statement, "Previous decision on scoping."
        )
        self.assertEqual(len(spec.prior_decisions), 1)
        self.assertEqual(
            spec.acceptance_criteria,
            [
                "All cited items resolve to physical vault lines.",
                "Excluded content is absent from every field.",
            ],
        )
        self.assertEqual(spec.exclusion_paths, ["Restricted/Sealed Note.md"])
        self.assertEqual(spec.exclusion_globs, ["Restricted/**"])
        self.assertEqual(spec.exclusion_tags, ["private"])
        self.assertEqual(
            spec.exclusion_directives, ["Do not infer client identity."]
        )

    def test_defaults_applied(self) -> None:
        spec = TaskSpec.from_payload({"objective": "A task."})
        self.assertIsNone(spec.task_id)
        self.assertIsNone(spec.query)
        self.assertEqual(spec.limit, 8)
        self.assertFalse(spec.include_candidates)
        self.assertEqual(spec.max_characters, 8000)
        self.assertEqual(spec.constraints, [])
        self.assertEqual(spec.prior_decisions, [])
        self.assertEqual(spec.acceptance_criteria, [])
        self.assertEqual(spec.exclusion_paths, [])
        self.assertEqual(spec.exclusion_globs, [])
        self.assertEqual(spec.exclusion_tags, [])
        self.assertEqual(spec.exclusion_directives, [])

    def test_unknown_top_level_keys_rejected_sorted(self) -> None:
        payload = valid_payload()
        payload["zzz"] = 1
        payload["aaa"] = 2
        with self.assertRaisesRegex(ValueError, "Unknown task spec key"):
            TaskSpec.from_payload(payload)
        try:
            TaskSpec.from_payload(payload)
        except ValueError as error:
            message = str(error)
            self.assertLess(message.index("aaa"), message.index("zzz"))
            self.assertIn("aaa", message)
            self.assertIn("zzz", message)

    def test_unknown_retrieval_key_rejected(self) -> None:
        payload = valid_payload()
        payload["retrieval"]["bogus"] = True
        with self.assertRaisesRegex(ValueError, "Unknown retrieval key"):
            TaskSpec.from_payload(payload)

    def test_spec_notes_rejected_as_unknown_key(self) -> None:
        payload = valid_payload()
        payload["notes"] = "Operator notes."
        with self.assertRaisesRegex(ValueError, "Unknown task spec key.*notes"):
            TaskSpec.from_payload(payload)

    def test_unknown_exclusions_key_rejected(self) -> None:
        payload = valid_payload()
        payload["exclusions"]["bogus"] = True
        with self.assertRaisesRegex(ValueError, "Unknown exclusions key"):
            TaskSpec.from_payload(payload)

    def test_unknown_constraint_item_key_rejected(self) -> None:
        payload = valid_payload()
        payload["constraints"][0]["bogus"] = 1
        with self.assertRaisesRegex(ValueError, "unknown key"):
            TaskSpec.from_payload(payload)

    def test_item_with_both_text_and_note_rejected(self) -> None:
        payload = valid_payload()
        payload["constraints"] = [
            {"text": "a", "note": "Projects/Foo.md"}
        ]
        with self.assertRaisesRegex(ValueError, "exactly one of 'text' or 'note'"):
            TaskSpec.from_payload(payload)

    def test_item_with_neither_text_nor_note_rejected(self) -> None:
        payload = valid_payload()
        payload["constraints"] = [{"statement": "orphan"}]
        with self.assertRaisesRegex(ValueError, "exactly one of 'text' or 'note'"):
            TaskSpec.from_payload(payload)

    def test_objective_missing_rejected(self) -> None:
        payload = valid_payload()
        del payload["objective"]
        with self.assertRaisesRegex(ValueError, "objective"):
            TaskSpec.from_payload(payload)

    def test_objective_empty_rejected(self) -> None:
        payload = valid_payload()
        payload["objective"] = ""
        with self.assertRaisesRegex(ValueError, "objective"):
            TaskSpec.from_payload(payload)

    def test_objective_over_2000_rejected(self) -> None:
        payload = valid_payload()
        payload["objective"] = "x" * 2001
        with self.assertRaisesRegex(ValueError, "objective"):
            TaskSpec.from_payload(payload)

    def test_limit_0_rejected(self) -> None:
        payload = valid_payload()
        payload["retrieval"]["limit"] = 0
        with self.assertRaises(ValueError):
            TaskSpec.from_payload(payload)

    def test_limit_51_rejected(self) -> None:
        payload = valid_payload()
        payload["retrieval"]["limit"] = 51
        with self.assertRaises(ValueError):
            TaskSpec.from_payload(payload)

    def test_limit_true_rejected(self) -> None:
        payload = valid_payload()
        payload["retrieval"]["limit"] = True
        with self.assertRaises(ValueError):
            TaskSpec.from_payload(payload)

    def test_limit_float_rejected(self) -> None:
        payload = valid_payload()
        payload["retrieval"]["limit"] = 1.5
        with self.assertRaises(ValueError):
            TaskSpec.from_payload(payload)

    def test_two_hundred_paths_accepted(self) -> None:
        payload = valid_payload()
        payload["exclusions"]["paths"] = [f"Note {i}.md" for i in range(200)]
        spec = TaskSpec.from_payload(payload)
        self.assertEqual(len(spec.exclusion_paths), 200)

    def test_two_hundred_one_paths_rejected(self) -> None:
        payload = valid_payload()
        payload["exclusions"]["paths"] = [f"Note {i}.md" for i in range(201)]
        with self.assertRaises(ValueError):
            TaskSpec.from_payload(payload)

    def test_utf8_bom_spec_parses(self) -> None:
        raw = json.dumps({"objective": "BOM task."}).encode("utf-8")
        bom = b"\xef\xbb\xbf" + raw
        spec = TaskSpec.from_bytes(bom)
        self.assertEqual(spec.objective, "BOM task.")

    def test_from_bytes_non_utf8_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Task spec must be UTF-8 JSON"):
            TaskSpec.from_bytes(b"\xff\xfe\x00\x01")

    def test_from_file_parses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spec.json"
            path.write_text(json.dumps(valid_payload()), encoding="utf-8")
            spec = TaskSpec.from_file(path)
            self.assertEqual(spec.objective, valid_payload()["objective"])


if __name__ == "__main__":
    unittest.main()
