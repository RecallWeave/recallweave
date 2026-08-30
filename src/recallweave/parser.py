from __future__ import annotations

import csv
import hashlib
import os
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from .model import HeadingRef, LinkEvidence, Note, Section

WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
INLINE_CODE_RE = re.compile(r"(`+)(.*?)\1")
TAG_RE = re.compile(r"(?<![\w/#])#([A-Za-z][A-Za-z0-9_/-]*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'’-]{1,}")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

MAX_FRONTMATTER_LINES = 500
MAX_FRONTMATTER_CHARACTERS = 64_000
MAX_FRONTMATTER_KEYS = 500
MAX_FRONTMATTER_VALUE_DEPTH = 32
MAX_SUPPORTED_COLLECTION_DEPTH = 1

STOPWORDS = {
    "about", "after", "again", "against", "also", "among", "and", "are", "because",
    "been", "before", "being", "between", "both", "but", "can", "could", "did",
    "does", "doing", "down", "each", "for", "from", "further", "had", "has", "have",
    "her", "here", "hers", "him", "his", "how", "into", "its", "itself", "just",
    "more", "most", "not", "now", "off", "once", "only", "other", "our", "ours",
    "out", "over", "own", "same", "she", "should", "some", "such", "than", "that",
    "the", "their", "theirs", "them", "then", "there", "these", "they", "this",
    "those", "through", "too", "under", "until", "very", "was", "were", "what",
    "when", "where", "which", "while", "who", "why", "will", "with", "would", "you",
    "your", "yours",
    "anything", "application", "below", "change", "check", "copy", "current",
    "development", "document", "file", "high", "index", "infra", "language",
    "launch", "manifest_only", "notes", "page", "practical", "source", "text",
    "tier", "txt", "version", "working",
}


def normalize_name(value: str) -> str:
    value = value.replace("\\", "/").strip()
    if value.lower().endswith(".md"):
        value = value[:-3]
    value = value.rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def tokenize(text: str) -> list[str]:
    result = []
    for raw in TOKEN_RE.findall(text):
        token = raw.casefold().replace("’", "'")
        if token in STOPWORDS or len(token) <= 2 or token.isdigit():
            continue
        compact = token.replace("-", "").replace("_", "")
        digit_count = sum(character.isdigit() for character in compact)
        if digit_count >= 4:
            continue
        if len(compact) >= 8 and all(character in "0123456789abcdef" for character in compact):
            continue
        result.append(token)
    return result


def _split_csv(value: str) -> list[str]:
    try:
        return next(csv.reader(StringIO(value), skipinitialspace=True))
    except (csv.Error, StopIteration):
        return [value]


def _strip_yaml_comment(value: str) -> str:
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(value):
        if double_quoted and character == "\\" and not escaped:
            escaped = True
            continue
        if character == "'" and not double_quoted and not escaped:
            single_quoted = not single_quoted
        elif character == '"' and not single_quoted and not escaped:
            double_quoted = not double_quoted
        elif (
            character == "#"
            and not single_quoted
            and not double_quoted
            and (index == 0 or value[index - 1].isspace())
        ):
            return value[:index].rstrip()
        escaped = False
    return value.rstrip()


def _unsupported_scalar(value: str, depth: int = 0) -> bool:
    if depth > MAX_FRONTMATTER_VALUE_DEPTH:
        return True
    value = _strip_yaml_comment(value).strip()
    if not value:
        return False
    if value[0] == "'":
        return len(value) < 2 or value[-1] != "'"
    if value[0] == '"':
        return len(value) < 2 or value[-1] != '"' or "\\" in value[1:-1]
    if value.startswith(("!", "&", "*", ">", "|", "{")):
        return True
    if value == "-" or value.startswith("- "):
        return True
    if value.startswith("["):
        if not value.endswith("]"):
            return True
        if depth >= MAX_SUPPORTED_COLLECTION_DEPTH:
            return True
        return any(
            _unsupported_scalar(item, depth + 1)
            for item in _split_flow_items(value[1:-1])
            if item.strip()
        )
    return False


