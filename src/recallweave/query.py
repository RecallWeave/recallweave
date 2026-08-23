from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import SCHEMA_VERSION, connect
from .parser import normalize_name, tokenize

MAX_EDGE_ROWS = 200
MAX_PATH_NEIGHBORS = 1_000
MAX_SHARED_TAG_FANOUT = 100


def _payload(operation: str, **values: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "operation": operation, **values}


def _note_count(connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0])


def _resolve_note(connection, value: str) -> int:
    normalized_path = value.replace("\\", "/")
    direct = connection.execute(
        "SELECT id FROM notes WHERE relative_path = ? COLLATE NOCASE",
        (normalized_path,),
    ).fetchall()
    if len(direct) == 1:
        return int(direct[0]["id"])
    if "/" in normalized_path:
        raise ValueError(f"Note path not found: {value}")
    rows = connection.execute(
        "SELECT DISTINCT note_id FROM note_names WHERE normalized_name = ?",
        (normalize_name(value),),
    ).fetchall()
    if not rows:
        raise ValueError(f"Note not found: {value}")
    if len(rows) > 1:
        raise ValueError(f"Ambiguous note name: {value}")
    return int(rows[0]["note_id"])


def _search(connection, query: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive.")
    query_counts: dict[str, int] = defaultdict(int)
    for token in tokenize(query):
        query_counts[token] += 1
    if not query_counts:
        return []

    terms = sorted(query_counts)
    placeholders = ",".join("?" for _ in terms)
    row_cap = max(2_000, limit * 400)
    rows = connection.execute(
        f"""
        SELECT
            t.section_id, t.note_id, t.term, t.term_count,
            s.heading, s.line_start, s.line_end, s.text,
            n.relative_path, n.title, n.status, n.domain
        FROM terms t
        JOIN sections s ON s.id = t.section_id
        JOIN notes n ON n.id = t.note_id
        WHERE t.term IN ({placeholders})
        ORDER BY t.term_count DESC, t.section_id
        LIMIT ?
        """,
        [*terms, row_cap],
    ).fetchall()
    if not rows:
        return []

    note_count = _note_count(connection)
    document_frequency = {
        term: int(
            connection.execute(
                "SELECT COUNT(DISTINCT note_id) FROM terms WHERE term = ?", (term,)
            ).fetchone()[0]
        )
        for term in terms
    }
    section_scores: dict[int, float] = defaultdict(float)
    section_rows: dict[int, Any] = {}
    matched: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        term = str(row["term"])
        idf = math.log((note_count + 1) / (document_frequency[term] + 1)) + 1.0
        score = (1.0 + math.log(int(row["term_count"]))) * idf * query_counts[term]
        section_id = int(row["section_id"])
        section_scores[section_id] += score
        section_rows[section_id] = row
        matched[section_id].add(term)

    results: list[dict[str, Any]] = []
    ranked = sorted(section_scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    for section_id, score in ranked:
        row = section_rows[section_id]
        status_boost = 1.10 if str(row["status"] or "").casefold() == "canonical" else 1.0
        results.append(
            {
                "section_id": section_id,
                "note_id": int(row["note_id"]),
                "score": round(score * status_boost, 6),
                "relative_path": row["relative_path"],
                "title": row["title"],
                "heading": row["heading"],
                "line_start": int(row["line_start"]),
                "line_end": int(row["line_end"]),
                "citation": f"{row['relative_path']}:{row['line_start']}-{row['line_end']}",
                "passage": row["text"],
                "truncated": False,
                "matched_terms": sorted(matched[section_id]),
                "status": row["status"],
                "domain": row["domain"],
            }
        )
    return results


def _edge_rows(
    connection,
    note_ids: list[int],
    include_candidates: bool,
    limit: int = MAX_EDGE_ROWS,
    excluded_note_ids: set[int] | None = None,
) -> list[Any]:
    """Fetch up to `limit` edges touching any of `note_ids`. When
    `excluded_note_ids` is provided (a set of note ids to exclude), edges whose
    source OR target is excluded are removed in SQL so the `limit` bound applies
    to ALLOWED edges only. Without this, a run of higher-ranked excluded edges
    could consume the whole bound and starve an allowed edge that ranks below
    them — an under-inclusion defect (recallweave-z1a)."""
    if not note_ids:
        return []
    placeholders = ",".join("?" for _ in note_ids)
    candidate_clause = "" if include_candidates else "AND e.is_verified = 1"
    params: list = [*note_ids, *note_ids]
    exclusion_clause = ""
    excluded = excluded_note_ids or set()
    if excluded:
        excluded_placeholders = ",".join("?" for _ in excluded)
        exclusion_clause = (
            f" AND e.source_note_id NOT IN ({excluded_placeholders})"
            f" AND e.target_note_id NOT IN ({excluded_placeholders})"
        )
        params += [*excluded, *excluded]
    params.append(limit)
    return connection.execute(
        f"""
        SELECT e.*, sn.relative_path AS source_path, sn.title AS source_title,
               tn.relative_path AS target_path, tn.title AS target_title
        FROM edges e
        JOIN notes sn ON sn.id = e.source_note_id
        JOIN notes tn ON tn.id = e.target_note_id
        WHERE (e.source_note_id IN ({placeholders}) OR e.target_note_id IN ({placeholders}))
        {candidate_clause}
        {exclusion_clause}
        ORDER BY e.is_verified DESC, e.score DESC, e.id
        LIMIT ?
        """,
        params,
    ).fetchall()


def _edge_count(
    connection,
    note_ids: list[int],
    include_candidates: bool,
) -> int:
    if not note_ids:
        return 0
    placeholders = ",".join("?" for _ in note_ids)
    candidate_clause = "" if include_candidates else "AND is_verified = 1"
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM edges
            WHERE (source_note_id IN ({placeholders}) OR target_note_id IN ({placeholders}))
            {candidate_clause}
            """,
            [*note_ids, *note_ids],
        ).fetchone()[0]
    )


def _co_tag_connection_count(connection, note_id: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM note_tags source_tag
            JOIN (
                SELECT tag, COUNT(*) AS note_count
                FROM note_tags
                GROUP BY tag
            ) frequencies ON frequencies.tag = source_tag.tag
            JOIN note_tags target_tag
              ON target_tag.tag = source_tag.tag AND target_tag.note_id != source_tag.note_id
            WHERE source_tag.note_id = ? AND frequencies.note_count <= ?
            """,
            (note_id, MAX_SHARED_TAG_FANOUT),
        ).fetchone()[0]
    )


