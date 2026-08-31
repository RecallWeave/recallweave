from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from recallweave.steward_policy import (
    APPEND_ONLY_CLASSES,
    MUTATION_CLASSES,
    POLICY_LEVELS,
    WRITE_POLICY_SPEC_VERSION,
    WritePolicy,
    resolve_level,
)

ROOT = Path(__file__).resolve().parents[1]


def _payload(**extra) -> dict:
    payload = {
        "spec_version": WRITE_POLICY_SPEC_VERSION,
        "default_level": "propose_only",
        "class_levels": {
            "create_new_file": "require_approval",
            "append_at_eof": "propose_only",
        },
        "protected": {},
        "source_overrides": {},
    }
    payload.update(extra)
    return payload


class StewardPolicyLoadTest(unittest.TestCase):
    def test_non_object_payload_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            WritePolicy.from_payload(["not", "an", "object"])

    def test_unknown_top_level_key_rejected_sorted(self) -> None:
        payload = _payload(bogus=1, zebra=2)
        with self.assertRaisesRegex(ValueError, "Unknown write policy key"):
            WritePolicy.from_payload(payload)

    def test_missing_spec_version_rejected(self) -> None:
        payload = _payload()
        del payload["spec_version"]
        with self.assertRaisesRegex(ValueError, "spec_version"):
            WritePolicy.from_payload(payload)

    def test_wrong_spec_version_rejected(self) -> None:
        payload = _payload(spec_version="recallweave.steward.policy.v9")
        with self.assertRaisesRegex(ValueError, "spec_version"):
            WritePolicy.from_payload(payload)

    def test_unknown_class_in_class_levels_rejected(self) -> None:
        payload = _payload(class_levels={"create_new_file": "auto_apply", "wipe_all": "auto_apply"})
        with self.assertRaisesRegex(ValueError, "Unknown mutation class"):
            WritePolicy.from_payload(payload)

    def test_unknown_class_in_source_override_rejected(self) -> None:
        payload = _payload(source_overrides={"ci": {"truncate_db": "propose_only"}})
        with self.assertRaisesRegex(ValueError, "Unknown mutation class"):
            WritePolicy.from_payload(payload)

    def test_unknown_level_value_rejected(self) -> None:
        payload = _payload(default_level="nuclear")
        with self.assertRaisesRegex(ValueError, "Unknown policy level"):
            WritePolicy.from_payload(payload)

    def test_auto_apply_on_replace_whole_section_rejected_globally(self) -> None:
        payload = _payload(
            class_levels={"replace_whole_section": "auto_apply"}
        )
        with self.assertRaisesRegex(ValueError, "auto_apply is not allowed"):
            WritePolicy.from_payload(payload)

    def test_auto_apply_on_replace_whole_section_via_override_rejected(self) -> None:
        payload = _payload(
            source_overrides={"ci": {"replace_whole_section": "auto_apply"}}
        )
        with self.assertRaisesRegex(ValueError, "auto_apply is not allowed"):
            WritePolicy.from_payload(payload)

    def test_principal_key_top_level_rejected(self) -> None:
        payload = _payload(role="admin")
        with self.assertRaisesRegex(ValueError, "Principal-like key"):
            WritePolicy.from_payload(payload)

    def test_principal_key_nested_in_override_rejected(self) -> None:
        payload = _payload(source_overrides={"ci": {"approvers": ["alice"]}})
        with self.assertRaisesRegex(ValueError, "Principal-like key"):
            WritePolicy.from_payload(payload)

    def test_principal_key_in_protected_rejected(self) -> None:
        payload = _payload(protected={"path_terms": ["x"], "user": ["bob"]})
        with self.assertRaisesRegex(ValueError, "Principal-like key"):
            WritePolicy.from_payload(payload)

    def test_numeric_confidence_key_rejected(self) -> None:
        payload = _payload(class_levels={"confidence": 0.9})
        with self.assertRaisesRegex(ValueError, "confidence"):
            WritePolicy.from_payload(payload)

    def test_remote_scheme_in_value_rejected(self) -> None:
        payload = _payload(protected={"paths": ["http://example.com/x"]})
        with self.assertRaisesRegex(ValueError, "Remote or URL"):
            WritePolicy.from_payload(payload)

    def test_max_files_per_apply_must_be_positive_int(self) -> None:
        payload = _payload(max_files_per_apply=0)
        with self.assertRaisesRegex(ValueError, "max_files_per_apply"):
            WritePolicy.from_payload(payload)

    def test_max_files_per_apply_cap(self) -> None:
        payload = _payload(max_files_per_apply=501)
        with self.assertRaisesRegex(ValueError, "max_files_per_apply"):
            WritePolicy.from_payload(payload)

    def test_require_git_must_be_bool(self) -> None:
        payload = _payload(require_git="yes")
        with self.assertRaisesRegex(ValueError, "require_git"):
            WritePolicy.from_payload(payload)

    def test_frontmatter_must_be_object_of_string_lists(self) -> None:
        payload = _payload(protected={"frontmatter": {"sensitivity": "sealed"}})
        with self.assertRaisesRegex(ValueError, "frontmatter"):
            WritePolicy.from_payload(payload)

    def test_bad_source_override_name_rejected(self) -> None:
        payload = _payload(source_overrides={"bad name!": {"create_new_file": "propose_only"}})
        with self.assertRaisesRegex(ValueError, "source override name"):
            WritePolicy.from_payload(payload)

    def test_unknown_protected_key_rejected_sorted(self) -> None:
        payload = _payload(protected={"globs": [], "bogus": []})
        with self.assertRaisesRegex(ValueError, "Unknown protected key"):
            WritePolicy.from_payload(payload)

    def test_valid_payload_parses(self) -> None:
        policy = WritePolicy.from_payload(_payload())
        self.assertEqual(policy.default_level, "propose_only")
        self.assertEqual(policy.class_levels["create_new_file"], "require_approval")
        self.assertEqual(policy.max_files_per_apply, 20)
        self.assertIsNone(policy.policy_sha256)

    def test_bom_tolerated(self) -> None:
        data = b"\xef\xbb\xbf" + json.dumps(_payload()).encode("utf-8")
        policy = WritePolicy.from_bytes(data)
        self.assertEqual(policy.default_level, "propose_only")

    def test_policy_sha256_stable_for_identical_bytes(self) -> None:
        data = json.dumps(_payload(), sort_keys=True).encode("utf-8")
        first = WritePolicy.from_bytes(data)
        second = WritePolicy.from_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(first.policy_sha256, expected)
        self.assertEqual(second.policy_sha256, expected)

    def test_example_file_parses_via_from_file(self) -> None:
        path = ROOT / "examples" / "steward-policy.example.json"
        policy = WritePolicy.from_file(path)
        self.assertEqual(policy.default_level, "propose_only")
        self.assertEqual(
            policy.source_overrides["ci-pipeline"]["append_at_eof"], "auto_apply"
        )
        self.assertEqual(policy.protected_globs, ["Restricted/**"])
        self.assertIsNotNone(policy.policy_sha256)