def _contains_mapping_indicator(value: str) -> bool:
    value = _strip_yaml_comment(value)
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(value):
        if double_quoted and character == "\\" and not escaped:
            escaped = True
            continue
        if character == "'" and not double_quoted and not escaped:
            single_quoted = not single_quoted
        elif character == '"' and not single_quoted and not escaped:
            double_quoted = not double_quoted
        elif (
            character == ":"
            and not single_quoted
            and not double_quoted
            and (index + 1 == len(value) or value[index + 1].isspace())
        ):
            return True
        escaped = False
    return False


def _split_flow_items(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    single_quoted = False
    double_quoted = False
    escaped = False
    depth = 0
    for index, character in enumerate(value):
        if double_quoted and character == "\\" and not escaped:
            escaped = True
            continue
        if character == "'" and not double_quoted and not escaped:
            single_quoted = not single_quoted
        elif character == '"' and not single_quoted and not escaped:
            double_quoted = not double_quoted
        elif not single_quoted and not double_quoted:
            if character in "[{":
                depth += 1
            elif character in "]}":
                depth = max(0, depth - 1)
            elif character == "," and depth == 0:
                items.append(value[start:index])
                start = index + 1
        escaped = False
    items.append(value[start:])
    return items


def _scalar(value: str, depth: int = 0) -> Any:
    if depth > MAX_FRONTMATTER_VALUE_DEPTH:
        return _strip_yaml_comment(value).strip()
    value = _strip_yaml_comment(value).strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        return [
            _scalar(item, depth + 1)
            for item in _split_flow_items(value[1:-1])
            if item.strip()
        ]
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    if value.casefold() in {"null", "none", "~"}:
        return None
    return value


def _merge_frontmatter_value(existing: Any, incoming: Any) -> list[Any]:
    left = existing if isinstance(existing, list) else [existing]
    right = incoming if isinstance(incoming, list) else [incoming]
    return [*left, *right]


def parse_frontmatter(
    lines: list[str],
) -> tuple[dict[str, Any], int, bool, str | None]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, True, None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, 0, False, "unterminated_frontmatter"
    if end > MAX_FRONTMATTER_LINES:
        return {}, end + 1, False, "frontmatter_too_many_lines"
    if sum(len(line) for line in lines[1:end]) > MAX_FRONTMATTER_CHARACTERS:
        return {}, end + 1, False, "frontmatter_too_large"

    data: dict[str, Any] = {}
    current_list: str | None = None
    valid = True
    error: str | None = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indented = len(raw) != len(raw.lstrip())
        if indented and stripped.startswith("- ") and current_list:
            existing = data.setdefault(current_list, [])
            if isinstance(existing, list):
                item = stripped[2:]
                unsupported = _unsupported_scalar(
                    item,
                    depth=MAX_SUPPORTED_COLLECTION_DEPTH,
                ) or _contains_mapping_indicator(item)
                if unsupported:
                    valid = False
                    error = error or "unsupported_frontmatter_value"
                existing.append(
                    _strip_yaml_comment(item).strip()
                    if unsupported
                    else _scalar(item, depth=MAX_SUPPORTED_COLLECTION_DEPTH)
                )
            continue
        if indented:
            # Nested mappings remain nested by being ignored by the supported
            # top-level scalar/list subset. An indented non-list continuation
            # after an empty value is valid YAML that this parser cannot
            # evaluate, so mark the frontmatter invalid rather than guessing.
            valid = False
            error = error or "unsupported_frontmatter_value"
            current_list = None
            continue
        if ":" not in raw:
            current_list = None
            valid = False
            error = error or "unsupported_frontmatter_syntax"
            continue
        key, value = raw.split(":", 1)
        key = key.strip().casefold()
        if not key or not re.fullmatch(r"[a-z0-9_.-]+", key):
            valid = False
            error = error or "invalid_frontmatter_key"
            current_list = None
            continue
        unsupported = _unsupported_scalar(value)
        if unsupported:
            valid = False
            error = error or "unsupported_frontmatter_value"
        parsed = (
            _strip_yaml_comment(value).strip()
            if unsupported
            else _scalar(value)
        )
        parsed = [] if parsed == "" else parsed
        if key in data:
            data[key] = _merge_frontmatter_value(data[key], parsed)
        else:
            data[key] = parsed
        if len(data) > MAX_FRONTMATTER_KEYS:
            return data, end + 1, False, "frontmatter_too_many_keys"
        current_list = key if parsed == [] else None
    return data, end + 1, valid, error


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_as_list(item))
        return result
    return [item.strip() for item in _split_csv(str(value)) if item.strip()]


