from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import IndexPolicy
from .safe_write import is_link_like

SOURCES_SPEC_VERSION = "recallweave.steward.sources.v1"
SOURCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
ALLOWED_REGISTRY_KEYS = {"spec_version", "sources"}
ALLOWED_SOURCE_KEYS = {"name", "type", "root", "mode", "policy"}
SOURCE_TYPES = {"folder", "file", "git-worktree"}
SOURCE_MODES = {"read_only", "proposable", "appliable"}


def _reject_remotes(value: Any) -> None:
    if isinstance(value, str):
        if "://" in value:
            raise ValueError(f"Remotes are not registrable: {value!r}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_remotes(key)
            _reject_remotes(item)
    elif isinstance(value, list):
        for item in value:
            _reject_remotes(item)


@dataclass(slots=True)
class StewardSource:
    name: str
    type: str
    root: Path
    mode: str
    policy: IndexPolicy


@dataclass(slots=True)
class SourceRegistry:
    sources: list[StewardSource]
    registry_sha256: str | None

    @classmethod
    def from_payload(
        cls, payload: Any, *, base_dir: Path | None = None
    ) -> "SourceRegistry":
        if not isinstance(payload, dict):
            raise ValueError("Source registry must be a JSON object.")
        unknown = sorted(set(payload) - ALLOWED_REGISTRY_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown source registry key(s): {', '.join(unknown)}"
            )
        if payload.get("spec_version") != SOURCES_SPEC_VERSION:
            raise ValueError(
                f"Unsupported spec_version {payload.get('spec_version')!r}; "
                f"expected {SOURCES_SPEC_VERSION!r}."
            )
        sources_payload = payload.get("sources")
        if not isinstance(sources_payload, list) or not sources_payload:
            raise ValueError("sources must be a non-empty list.")
        _reject_remotes(payload)

        seen: set[str] = set()
        parsed: list[StewardSource] = []
        for source in sources_payload:
            parsed.append(cls._parse_source(source, base_dir, seen))

        resolved = [(item.name, item.root) for item in parsed]
        for i in range(len(resolved)):
            for j in range(i + 1, len(resolved)):
                name_a, root_a = resolved[i]
                name_b, root_b = resolved[j]
                if root_a.is_relative_to(root_b) or root_b.is_relative_to(root_a):
                    raise ValueError(
                        f"Source roots overlap: {name_a} ({root_a}) and "
                        f"{name_b} ({root_b})."
                    )
                try:
                    same = os.path.samefile(root_a, root_b)
                except OSError:
                    same = False
                if same:
                    raise ValueError(
                        f"Source roots overlap: {name_a} ({root_a}) and "
                        f"{name_b} ({root_b})."
                    )
        return cls(sources=parsed, registry_sha256=None)

    @classmethod
    def _parse_source(
        cls,
        source: Any,
        base_dir: Path | None,
        seen: set[str],
    ) -> StewardSource:
        if not isinstance(source, dict):
            raise ValueError("Each source must be a JSON object.")
        unknown = sorted(set(source) - ALLOWED_SOURCE_KEYS)
        if unknown:
            raise ValueError(f"Unknown source key(s): {', '.join(unknown)}")
        for key in ("name", "type", "root", "mode"):
            if key not in source:
                raise ValueError(f"Each source requires a '{key}'.")

        name = source["name"]
        if not isinstance(name, str) or not SOURCE_NAME_PATTERN.fullmatch(name):
            raise ValueError("source name may only contain [A-Za-z0-9._:-].")
        if name in seen:
            raise ValueError(f"Duplicate source name: {name!r}")
        seen.add(name)

        type_ = source["type"]
        if type_ not in SOURCE_TYPES:
            raise ValueError(
                f"Unknown source type {type_!r}; "
                f"expected one of {sorted(SOURCE_TYPES)}."
            )
        mode = source["mode"]
        if mode not in SOURCE_MODES:
            raise ValueError(
                f"Unknown source mode {mode!r}; "
                f"expected one of {sorted(SOURCE_MODES)}."
            )

        root_raw = source["root"]
        if not isinstance(root_raw, str):
            raise ValueError("source root must be a string.")
        raw = Path(root_raw).expanduser()
        if not raw.is_absolute():
            if base_dir is None:
                raise ValueError(
                    f"Relative source root {root_raw!r} requires a base_dir."
                )
            raw = base_dir / raw
        if is_link_like(raw):
            raise ValueError(f"Source root may not be a symlink: {raw}")
        try:
            resolved = raw.resolve(strict=True)
        except FileNotFoundError:
            raise ValueError(f"Source root does not exist: {raw}")
        if type_ == "file":
            if not resolved.is_file():
                raise ValueError(f"Source root must be a file: {resolved}")
        elif not resolved.is_dir():
            raise ValueError(f"Source root must be a directory: {resolved}")

        if "policy" in source:
            policy = IndexPolicy.from_payload(source["policy"])
        else:
            policy = IndexPolicy()
        if mode == "appliable" and not policy.include_paths:
            raise ValueError(
                "An appliable source must declare a non-empty include_paths "
                "allowlist."
            )
        return StewardSource(
            name=name, type=type_, root=resolved, mode=mode, policy=policy
        )

    @classmethod
    def from_bytes(
        cls, data: bytes, *, base_dir: Path | None = None
    ) -> "SourceRegistry":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Source registry must be UTF-8 JSON.") from error
        payload = json.loads(text)
        registry = cls.from_payload(payload, base_dir=base_dir)
        registry.registry_sha256 = hashlib.sha256(data).hexdigest()
        return registry

    @classmethod
    def from_file(cls, path: Path) -> "SourceRegistry":
        return cls.from_bytes(path.read_bytes(), base_dir=path.parent)


def load_registry(path: Path) -> SourceRegistry:
    return SourceRegistry.from_file(path)
