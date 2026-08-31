from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRITE_POLICY_SPEC_VERSION = "recallweave.steward.policy.v1"
POLICY_LEVELS = ("disabled", "propose_only", "require_approval", "auto_apply")
MUTATION_CLASSES = (
    "create_new_file",
    "append_at_eof",
    "replace_whole_section",
    "fix_unresolved_link",
    "move_to_trash",
)
MUTATION_CLASSES_SET = frozenset(MUTATION_CLASSES)
APPEND_ONLY_CLASSES = frozenset({"create_new_file", "append_at_eof"})
ALLOWED_POLICY_KEYS = {
    "spec_version",
    "default_level",
    "class_levels",
    "protected",
    "source_overrides",
    "require_git",
    "max_files_per_apply",
}
ALLOWED_PROTECTED_KEYS = {"paths", "globs", "path_terms", "frontmatter"}
SOURCE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")
PRINCIPAL_KEY_NAMES = frozenset(
    {
        "approver",
        "approvers",
        "assignee",
        "role",
        "roles",
        "user",
        "users",
        "account",
        "submitted_by",
        "requires_approval_from",
    }
)


def _reject_document_hazards(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PRINCIPAL_KEY_NAMES:
                raise ValueError(
                    f"Principal-like key {key!r} is not allowed in a write policy."
                )
            if key == "confidence" and isinstance(item, (int, float)) and not isinstance(
                item, bool
            ):
                raise ValueError(
                    "Numeric 'confidence' keys are not allowed in a write policy."
                )
            _reject_document_hazards(item)
    elif isinstance(value, list):
        for item in value:
            _reject_document_hazards(item)
    elif isinstance(value, str) and "://" in value:
        raise ValueError(f"Remote or URL value is not allowed in a write policy: {value!r}")


def _require_level(level: Any, *, where: str) -> None:
    if level not in POLICY_LEVELS:
        raise ValueError(
            f"Unknown policy level {level!r} in {where}; "
            f"expected one of {sorted(POLICY_LEVELS)}."
        )


def _check_class_level(cls: Any, level: Any, *, where: str) -> None:
    if cls not in MUTATION_CLASSES_SET:
        raise ValueError(f"Unknown mutation class {cls!r} in {where}.")
    _require_level(level, where=where)
    if level == "auto_apply" and cls not in APPEND_ONLY_CLASSES:
        raise ValueError(
            f"auto_apply is not allowed for mutation class {cls!r} in {where}; "
            f"only {sorted(APPEND_ONLY_CLASSES)} may be auto_apply."
        )


def _values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_values(item))
        return result
    return {
        item.strip().strip("\"'").casefold()
        for item in str(value).split(",")
        if item.strip()
    }


