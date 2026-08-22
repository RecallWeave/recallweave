from __future__ import annotations

from typing import Any

MIN_FENCE = 3
FENCE_LANGUAGE = "text"
NONE_RECORDED = "None recorded."


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


def _blockquote(document: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    schema = document.get("schema_version")
    if isinstance(schema, str):
        lines.append(f"> Schema: {_escape_inline(schema)}")
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        generated = provenance.get("generated_at")
        if isinstance(generated, str):
            lines.append(f"> Generated at: {_escape_inline(generated)}")
    handling = document.get("handling")
    if isinstance(handling, dict):
        statement = handling.get("statement")
        if isinstance(statement, str) and statement:
            lines.append(_quote_line(statement))
    return lines or [f"> {NONE_RECORDED}"]


def _render_objective(document: dict[str, Any]) -> str | list[str]:
    task = document.get("task")
    if isinstance(task, dict):
        objective = task.get("objective")
        if isinstance(objective, str) and objective:
            if "\n" in objective:
                return _fenced(objective)
            return _escape_inline(objective)
    return NONE_RECORDED


def _render_acceptance(document: dict[str, Any]) -> str | list[str]:
    items = document.get("acceptance_criteria")
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ac_id = _escape_inline(_as_str(item.get("id")))
        statement = _escape_inline(_as_str(item.get("statement")))
        lines.append(f"- [ ] {ac_id} {statement}")
    return lines or NONE_RECORDED


def _render_cited_items(
    document: dict[str, Any], key: str
) -> str | list[str]:
    items = document.get(key)
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        statement = _as_str(item.get("statement"))
        citation = item.get("citation")
        cit_str = _citation_inline(_as_str(citation)) if citation else None
        if "\n" in statement:
            if cit_str is not None:
                lines.append(f"- {cit_str}")
            lines.append(_fenced(statement))
        else:
            stmt = _escape_inline(statement)
            if cit_str is not None:
                lines.append(f"- {stmt}  (`{cit_str}`)")
            else:
                lines.append(f"- {stmt}")
    return lines or NONE_RECORDED


def _render_retrieved(document: dict[str, Any]) -> str | list[str]:
    items = document.get("retrieved_context")
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        citation = _escape_inline(_as_str(item.get("citation")))
        parts.append(f"### {citation}")
        passage = _as_str(item.get("passage"))
        parts.append(_fenced(passage))
    return parts or NONE_RECORDED


def _render_connections(document: dict[str, Any]) -> str | list[str]:
    items = document.get("connections")
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    lines = [
        "| source | target | kind | verified |",
        "| --- | --- | --- | --- |",
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        source = _cell(item.get("source"))
        target = _cell(item.get("target"))
        kind = _cell(item.get("kind"))
        verified = "true" if item.get("verified") else "false"
        lines.append(f"| {source} | {target} | {kind} | {verified} |")
    return lines or NONE_RECORDED


def _render_exclusions(document: dict[str, Any]) -> str | list[str]:
    exclusions = document.get("exclusions")
    if not isinstance(exclusions, dict):
        return NONE_RECORDED
    lines: list[str] = []
    for key, label in (
        ("paths", "path"),
        ("globs", "glob"),
        ("tags", "tag"),
        ("directives", "directive"),
    ):
        values = exclusions.get(key)
        if isinstance(values, list):
            for value in values:
                lines.append(f"- {label}: {_escape_inline(_as_str(value))}")
    suppressed = exclusions.get("suppressed")
    if isinstance(suppressed, dict):
        for key in ("retrieved_context", "connections", "notes"):
            if key in suppressed:
                lines.append(
                    f"- suppressed.{key}: {_escape_inline(_as_str(suppressed[key]))}"
                )
    enforced = exclusions.get("enforced")
    if isinstance(enforced, bool):
        lines.append(f"- enforced: {str(enforced).lower()}")
    return lines or NONE_RECORDED


def _render_provenance(document: dict[str, Any]) -> str | list[str]:
    lines: list[str] = []
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        index = provenance.get("index")
        if isinstance(index, dict):
            schema = _escape_inline(_as_str(index.get("schema_version")))
            indexed_at = _escape_inline(_as_str(index.get("indexed_at")))
            lines.append(f"- Index schema: {schema}, indexed at: {indexed_at}")
        citations = provenance.get("citations")
        if isinstance(citations, list) and citations:
            lines.append("- Citations:")
            for citation in citations:
                lines.append(f"  - {_escape_inline(_as_str(citation))}")
    budget = document.get("budget")
    if isinstance(budget, dict):
        used = _escape_inline(_as_str(budget.get("characters_used")))
        total = _escape_inline(_as_str(budget.get("character_budget")))
        truncated = budget.get("truncated")
        trunc = str(truncated).lower() if isinstance(truncated, bool) else ""
        lines.append(f"- Budget: {used} / {total} characters (truncated: {trunc})")
    return lines or NONE_RECORDED


def _section(heading: str, body: str | list[str]) -> list[str]:
    lines = [f"## {heading}", ""]
    if body == NONE_RECORDED:
        lines.append(NONE_RECORDED)
    elif isinstance(body, str):
        lines.append(body)
    else:
        lines.extend(body)
    return lines


def render_contract_markdown(document: dict[str, Any]) -> str:
    lines: list[str] = [f"# Task contract — {_title(document)}", ""]
    lines.extend(_blockquote(document))
    lines.append("")
    lines.extend(_section("1. Objective", _render_objective(document)))
    lines.append("")
    lines.extend(_section("2. Acceptance criteria", _render_acceptance(document)))
    lines.append("")
    lines.extend(_section("3. Constraints", _render_cited_items(document, "constraints")))
    lines.append("")
    lines.extend(
        _section("4. Prior decisions", _render_cited_items(document, "prior_decisions"))
    )
    lines.append("")
    lines.extend(_section("5. Retrieved context", _render_retrieved(document)))
    lines.append("")
    lines.extend(_section("6. Connections", _render_connections(document)))
    lines.append("")
    lines.extend(_section("7. Exclusions and scope", _render_exclusions(document)))
    lines.append("")
    lines.extend(_section("8. Provenance", _render_provenance(document)))
    return "\n".join(lines) + "\n"
