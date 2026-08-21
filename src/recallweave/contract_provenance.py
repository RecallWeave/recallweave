from __future__ import annotations

from typing import Any


def index_provenance(connection: Any) -> dict[str, Any]:
    def meta_value(key: str) -> str:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Index is missing required meta key {key!r}; cannot state index provenance."
            )
        return row["value"]

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