def _co_tag_connections(connection, note_id: int, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT source_tag.tag, source_tag.evidence_json AS source_evidence,
               target_tag.note_id AS target_note_id,
               target_tag.evidence_json AS target_evidence,
               target.relative_path, target.title,
               frequencies.note_count
        FROM note_tags source_tag
        JOIN (
            SELECT tag, COUNT(*) AS note_count
            FROM note_tags
            GROUP BY tag
        ) frequencies ON frequencies.tag = source_tag.tag
        JOIN note_tags target_tag
          ON target_tag.tag = source_tag.tag AND target_tag.note_id != source_tag.note_id
        JOIN notes target ON target.id = target_tag.note_id
        WHERE source_tag.note_id = ? AND frequencies.note_count <= ?
        ORDER BY frequencies.note_count, source_tag.tag, target.relative_path
        LIMIT ?
        """,
        (note_id, MAX_SHARED_TAG_FANOUT, limit),
    ).fetchall()
    return [
        {
            "note": row["relative_path"],
            "title": row["title"],
            "direction": "supporting",
            "kind": "co_tag",
            "verified": False,
            "score": round(1.0 / math.log2(int(row["note_count"]) + 1), 6),
            "evidence": {
                "tag": row["tag"],
                "tag_note_count": int(row["note_count"]),
                "source": json.loads(row["source_evidence"]),
                "target": json.loads(row["target_evidence"]),
                "explanation": "Supporting signal only: a shared tag is not an authored relationship.",
            },
        }
        for row in rows
    ]


def context_packet(
    database: Path,
    query: str,
    limit: int = 8,
    max_characters: int = 12_000,
    include_candidates: bool = False,
) -> dict[str, Any]:
    if max_characters <= 0:
        raise ValueError("max characters must be positive.")
    with connect(database, readonly=True) as connection:
        hits = _search(connection, query, max(limit * 2, limit))
        selected: list[dict[str, Any]] = []
        used = 0
        for hit in hits:
            remaining = max_characters - used
            if remaining <= 0:
                break
            if selected and remaining < 80:
                break
            passage = str(hit["passage"])
            truncated = len(passage) > remaining
            if truncated:
                passage = passage[: max(0, remaining - 1)].rstrip() + "…"
            item = {**hit, "passage": passage, "truncated": truncated}
            selected.append(item)
            used += len(passage)
            if len(selected) >= limit:
                break

        seed_ids = list(dict.fromkeys(int(hit["note_id"]) for hit in selected))
        edge_rows = _edge_rows(connection, seed_ids, include_candidates)
        edge_total = _edge_count(connection, seed_ids, include_candidates)
        edges = [
            {
                "source": row["source_path"],
                "target": row["target_path"],
                "kind": row["kind"],
                "verified": bool(row["is_verified"]),
                "score": row["score"],
                "evidence": json.loads(row["evidence_json"]),
            }
            for row in edge_rows
        ]
    return _payload(
        "query",
        query=query,
        policy={
            "read_only": True,
            "network_calls": 0,
            "vault_writes": 0,
            "candidate_edges_included": include_candidates,
        },
        passages=selected,
        connections=edges,
        connections_total=edge_total,
        connections_returned=len(edges),
        connections_truncated=len(edges) < edge_total,
        characters_used=used,
        character_budget=max_characters,
        citations=[item["citation"] for item in selected],
    )


def connections(
    database: Path,
    note: str,
    include_candidates: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive.")
    with connect(database, readonly=True) as connection:
        note_id = _resolve_note(connection, note)
        source = connection.execute(
            "SELECT relative_path, title FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        rows = _edge_rows(connection, [note_id], include_candidates, limit=limit)
        edge_total = _edge_count(connection, [note_id], include_candidates)
        co_tag_total = (
            _co_tag_connection_count(connection, note_id)
            if include_candidates
            else 0
        )
        items = []
        for row in rows:
            outgoing = int(row["source_note_id"]) == note_id
            items.append(
                {
                    "note": row["target_path"] if outgoing else row["source_path"],
                    "title": row["target_title"] if outgoing else row["source_title"],
                    "direction": "outgoing" if outgoing else "incoming",
                    "kind": row["kind"],
                    "verified": bool(row["is_verified"]),
                    "score": row["score"],
                    "evidence": json.loads(row["evidence_json"]),
                }
            )
        if include_candidates and len(items) < limit:
            items.extend(_co_tag_connections(connection, note_id, limit - len(items)))
        total = edge_total + co_tag_total
    return _payload(
        "connections",
        source={"path": source["relative_path"], "title": source["title"]},
        connections=items,
        connections_total=total,
        connections_returned=len(items),
        connections_truncated=len(items) < total,
    )


def _parse_age(value: str | None, fallback: str) -> float:
    candidate = value or fallback
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86_400)
    except (ValueError, TypeError):
        return 0.0


def resurface(
    database: Path,
    query: str,
    limit: int = 6,
    minimum_age_days: int = 30,
) -> dict[str, Any]:
    with connect(database, readonly=True) as connection:
        hits = _search(connection, query, max(limit * 8, 40))
        best_by_note: dict[int, dict[str, Any]] = {}
        for hit in hits:
            note_id = int(hit["note_id"])
            if note_id not in best_by_note or hit["score"] > best_by_note[note_id]["score"]:
                best_by_note[note_id] = hit
        if not best_by_note:
            return _payload("resurface", query=query, results=[])

        max_relevance = max(float(hit["score"]) for hit in best_by_note.values()) or 1.0
        results = []
        for note_id, hit in best_by_note.items():
            note = connection.execute(
                "SELECT created_at, updated_at, modified_at FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            age_days = _parse_age(note["updated_at"] or note["created_at"], note["modified_at"])
            if age_days < minimum_age_days:
                continue
            verified_degree = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM edges
                    WHERE is_verified = 1
                      AND kind IN ('wikilink', 'markdown_link')
                      AND (source_note_id = ? OR target_note_id = ?)
                    """,
                    (note_id, note_id),
                ).fetchone()[0]
            )
            relevance = float(hit["score"]) / max_relevance
            dormancy = min(1.0, age_days / 730.0)
            underlinked = 1.0 / (1.0 + verified_degree)
            score = 0.65 * relevance + 0.25 * dormancy + 0.10 * underlinked
            results.append(
                {
                    **hit,
                    "resurface_score": round(score, 6),
                    "age_days": round(age_days, 1),
                    "verified_degree": verified_degree,
                    "why": [
                        f"matches: {', '.join(hit['matched_terms'])}",
                        f"dormant for about {round(age_days)} days",
                        f"{verified_degree} authored graph connections",
                    ],
                }
            )
    results.sort(key=lambda item: item["resurface_score"], reverse=True)
    return _payload(
        "resurface",
        query=query,
        minimum_age_days=minimum_age_days,
        results=results[:limit],
        claim="Suggestions for review, not assertions of truth or importance.",
    )


