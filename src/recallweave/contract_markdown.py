from __future__ import annotations

from typing import Any

MIN_FENCE = 3
FENCE_LANGUAGE = "text"
NONE_RECORDED = "None recorded."


class _Escaped:
    """A string already escaped for a specific output position.

    Every value that reaches the output buffer is wrapped in _Escaped by an
    explicit position helper (inline / fenced) or by _literal() for a trusted
    static literal. The instance cannot be built directly from an arbitrary
    raw string; a future branch must make an explicit escaping choice before
    a value can reach the buffer.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        raise TypeError(
            "_Escaped must be created via _literal() or a position helper"
        )

    @classmethod
    def _make(cls, text: str) -> "_Escaped":
        obj = cls.__new__(cls)
        obj.text = text
        return obj


def _literal(text: str) -> _Escaped:
    """Wrap a trusted static literal (format punctuation, headings, and other
    text authored by this module). Dynamic values must use a position helper."""
    return _Escaped._make(text)


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
    return _Escaped._make("".join(piece.text for piece in pieces))


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


# --- Position helpers: every untrusted value reaching the output buffer must
# pass through exactly one of these, which both performs the escaping and
# returns an _Escaped wrapper so it cannot be interpolated as a bare string.


def _normalize_lines(value: str) -> str:
    """Normalize CRLF and bare CR to LF so every line boundary is recognized
    consistently (matching RecallWeave's model of CRLF, CR and LF)."""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _fenced_esc(value: Any) -> _Escaped:
    """A block rendered raw inside a fence (already inert). Content is emitted
    verbatim after normalizing CRLF and bare CR to LF, per the fence rule."""
    return _Escaped._make(_fenced(_normalize_lines(_as_str(value))))


def _field(label: str, value: Any) -> list[_Escaped]:
    """One fenced block per document field, each preceded by its own trusted
    chrome label. Never concatenate two document fields into one fence: merging
    fields destroys the evidence boundary (an operator statement carrying the
    citation's bytes renders byte-identically to a cited statement carrying
    them). An absent (None) field renders the explicit trusted marker so that
    absence is distinguishable from an empty string."""
    content = _as_str(value) if value is not None else NONE_RECORDED
    return [_literal(label), _fenced_esc(content)]


def _render_schema(document: dict[str, Any]) -> list[_Escaped] | None:
    """The top-level document schema_version is material contract content that
    the base renderer emitted and the Cycle-7 restructuring dropped. It is
    document-derived and therefore untrusted: a trusted label precedes a fenced
    block (never a blockquote or inline value)."""
    schema = document.get("schema_version")
    if not isinstance(schema, str) or not schema:
        return None
    return [_literal("Schema:"), _fenced_esc(schema)]


def _render_handling(document: dict[str, Any]) -> list[_Escaped]:
    """The handling statement and scope are operator-authored text; each is
    rendered inertly inside its own fenced block (the handling blockquote is
    removed under FROZEN INTERFACE v3)."""
    handling = document.get("handling")
    lines: list[_Escaped] = []
    if isinstance(handling, dict):
        statement = handling.get("statement")
        if isinstance(statement, str) and statement:
            lines.append(_literal("Handling statement:"))
            lines.append(_fenced_esc(statement))
        scope = handling.get("scope")
        if isinstance(scope, str) and scope:
            lines.append(_literal("Handling scope:"))
            lines.append(_fenced_esc(scope))
    return lines


def _render_objective(document: dict[str, Any]) -> list[_Escaped] | None:
    """The task id (operator-controlled) and objective are untrusted; each is
    rendered inertly inside its own fenced block under section 1. The title no
    longer interpolates either value."""
    task = document.get("task")
    lines: list[_Escaped] = []
    if isinstance(task, dict):
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            lines.append(_literal("Task id:"))
            lines.append(_fenced_esc(task_id))
        objective = task.get("objective")
        if isinstance(objective, str) and objective:
            lines.append(_literal("Objective:"))
            lines.append(_fenced_esc(objective))
    return lines or None


def _render_acceptance(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("acceptance_criteria")
    if not isinstance(items, list) or not items:
        return None
    lines: list[_Escaped] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        statement = _as_str(item.get("statement"))
        if not statement:
            continue
        # The AC id and statement are read from the document and are therefore
        # untrusted; each gets its own trusted label and its own fenced block,
        # so an id carrying the statement's content cannot collide with a
        # statement carrying the id's content.
        base = f"Acceptance criterion {index}"
        lines.extend(_field(f"{base} id:", item.get("id")))
        lines.extend(_field(f"{base} statement:", item.get("statement")))
    return lines or None


def _render_cited_items(
    document: dict[str, Any], key: str
) -> list[_Escaped] | None:
    items = document.get(key)
    if not isinstance(items, list) or not items:
        return None
    label = "Constraint" if key == "constraints" else "Prior decision"
    lines: list[_Escaped] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        # One fenced block PER field, each preceded by its own trusted label:
        # statement, citation, and evidence_class never share a fence, so an
        # operator-authored statement carrying the citation's bytes cannot
        # render byte-identically to a cited statement carrying them. No inline
        # code span. An absent field renders the explicit trusted marker.
        base = f"{label} {index}"
        lines.extend(_field(f"{base} statement:", item.get("statement")))
        lines.extend(_field(f"{base} citation:", item.get("citation")))
        lines.extend(
            _field(f"{base} evidence class:", item.get("evidence_class"))
        )
    return lines or None


def _render_retrieved(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("retrieved_context")
    if not isinstance(items, list) or not items:
        return None
    parts: list[_Escaped] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        # Trusted '### Passage N' heading; the citation, passage and evidence
        # class each get their own trusted label and their own fenced block, so
        # the citation can never become a live heading and a citation carrying
        # the passage's content cannot collide with a passage carrying the
        # citation's content.
        parts.append(_literal(f"### Passage {index}"))
        base = f"Passage {index}"
        parts.extend(_field(f"{base} citation:", item.get("citation")))
        parts.extend(_field(f"{base} passage:", item.get("passage")))
        parts.extend(
            _field(f"{base} evidence class:", item.get("evidence_class"))
        )
    return parts or None


def _render_connections(document: dict[str, Any]) -> list[_Escaped] | None:
    items = document.get("connections")
    if not isinstance(items, list) or not items:
        return None
    # The table is removed: a CommonMark table cell cannot hold a fenced block.
    # Each connection becomes a trusted label followed by fenced blocks — one
    # fenced block PER field (source, target, kind, verified), each preceded by
    # its own trusted label. Never concatenate two document fields into one
    # fence: merging fields destroys the evidence boundary (a source carrying
    # the target's bytes renders byte-identically to a target carrying them).
    # An absent field renders the explicit trusted marker so that absence is
    # distinguishable from an empty string.
    lines: list[_Escaped] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        base = f"Connection {index}"
        lines.extend(_field(f"{base} source:", item.get("source")))
        lines.extend(_field(f"{base} target:", item.get("target")))
        lines.extend(_field(f"{base} kind:", item.get("kind")))
        verified = item.get("verified")
        verified_str = "true" if verified else "false"
        if verified is None:
            verified_str = NONE_RECORDED
        lines.extend(_field(f"{base} verified:", verified_str))
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
                lines.append(_literal(f"{label}:"))
                lines.append(_fenced_esc(value))
    suppressed = exclusions.get("suppressed")
    if isinstance(suppressed, dict):
        for key in ("retrieved_context", "connections", "notes"):
            if key in suppressed:
                # The suppressed count is document-derived and therefore
                # untrusted; a trusted literal label precedes a fenced block.
                lines.append(_literal(f"suppressed.{key}:"))
                lines.append(_fenced_esc(suppressed[key]))
    enforced = exclusions.get("enforced")
    if isinstance(enforced, bool):
        lines.append(
            _join(_literal("enforced: "), _literal(str(enforced).lower()))
        )
    return lines or None


def _render_provenance(document: dict[str, Any]) -> list[_Escaped] | None:
    lines: list[_Escaped] = []
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        generated = provenance.get("generated_at")
        if isinstance(generated, str) and generated:
            lines.append(_literal("Generated at:"))
            lines.append(_fenced_esc(generated))
        index = provenance.get("index")
        if isinstance(index, dict):
            schema = index.get("schema_version")
            if isinstance(schema, str) and schema:
                lines.append(_literal("Index schema:"))
                lines.append(_fenced_esc(schema))
            indexed_at = index.get("indexed_at")
            if isinstance(indexed_at, str) and indexed_at:
                lines.append(_literal("indexed at:"))
                lines.append(_fenced_esc(indexed_at))
        citations = provenance.get("citations")
        if isinstance(citations, list) and citations:
            lines.append(_literal("Citations:"))
            for citation in citations:
                lines.append(_fenced_esc(citation))
    budget = document.get("budget")
    if isinstance(budget, dict):
        # The budget numbers are document-derived and therefore untrusted; each
        # gets its own trusted label and its own fenced block, so one numeric
        # field carrying another's label+value cannot render byte-identically to
        # that field carrying the value (same one-fence-per-field rule as the
        # evidence sections).
        for key, label in (
            ("characters_used", "characters used:"),
            ("character_budget", "character budget:"),
        ):
            value = budget.get(key)
            if value is not None and _as_str(value):
                lines.extend(_field(label, value))
        truncated = budget.get("truncated")
        if isinstance(truncated, bool):
            lines.extend(_field("truncated:", str(truncated).lower()))
    return lines or None


def _section(heading: str, body: list[_Escaped] | None) -> list[_Escaped]:
    lines: list[_Escaped] = [_literal(f"## {heading}"), _literal("")]
    if not body:
        lines.append(_literal(NONE_RECORDED))
    else:
        lines.extend(body)
    return lines


def render_contract_markdown(document: dict[str, Any]) -> str:
    lines: list[_Escaped] = [
        _literal("# Task contract"),
        _literal(""),
    ]
    schema = _render_schema(document)
    if schema:
        lines.extend(schema)
        lines.append(_literal(""))
    handling = _render_handling(document)
    if handling:
        lines.extend(handling)
        lines.append(_literal(""))
    lines.extend(_section("1. Objective", _render_objective(document)))
    lines.append(_literal(""))
    lines.extend(_section("2. Acceptance criteria", _render_acceptance(document)))
    lines.append(_literal(""))
    lines.extend(
        _section("3. Constraints", _render_cited_items(document, "constraints"))
    )
    lines.append(_literal(""))
    lines.extend(
        _section("4. Prior decisions", _render_cited_items(document, "prior_decisions"))
    )
    lines.append(_literal(""))
    lines.extend(_section("5. Retrieved context", _render_retrieved(document)))
    lines.append(_literal(""))
    lines.extend(_section("6. Connections", _render_connections(document)))
    lines.append(_literal(""))
    lines.extend(_section("7. Exclusions and scope", _render_exclusions(document)))
    lines.append(_literal(""))
    lines.extend(_section("8. Provenance", _render_provenance(document)))
    return "\n".join(line.text for line in lines) + "\n"
