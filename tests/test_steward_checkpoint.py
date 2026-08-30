from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from recallweave.steward_checkpoint import (
    CHECKPOINT_KIND,
    CheckpointEntry,
    CheckpointError,
    load_checkpoint,
    manifest_digest,
    save_checkpoint,
)
from recallweave.steward_state import STEWARD_SCHEMA_VERSION, ensure_state_layout


def entry(relative_path: str, **overrides) -> CheckpointEntry:
    values = {
        "relative_path": relative_path,
        "content_hash": "abc123",
        "size": 10,
        "mtime_ns": 1000,
        "file_dev": 1,
        "file_ino": 2,
    }
    values.update(overrides)
    return CheckpointEntry(**values)


class CheckpointManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.dirs = ensure_state_layout(self.root)
        self.source_id = "local-vault"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load(self) -> dict | None:
        return load_checkpoint(self.dirs, self.source_id)

    def test_round_trip_preserves_entries(self) -> None:
        entries = [
            entry("b.md", content_hash="h2", size=20, mtime_ns=2000, file_ino=22),
            entry("a.md", content_hash="h1", size=10, mtime_ns=1000, file_ino=11),
        ]
        save_checkpoint(
            self.dirs,
            self.source_id,
            entries,
            generated_at="2026-01-01T00:00:00+00:00",
            registry_sha256="reg",
        )
        payload = self.load()
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["schema_version"], STEWARD_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], CHECKPOINT_KIND)
        self.assertEqual(payload["source_id"], self.source_id)
        self.assertEqual(payload["registry_sha256"], "reg")
        self.assertEqual(
            [e["relative_path"] for e in payload["entries"]], ["a.md", "b.md"]
        )

    def test_digest_is_order_independent(self) -> None:
        entries_a = [entry("a.md"), entry("b.md"), entry("c.md")]
        entries_b = [entry("c.md"), entry("a.md"), entry("b.md")]
        self.assertEqual(
            manifest_digest(self.source_id, entries_a),
            manifest_digest(self.source_id, entries_b),
        )

    def test_digest_differs_when_field_changes(self) -> None:
        base = manifest_digest(self.source_id, [entry("a.md", size=10)])
        changed = manifest_digest(self.source_id, [entry("a.md", size=11)])
        self.assertNotEqual(base, changed)

    def test_absent_returns_none(self) -> None:
        self.assertIsNone(self.load())

    def test_tamper_raises_checkpoint_error(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        raw = bytearray(path.read_bytes())
        raw[len(raw) // 2] ^= 0x01
        path.write_bytes(bytes(raw))
        with self.assertRaises(CheckpointError):
            self.load()

    def test_wrong_source_id_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source_id"] = "other-source"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_wrong_kind_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["kind"] = "wrong_kind"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_wrong_schema_version_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = "wrong"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_unsorted_entries_raise(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"] = [
            {"relative_path": "b.md", "content_hash": "h", "size": 1, "mtime_ns": 1, "file_dev": 1, "file_ino": 1},
            {"relative_path": "a.md", "content_hash": "h", "size": 1, "mtime_ns": 1, "file_dev": 1, "file_ino": 1},
        ]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_missing_entry_field_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["entries"][0]["content_hash"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_wrong_entry_type_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"][0]["size"] = "not-an-int"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_invalid_json_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()

    def test_missing_required_key_raises(self) -> None:
        save_checkpoint(
            self.dirs, self.source_id, [entry("a.md")], generated_at="g", registry_sha256=None
        )
        path = self.dirs["checkpoints"] / f"{self.source_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["generated_at"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CheckpointError):
            self.load()


if __name__ == "__main__":
    unittest.main()
