from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Section:
    heading: str
    line_start: int
    line_end: int
    text: str


@dataclass(slots=True)
class LinkEvidence:
    kind: str
    target: str
    line: int
    text: str


@dataclass(slots=True)
class Note:
    path: Path
    relative_path: str
    title: str
    aliases: list[str]
    tags: list[str]
    status: str | None
    domain: str | None
    created_at: str | None
    updated_at: str | None
    modified_at: str
    content_hash: str
    sections: list[Section] = field(default_factory=list)
    links: list[LinkEvidence] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    frontmatter_valid: bool = True
    frontmatter_error: str | None = None
