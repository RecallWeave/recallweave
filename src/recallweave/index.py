from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from .model import LinkEvidence, Note
from .parser import normalize_name, parse_note, tokenize
from .policy import IndexPolicy, RESERVED_DIRECTORY_NAMES

# This is the PUBLIC receipt schema version, shared by every command's JSON
# output (see query.py and docs/json-output.md), not a private index revision.
# Do not bump it to record a new index column: adding `heading_line` and
# `heading_level` to `sections` for recallweave-kob changed what an index
# stores, not what a receipt promises. The contract builder detects that
# capability directly and refuses an index that predates it.
SCHEMA_VERSION = "2"
APPLICATION_ID = "recallweave"
DISCOVERY_POSTING_WINDOW = 12

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT,
    domain TEXT,
    created_at TEXT,
    updated_at TEXT,
    modified_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);
CREATE TABLE note_names (
    normalized_name TEXT NOT NULL,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    name_kind TEXT NOT NULL,
    UNIQUE(normalized_name, note_id, name_kind)
);
CREATE INDEX idx_note_names_name ON note_names(normalized_name);
CREATE TABLE sections (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    heading TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE note_headings (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    line INTEGER NOT NULL,
    level INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY(note_id, line)
);
CREATE INDEX idx_sections_note ON sections(note_id);
CREATE TABLE terms (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    term TEXT NOT NULL,
    term_count INTEGER NOT NULL,
    PRIMARY KEY(section_id, term)
);
CREATE INDEX idx_terms_term ON terms(term);
CREATE INDEX idx_terms_note ON terms(note_id);
CREATE TABLE note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(note_id, tag)
);
CREATE INDEX idx_note_tags_tag ON note_tags(tag);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    source_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    target_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    is_verified INTEGER NOT NULL CHECK(is_verified IN (0, 1)),
    score REAL NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(source_note_id, target_note_id, kind)
);
CREATE INDEX idx_edges_source ON edges(source_note_id);
CREATE INDEX idx_edges_target ON edges(target_note_id);
CREATE INDEX idx_edges_verified ON edges(is_verified);
CREATE TABLE unresolved_links (
    source_note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    target_text TEXT NOT NULL,
    line INTEGER NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX idx_unresolved_source ON unresolved_links(source_note_id);
"""


class ClosingConnection(sqlite3.Connection):
    """A SQLite context manager that also releases its Windows file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_database_for_vault(vault: Path) -> Path:
    resolved = vault.expanduser().resolve()
    fingerprint = hashlib.sha256(
        resolved.as_posix().casefold().encode("utf-8")
    ).hexdigest()[:20]
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "RecallWeave" / "indexes" / f"{fingerprint}.sqlite"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RecallWeave" / "indexes" / f"{fingerprint}.sqlite"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "recallweave" / "indexes" / f"{fingerprint}.sqlite"


def connect(database: Path, readonly: bool = False) -> sqlite3.Connection:
    database = database.expanduser().resolve()
    if readonly and not database.is_file():
        raise ValueError(
            f"RecallWeave database not found: {database}. Run 'recallweave index <vault>' first."
        )
    if readonly:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            factory=ClosingConnection,
        )
    else:
        connection = sqlite3.connect(database, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if readonly:
        try:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'application_id'"
            ).fetchone()
        except sqlite3.Error as error:
            connection.close()
            raise ValueError(f"Not a RecallWeave database: {database}") from error
        if row is None or row["value"] != APPLICATION_ID:
            connection.close()
            raise ValueError(f"Not a RecallWeave database: {database}")
    return connection


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _validate_destination(database: Path, force: bool) -> None:
    if not database.exists():
        return
    if not database.is_file():
        raise ValueError(f"Database destination is not a file: {database}")
    recognized = False
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            meta = {
                str(key): str(value)
                for key, value in connection.execute(
                    "SELECT key, value FROM meta WHERE key IN ('application_id', 'schema_version', 'vault_fingerprint')"
                )
            }
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            recognized = (
                meta.get("application_id") == APPLICATION_ID
                or (
                    bool(meta.get("schema_version"))
                    and meta.get("vault_fingerprint") == "paths-relative-content-hashes-only"
                    and {"notes", "sections", "edges"}.issubset(tables)
                )
            )
        finally:
            connection.close()
    except sqlite3.Error:
        recognized = False
    if not recognized and not force:
        raise ValueError(
            f"Refusing to overwrite a non-RecallWeave file: {database}. "
            "Choose another --database path or pass --force deliberately."
        )


