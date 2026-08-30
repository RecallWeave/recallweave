from __future__ import annotations

"""Steward's optional git wrapper.

Git is an ADDITIONAL record of an apply, never the primary rollback -- the
apply journal and the state-directory backups (see ``steward_apply.py``) stay
authoritative for recovery. This module is a thin subprocess client over the
operator's own ``git`` binary (located with ``shutil.which``); it carries no
git library dependency and never runs a subcommand that talks to another
repository or rewrites history. It only ever reads status, stages the exact
files an apply just wrote, and creates one new commit on the current branch.

This is the one steward module allowed to use ``subprocess``
(``tests/test_dependency_posture.py`` enforces that boundary), and a
dedicated test greps this file for the disallowed network subcommands.

Every guarantee ``steward_apply.py`` makes to a plain, non-git folder holds
identically here: a caller that never gets a git repository back from
``check_apply_preconditions`` sees no behavior change at all.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 30

_ENV_OVERRIDES_TO_STRIP = ("GIT_DIR", "GIT_WORK_TREE")


class GitError(ValueError):
    """A git precondition failed, or the git binary misbehaved."""


def git_available() -> bool:
    return shutil.which("git") is not None


def _clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _ENV_OVERRIDES_TO_STRIP
    }


def _run_git(
    args: list[str], root: Path, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run one git invocation under ``root``.

    Every call is captured, text-decoded, time-bounded, and stripped of the
    two environment overrides that could redirect it at a different work
    tree than the one the caller asked about."""

    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(
            f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS}s "
            f"in {root}."
        ) from error
    except OSError as error:
        raise GitError(
            f"git {' '.join(args)} could not be started: {error}"
        ) from error
    if check and result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-10:])
        raise GitError(
            f"git {' '.join(args)} failed (exit {result.returncode}) in "
            f"{root}: {tail}"
        )
    return result


def _hooks_dir(root: Path) -> Path | None:
    result = _run_git(["rev-parse", "--git-path", "hooks"], root, check=False)
    if result.returncode != 0:
        return None
    reported = result.stdout.strip()
    if not reported:
        return None
    # ``--git-path`` returns a path relative to ``root`` unless a
    # ``core.hooksPath`` override is itself absolute; the ``/`` operator
    # already does the right thing in both cases.
    return root / reported


def _has_content_rewriting_hooks(root: Path) -> bool:
    hooks_dir = _hooks_dir(root)
    if hooks_dir is None:
        return False
    pre_commit = hooks_dir / "pre-commit"
    try:
        return pre_commit.is_file() and os.access(pre_commit, os.X_OK)
    except OSError:
        return False


def _parse_porcelain_z_paths(output: str) -> list[str]:
    """Parse `git status --porcelain=v1 -z` output.

    NUL-delimited records: "XY <path>\0" ordinarily, and for renames/copies
    "XY <new>\0<old>\0". No C-style quoting is applied in -z mode, so paths
    with quotes, backslashes, newlines, or " -> " arrive as literal bytes."""

    paths: list[str] = []
    records = output.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        paths.append(record[3:])
        if status and status[0] in ("R", "C") and index < len(records):
            # The old path follows as its own NUL-delimited record.
            old_path = records[index]
            index += 1
            if old_path:
                paths.append(old_path)
    return paths