def _neighbors(connection, note_id: int, include_candidates: bool) -> tuple[list[Any], bool]:
    candidate_clause = "" if include_candidates else "AND is_verified = 1"
    rows = connection.execute(
        f"""
        SELECT source_note_id, target_note_id, kind, is_verified, score, evidence_json
        FROM edges
        WHERE (source_note_id = ? OR target_note_id = ?)
          {candidate_clause}
        ORDER BY is_verified DESC, score DESC, id
        LIMIT ?
        """,
        (note_id, note_id, MAX_PATH_NEIGHBORS + 1),
    ).fetchall()
    return rows[:MAX_PATH_NEIGHBORS], len(rows) > MAX_PATH_NEIGHBORS


def path_between(
    database: Path,
    source: str,
    target: str,
    include_candidates: bool = False,
    max_hops: int = 6,
) -> dict[str, Any]:
    if max_hops <= 0:
        raise ValueError("max hops must be positive.")
    with connect(database, readonly=True) as connection:
        source_id = _resolve_note(connection, source)
        target_id = _resolve_note(connection, target)
        queue = deque([(source_id, [])])
        visited = {source_id}
        found: list[tuple[int, int, Any]] | None = None
        search_truncated = False
        while queue:
            current, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            neighbor_rows, neighbor_truncated = _neighbors(
                connection,
                current,
                include_candidates,
            )
            search_truncated = search_truncated or neighbor_truncated
            for edge in neighbor_rows:
                left, right = int(edge["source_note_id"]), int(edge["target_note_id"])
                neighbor = right if left == current else left
                if neighbor in visited:
                    continue
                next_path = [*path, (current, neighbor, edge)]
                if neighbor == target_id:
                    found = next_path
                    queue.clear()
                    break
                visited.add(neighbor)
                queue.append((neighbor, next_path))

        if found is None:
            return _payload(
                "path",
                source=source,
                target=target,
                found=False,
                candidate_edges_included=include_candidates,
                search_truncated=search_truncated,
            )

        note_ids = sorted({item for step in found for item in step[:2]})
        placeholders = ",".join("?" for _ in note_ids)
        names = {
            int(row["id"]): {"path": row["relative_path"], "title": row["title"]}
            for row in connection.execute(
                f"SELECT id, relative_path, title FROM notes WHERE id IN ({placeholders})",
                note_ids,
            )
        }
        steps = [
            {
                "from": names[left],
                "to": names[right],
                "kind": edge["kind"],
                "verified": bool(edge["is_verified"]),
                "score": edge["score"],
                "evidence": json.loads(edge["evidence_json"]),
            }
            for left, right, edge in found
        ]
    return _payload(
        "path",
        source=names[source_id],
        target=names[target_id],
        found=True,
        candidate_edges_included=include_candidates,
        search_truncated=search_truncated,
        steps=steps,
    )