@dataclass(slots=True)
class WritePolicy:
    default_level: str = "propose_only"
    class_levels: dict[str, str] = field(default_factory=dict)
    protected_paths: list[str] = field(default_factory=list)
    protected_globs: list[str] = field(default_factory=list)
    protected_path_terms: list[str] = field(default_factory=list)
    protected_frontmatter: dict[str, list[str]] = field(default_factory=dict)
    source_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    require_git: bool = False
    max_files_per_apply: int = 20
    policy_sha256: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "WritePolicy":
        if not isinstance(payload, dict):
            raise ValueError("Write policy must be a JSON object.")
        _reject_document_hazards(payload)
        unknown = sorted(set(payload) - ALLOWED_POLICY_KEYS)
        if unknown:
            raise ValueError(f"Unknown write policy key(s): {', '.join(unknown)}")
        if payload.get("spec_version") != WRITE_POLICY_SPEC_VERSION:
            raise ValueError(
                f"Unsupported spec_version {payload.get('spec_version')!r}; "
                f"expected {WRITE_POLICY_SPEC_VERSION!r}."
            )

        default_level = payload.get("default_level", "propose_only")
        _require_level(default_level, where="default_level")
        if default_level == "auto_apply":
            # A blanket auto default would grant auto_apply to destructive
            # classes by inheritance; only explicit, append-only class grants
            # may ever be auto_apply.
            raise ValueError(
                "default_level may not be auto_apply; grant auto_apply "
                "explicitly per append-only mutation class instead."
            )

        class_levels = payload.get("class_levels", {})
        if not isinstance(class_levels, dict):
            raise ValueError("class_levels must be an object mapping mutation classes to levels.")
        for mutation_cls, level in class_levels.items():
            _check_class_level(mutation_cls, level, where="class_levels")

        protected = payload.get("protected", {})
        if not isinstance(protected, dict):
            raise ValueError("protected must be an object.")
        unknown_protected = sorted(set(protected) - ALLOWED_PROTECTED_KEYS)
        if unknown_protected:
            raise ValueError(f"Unknown protected key(s): {', '.join(unknown_protected)}")
        protected_paths: list[str] = []
        protected_globs: list[str] = []
        protected_path_terms: list[str] = []
        protected_frontmatter: dict[str, list[str]] = {}
        for key in ("paths", "globs", "path_terms"):
            if key in protected:
                items = protected[key]
                if not isinstance(items, list) or any(
                    not isinstance(item, str) for item in items
                ):
                    raise ValueError(f"protected.{key} must be a list of strings.")
                if key == "paths":
                    protected_paths = items
                elif key == "globs":
                    protected_globs = items
                else:
                    protected_path_terms = items
        if "frontmatter" in protected:
            frontmatter = protected["frontmatter"]
            if not isinstance(frontmatter, dict):
                raise ValueError("protected.frontmatter must be an object of string lists.")
            for fm_key, fm_values in frontmatter.items():
                if (
                    not isinstance(fm_key, str)
                    or not isinstance(fm_values, list)
                    or any(not isinstance(item, str) for item in fm_values)
                ):
                    raise ValueError(
                        "protected.frontmatter must be an object of string lists."
                    )
                protected_frontmatter[fm_key] = fm_values

        source_overrides = payload.get("source_overrides", {})
        if not isinstance(source_overrides, dict):
            raise ValueError(
                "source_overrides must be an object mapping source names to class-level objects."
            )
        parsed_overrides: dict[str, dict[str, str]] = {}
        for name, overrides in source_overrides.items():
            if not isinstance(name, str) or not SOURCE_NAME_PATTERN.fullmatch(name):
                raise ValueError(
                    "source override name may only contain [A-Za-z0-9._:-]."
                )
            if not isinstance(overrides, dict):
                raise ValueError(f"source override for {name!r} must be an object.")
            for mutation_cls, level in overrides.items():
                _check_class_level(mutation_cls, level, where=f"source override {name!r}")
            parsed_overrides[name] = dict(overrides)

        require_git = payload.get("require_git", False)
        if not isinstance(require_git, bool):
            raise ValueError("require_git must be a boolean.")

        max_files_per_apply = payload.get("max_files_per_apply", 20)
        if (
            not isinstance(max_files_per_apply, int)
            or isinstance(max_files_per_apply, bool)
            or max_files_per_apply <= 0
            or max_files_per_apply > 500
        ):
            raise ValueError(
                "max_files_per_apply must be a positive integer no greater than 500."
            )

        return cls(
            default_level=default_level,
            class_levels=dict(class_levels),
            protected_paths=protected_paths,
            protected_globs=protected_globs,
            protected_path_terms=protected_path_terms,
            protected_frontmatter=protected_frontmatter,
            source_overrides=parsed_overrides,
            require_git=require_git,
            max_files_per_apply=max_files_per_apply,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "WritePolicy":
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Write policy must be UTF-8 JSON.") from error
        policy = cls.from_payload(json.loads(text))
        policy.policy_sha256 = hashlib.sha256(payload).hexdigest()
        return policy

    @classmethod
    def from_file(cls, path: Path | None) -> "WritePolicy":
        if path is None:
            return cls()
        return cls.from_bytes(path.read_bytes())


def resolve_level(
    policy: WritePolicy,
    *,
    mutation_class: str,
    source_name: str | None,
    relative_path: str,
    frontmatter: dict | None = None,
) -> tuple[str, str]:
    folded = relative_path.replace("\\", "/").casefold()
    if folded in {path.casefold() for path in policy.protected_paths}:
        return "disabled", "protected:paths"
    if any(fnmatch.fnmatch(folded, glob.casefold()) for glob in policy.protected_globs):
        return "disabled", "protected:globs"
    if any(term.casefold() in folded for term in policy.protected_path_terms):
        return "disabled", "protected:path_terms"
    if policy.protected_frontmatter:
        fm = frontmatter if frontmatter is not None else {}
        for key, denied in policy.protected_frontmatter.items():
            if _values(fm.get(key)).intersection({v.casefold() for v in denied}):
                return "disabled", f"protected:frontmatter:{key}"
    if mutation_class not in MUTATION_CLASSES_SET:
        return "disabled", "unknown_class"
    if source_name is not None and source_name in policy.source_overrides:
        overrides = policy.source_overrides[source_name]
        if mutation_class in overrides:
            return overrides[mutation_class], "source_override"
    if mutation_class in policy.class_levels:
        return policy.class_levels[mutation_class], "class_level"
    level = policy.default_level
    # Defense in depth: no resolution path may hand auto_apply to a class
    # outside the append-only set, whatever a policy object claims.
    if level == "auto_apply" and mutation_class not in APPEND_ONLY_CLASSES:
        return "require_approval", "auto_apply_clamped"
    return level, "default"
