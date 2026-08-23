from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract_text import sanitize

CONTRACT_SPEC_VERSION = "recallweave.contract.spec.v1"

ALLOWED_SPEC_KEYS = {
    "spec_version",
    "task_id",
    "objective",
    "retrieval",
    "constraints",
    "prior_decisions",
    "acceptance_criteria",
    "exclusions",
}
ALLOWED_RETRIEVAL_KEYS = {"query", "limit", "include_candidates", "max_characters"}
ALLOWED_ITEM_KEYS = {"text", "note", "heading", "statement"}
ALLOWED_EXCLUSION_KEYS = {"paths", "globs", "tags", "directives"}

TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9._:\-]+")

DEFAULT_LIMIT = 8
DEFAULT_INCLUDE_CANDIDATES = False
DEFAULT_MAX_CHARACTERS = 8000

MAX_OBJECTIVE_CHARS = 2000
MAX_TASK_ID_CHARS = 128
MAX_QUERY_CHARS = 1000
MAX_ITEMS = 50
MAX_CRITERION_CHARS = 500
MAX_STATEMENT_CHARS = 500
MAX_DIRECTIVE_CHARS = 500
MAX_EXCLUSION_ENTRIES = 200


@dataclass(slots=True)
class SourceRef:
    text: str | None
    note: str | None
    heading: str | None
    statement: str | None


