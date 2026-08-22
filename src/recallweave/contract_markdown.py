from __future__ import annotations

from typing import Any

MIN_FENCE = 3
FENCE_LANGUAGE = "text"
NONE_RECORDED = "None recorded."


class _Escaped:
    """A string already escaped for a specific output position.

    Every value that reaches the output buffer is wrapped in _Escaped by an
    explicit position helper (inline / cell / citation / fenced / quoted).
    _join() refuses to accept a bare unconverted str, so a future branch must
    make an explicit escaping choice for any new field before it can compile.
    """

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def _join(*pieces: _Escaped) -> _Escaped:
    """Concatenate already-escaped pieces into one escaped line.

    Only _Escaped values are accepted; a bare str is a hard error rather than
    silently reaching the buffer unescaped.
    """
    for piece in pieces:
        if not isinstance(piece, _Escaped):
            raise TypeError(
                f"unconverted string reached the output buffer: {piece!r}"
            )
    return _Escaped("".join(piece.text for piece in pieces))


def _inline(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _collapse_newlines(value: str) -> str:
    return " ".join(value.split())


def _escape_metachars(value: str) -> str:
    """Backslash-escape the inline Markdown metacharacters that can open a live
    inline construct: backslash, backtick, brackets, parentheses, exclamation
    (image), emphasis markers, and tilde. Angle brackets are handled separately
    by _inline's HTML-escaping so autolinks stay inert."""
    value = value.replace("\\", "\\\\")
    for ch in "`[]()!*_~":
        value = value.replace(ch, "\\" + ch)
    return value


def _escape_inline(value: str) -> str:
    """Escape an untrusted single-line inline field for the position it holds.

    Newlines are collapsed so the value cannot open a new structural line, then
    leading Markdown block-structure characters are escaped so the single-line
    value cannot start a construct (heading, list, quote, fence, code span).
    Inline metacharacters are escaped so no live image, link, autolink, code
    span, or emphasis marker survives.
    """
    value = _collapse_newlines(value)
    value = _inline(value)
    value = _escape_metachars(value)
    if value and value[0] in "#>*+|-`":
        value = "\\" + value
    elif value and value[0].isdigit() and len(value) > 1 and value[1] in ".)":
        value = "\\" + value
    return value


def _citation_inline(value: str) -> str:
    """Collapse newlines and neutralize backticks for an inline code span."""
    return _collapse_newlines(value).replace("`", " ")


def _cell(value: Any) -> str:
    """Escape an untrusted connection table cell."""
    v = _collapse_newlines(_as_str(value))
    v = _inline(v)
    v = _escape_metachars(v)
    return v.replace("|", "\\|")


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _longest_backtick_run(value: str) -> int:
    longest = 0
    current = 0
    for ch in value:
        if ch == "`":
            current += 1
            if current > longest:
                longest = current
        else:
            current = 0
    return longest


def _fence_for(value: str) -> str:
    return "`" * max(_longest_backtick_run(value) + 1, MIN_FENCE)


def _fenced(value: str) -> str:
    fence = _fence_for(value)
    return f"{fence}{FENCE_LANGUAGE}\n{value}\n{fence}"


def _quote_line(text: str) -> str:
    return "\n".join(
        f"> {_escape_blockquote_line(line)}" if line else ">"
        for line in text.split("\n")
    )


def _escape_blockquote_line(line: str) -> str:
    """Escape a single line destined for a blockquote. Inline metacharacters
    (image, link, autolink, code span, emphasis) and leading block-structure
    markers are escaped so the line cannot open a live construct or node."""
    line = _inline(line)
    line = _escape_metachars(line)
    if line and line[0] in "#>*+|-`":
        line = "\\" + line
    elif line and line[0].isdigit() and len(line) > 1 and line[1] in ".)":
        line = "\\" + line
    return line


# --- Position helpers: every untrusted value reaching the output buffer must
# pass through exactly one of these, which both performs the escaping and
# returns an _Escaped wrapper so it cannot be interpolated as a bare string.


def _inline_esc(value: Any) -> _Escaped:
    return _Escaped(_escape_inline(_as_str(value)))


def _cell_esc(value: Any) -> _Escaped:
    return _Escaped(_cell(value))


def _citation_esc(value: Any) -> _Escaped:
    """A citation rendered inertly inside an inline code span."""
    return _Escaped(_citation_inline(_as_str(value)))


def _fenced_esc(value: Any) -> _Escaped:
    """A block rendered raw inside a fence (already inert)."""
    return _Escaped(_fenced(_as_str(value)))


def _quoted_esc(text: Any) -> _Escaped:
    """A multi-line string rendered as an escaped blockquote."""
    return _Escaped(_quote_line(_as_str(text)))


def _title(document: dict[str, Any]) -> str:
    task = document.get("task")
    if isinstance(task, dict):
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            return _escape_inline(task_id)
        objective = task.get("objective")
        if isinstance(objective, str) and objective:
            return _escape_inline(objective.split("\n", 1)[0])
    return ""


def _blockquote(document: dict[str, Any]) -> list[_Escaped]:
    lines: list[_Escaped] = []
    schema = document.get("schema_version")
    if isinstance(schema, str):
        lines.append(_join(_Escaped("> Schema: "), _inline_esc(schema)))
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        generated = provenance.get("generated_at")
        if isinstance(generated, str):
            lines.append(_join(_Escaped("> Generated at: "), _inline_esc(generated)))
    handling = document.get("handling")
    if isinstance(handling, dict):
        statement = handling.get("statement")
        if isinstance(statement, str) and statement:
            lines.append(_quoted_esc(statement))
    return lines or [_Escaped(f"> {NONE_RECORDED}")]


def _render_objective(document: dict[str, Any]) -> list[_Escaped] | None:
    task = document.get("task")
    if isinstance(task, dict):
        objective = task.get("objective")
        if isinstance(objective, str) and objective:
            if "\n" in objective:
                return [_fenced_esc(objective)]
            return [_inline_esc(objective)]
    return None


def _render_acceptance(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("acceptance_criteria")
    if not isinstance(items, list) or not items:
        return None
    lines: list[_Escaped] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.append(
            _join(
                _Escaped("- [ ] "),
                _inline_esc(item.get("id")),
                _Escaped(" "),
                _inline_esc(item.get("statement")),
            )
        )
    return lines or None


def _render_cited_items(
    document: dict[str, Any], key: str
) -> list[_Escaped] | None:
    items = document.get(key)
    if not isinstance(items, list) or not items:
        return None
    lines: list[_Escaped] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        statement = _as_str(item.get("statement"))
        citation = item.get("citation")
        if "\n" in statement:
            # Multiline statements are fenced; their citation leads on its own
            # bullet line and must be inline-escaped (blockquote/code-span
            # neutralization does not apply here) so it cannot open a live
            # construct. Same inertness policy as the single-line branch.
            if citation is not None:
                lines.append(_join(_Escaped("- "), _inline_esc(citation)))
            lines.append(_fenced_esc(statement))
        else:
            stmt = _inline_esc(statement)
            if citation is not None:
                lines.append(
                    _join(
                        _Escaped("- "),
                        stmt,
                        _Escaped("  (`"),
                        _citation_esc(citation),
                        _Escaped("`)"),
                    )
                )
            else:
                lines.append(_join(_Escaped("- "), stmt))
    return lines or None


def _render_retrieved(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("retrieved_context")
    if not isinstance(items, list) or not items:
        return None
    parts: list[_Escaped] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        parts.append(_join(_Escaped("### "), _inline_esc(item.get("citation"))))
        parts.append(_fenced_esc(item.get("passage")))
    return parts or None


def _render_connections(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("connections")
    if not isinstance(items, list) or not items:
        return None
    lines: list[_Escaped] = [
        _Escaped("| source | target | kind | verified |"),
        _Escaped("| --- | --- | --- | --- |"),
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        source = _cell_esc(item.get("source"))
        target = _cell_esc(item.get("target"))
        kind = _cell_esc(item.get("kind"))
        verified = _Escaped("true" if item.get("verified") else "false")
        lines.append(
            _join(
                _Escaped("| "),
                source,
                _Escaped(" | "),
                target,
                _Escaped(" | "),
                kind,
                _Escaped(" | "),
                verified,
                _Escaped(" |"),
            )
        )
    return lines or None


def _render_exclusions(document: dict[str, Any]) -> list[_Escaped] | None:
    exclusions = document.get("exclusions")
    if not isinstance(exclusions, dict):
        return None
    lines: list[_Escaped] = []
    for key, label in (
        ("paths", "path"),
        ("globs", "glob"),
        ("tags", "tag"),
        ("directives", "directive"),
    ):
        values = exclusions.get(key)
        if isinstance(values, list):
            for value in values:
                lines.append(
                    _join(_Escaped(f"- {label}: "), _inline_esc(value))
                )
    suppressed = exclusions.get("suppressed")
    if isinstance(suppressed, dict):
        for key in ("retrieved_context", "connections", "notes"):
            if key in suppressed:
                lines.append(
                    _join(
                        _Escaped(f"- suppressed.{key}: "),
                        _inline_esc(suppressed[key]),
                    )
                )
    enforced = exclusions.get("enforced")
    if isinstance(enforced, bool):
        lines.append(
            _join(_Escaped("- enforced: "), _Escaped(str(enforced).lower()))
        )
    return lines or None


def _render_provenance(document: dict[str, Any]) -> list[_Escaped] | None:
    lines: list[_Escaped] = []
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        index = provenance.get("index")
        if isinstance(index, dict):
            schema = _inline_esc(index.get("schema_version"))
            indexed_at = _inline_esc(index.get("indexed_at"))
            lines.append(
                _join(
                    _Escaped("- Index schema: "),
                    schema,
                    _Escaped(", indexed at: "),
                    indexed_at,
                )
            )
        citations = provenance.get("citations")
        if isinstance(citations, list) and citations:
            lines.append(_Escaped("- Citations:"))
            for citation in citations:
                lines.append(_join(_Escaped("  - "), _inline_esc(citation)))
    budget = document.get("budget")
    if isinstance(budget, dict):
        used = _inline_esc(budget.get("characters_used"))
        total = _inline_esc(budget.get("character_budget"))
        truncated = budget.get("truncated")
        trunc = str(truncated).lower() if isinstance(truncated, bool) else ""
        lines.append(
            _join(
                _Escaped("- Budget: "),
                used,
                _Escaped(" / "),
                total,
                _Escaped(" characters (truncated: "),
                _Escaped(trunc),
                _Escaped(")"),
            )
        )
    return lines or None


def _section(heading: str, body: list[_Escaped] | None) -> list[_Escaped]:
    lines: list[_Escaped] = [_Escaped(f"## {heading}"), _Escaped("")]
    if not body:
        lines.append(_Escaped(NONE_RECORDED))
    else:
        lines.extend(body)
    return lines


def render_contract_markdown(document: dict[str, Any]) -> str:
    lines: list[_Escaped] = [
        _Escaped(f"# Task contract — {_title(document)}"),
        _Escaped(""),
    ]
    lines.extend(_blockquote(document))
    lines.append(_Escaped(""))
    lines.extend(_section("1. Objective", _render_objective(document)))
    lines.append(_Escaped(""))
    lines.extend(_section("2. Acceptance criteria", _render_acceptance(document)))
    lines.append(_Escaped(""))
    lines.extend(
        _section("3. Constraints", _render_cited_items(document, "constraints"))
    )
    lines.append(_Escaped(""))
    lines.extend(
        _section("4. Prior decisions", _render_cited_items(document, "prior_decisions"))
    )
    lines.append(_Escaped(""))
    lines.extend(_section("5. Retrieved context", _render_retrieved(document)))
    lines.append(_Escaped(""))
    lines.extend(_section("6. Connections", _render_connections(document)))
    lines.append(_Escaped(""))
    lines.extend(_section("7. Exclusions and scope", _render_exclusions(document)))
    lines.append(_Escaped(""))
    lines.extend(_section("8. Provenance", _render_provenance(document)))
    return "\n".join(line.text for line in lines) + "\n"
