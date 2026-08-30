from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recallweave.policy import IndexPolicy
from recallweave.steward_sources import (
    SOURCES_SPEC_VERSION,
    SourceRegistry,
    StewardSource,
    load_registry,
)


def _registry(*sources) -> dict:
    return {"spec_version": SOURCES_SPEC_VERSION, "sources": list(sources)}


def _source(name: str, root: str, type_: str = "folder", mode: str = "read_only", **extra) -> dict:
    item = {"name": name, "type": type_, "root": root, "mode": mode}
    item.update(extra)
    return item


class StewardSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault_a = self.root / "vault-a"
        self.vault_b = self.root / "vault-b"
        self.vault_a.mkdir()
        self.vault_b.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_two_source_registry_parses(self) -> None:
        payload = _registry(
            _source("alpha", str(self.vault_a)),
            _source("beta", str(self.vault_b)),
        )
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertEqual(len(registry.sources), 2)
        self.assertEqual(registry.sources[0].name, "alpha")
        self.assertEqual(registry.sources[0].root, self.vault_a.resolve())
        self.assertEqual(registry.sources[0].mode, "read_only")
        self.assertIsInstance(registry.sources[0].policy, IndexPolicy)
        self.assertIsNone(registry.registry_sha256)

    def test_sha256_present_and_stable_for_identical_bytes(self) -> None:
        data = json.dumps(
            _registry(_source("alpha", str(self.vault_a))), sort_keys=True
        ).encode("utf-8")
        first = SourceRegistry.from_bytes(data, base_dir=self.root)
        second = SourceRegistry.from_bytes(data, base_dir=self.root)
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(first.registry_sha256, expected)
        self.assertEqual(second.registry_sha256, expected)

    def test_relative_root_resolves_against_base_dir(self) -> None:
        payload = _registry(_source("alpha", "vault-a"))
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertEqual(registry.sources[0].root, self.vault_a.resolve())

    def test_file_type_root_must_be_file(self) -> None:
        note = self.root / "note.md"
        note.write_text("content", encoding="utf-8")
        payload = _registry(
            _source("note", str(note), type_="file", mode="read_only")
        )
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertEqual(registry.sources[0].type, "file")
        self.assertEqual(registry.sources[0].root, note.resolve())

    def test_file_source_descriptor_opened_binary(self) -> None:
        # A file source's pinned descriptor becomes the note fd observe dups,
        # so it must carry O_BINARY: on Windows a text-mode descriptor would
        # translate CRLF on read and desync the bytes from st_size / the hash.
        # O_BINARY is 0 on POSIX, so pin a nonzero sentinel to make the flag
        # observable cross-platform.
        note = self.root / "note.md"
        note.write_text("content", encoding="utf-8")
        payload = _registry(
            _source("note", str(note), type_="file", mode="read_only")
        )
        import recallweave.steward_sources as _src

        sentinel = 0x8000
        real_open = os.open
        captured: list[int] = []

        def recording_open(path, flags, *args, **kwargs):
            captured.append(flags)
            return real_open(path, flags & ~sentinel, *args, **kwargs)

        with patch.object(_src.os, "O_BINARY", sentinel, create=True), patch.object(
            _src.os, "open", side_effect=recording_open
        ):
            SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertTrue(captured, "expected the file source root to be opened")
        self.assertTrue(
            all(flags & sentinel for flags in captured),
            f"file source descriptor opened without O_BINARY: {captured!r}",
        )

    def test_policy_parsed_and_defaults_applied(self) -> None:
        payload = _registry(
            _source(
                "alpha",
                str(self.vault_a),
                policy={"include_paths": ["Note.md"], "max_file_bytes": 100},
            ),
            _source("beta", str(self.vault_b)),
        )
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertEqual(registry.sources[0].policy.include_paths, ["note.md"])
        self.assertEqual(registry.sources[0].policy.max_file_bytes, 100)
        self.assertEqual(registry.sources[1].policy.max_file_bytes, 2_000_000)

    def test_unknown_registry_key_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.vault_a)))
        payload["bogus"] = 1
        with self.assertRaisesRegex(ValueError, "Unknown source registry key"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_unknown_source_key_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.vault_a), bogus=1))
        with self.assertRaisesRegex(ValueError, "Unknown source key"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_bad_name_rejected(self) -> None:
        payload = _registry(_source("bad name!", str(self.vault_a)))
        with self.assertRaisesRegex(ValueError, "name may only contain"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_overlong_name_rejected(self) -> None:
        payload = _registry(_source("a" * 200, str(self.vault_a)))
        with self.assertRaisesRegex(ValueError, "too long"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_colon_in_name_rejected_for_windows_portability(self) -> None:
        # Colons are legal on POSIX but reserved in Windows filenames; a source
        # name is embedded verbatim into state artifact filenames, so it must be
        # rejected up front rather than failing only when Steward writes state.
        payload = _registry(_source("vault:one", str(self.vault_a)))
        with self.assertRaisesRegex(ValueError, "name may only contain"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_duplicate_names_rejected(self) -> None:
        payload = _registry(
            _source("alpha", str(self.vault_a)),
            _source("alpha", str(self.vault_b)),
        )
        with self.assertRaisesRegex(ValueError, "Duplicate source name"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_bad_type_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.vault_a), type_="weird"))
        with self.assertRaisesRegex(ValueError, "Unknown source type"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_bad_mode_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.vault_a), mode="weird"))
        with self.assertRaisesRegex(ValueError, "Unknown source mode"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_remote_scheme_in_root_rejected(self) -> None:
        payload = _registry(_source("alpha", "https://example.com/vault"))
        with self.assertRaisesRegex(ValueError, "Remotes are not registrable"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_remote_scheme_nested_policy_value_rejected(self) -> None:
        payload = _registry(
            _source(
                "alpha",
                str(self.vault_a),
                policy={"deny_path_terms": ["http://example.com/x"]},
            )
        )
        with self.assertRaisesRegex(ValueError, "Remotes are not registrable"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_missing_root_rejected(self) -> None:
        payload = _registry(
            {"name": "alpha", "type": "folder", "mode": "read_only"}
        )
        with self.assertRaisesRegex(ValueError, "requires a 'root'"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_symlinked_root_rejected(self) -> None:
        target = self.root / "real"
        target.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable: {error}")
        payload = _registry(_source("alpha", str(link)))
        with self.assertRaisesRegex(ValueError, "may not be a symlink"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_missing_root_directory_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.root / "nope")))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_appliable_without_include_paths_rejected(self) -> None:
        payload = _registry(
            _source("alpha", str(self.vault_a), mode="appliable")
        )
        with self.assertRaisesRegex(ValueError, "include_paths"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_appliable_with_include_paths_accepted(self) -> None:
        payload = _registry(
            _source(
                "alpha",
                str(self.vault_a),
                mode="appliable",
                policy={"include_paths": ["Note.md"]},
            )
        )
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        self.assertEqual(registry.sources[0].mode, "appliable")

    def test_overlapping_roots_contained_rejected(self) -> None:
        nested = self.vault_a / "sub"
        nested.mkdir()
        payload = _registry(
            _source("outer", str(self.vault_a)),
            _source("inner", str(nested)),
        )
        with self.assertRaisesRegex(ValueError, "roots overlap"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_overlapping_roots_containing_rejected(self) -> None:
        nested = self.vault_a / "sub"
        nested.mkdir()
        payload = _registry(
            _source("inner", str(nested)),
            _source("outer", str(self.vault_a)),
        )
        with self.assertRaisesRegex(ValueError, "roots overlap"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_identical_roots_rejected(self) -> None:
        payload = _registry(
            _source("alpha", str(self.vault_a)),
            _source("beta", str(self.vault_a)),
        )
        with self.assertRaisesRegex(ValueError, "roots overlap"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_non_object_payload_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            SourceRegistry.from_payload(["not", "an", "object"], base_dir=self.root)

    def test_wrong_spec_version_rejected(self) -> None:
        payload = _registry(_source("alpha", str(self.vault_a)))
        payload["spec_version"] = "wrong.version"
        with self.assertRaisesRegex(ValueError, "spec_version"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_empty_sources_rejected(self) -> None:
        payload = {"spec_version": SOURCES_SPEC_VERSION, "sources": []}
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_sources_not_a_list_rejected(self) -> None:
        payload = {
            "spec_version": SOURCES_SPEC_VERSION,
            "sources": {"alpha": 1},
        }
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_source_not_an_object_rejected(self) -> None:
        payload = _registry("not-an-object")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            SourceRegistry.from_payload(payload, base_dir=self.root)

    def test_from_file_and_load_registry(self) -> None:
        path = self.root / "sources.json"
        path.write_text(
            json.dumps(
                _registry(_source("alpha", str(self.vault_a))), sort_keys=True
            ),
            encoding="utf-8",
        )
        registry = SourceRegistry.from_file(path)
        loaded = load_registry(path)
        self.assertEqual(registry.sources[0].name, "alpha")
        self.assertEqual(loaded.registry_sha256, registry.registry_sha256)
        self.assertEqual(loaded.sources[0].root, self.vault_a.resolve())

    def test_bom_tolerated(self) -> None:
        data = b"\xef\xbb\xbf" + json.dumps(
            _registry(_source("alpha", str(self.vault_a))), sort_keys=True
        ).encode("utf-8")
        registry = SourceRegistry.from_bytes(data, base_dir=self.root)
        self.assertEqual(registry.sources[0].name, "alpha")

    def test_steward_source_dataclass_fields(self) -> None:
        payload = _registry(
            _source("alpha", str(self.vault_a), mode="proposable")
        )
        registry = SourceRegistry.from_payload(payload, base_dir=self.root)
        source = registry.sources[0]
        self.assertIsInstance(source, StewardSource)
        self.assertEqual(
            (source.name, source.type, source.mode),
            ("alpha", "folder", "proposable"),
        )
        self.assertIsInstance(source.root, Path)
        self.assertIsInstance(source.policy, IndexPolicy)


if __name__ == "__main__":
    unittest.main()