@dataclass(slots=True)
class TaskSpec:
    objective: str
    task_id: str | None
    query: str | None
    limit: int
    include_candidates: bool
    max_characters: int
    constraints: list[SourceRef]
    prior_decisions: list[SourceRef]
    acceptance_criteria: list[str]
    exclusion_paths: list[str]
    exclusion_globs: list[str]
    exclusion_tags: list[str]
    exclusion_directives: list[str]

    @classmethod
    def from_payload(cls, payload: Any) -> "TaskSpec":
        if not isinstance(payload, dict):
            raise ValueError("Task spec must be a JSON object.")
        unknown = sorted(set(payload) - ALLOWED_SPEC_KEYS)
        if unknown:
            raise ValueError(f"Unknown task spec key(s): {', '.join(unknown)}")

        if "spec_version" in payload:
            if payload["spec_version"] != CONTRACT_SPEC_VERSION:
                raise ValueError(
                    f"Unsupported spec_version {payload['spec_version']!r}; "
                    f"expected {CONTRACT_SPEC_VERSION!r}."
                )

        task_id = payload.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str):
                raise ValueError("task_id must be a string.")
            if len(task_id) > MAX_TASK_ID_CHARS:
                raise ValueError(
                    f"task_id must be at most {MAX_TASK_ID_CHARS} characters."
                )
            if not TASK_ID_PATTERN.fullmatch(task_id):
                raise ValueError("task_id may only contain [A-Za-z0-9._:-].")

        objective = payload.get("objective")
        if not isinstance(objective, str):
            raise ValueError("objective is required and must be a string.")
        if len(objective) < 1:
            raise ValueError("objective must not be empty.")
        if len(objective) > MAX_OBJECTIVE_CHARS:
            raise ValueError(
                f"objective must be at most {MAX_OBJECTIVE_CHARS} characters."
            )

        query: str | None = None
        limit = DEFAULT_LIMIT
        include_candidates = DEFAULT_INCLUDE_CANDIDATES
        max_characters = DEFAULT_MAX_CHARACTERS
        if "retrieval" in payload:
            retrieval = payload["retrieval"]
            if not isinstance(retrieval, dict):
                raise ValueError("retrieval must be a JSON object.")
            unknown = sorted(set(retrieval) - ALLOWED_RETRIEVAL_KEYS)
            if unknown:
                raise ValueError(f"Unknown retrieval key(s): {', '.join(unknown)}")
            if "query" not in retrieval:
                raise ValueError("retrieval requires a query.")
            raw_query = retrieval["query"]
            if not isinstance(raw_query, str):
                raise ValueError("retrieval.query must be a string.")
            if len(raw_query) < 1:
                raise ValueError("retrieval.query must not be empty.")
            if len(raw_query) > MAX_QUERY_CHARS:
                raise ValueError(
                    f"retrieval.query must be at most {MAX_QUERY_CHARS} characters."
                )
            query = raw_query
            if "limit" in retrieval:
                raw_limit = retrieval["limit"]
                if (
                    not isinstance(raw_limit, int)
                    or isinstance(raw_limit, bool)
                    or not (1 <= raw_limit <= 50)
                ):
                    raise ValueError("retrieval.limit must be an integer from 1 to 50.")
                limit = raw_limit
            if "include_candidates" in retrieval:
                raw_candidates = retrieval["include_candidates"]
                if not isinstance(raw_candidates, bool):
                    raise ValueError("retrieval.include_candidates must be a boolean.")
                include_candidates = raw_candidates
            if "max_characters" in retrieval:
                raw_max = retrieval["max_characters"]
                if (
                    not isinstance(raw_max, int)
                    or isinstance(raw_max, bool)
                    or not (1 <= raw_max <= 100000)
                ):
                    raise ValueError(
                        "retrieval.max_characters must be an integer from 1 to 100000."
                    )
                max_characters = raw_max

        constraints = cls._parse_items(payload.get("constraints", []), "constraints")
        prior_decisions = cls._parse_items(
            payload.get("prior_decisions", []), "prior_decisions"
        )

        acceptance_criteria = cls._parse_criteria(payload.get("acceptance_criteria", []))

        (
            exclusion_paths,
            exclusion_globs,
            exclusion_tags,
            exclusion_directives,
        ) = cls._parse_exclusions(payload.get("exclusions"))

        return cls(
            objective=objective,
            task_id=task_id,
            query=query,
            limit=limit,
            include_candidates=include_candidates,
            max_characters=max_characters,
            constraints=constraints,
            prior_decisions=prior_decisions,
            acceptance_criteria=acceptance_criteria,
            exclusion_paths=exclusion_paths,
            exclusion_globs=exclusion_globs,
            exclusion_tags=exclusion_tags,
            exclusion_directives=exclusion_directives,
        )

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Decode one JSON object, refusing a repeated key.

        `json.loads` silently keeps the LAST value for a repeated key, so a spec
        that visibly carries a restrictive rule can be overridden by a later
        duplicate:

            {"exclusions": {"paths": ["Secret.md"]}, "exclusions": {"paths": []}}

        A reviewer reading that artifact sees the exclusion; the exporter
        applies the empty one, includes the protected note, and still reports
        exclusions as enforced. The spec is the operator's authority over what
        may leave the vault, so it is read strictly: a key that appears twice is
        an error, not a last-one-wins."""
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(
                    f"Task spec contains a duplicate key {key!r}; a repeated "
                    "key silently overrides the earlier value, so the spec a "
                    "reader reviews would differ from the one applied."
                )
            seen.add(key)
        return dict(pairs)

    @classmethod
    def from_bytes(cls, payload: bytes) -> "TaskSpec":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Task spec must be UTF-8 JSON.") from error
        return cls.from_payload(
            json.loads(text, object_pairs_hook=cls._reject_duplicate_keys)
        )

    @classmethod
    def from_file(cls, path: Path) -> "TaskSpec":
        return cls.from_bytes(path.read_bytes())

    @classmethod
    def _parse_items(cls, value: Any, name: str) -> list[SourceRef]:
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list.")
        if len(value) > MAX_ITEMS:
            raise ValueError(f"{name} must contain at most {MAX_ITEMS} items.")
        result: list[SourceRef] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise ValueError(f"{name}[{index}] must be a JSON object.")
            unknown = sorted(set(item) - ALLOWED_ITEM_KEYS)
            if unknown:
                raise ValueError(f"{name}[{index}] unknown key(s): {', '.join(unknown)}")
            has_text = "text" in item
            has_note = "note" in item
            if has_text == has_note:
                raise ValueError(
                    f"{name}[{index}] must have exactly one of 'text' or 'note'."
                )
            # The discriminator above is key PRESENCE, so `{"text": null}`
            # selects the text branch. Skipping validation when the value is
            # None then produced a SourceRef with BOTH fields unset, and
            # contract construction later called _resolve_note(None) and raised
            # an uncaught AttributeError -- the CLI printed a traceback instead
            # of its structured error response, which is a break in the output
            # contract rather than a bad spec being reported. The SELECTED key's
            # value is validated unconditionally.
            text = item.get("text")
            note = item.get("note")
            if has_text and (not isinstance(text, str) or len(text) < 1):
                raise ValueError(f"{name}[{index}].text must be a non-empty string.")
            if has_note and (not isinstance(note, str) or len(note) < 1):
                raise ValueError(f"{name}[{index}].note must be a non-empty string.")
            heading = item.get("heading")
            if heading is not None and not isinstance(heading, str):
                raise ValueError(f"{name}[{index}].heading must be a string.")
            statement = item.get("statement")
            if statement is not None:
                if not isinstance(statement, str):
                    raise ValueError(f"{name}[{index}].statement must be a string.")
                if len(statement) > MAX_STATEMENT_CHARS:
                    raise ValueError(
                        f"{name}[{index}].statement must be at most "
                        f"{MAX_STATEMENT_CHARS} characters."
                    )
            result.append(
                SourceRef(text=text, note=note, heading=heading, statement=statement)
            )
        return result

    @classmethod
    def _parse_criteria(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("acceptance_criteria must be a list.")
        if len(value) > MAX_ITEMS:
            raise ValueError(
                f"acceptance_criteria must contain at most {MAX_ITEMS} items."
            )
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"acceptance_criteria[{index}] must be a string.")
            if len(item) < 1:
                raise ValueError(f"acceptance_criteria[{index}] must not be empty.")
            if len(item) > MAX_CRITERION_CHARS:
                raise ValueError(
                    f"acceptance_criteria[{index}] must be at most "
                    f"{MAX_CRITERION_CHARS} characters."
                )
            result.append(item)
        return result

    @classmethod
    def _parse_exclusions(
        cls, value: Any
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        if value is None:
            return [], [], [], []
        if not isinstance(value, dict):
            raise ValueError("exclusions must be a JSON object.")
        unknown = sorted(set(value) - ALLOWED_EXCLUSION_KEYS)
        if unknown:
            raise ValueError(f"Unknown exclusions key(s): {', '.join(unknown)}")
        paths = cls._parse_string_list(
            value.get("paths", []), "exclusions.paths", max_count=MAX_EXCLUSION_ENTRIES
        )
        globs = cls._parse_string_list(
            value.get("globs", []), "exclusions.globs", max_count=MAX_EXCLUSION_ENTRIES
        )
        tags = cls._parse_string_list(
            value.get("tags", []), "exclusions.tags", max_count=MAX_EXCLUSION_ENTRIES
        )
        directives = cls._parse_string_list(
            value.get("directives", []),
            "exclusions.directives",
            max_count=MAX_ITEMS,
            max_len=MAX_DIRECTIVE_CHARS,
        )
        return paths, globs, tags, directives

    @staticmethod
    def _parse_string_list(
        value: Any, name: str, *, max_count: int, max_len: int | None = None
    ) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a list of strings.")
        if len(value) > max_count:
            raise ValueError(f"{name} must contain at most {max_count} items.")
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"{name}[{index}] must be a string.")
            if max_len is not None and len(item) > max_len:
                raise ValueError(
                    f"{name}[{index}] must be at most {max_len} characters."
                )
            # An exclusion selector must be exactly what the contract will match
            # AND display. One containing a character sanitization removes would
            # be matched raw and shown clean, so the artifact could report an
            # exclusion that never applied. ExclusionSet enforces this too, at
            # build time; rejecting here reports it against the spec, where the
            # operator can see which entry is at fault.
            if sanitize(item) != item:
                raise ValueError(
                    f"{name}[{index}] must not contain control, bidi or "
                    "zero-width characters: what it matches and what the "
                    "contract displays would differ."
                )
        return value
