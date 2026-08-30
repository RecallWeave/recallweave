from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .steward_state import STEWARD_SCHEMA_VERSION, atomic_write_json

CHECKPOINT_KIND = "checkpoint_manifest"

_REQUIRED_MANIFEST_KEYS = (
    "schema_version",
    "kind",
    "source_id",
    "generated_at",
    "digest",
    "entries",
)
_ENTRY_FIELDS = (
    ("relative_path", str),
    ("content_hash", str),
    ("size", int),
    ("mtime_ns", int),
    ("file_dev", int),
    ("file_ino", int),
)


class CheckpointError(ValueError):
    pass


@dataclass(slots=True)
class CheckpointEntry:
    relative_path: str
    content_hash: str
    size: int
    mtime_ns: int
    file_dev: int
    file_ino: int


def _sort_key(entry: CheckpointEntry) -> str:
    return entry.relative_path


def manifest_digest(source_id: str, entries: list[CheckpointEntry]) -> str:
    lines = [source_id]
    for entry in sorted(entries, key=_sort_key):
        lines.append(
            f"{entry.relative_path}\t{entry.content_hash}\t{entry.size}\t{entry.mtime_ns}"
        )
    canonical = "\n".join(lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_checkpoint(
    state_dirs,
    source_id: str,
    entries: list[CheckpointEntry],
    *,
    generated_at: str,
    registry_sha256: str | None,
) -> Path:
    ordered = sorted(entries, key=_sort_key)
    payload: dict[str, Any] = {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": CHECKPOINT_KIND,
        "source_id": source_id,
        "generated_at": generated_at,
        "registry_sha256": registry_sha256,
        "digest": manifest_digest(source_id, ordered),
        "entries": [asdict(entry) for entry in ordered],
    }
    path = state_dirs["checkpoints"] / f"{source_id}.json"
    atomic_write_json(path, payload, within=state_dirs["checkpoints"])
    return path


def _entry_error(entry: Any, reason: str) -> CheckpointError:
    return CheckpointError(f"Invalid checkpoint entry {entry!r}: {reason}")


def load_checkpoint(state_dirs, source_id) -> dict | None:
    path = state_dirs["checkpoints"] / f"{source_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CheckpointError(
            f"Checkpoint {path} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise CheckpointError(f"Checkpoint {path} is not a JSON object.")
    for key in _REQUIRED_MANIFEST_KEYS:
        if key not in payload:
            raise CheckpointError(f"Checkpoint {path} is missing required key: {key}")
    if payload.get("schema_version") != STEWARD_SCHEMA_VERSION:
        raise CheckpointError(
            f"Checkpoint {path} has unexpected schema_version: {payload.get('schema_version')!r}"
        )
    if payload.get("kind") != CHECKPOINT_KIND:
        raise CheckpointError(
            f"Checkpoint {path} has unexpected kind: {payload.get('kind')!r}"
        )
    if payload.get("source_id") != source_id:
        raise CheckpointError(
            f"Checkpoint {path} is for source_id {payload.get('source_id')!r}, "
            f"expected {source_id!r}"
        )

    entries = payload["entries"]
    if not isinstance(entries, list):
        raise CheckpointError(f"Checkpoint {path} entries must be a list.")
    parsed: list[CheckpointEntry] = []
    for index, entry in enumerate(entries):
        parsed.append(_parse_entry(entry, path, index))

    previous: str | None = None
    for entry in parsed:
        if previous is not None and entry.relative_path < previous:
            raise CheckpointError(f"Checkpoint {path} entries are not sorted.")
        previous = entry.relative_path

    expected = manifest_digest(source_id, parsed)
    if payload.get("digest") != expected:
        raise CheckpointError(
            f"Checkpoint {path} digest mismatch: expected {expected}, "
            f"found {payload.get('digest')!r}"
        )
    return payload


def _parse_entry(entry: Any, path: Path, index: int) -> CheckpointEntry:
    if not isinstance(entry, dict):
        raise CheckpointError(
            f"Checkpoint {path} entry {index} must be an object."
        )
    values: dict[str, Any] = {}
    for field_name, field_type in _ENTRY_FIELDS:
        if field_name not in entry:
            raise CheckpointError(
                f"Checkpoint {path} entry {index} is missing field: {field_name}"
            )
        value = entry[field_name]
        if field_type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise CheckpointError(
                    f"Checkpoint {path} entry {index} field {field_name} must be an integer."
                )
        elif not isinstance(value, field_type):
            raise CheckpointError(
                f"Checkpoint {path} entry {index} field {field_name} must be a {field_type.__name__}."
            )
        values[field_name] = value
    return CheckpointEntry(
        relative_path=values["relative_path"],
        content_hash=values["content_hash"],
        size=values["size"],
        mtime_ns=values["mtime_ns"],
        file_dev=values["file_dev"],
        file_ino=values["file_ino"],
    )