def _heading_positions(lines: list[str], body_start: int) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    for index in range(body_start, len(lines)):
        fence_match = FENCE_RE.match(lines[index])
        if fence_match:
            marker_character = fence_match.group(1)[0]
            if fence is None:
                fence = marker_character
            elif fence == marker_character:
                fence = None
            continue
        if fence is not None:
            continue
        match = HEADING_RE.match(lines[index])
        if match:
            headings.append((index, match.group(2).strip()))
    return headings


def _heading_refs(lines: list[str], body_start: int) -> list[HeadingRef]:
    """Every heading line in the body, with its physical line and `#` level.

    Independent of _sections(), which drops a heading with no body beneath it.
    Links are extracted from every heading line, so a bodyless heading can still
    produce an authored edge, and that edge needs its coordinate on record.
    Fenced headings are excluded here for the same reason they are excluded from
    sections: _heading_positions() skips anything inside a code fence."""
    refs: list[HeadingRef] = []
    for index, text in _heading_positions(lines, body_start):
        match = HEADING_RE.match(lines[index])
        if match:
            refs.append(
                HeadingRef(
                    line=index + 1,
                    level=len(match.group(1)),
                    text=text,
                    source_text=lines[index].strip(),
                )
            )
    return refs


def _sections(lines: list[str], body_start: int) -> list[Section]:
    headings = _heading_positions(lines, body_start)
    starts: list[tuple[int, str]] = []
    if body_start < len(lines) and (not headings or headings[0][0] > body_start):
        starts.append((body_start, "Overview"))
    starts.extend(headings)
    if not starts:
        return []

    result: list[Section] = []
    for position, (start, heading) in enumerate(starts):
        end = starts[position + 1][0] - 1 if position + 1 < len(starts) else len(lines) - 1
        text_start = start + 1 if HEADING_RE.match(lines[start]) else start
        while text_start <= end and not lines[text_start].strip():
            text_start += 1
        while end >= text_start and not lines[end].strip():
            end -= 1
        text = "\n".join(lines[text_start : end + 1])
        if text:
            result.append(
                Section(
                    heading=heading,
                    line_start=text_start + 1,
                    line_end=end + 1,
                    text=text,
                )
            )
    return result


def _without_inline_code(line: str) -> str:
    return INLINE_CODE_RE.sub(lambda match: " " * len(match.group(0)), line)


