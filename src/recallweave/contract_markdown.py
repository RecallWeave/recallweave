from __future__ import annotations

from typing import Any

MIN_FENCE = 3
FENCE_LANGUAGE = "text"
NONE_RECORDED = "None recorded."


def _inline(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _title(document: dict[str, Any]) -> str:
    task = document.get("task")
    if isinstance(task, dict):
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            return _inline(task_id)
        objective = task.get("objective")
        if isinstance(objective, str) and objective:
            return _inline(objective.split("\n", 1)[0])
    return ""


def _blockquote(document: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    schema = document.get("schema_version")
    if isinstance(schema, str):
        lines.append(f"> Schema: {_inline(schema)}")
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        generated = provenance.get("generated_at")
        if isinstance(generated, str):
            lines.append(f"> Generated at: {_inline(generated)}")
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
            return _inline(objective)
    return NONE_RECORDED


def _render_acceptance(document: dict[str, Any]) -> str | list[str]:
    items = document.get("acceptance_criteria")
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ac_id = _inline(_as_str(item.get("id")))
        statement = _inline(_as_str(item.get("statement")))
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
        statement = _inline(_as_str(item.get("statement")))
        citation = item.get("citation")
        if isinstance(citation, str) and citation:
            lines.append(f"- {statement}  (`{citation}`)")
        else:
            lines.append(f"- {statement}")
    return lines or NONE_RECORDED


def _render_retrieved(document: dict[str, Any]) -> str | list[str]:
    items = document.get("retrieved_context")
    if not isinstance(items, list) or not items:
        return NONE_RECORDED
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        citation = _inline(_as_str(item.get("citation")))
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
        source = _inline(_as_str(item.get("source")))
        target = _inline(_as_str(item.get("target")))
        kind = _inline(_as_str(item.get("kind")))
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
                lines.append(f"- {label}: {_inline(_as_str(value))}")
    suppressed = exclusions.get("suppressed")
    if isinstance(suppressed, dict):
        for key in ("retrieved_context", "connections", "notes"):
            if key in suppressed:
                lines.append(f"- suppressed.{key}: {_as_str(suppressed[key])}")
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
            schema = _inline(_as_str(index.get("schema_version")))
            indexed_at = _inline(_as_str(index.get("indexed_at")))
            lines.append(f"- Index schema: {schema}, indexed at: {indexed_at}")
        citations = provenance.get("citations")
        if isinstance(citations, list) and citations:
            lines.append("- Citations:")
            for citation in citations:
                lines.append(f"  - {_inline(_as_str(citation))}")
    budget = document.get("budget")
    if isinstance(budget, dict):
        used = _as_str(budget.get("characters_used"))
        total = _as_str(budget.get("character_budget"))
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