class StewardPolicyResolveTest(unittest.TestCase):
    def test_fresh_policy_defaults_to_propose_only(self) -> None:
        policy = WritePolicy()
        for cls in MUTATION_CLASSES:
            level, reason = resolve_level(
                policy, mutation_class=cls, source_name=None, relative_path="Notes/A.md"
            )
            self.assertEqual((level, reason), ("propose_only", "default"))

    def test_class_level_beats_default(self) -> None:
        policy = WritePolicy(class_levels={"move_to_trash": "require_approval"})
        level, reason = resolve_level(
            policy,
            mutation_class="move_to_trash",
            source_name=None,
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("require_approval", "class_level"))

    def test_source_override_beats_class_level(self) -> None:
        policy = WritePolicy(
            class_levels={"append_at_eof": "propose_only"},
            source_overrides={"ci": {"append_at_eof": "auto_apply"}},
        )
        level, reason = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name="ci",
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("auto_apply", "source_override"))

    def test_protected_paths_beats_override(self) -> None:
        policy = WritePolicy(
            protected_paths=["notes/A.md"],
            source_overrides={"ci": {"append_at_eof": "auto_apply"}},
        )
        level, reason = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name="ci",
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("disabled", "protected:paths"))

    def test_protected_globs_beats_override(self) -> None:
        policy = WritePolicy(
            protected_globs=["Restricted/**"],
            source_overrides={"ci": {"append_at_eof": "auto_apply"}},
        )
        level, reason = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name="ci",
            relative_path="Restricted/A.md",
        )
        self.assertEqual((level, reason), ("disabled", "protected:globs"))

    def test_protected_path_terms_beats_override(self) -> None:
        policy = WritePolicy(
            protected_path_terms=["credentials"],
            source_overrides={"ci": {"append_at_eof": "auto_apply"}},
        )
        level, reason = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name="ci",
            relative_path="secrets/credentials.md",
        )
        self.assertEqual((level, reason), ("disabled", "protected:path_terms"))

    def test_protected_frontmatter_beats_override(self) -> None:
        policy = WritePolicy(
            protected_frontmatter={"sensitivity": ["sealed"]},
            source_overrides={"ci": {"append_at_eof": "auto_apply"}},
        )
        level, reason = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name="ci",
            relative_path="Notes/A.md",
            frontmatter={"sensitivity": "sealed"},
        )
        self.assertEqual((level, reason), ("disabled", "protected:frontmatter:sensitivity"))

    def test_protected_frontmatter_comma_split_values(self) -> None:
        policy = WritePolicy(protected_frontmatter={"sensitivity": ["sealed"]})
        level, _ = resolve_level(
            policy,
            mutation_class="append_at_eof",
            source_name=None,
            relative_path="Notes/A.md",
            frontmatter={"sensitivity": "public, sealed"},
        )
        self.assertEqual(level, "disabled")

    def test_unknown_class_in_resolve_is_disabled(self) -> None:
        policy = WritePolicy(default_level="auto_apply")
        level, reason = resolve_level(
            policy,
            mutation_class="wipe_all",
            source_name=None,
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("disabled", "unknown_class"))

    def test_default_reason_returned(self) -> None:
        policy = WritePolicy()
        level, reason = resolve_level(
            policy,
            mutation_class="move_to_trash",
            source_name=None,
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("propose_only", "default"))

    def test_protected_precedence_over_unknown_class(self) -> None:
        policy = WritePolicy(protected_paths=["notes/A.md"])
        level, reason = resolve_level(
            policy,
            mutation_class="wipe_all",
            source_name=None,
            relative_path="Notes/A.md",
        )
        self.assertEqual((level, reason), ("disabled", "protected:paths"))

    def test_append_only_class_allows_auto_apply(self) -> None:
        self.assertIn("create_new_file", APPEND_ONLY_CLASSES)
        self.assertIn("append_at_eof", APPEND_ONLY_CLASSES)
        policy = WritePolicy(class_levels={"create_new_file": "auto_apply"})
        level, _ = resolve_level(
            policy,
            mutation_class="create_new_file",
            source_name=None,
            relative_path="New.md",
        )
        self.assertEqual(level, "auto_apply")

    def test_levels_and_classes_public_constants(self) -> None:
        self.assertEqual(
            POLICY_LEVELS,
            ("disabled", "propose_only", "require_approval", "auto_apply"),
        )
        self.assertEqual(len(MUTATION_CLASSES), 5)
        self.assertEqual(
            WRITE_POLICY_SPEC_VERSION, "recallweave.steward.policy.v1"
        )


if __name__ == "__main__":
    unittest.main()
