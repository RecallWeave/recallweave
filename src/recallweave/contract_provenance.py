from __future__ import annotations

from typing import Any

from .contract_text import sanitize


def index_provenance(connection: Any) -> dict[str, Any]:
    """Index provenance, with every emitted string SANITIZED.

    These values come from the index's `meta` table, which a malformed or
    hand-edited index can fill with anything. They cannot carry a vault passage
    or change an evidence class, so this is completeness rather than a leak --
    but the documented invariant is that EVERY string reaching the document is
    sanitized, and an invariant with an exception is not one."""
    def meta_value(key: str) -> str:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Index is missing required meta key {key!r}; cannot state index provenance."
            )
        return sanitize(str(row["value"]))

    schema_version = meta_value("schema_version")
    indexed_at = meta_value("indexed_at")
    notes = int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
    sections = int(connection.execute("SELECT COUNT(*) FROM sections").fetchone()[0])
    return {
        "schema_version": schema_version,
        "indexed_at": indexed_at,
        "notes": notes,
        "sections": sections,
    }
