from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_POLICY_KEYS = {
    "include_paths",
    "exclude_globs",
    "deny_path_terms",
    "deny_frontmatter",
    "max_file_bytes",
}
RESERVED_DIRECTORY_NAMES = {".git", ".obsidian", ".trash", ".recallweave", "node_modules"}


@dataclass(slots=True)
class IndexPolicy:
    include_paths: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(
        default_factory=lambda: [
            ".git/**",
            ".obsidian/**",
            ".trash/**",
            ".recallweave/**",
            "node_modules/**",
        ]
    )
    deny_path_terms: list[str] = field(default_factory=list)
    deny_frontmatter: dict[str, list[str]] = field(default_factory=dict)
    max_file_bytes: int = 2_000_000
    _include_set: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.max_file_bytes, int) or isinstance(self.max_file_bytes, bool):
            raise ValueError("max_file_bytes must be an integer.")
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive.")
        self.include_paths = [
            str(item).replace("\\", "/").casefold() for item in self.include_paths
        ]
        self._include_set = set(self.include_paths)
        self.exclude_globs = [str(item) for item in self.exclude_globs]
        self.deny_path_terms = [str(item).casefold() for item in self.deny_path_terms]
        self.deny_frontmatter = {
            str(key).casefold(): [str(item).casefold() for item in values]
            for key, values in self.deny_frontmatter.items()
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "IndexPolicy":
        if not isinstance(payload, dict):
            raise ValueError("Policy config must be a JSON object.")
        unknown = sorted(set(payload) - ALLOWED_POLICY_KEYS)
        if unknown:
            raise ValueError(f"Unknown policy config key(s): {', '.join(unknown)}")
        for key in ("include_paths", "exclude_globs", "deny_path_terms"):
            if key in payload and (
                not isinstance(payload[key], list)
                or any(not isinstance(item, str) for item in payload[key])
            ):
                raise ValueError(f"{key} must be a list of strings.")
        if "deny_frontmatter" in payload:
            if not isinstance(payload["deny_frontmatter"], dict):
                raise ValueError("deny_frontmatter must be an object of string lists.")
            for key, values in payload["deny_frontmatter"].items():
                if not isinstance(key, str) or not isinstance(values, list) or any(
                    not isinstance(item, str) for item in values
                ):
                    raise ValueError("deny_frontmatter must be an object of string lists.")
        if "max_file_bytes" in payload and (
            not isinstance(payload["max_file_bytes"], int)
            or isinstance(payload["max_file_bytes"], bool)
            or payload["max_file_bytes"] <= 0
        ):
            raise ValueError("max_file_bytes must be a positive integer.")
        return cls(
            include_paths=payload.get("include_paths", []),
            exclude_globs=payload.get(
                "exclude_globs",
                [".git/**", ".obsidian/**", ".trash/**", ".recallweave/**", "node_modules/**"],
            ),
            deny_path_terms=payload.get("deny_path_terms", []),
            deny_frontmatter=payload.get("deny_frontmatter", {}),
            max_file_bytes=payload.get("max_file_bytes", 2_000_000),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "IndexPolicy":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Policy config must be UTF-8 JSON.") from error
        return cls.from_payload(json.loads(text))

    @classmethod
    def from_file(cls, path: Path | None) -> "IndexPolicy":
        if path is None:
            return cls()
        return cls.from_bytes(path.read_bytes())

    def path_allowed(self, relative_path: str, size: int) -> tuple[bool, str | None]:
        normalized = relative_path.replace("\\", "/")
        folded = normalized.casefold()
        if self._include_set and folded not in self._include_set:
            return False, "not_allowlisted"
        if size > self.max_file_bytes:
            return False, "file_too_large"
        directories = {part.casefold() for part in Path(normalized).parts[:-1]}
        if directories.intersection(RESERVED_DIRECTORY_NAMES):
            return False, "excluded_directory"
        if any(fnmatch.fnmatch(folded, pattern.casefold()) for pattern in self.exclude_globs):
            return False, "excluded_glob"
        if any(term in folded for term in self.deny_path_terms):
            return False, "denied_path_term"
        return True, None

    @staticmethod
    def _values(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, list):
            result: set[str] = set()
            for item in value:
                result.update(IndexPolicy._values(item))
            return result
        return {
            item.strip().strip("\"'").casefold()
            for item in str(value).split(",")
            if item.strip()
        }

    def frontmatter_allowed(
        self,
        frontmatter: dict[str, Any],
        valid: bool = True,
    ) -> tuple[bool, str | None]:
        if self.deny_frontmatter and not valid:
            return False, "unparseable_frontmatter"
        for key, denied_values in self.deny_frontmatter.items():
            if self._values(frontmatter.get(key)).intersection(denied_values):
                return False, f"denied_frontmatter:{key}"
        return True, None