def _insert_notes(connection: sqlite3.Connection, notes: list[Note]) -> dict[str, list[int]]:
    lookup: dict[str, list[int]] = defaultdict(list)
    for note in notes:
        cursor = connection.execute(
            """
            INSERT INTO notes(
                relative_path, title, aliases_json, tags_json, status, domain,
                created_at, updated_at, modified_at, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note.relative_path,
                note.title,
                json.dumps(note.aliases),
                json.dumps(note.tags),
                note.status,
                note.domain,
                note.created_at,
                note.updated_at,
                note.modified_at,
                note.content_hash,
            ),
        )
        note_id = int(cursor.lastrowid)
        names = [
            (note.title, "title"),
            (Path(note.relative_path).stem, "stem"),
            *[(alias, "alias") for alias in note.aliases],
        ]
        for name, kind in names:
            normalized = normalize_name(name)
            if not normalized:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO note_names(normalized_name, note_id, name_kind) VALUES (?, ?, ?)",
                (normalized, note_id, kind),
            )
            if note_id not in lookup[normalized]:
                lookup[normalized].append(note_id)

        # Every heading line, whether or not it has a body. Sections are
        # body-driven and drop a bodyless heading, but links are extracted from
        # every heading line, so an authored edge can point at one
        # (recallweave-kob).
        connection.executemany(
            "INSERT OR IGNORE INTO note_headings(note_id, line, level, text) "
            "VALUES (?, ?, ?, ?)",
            [
                (note_id, heading.line, heading.level, heading.text)
                for heading in note.headings
            ],
        )

        for section in note.sections:
            section_cursor = connection.execute(
                """
                INSERT INTO sections(note_id, heading, line_start, line_end, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                (note_id, section.heading, section.line_start, section.line_end, section.text),
            )
            section_id = int(section_cursor.lastrowid)
            counts = Counter(tokenize(f"{section.heading} {section.text}"))
            connection.executemany(
                "INSERT INTO terms(section_id, note_id, term, term_count) VALUES (?, ?, ?, ?)",
                [(section_id, note_id, term, count) for term, count in counts.items()],
            )
    return lookup


def _path_key(value: str) -> str:
    value = unquote(value).replace("\\", "/").strip()
    value = value.split("#", 1)[0]
    if value.casefold().endswith(".md"):
        value = value[:-3]
    return posixpath.normpath(value).lstrip("/").casefold()


def _resolve_link(
    note: Note,
    link: LinkEvidence,
    lookup: dict[str, list[int]],
    exact_paths: dict[str, int],
) -> tuple[list[int], str | None]:
    raw_target = unquote(link.target).replace("\\", "/").strip()
    target_without_anchor = raw_target.split("#", 1)[0]
    qualified = "/" in target_without_anchor or target_without_anchor.startswith(".")
    if not qualified:
        candidates = lookup.get(normalize_name(target_without_anchor), [])
        return candidates, None if candidates else "not_found"

    target_key = _path_key(target_without_anchor)
    if target_key == ".." or target_key.startswith("../"):
        return [], "path_outside_vault"
    source_parent = PurePosixPath(note.relative_path).parent.as_posix()
    relative_key = _path_key(posixpath.join(source_parent, target_without_anchor))
    ordered = (
        [relative_key, target_key]
        if link.kind == "markdown_link"
        else [target_key, relative_key]
    )
    candidates: list[int] = []
    for key in ordered:
        note_id = exact_paths.get(key)
        if note_id is not None and note_id not in candidates:
            candidates.append(note_id)
    if not candidates:
        return [], "path_not_found"
    if len(candidates) > 1:
        return candidates, "ambiguous_path"
    return candidates, None


def _insert_explicit_edges(
    connection: sqlite3.Connection,
    notes: list[Note],
    lookup: dict[str, list[int]],
) -> tuple[int, int]:
    path_to_id = {
        row["relative_path"]: int(row["id"])
        for row in connection.execute("SELECT id, relative_path FROM notes")
    }
    exact_paths = {_path_key(path): note_id for path, note_id in path_to_id.items()}
    unresolved = 0

    for note in notes:
        source_id = path_to_id[note.relative_path]
        for link in note.links:
            if link.kind == "tag":
                tag = link.target.casefold().lstrip("#")
                evidence = json.dumps(
                    {
                        "path": note.relative_path,
                        "line": link.line,
                        "source_text": link.text,
                    },
                    sort_keys=True,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO note_tags(note_id, tag, evidence_json) VALUES (?, ?, ?)",
                    (source_id, tag, evidence),
                )
                continue

            candidates, path_reason = _resolve_link(note, link, lookup, exact_paths)
            if len(candidates) != 1 or path_reason is not None:
                reason = path_reason or ("not_found" if not candidates else "ambiguous")
                connection.execute(
                    """
                    INSERT INTO unresolved_links(source_note_id, kind, target_text, line, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (source_id, link.kind, link.target, link.line, reason),
                )
                unresolved += 1
                continue
            target_id = candidates[0]
            if target_id == source_id:
                continue
            evidence = json.dumps(
                {"line": link.line, "source_text": link.text, "target_text": link.target},
                sort_keys=True,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO edges(
                    source_note_id, target_note_id, kind, is_verified, score, evidence_json
                ) VALUES (?, ?, ?, 1, 1.0, ?)
                """,
                (source_id, target_id, link.kind, evidence),
            )

    inserted = int(
        connection.execute("SELECT COUNT(*) FROM edges WHERE is_verified = 1").fetchone()[0]
    )
    return inserted, unresolved


def _insert_candidate_edges(
    connection: sqlite3.Connection,
    minimum_score: float,
    max_per_note: int,
) -> tuple[int, dict[str, Any]]:
    note_count = int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
    diagnostics: dict[str, Any] = {
        "strategy": "relative_document_frequency_with_bounded_posting_window",
        "posting_window": DISCOVERY_POSTING_WINDOW,
        "notes": note_count,
        "terms_total": 0,
        "terms_usable": 0,
        "terms_skipped_too_rare": 0,
        "terms_skipped_too_common": 0,
        "pairs_compared": 0,
        "warnings": [],
    }
    if note_count < 2:
        diagnostics["warnings"].append("Discovery needs at least two indexed notes.")
        return 0, diagnostics
    if minimum_score < 0 or minimum_score > 1:
        raise ValueError("candidate threshold must be between 0 and 1.")
    if max_per_note < 0:
        raise ValueError("max candidates per note cannot be negative.")

    note_term_counts: dict[int, Counter[str]] = defaultdict(Counter)
    term_notes: dict[str, set[int]] = defaultdict(set)
    for row in connection.execute(
        "SELECT note_id, term, SUM(term_count) AS n FROM terms GROUP BY note_id, term"
    ):
        note_id = int(row["note_id"])
        term = str(row["term"])
        note_term_counts[note_id][term] = int(row["n"])
        term_notes[term].add(note_id)

    max_df = max(4, math.ceil(note_count * 0.20))
    allowed_terms = {
        term for term, note_ids in term_notes.items() if 2 <= len(note_ids) <= max_df
    }
    diagnostics.update(
        {
            "terms_total": len(term_notes),
            "terms_usable": len(allowed_terms),
            "terms_skipped_too_rare": sum(len(ids) < 2 for ids in term_notes.values()),
            "terms_skipped_too_common": sum(len(ids) > max_df for ids in term_notes.values()),
            "maximum_document_frequency": max_df,
        }
    )
    if not allowed_terms:
        diagnostics["warnings"].append(
            "No discovery terms passed the relative document-frequency filter."
        )
        return 0, diagnostics

    vectors: dict[int, dict[str, float]] = {}
    norms: dict[int, float] = {}
    postings: dict[str, list[int]] = defaultdict(list)
    for note_id, counts in note_term_counts.items():
        vector: dict[str, float] = {}
        for term, count in counts.most_common(80):
            if term not in allowed_terms:
                continue
            idf = math.log((note_count + 1) / (len(term_notes[term]) + 1)) + 1.0
            vector[term] = (1.0 + math.log(count)) * idf
            postings[term].append(note_id)
        vectors[note_id] = vector
        norms[note_id] = math.sqrt(sum(weight * weight for weight in vector.values()))

    candidate_pairs: set[tuple[int, int]] = set()
    for note_ids in postings.values():
        ordered = sorted(set(note_ids))
        window = min(DISCOVERY_POSTING_WINDOW, len(ordered) - 1)
        for index, left in enumerate(ordered):
            for offset in range(1, window + 1):
                right = ordered[(index + offset) % len(ordered)]
                if left != right:
                    candidate_pairs.add(tuple(sorted((left, right))))
    diagnostics["pairs_compared"] = len(candidate_pairs)

    ranked_by_note: dict[int, list[tuple[float, int, list[str]]]] = defaultdict(list)
    for left, right in candidate_pairs:
        shared = sorted(set(vectors[left]).intersection(vectors[right]))
        if len(shared) < 2 or not norms[left] or not norms[right]:
            continue
        dot = sum(vectors[left][term] * vectors[right][term] for term in shared)
        score = dot / (norms[left] * norms[right])
        if score < minimum_score:
            continue
        ranked_by_note[left].append((score, right, shared))
        ranked_by_note[right].append((score, left, shared))

    accepted: set[tuple[int, int]] = set()
    for note_id, ranked in ranked_by_note.items():
        for score, other, shared in sorted(ranked, reverse=True)[:max_per_note]:
            accepted.add(tuple(sorted((note_id, other))))

    def cited_passage(note_id: int, shared_terms: list[str]) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in shared_terms)
        row = connection.execute(
            f"""
            SELECT n.relative_path, s.heading, s.line_start, s.line_end, s.text,
                   SUM(t.term_count) AS overlap
            FROM sections s
            JOIN notes n ON n.id = s.note_id
            JOIN terms t ON t.section_id = s.id
            WHERE s.note_id = ? AND t.term IN ({placeholders})
            GROUP BY s.id
            ORDER BY overlap DESC, s.id
            LIMIT 1
            """,
            [note_id, *shared_terms],
        ).fetchone()
        if row is None:
            return {}
        passage = str(row["text"])
        truncated = len(passage) > 500
        return {
            "citation": f"{row['relative_path']}:{row['line_start']}-{row['line_end']}",
            "heading": row["heading"],
            "passage": passage[:500].rstrip() + ("…" if truncated else ""),
            "truncated": truncated,
        }

    for left, right in sorted(accepted):
        shared = sorted(set(vectors[left]).intersection(vectors[right]))
        dot = sum(vectors[left][term] * vectors[right][term] for term in shared)
        score = dot / (norms[left] * norms[right])
        ranked_terms = sorted(
            shared,
            key=lambda term: vectors[left][term] * vectors[right][term],
            reverse=True,
        )[:8]
        evidence = json.dumps(
            {
                "method": "local_tfidf_cosine",
                "shared_terms": ranked_terms,
                "source_evidence": cited_passage(left, ranked_terms),
                "target_evidence": cited_passage(right, ranked_terms),
                "explanation": "Candidate only: lexical overlap is not proof of a factual relationship.",
            },
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO edges(
                source_note_id, target_note_id, kind, is_verified, score, evidence_json
            ) VALUES (?, ?, 'discovery_candidate', 0, ?, ?)
            """,
            (left, right, round(score, 6), evidence),
        )
    inserted = int(
        connection.execute("SELECT COUNT(*) FROM edges WHERE is_verified = 0").fetchone()[0]
    )
    if inserted == 0 and note_count >= 20:
        diagnostics["warnings"].append(
            "Discovery produced zero candidates; inspect term diagnostics and threshold."
        )
    return inserted, diagnostics


def _markdown_files(vault: Path, skipped: Counter[str]) -> list[Path]:
    candidates: list[tuple[Path, Path]] = []
    for root, directory_names, file_names in os.walk(vault, followlinks=False):
        root_path = Path(root)
        kept_directories = []
        for name in directory_names:
            directory = root_path / name
            if directory.is_symlink():
                skipped["symlink"] += 1
                continue
            if name.casefold() in RESERVED_DIRECTORY_NAMES:
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = root_path / name
            if path.suffix.casefold() != ".md":
                continue
            if path.is_symlink():
                skipped["symlink"] += 1
                continue
            try:
                if path.stat(follow_symlinks=False).st_nlink > 1:
                    skipped["hardlink"] += 1
                    continue
                resolved = path.resolve(strict=True)
            except OSError:
                skipped["unreadable_path"] += 1
                continue
            if not _inside(resolved, vault):
                skipped["outside_vault"] += 1
                continue
            candidates.append((path, resolved))

    paths: list[Path] = []
    seen_resolved_paths: set[str] = set()
    for path, resolved in sorted(
        candidates,
        key=lambda item: item[0].relative_to(vault).as_posix().casefold(),
    ):
        resolved_key = os.path.normcase(str(resolved))
        if resolved_key in seen_resolved_paths:
            skipped["duplicate_resolved_path"] += 1
            continue
        seen_resolved_paths.add(resolved_key)
        paths.append(path)
    return paths


def build_index(
    vault: Path,
    database: Path | None = None,
    policy: IndexPolicy | None = None,
    minimum_candidate_score: float = 0.16,
    max_candidates_per_note: int = 8,
    allow_in_vault: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"Vault is not a directory: {vault}")
    database = (database or default_database_for_vault(vault)).expanduser().resolve()
    database_in_vault = _inside(database, vault)
    if database_in_vault and not allow_in_vault:
        raise ValueError(
            "Refusing to place the RecallWeave database inside the vault. "
            "Use the default external location, choose --database outside the vault, "
            "or pass --allow-in-vault deliberately."
        )
    _validate_destination(database, force)
    policy = policy or IndexPolicy()

    notes: list[Note] = []
    skipped: Counter[str] = Counter()
    for path in _markdown_files(vault, skipped):
        relative = path.relative_to(vault).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            skipped["unreadable_path"] += 1
            continue
        allowed, reason = policy.path_allowed(relative, size)
        if not allowed:
            skipped[reason or "policy"] += 1
            continue
        try:
            note = parse_note(path, vault)
        except UnicodeError:
            skipped["unsupported_encoding"] += 1
            continue
        except RecursionError:
            skipped["unparseable_frontmatter"] += 1
            continue
        allowed, reason = policy.frontmatter_allowed(
            note.frontmatter,
            valid=note.frontmatter_valid,
        )
        if not allowed:
            skipped[reason or "frontmatter_policy"] += 1
            continue
        notes.append(note)

    database.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f"{database.name}.",
        suffix=".building",
        dir=database.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    handle.close()
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(temporary)
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("application_id", APPLICATION_ID),
                ("schema_version", SCHEMA_VERSION),
                ("indexed_at", _utc_now()),
                ("vault_fingerprint", "paths-relative-content-hashes-only"),
            ],
        )
        lookup = _insert_notes(connection, notes)
        explicit_edges, unresolved = _insert_explicit_edges(connection, notes, lookup)
        candidate_edges, discovery = _insert_candidate_edges(
            connection,
            minimum_score=minimum_candidate_score,
            max_per_note=max_candidates_per_note,
        )
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('discovery_diagnostics', ?)",
            (json.dumps(discovery, sort_keys=True),),
        )
        connection.commit()
    except Exception:
        if connection is not None:
            connection.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if connection is not None:
            connection.close()

    os.replace(temporary, database)
    with connect(database, readonly=True) as check:
        section_count = int(check.execute("SELECT COUNT(*) FROM sections").fetchone()[0])
        tag_count = int(check.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0])
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "index",
        "database": str(database),
        "notes_indexed": len(notes),
        "sections_indexed": section_count,
        "verified_edges": explicit_edges,
        "candidate_edges": candidate_edges,
        "note_tags": tag_count,
        "unresolved_links": unresolved,
        "discovery": discovery,
        "skipped": dict(sorted(skipped.items())),
        "network_calls": 0,
        "vault_writes": 1 if database_in_vault else 0,
    }