def doctor(database: Path, limit: int = 100) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be positive.")
    with connect(database, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT n.relative_path, u.kind, u.target_text, u.line, u.reason
            FROM unresolved_links u
            JOIN notes n ON n.id = u.source_note_id
            ORDER BY n.relative_path, u.line
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = int(connection.execute("SELECT COUNT(*) FROM unresolved_links").fetchone()[0])
    return _payload(
        "doctor",
        unresolved_total=total,
        returned=len(rows),
        unresolved=[
            {
                "source": row["relative_path"],
                "line": int(row["line"]),
                "kind": row["kind"],
                "target": row["target_text"],
                "reason": row["reason"],
            }
            for row in rows
        ],
    )


def stats(database: Path) -> dict[str, Any]:
    with connect(database, readonly=True) as connection:
        counts = {
            "notes": int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]),
            "sections": int(connection.execute("SELECT COUNT(*) FROM sections").fetchone()[0]),
            "verified_edges": int(
                connection.execute("SELECT COUNT(*) FROM edges WHERE is_verified = 1").fetchone()[0]
            ),
            "candidate_edges": int(
                connection.execute("SELECT COUNT(*) FROM edges WHERE is_verified = 0").fetchone()[0]
            ),
            "note_tags": int(connection.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0]),
            "unresolved_links": int(
                connection.execute("SELECT COUNT(*) FROM unresolved_links").fetchone()[0]
            ),
        }
        indexed_at = connection.execute(
            "SELECT value FROM meta WHERE key = 'indexed_at'"
        ).fetchone()["value"]
        discovery = json.loads(
            connection.execute(
                "SELECT value FROM meta WHERE key = 'discovery_diagnostics'"
            ).fetchone()["value"]
        )
    return _payload("stats", indexed_at=indexed_at, discovery=discovery, **counts)
