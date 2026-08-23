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
class HeadingRef:
    """A heading line as it physically appears, independent of whether it has a
    body beneath it.

    Sections are body-driven: a heading with nothing under it produces no
    Section at all. Links, however, are extracted from EVERY heading line, so a
    bodyless heading can still produce an authored edge. Recording headings
    separately is what lets such an edge have its coordinate bound; hanging the
    coordinate off Section rejected those genuine edges instead
    (recallweave-kob)."""

    line: int
    level: int
    text: str
    # The heading line exactly as it appears, stripped -- the same value the
    # parser puts in LinkEvidence.text for a link on this line. Stored rather
    # than reconstructed from `level` and `text`, because HEADING_RE accepts any
    # run of whitespace after the markers: `##  Related` and `##\tRelated` are
    # genuine headings that no canonical reconstruction reproduces, and
    # rebuilding the line rejected those genuine edges (recallweave-kob).
    source_text: str


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
    headings: list[HeadingRef] = field(default_factory=list)
    links: list[LinkEvidence] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    frontmatter_valid: bool = True
    frontmatter_error: str | None = None
