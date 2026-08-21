from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .contract_spec import TaskSpec


def _normalize(value: str) -> str:
    return value.replace("\\", "/").casefold()


def _clean_tag(value: str) -> str:
    return value[1:] if value.startswith("#") else value


def _validate(name: str, values: Any) -> list[str]:
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list of strings.")
    for item in values:
        if not isinstance(item, str) or item == "":
            raise ValueError(f"{name} entries must be non-empty strings.")
    return list(values)


@dataclass(slots=True)
class ExclusionSet:
    paths: list[str] = field(default_factory=list)
    globs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.paths = _validate("paths", self.paths)
        self.globs = _validate("globs", self.globs)
        self.tags = _validate("tags", self.tags)
        self.directives = _validate("directives", self.directives)
        self.paths = [_normalize(path) for path in self.paths]
        self.globs = [_normalize(glob) for glob in self.globs]
        self.tags = [_clean_tag(tag).casefold() for tag in self.tags]
        for glob in self.globs:
            try:
                re.compile(fnmatch.translate(glob))
            except re.error as error:
                raise ValueError(f"Exclusion glob could not be compiled: {glob!r}") from error

    @classmethod
    def from_spec(cls, spec: TaskSpec) -> "ExclusionSet":
        return cls(
            paths=list(spec.exclusion_paths),
            globs=list(spec.exclusion_globs),
            tags=list(spec.exclusion_tags),
            directives=list(spec.exclusion_directives),
        )

    def excludes_path(self, relative_path: str) -> tuple[bool, str | None]:
        folded = _normalize(relative_path)
        if folded in self.paths:
            return True, "excluded_path"
        if any(fnmatch.fnmatch(folded, glob) for glob in self.globs):
            return True, "excluded_glob"
        return False, None

    def excludes_tags(self, tags: list[str]) -> tuple[bool, str | None]:
        probe = {_clean_tag(str(tag)).casefold() for tag in tags}
        if probe.intersection(self.tags):
            return True, "excluded_tag"
        return False, None

    def is_empty(self) -> bool:
        return not (self.paths or self.globs or self.tags or self.directives)