def repo_status(root: Path) -> dict[str, Any] | None:
    """Describe the git work tree containing ``root``, or ``None``.

    ``None`` covers both "git is unavailable" and "``root`` is not inside a
    git work tree" -- callers that only care about the additive-record
    behavior treat both identically."""

    root = Path(root)
    if not git_available():
        return None

    inside = _run_git(
        ["rev-parse", "--is-inside-work-tree"], root, check=False
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None

    toplevel = _run_git(["rev-parse", "--show-toplevel"], root)
    repo_root = toplevel.stdout.strip()

    branch_result = _run_git(
        ["symbolic-ref", "--short", "-q", "HEAD"], root, check=False
    )
    detached = branch_result.returncode != 0
    branch = branch_result.stdout.strip() if not detached else None

    head_result = _run_git(["rev-parse", "--verify", "HEAD"], root, check=False)
    head = head_result.stdout.strip() if head_result.returncode == 0 else None

    status_result = _run_git(
        ["-c", "status.relativePaths=false", "status", "--porcelain=v1", "-z"], root
    )
    dirty_paths = _parse_porcelain_z_paths(status_result.stdout)

    return {
        "head": head,
        "branch": branch,
        "detached": detached,
        "dirty_paths": dirty_paths,
        "repo_root": repo_root,
        "has_content_rewriting_hooks": _has_content_rewriting_hooks(root),
    }


def check_apply_preconditions(
    root: Path, touched_relative_paths: list[str], *, require_git: bool
) -> dict[str, Any]:
    """Refuse an apply whose git state would make the record ambiguous.

    Returns the fields the apply receipt needs. When ``root`` is not a git
    work tree (or git is unavailable), that is a normal, silent case unless
    ``require_git`` demands one."""

    root = Path(root)
    status = repo_status(root)
    if status is None:
        if require_git:
            raise GitError(
                f"Write policy sets require_git; {root} is not inside a git "
                "work tree (or the git binary is unavailable). Initialize a "
                "git repository there, or turn require_git off."
            )
        return {"git_used": False, "head": None, "branch": None}

    if status["detached"]:
        raise GitError(
            f"Refusing to apply: {root} has a detached HEAD; the apply "
            "commit would have no branch to live on. Check out a branch "
            "first."
        )
    if status["has_content_rewriting_hooks"]:
        raise GitError(
            f"Refusing to apply: {root} has an executable pre-commit hook, "
            "which could rewrite bytes this apply already hashed and "
            "validated. Remove or disable the hook first."
        )

    repo_root = Path(status["repo_root"])
    try:
        prefix = root.resolve().relative_to(repo_root.resolve())
    except ValueError:
        prefix = Path(".")
    dirty = set(status["dirty_paths"])
    overlap = []
    for touched in touched_relative_paths:
        candidates = {touched}
        if str(prefix) not in ("", "."):
            candidates.add((prefix / touched).as_posix())
        if candidates & dirty:
            overlap.append(touched)
    if overlap:
        raise GitError(
            "Refusing to apply: these edit targets already have uncommitted "
            f"changes: {sorted(overlap)}. Commit or stash them before "
            "applying, so the apply's own commit is unambiguous."
        )

    return {"git_used": True, "head": status["head"], "branch": status["branch"]}


def commit_applied(
    root: Path,
    touched_relative_paths: list[str],
    *,
    proposal_id: str,
    journal_ref: str,
) -> dict[str, Any]:
    """Stage exactly the touched paths and commit them.

    Preconditions (clean HEAD, no rewriting hook) were already checked by
    ``check_apply_preconditions``; ``--no-verify`` guards only against a hook
    appearing in the window between that check and this commit. Raises
    ``GitError`` on any failure -- the caller treats that as non-fatal
    because the apply itself already succeeded and is journaled."""

    root = Path(root)
    _run_git(["add", "--"] + list(touched_relative_paths), root)

    identity_args: list[str] = []
    email_result = _run_git(["config", "user.email"], root, check=False)
    if email_result.returncode != 0 or not email_result.stdout.strip():
        identity_args = [
            "-c",
            "user.name=RecallWeave Steward",
            "-c",
            "user.email=steward@localhost",
        ]

    message = (
        f"steward-apply {proposal_id}\n\n"
        f"Journal: {journal_ref}\n"
        "Automated, operator-approved steward apply."
    )
    # Pathspec-limited commit: only the touched paths enter this commit,
    # even if unrelated content was already staged before the apply ran.
    _run_git(
        identity_args
        + ["commit", "--no-verify", "-m", message, "--"]
        + list(touched_relative_paths),
        root,
    )

    sha_result = _run_git(["rev-parse", "HEAD"], root)
    return {"committed": True, "commit": sha_result.stdout.strip()}