def _markdown_target(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = re.sub(r"""\s+["'][^"']*["']\s*$""", "", value)
    return unquote(value)


def _valid_tag(value: str) -> bool:
    folded = value.casefold()
    leaf = folded.rsplit("/", 1)[-1]
    return folded != "include" and re.fullmatch(r"[0-9a-f]{3,8}", leaf) is None


def _links(lines: Iterable[str], body_start: int) -> list[LinkEvidence]:
    result: list[LinkEvidence] = []
    fence: str | None = None
    for zero_index, line in enumerate(lines):
        if zero_index < body_start:
            continue
        fence_match = FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        line_number = zero_index + 1
        visible = _without_inline_code(line)
        for match in WIKILINK_RE.finditer(visible):
            result.append(
                LinkEvidence("wikilink", match.group(1).strip(), line_number, line.strip())
            )
        markdown_matches = list(MARKDOWN_LINK_RE.finditer(visible))
        for match in markdown_matches:
            target = _markdown_target(match.group(1))
            target_path = target.split("#", 1)[0]
            suffix = Path(target_path).suffix.casefold()
            if target_path and "://" not in target and (not suffix or suffix == ".md"):
                result.append(LinkEvidence("markdown_link", target, line_number, line.strip()))
        tag_text = visible
        for match in reversed(markdown_matches):
            tag_text = (
                tag_text[: match.start()]
                + (" " * len(match.group(0)))
                + tag_text[match.end() :]
            )
        for match in TAG_RE.finditer(tag_text):
            if _valid_tag(match.group(1)):
                result.append(LinkEvidence("tag", match.group(1), line_number, line.strip()))
    return result


def _frontmatter_tag_links(
    lines: list[str], body_start: int, tags: list[str]
) -> list[LinkEvidence]:
    if not tags or body_start == 0:
        return []
    tag_line = next(
        (
            index
            for index in range(1, body_start)
            if lines[index].strip().casefold().startswith(("tags:", "tag:"))
        ),
        None,
    )
    if tag_line is None:
        return []
    return [
        LinkEvidence("tag", tag, tag_line + 1, lines[tag_line].strip())
        for tag in tags
    ]


def _filesystem_birth_timestamp(path: Path) -> str | None:
    """Return a UTC ISO-8601 birth time when the platform exposes one.

    Prefer ``st_birthtime`` (macOS/BSD; some Linux filesystems). On Windows,
    ``st_ctime`` is creation time. Never substitute ``st_mtime`` — that is
    modification, not birth — and never invent a clock when birth is unknown.
    """

    try:
        info = path.stat()
    except OSError:
        return None
    birth: float | None = getattr(info, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = float(info.st_ctime)
    if birth is None or birth <= 0:
        return None
    return datetime.fromtimestamp(birth, timezone.utc).isoformat()


def parse_note(path: Path, vault: Path) -> Note:
    content = path.read_bytes()
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnicodeError("UTF-16 Markdown is not supported; convert the note to UTF-8.")
    raw = content.decode("utf-8-sig", errors="strict")
    lines = re.split(r"\r\n|\r|\n", raw)
    frontmatter, body_start, frontmatter_valid, frontmatter_error = parse_frontmatter(lines)

    headings = _heading_positions(lines, body_start)
    first_heading = headings[0][1] if headings else None
    title = str(frontmatter.get("title") or first_heading or path.stem)
    aliases = _as_list(frontmatter.get("aliases") or frontmatter.get("alias"))
    tags = _as_list(frontmatter.get("tags") or frontmatter.get("tag"))
    stat = path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    created_at = (
        str(frontmatter["created"])
        if frontmatter.get("created")
        else _filesystem_birth_timestamp(path)
    )

    return Note(
        path=path,
        relative_path=path.relative_to(vault).as_posix(),
        title=title,
        aliases=aliases,
        tags=tags,
        status=str(frontmatter["status"]) if frontmatter.get("status") else None,
        domain=str(frontmatter["domain"]) if frontmatter.get("domain") else None,
        created_at=created_at,
        updated_at=str(frontmatter["updated"]) if frontmatter.get("updated") else None,
        modified_at=modified,
        content_hash=hashlib.sha256(content).hexdigest(),
        sections=_sections(lines, body_start),
        headings=_heading_refs(lines, body_start),
        links=[*_links(lines, body_start), *_frontmatter_tag_links(lines, body_start, tags)],
        frontmatter=frontmatter,
        frontmatter_valid=frontmatter_valid,
        frontmatter_error=frontmatter_error,
    )
