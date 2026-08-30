from __future__ import annotations

"""Steward stage S5: one-shot read-only sweep driver + stewardship report + status.

``sweep_registry`` is a DRIVER, not a new stage: it contains no behavior that
is unreachable by running ``steward-observe``, ``steward-assess`` and
``steward-propose`` by hand, in that order, against the same state directory.
It is a literal composition of ``observe_registry``, ``assess_latest`` and
``propose_latest`` -- each of those stage functions manages its own
``StateLock`` exactly as it does when invoked directly; this module does not
wrap them in an outer lock or otherwise restructure their locking. All this
module adds is: running the three in sequence, then reading back the on-disk
artifacts they left in ``state_root`` (the latest change batch, assessment,
and every proposal file) to assemble one deterministic, machine-readable
``stewardship_report`` document, and writing that document under
``reports/``.

This is a strictly ONE-SHOT local command: there is no daemon, no polling
loop, and no long-running process. Nothing in this module or in
``cli.py``'s ``steward-sweep``/``steward-status`` subparsers may accept a
``--daemon``/``--serve``/``--watch``/``--interval`` flag.

Machine-readable result semantics (frozen for v1; see ``SWEEP_RESULTS`` and
``SWEEP_EXIT_CODES``):

- ``approval_required`` -- this sweep created at least one proposal, OR
  proposals from an earlier run are still pending in ``proposals/`` (v1 has
  no apply step, so every proposal that ever gets written stays "pending"
  until an operator deletes it or a future milestone adds apply).
- ``findings`` -- no proposals (created this run or pending from before), but
  at least one deterministic assessment relation was recorded across every
  source's latest assessment.
- ``no_change`` -- neither of the above.

``applied`` and ``validation_failed_rolled_back`` are reserved for a future
milestone (G2, real apply) and are structurally unreachable from this
module's v1 code paths: nothing in ``sweep_registry`` can construct or return
those two values. ``error`` is not returned by this module at all -- an
exception here propagates to the caller (the CLI's existing exception
handling turns any escaping ``OSError``/``ValueError`` into exit code 2), and
``SWEEP_EXIT_CODES["error"]`` documents the intended CLI exit code for that
already-existing path.

``status_report`` is a read-mostly companion command: it counts what is
currently on disk under ``state_root`` (change batches, assessments,
proposals, receipts, reports), reports the newest sweep report's result, the
steward lock's state, and how many bytes ``backups/`` holds. When
``prune_older_than_days`` is given it also deletes files older than that many
days from ``changes/``, ``assessments/`` and ``reports/`` -- and *only* those
three subdirectories; ``proposals/``, ``receipts/`` and ``backups/`` are never
pruned, since a pending proposal or a backup is never safe to discard by age
alone. Per the machine-local doctrine that runs through every steward
document, neither function ever emits ``state_root`` (or any other absolute
path) in its output; ``status_report`` reports only which subdirectories
exist and how many files they hold.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .index import connect
from .safe_write import is_link_like
from .steward_assess import DETERMINISTIC_RELATIONS, assess_latest
from .steward_observe import observe_registry
from .steward_propose import propose_latest
from .steward_sources import SourceRegistry
from .steward_state import (
    STEWARD_SCHEMA_VERSION,
    STEWARD_SUBDIRS,
    atomic_write_json,
    ensure_state_layout,
    ensure_state_root_outside_sources,
    guard_within,
    lock_state,
)

REPORT_KIND = "stewardship_report"
STATUS_KIND = "steward_status"

# Per-list ceiling on the evidence arrays copied into a stewardship report.
# Bounds report size deterministically; a truncated list is flagged with its
# full length under integrity.evidence_truncated so a consumer can tell a
# complete list from a Steward-truncated one.
REPORT_EVIDENCE_LIMIT = 1000

# Frozen for v1. Do not add, remove, or reorder without a corresponding audit
# of every caller that indexes SWEEP_EXIT_CODES by these exact strings.
SWEEP_RESULTS = (
    "no_change",
    "findings",
    "approval_required",
    "applied",
    "validation_failed_rolled_back",
    "error",
)
SWEEP_EXIT_CODES = {
    "no_change": 0,
    "findings": 3,
    "approval_required": 4,
    "applied": 5,
    "validation_failed_rolled_back": 6,
    "error": 2,
}

# The only three values sweep_registry's v1 code paths can ever produce.
# "applied" and "validation_failed_rolled_back" are reserved for G2 and are
# unreachable from this module; "error" surfaces via the exception path.
_V1_RESULTS = ("no_change", "findings", "approval_required")

# Pruning never touches proposals/, receipts/, or backups/: a pending
# proposal or a backup is never safe to discard by age alone. reports/ is
# pruned purely by age; changes/ and assessments/ are pruned only when their
# downstream stage is durably complete (see _fully_processed_artifact_names).

_REPORT_FORMATS = ("json", "markdown")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_timestamp(iso: str) -> str:
    value = datetime.fromisoformat(iso)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # Microsecond precision, matching steward_observe: report names from
    # back-to-back sweeps must not collide.
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _atomic_write_text(path: Path, text: str, *, within: Path | None = None) -> None:
    if within is not None:
        guard_within(path, within)
    """Write text atomically, mirroring steward_state.atomic_write_json."""
    if is_link_like(path):
        raise ValueError(f"Refusing to replace a symlink or junction: {path}")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error


def _source_name_from_artifact(name: str) -> str | None:
    """Exact source name from a ``<ts>-<source>.json`` artifact filename.

    The timestamp segment carries no hyphen, so the source is everything after
    the first hyphen -- this avoids the glob-suffix collision where ``*-a.json``
    also matches ``<ts>-x-a.json`` (source ``x-a``)."""

    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    _, sep, source = stem.partition("-")
    return source if sep else None


def _source_files(directory: Path, source_name: str) -> list[Path]:
    return sorted(
        p
        for p in directory.glob(f"*-{source_name}.json")
        if _source_name_from_artifact(p.name) == source_name
    )


def _latest_file(directory: Path, source_name: str) -> Path | None:
    matches = _source_files(directory, source_name)
    return matches[-1] if matches else None


# --- Report assembly: reads only what is on disk in state_root (plus the
# in-memory observe_receipt for the one transient fact -- "was a source
# missing on this run" -- that is never written to any file) so that a
# hand-run observe/assess/propose sequence and a sweep_registry call over the
# same fixture produce byte-comparable report sections.


def _aggregate_from_batches(
    dirs: dict[str, Path],
    registry: SourceRegistry,
    exclude_sources: set[str] | None = None,
) -> dict[str, Any]:
    changes: dict[str, dict[str, int]] = {}
    skipped_total: dict[str, int] = {}
    changed_during_observe_total = 0
    rename_candidates_pending = 0
    checkpoint_invalid: list[str] = []

    for source in registry.sources:
        if exclude_sources and source.name in exclude_sources:
            # A source that errored this run contributes nothing current; its
            # historical batches must not masquerade as this sweep's data.
            changes[source.name] = {"added": 0, "modified": 0, "removed": 0}
            continue
        latest = _latest_file(dirs["changes"], source.name)
        if latest is None:
            changes[source.name] = {"added": 0, "modified": 0, "removed": 0}
            continue
        batch = _load_json(latest)
        summary = batch.get("change_summary") or {}
        changes[source.name] = {
            "added": int(summary.get("added", 0)),
            "modified": int(summary.get("modified", 0)),
            "removed": int(summary.get("removed", 0)),
        }
        for reason, count in (batch.get("skipped") or {}).items():
            skipped_total[reason] = skipped_total.get(reason, 0) + int(count)
        changed_during_observe_total += len(batch.get("changed_during_observe") or [])
        rename_candidates_pending += len(batch.get("rename_candidates") or [])
        if batch.get("checkpoint_invalid"):
            checkpoint_invalid.append(source.name)

    return {
        "changes": changes,
        "skipped_total": dict(sorted(skipped_total.items())),
        "changed_during_observe": changed_during_observe_total,
        "rename_candidates_pending": rename_candidates_pending,
        "checkpoint_invalid": sorted(checkpoint_invalid),
    }


def _aggregate_assessments(
    dirs: dict[str, Path],
    registry: SourceRegistry,
    exclude_sources: set[str] | None = None,
) -> tuple[dict[str, int], list[str], list[str]]:
    summary: dict[str, int] = {
        "index_current": 0,
        "never_indexed": 0,
        "skipped_changed_during_observe": 0,
    }
    for relation in DETERMINISTIC_RELATIONS:
        summary[relation] = 0

    broken_citations: list[str] = []
    duplicates: list[str] = []
    # Reconcile to CURRENT state, not a union of history. assess_latest processes
    # every unassessed batch, so several retained assessments can describe the
    # same path at different times; unioning them would report contradictory,
    # stale findings (e.g. a DELETED that a later NEW already superseded) and
    # make the report depend on retention rather than the source's current
    # state. For each (source, relative_path), the LATEST assessment that
    # mentions it wins; a path only an older assessment mentions is preserved
    # (nothing newer changed it). Counts and evidence are then derived from that
    # reconciled current state, so they are consistent with each other.
    current_items: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for source in registry.sources:
        if exclude_sources and source.name in exclude_sources:
            continue
        # _source_files returns ascending (oldest-first) filename/timestamp
        # order, so later assessments overwrite earlier per-path state below.
        for assessment_path in _source_files(dirs["assessments"], source.name):
            assessment = _load_json(assessment_path)
            for key, value in (assessment.get("summary") or {}).items():
                # Relation counts are recomputed from the reconciled state below;
                # only the non-relation bookkeeping stats are carried here.
                if key in DETERMINISTIC_RELATIONS:
                    continue
                if isinstance(value, int) and not isinstance(value, bool):
                    summary[key] = summary.get(key, 0) + value
            this_assessment: dict[str, list[dict[str, Any]]] = {}
            for item in assessment.get("assessments") or []:
                path = item.get("relative_path")
                if isinstance(path, str):
                    this_assessment.setdefault(path, []).append(item)
            for path, items in this_assessment.items():
                # Latest assessment mentioning this path supersedes earlier state.
                current_items[(source.name, path)] = items

    distinct_relations: dict[str, set[tuple[str, str]]] = {
        relation: set() for relation in DETERMINISTIC_RELATIONS
    }
    for (source_name, path), items in current_items.items():
        for item in items:
            relation = item.get("relation")
            if relation in distinct_relations:
                distinct_relations[relation].add((source_name, path))
            if relation == "CITATION_BROKEN":
                for citation in (item.get("inputs") or {}).get("broken_citations") or []:
                    text = citation.get("citation") if isinstance(citation, dict) else None
                    if isinstance(text, str):
                        broken_citations.append(text)
            elif relation == "DUPLICATES_EXACT_BYTES":
                duplicates.append(path)

    for relation, keys in distinct_relations.items():
        summary[relation] = len(keys)

    return summary, sorted(set(broken_citations)), sorted(set(duplicates))


def _aggregate_proposals(
    dirs: dict[str, Path], registry_sha256: str | None = None
) -> tuple[int, dict[str, int], list[str]]:
    """Scan proposals/ for PENDING proposals (applied ones no longer pend).

    A proposal from a prior registry revision (foreign registry_sha256) is not
    this registry's pending work: it is excluded so a stale artifact cannot hold
    the sweep result at ``approval_required`` (auto-apply already skips it)."""
    by_action: dict[str, int] = {}
    dangling: set[str] = set()
    total = 0
    for path in sorted(dirs["proposals"].glob("*.json")):
        proposal = _load_json(path)
        if not isinstance(proposal, dict):
            # Valid JSON that is not an object is a malformed proposal; count it
            # as pending (it needs operator attention) rather than calling .get()
            # on a non-dict.
            total += 1
            continue
        if proposal.get("status") == "applied":
            continue
        if registry_sha256 is not None and (
            proposal.get("registry_sha256") != registry_sha256
        ):
            continue
        total += 1
        action = proposal.get("action")
        if isinstance(action, str):
            by_action[action] = by_action.get(action, 0) + 1
            if action == "review_dangling_references":
                deleted_path = (proposal.get("evidence") or {}).get("deleted_path")
                if isinstance(deleted_path, str):
                    dangling.add(deleted_path)
    return total, dict(sorted(by_action.items())), sorted(dangling)


def _index_info(database: Path) -> dict[str, Any]:
    connection = connect(database, readonly=True)
    try:
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM meta")
        }
    finally:
        connection.close()
    return {
        "indexed_at": meta.get("indexed_at"),
        "schema_version": meta.get("schema_version"),
    }


def _classify_result(*, pending_total: int, total_relations: int) -> str:
    if pending_total >= 1:
        result = "approval_required"
    elif total_relations >= 1:
        result = "findings"
    else:
        result = "no_change"
    assert result in _V1_RESULTS
    return result


def _assemble_report(
    registry: SourceRegistry,
    dirs: dict[str, Path],
    database: Path,
    *,
    generated_at: str,
    observe_receipt: dict[str, Any],
    proposals_created_this_sweep: int,
    apply_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources_errored = {
        item["source"]
        for item in observe_receipt.get("sources", [])
        if isinstance(item, dict) and item.get("error") is not None
    }
    batch_agg = _aggregate_from_batches(dirs, registry, sources_errored)
    relation_summary, broken_citations, duplicates = _aggregate_assessments(
        dirs, registry, sources_errored
    )
    proposals_pending_total, proposals_by_action, dangling_references = _aggregate_proposals(
        dirs, registry.registry_sha256
    )

    # Bound every unbounded evidence array so report size has a defined ceiling
    # and consumers can tell a complete list from a Steward-truncated one. The
    # budget is deterministic (stable ordering in, fixed cap) and each truncated
    # list is annotated with its full length; physical line-range citations in
    # the retained entries are untouched.
    evidence_truncation: dict[str, dict[str, int]] = {}

    def _bound(name: str, items: list[Any]) -> list[Any]:
        if len(items) > REPORT_EVIDENCE_LIMIT:
            evidence_truncation[name] = {
                "reported": REPORT_EVIDENCE_LIMIT,
                "total": len(items),
            }
            return items[:REPORT_EVIDENCE_LIMIT]
        return items

    broken_citations = _bound("broken_citations", broken_citations)
    dangling_references = _bound("dangling_references", dangling_references)
    duplicates = _bound("duplicates", duplicates)

    sources_missing = sorted(
        item["source"]
        for item in observe_receipt.get("sources", [])
        if isinstance(item, dict) and item.get("error") == "source_missing"
    )

    total_relations = sum(
        relation_summary.get(relation, 0) for relation in DETERMINISTIC_RELATIONS
    )
    if apply_summary is not None and apply_summary.get("failures"):
        result = "validation_failed_rolled_back"
    elif apply_summary is not None and apply_summary.get("applied"):
        result = "applied" if proposals_pending_total == 0 else "approval_required"
    else:
        result = _classify_result(
            pending_total=proposals_pending_total,
            total_relations=total_relations,
        )
    assert result in SWEEP_RESULTS

    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": REPORT_KIND,
        "operation": "steward_sweep",
        "generated_at": generated_at,
        "result": result,
        "registry_sha256": registry.registry_sha256,
        "integrity": {
            "broken_citations": broken_citations,
            "dangling_references": dangling_references,
            "duplicates": duplicates,
            "rename_candidates_pending": batch_agg["rename_candidates_pending"],
            "sources_missing": sources_missing,
            "checkpoint_invalid": batch_agg["checkpoint_invalid"],
            "evidence_truncated": evidence_truncation,
        },
        "changes": batch_agg["changes"],
        "assessments": relation_summary,
        "proposals": {
            "created_this_sweep": proposals_created_this_sweep,
            "pending_total": proposals_pending_total,
            "by_action": proposals_by_action,
        },
        "apply": apply_summary,
        "observe": {
            "skipped_total": batch_agg["skipped_total"],
            "changed_during_observe": batch_agg["changed_during_observe"],
        },
        "index": {
            "indexed_at": _index_info(database)["indexed_at"],
            "schema_version": _index_info(database)["schema_version"],
        },
        "network_calls": 0,
        # A read-only sweep performs no writes; an --apply sweep reports the
        # mutations its auto-apply leg actually made, so the standard receipt
        # field never claims a false no-mutation result after applying.
        "vault_writes": (
            apply_summary.get("mutations", 0) if apply_summary is not None else 0
        ),
    }


# --- Markdown projection: leads with the integrity section. Every string
# sourced from vault content (citations, relative paths) or from the
# operator-authored sources registry (source names) is rendered inside its
# own fenced code block, never interpolated inline, so it cannot be read as
# Markdown structure.

_FENCE_LANGUAGE = "text"


def _fenced(value: str) -> str:
    body = str(value)
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}{_FENCE_LANGUAGE}\n{body}\n{fence}"


def _fenced_list_section(title: str, items: list[str]) -> list[str]:
    lines = [f"### {title}", ""]
    if not items:
        lines.append("None recorded.")
        lines.append("")
        return lines
    for item in items:
        lines.append(_fenced(item))
    lines.append("")
    return lines


def render_sweep_markdown(report: dict[str, Any]) -> str:
    integrity = report.get("integrity") or {}
    lines: list[str] = [
        "# Stewardship report",
        "",
        f"- Result: `{report.get('result')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        "",
        "## Integrity",
        "",
    ]
    lines.extend(
        _fenced_list_section("Broken citations", integrity.get("broken_citations") or [])
    )
    lines.extend(
        _fenced_list_section(
            "Dangling references", integrity.get("dangling_references") or []
        )
    )
    lines.extend(_fenced_list_section("Duplicates", integrity.get("duplicates") or []))
    truncated = integrity.get("evidence_truncated") or {}
    if truncated:
        lines.append("### Evidence truncated")
        lines.append("")
        for name in sorted(truncated):
            info = truncated[name]
            lines.append(
                f"- {name}: showing {info.get('reported')} of "
                f"{info.get('total')}"
            )
        lines.append("")
    lines.append(
        f"Rename candidates pending: {integrity.get('rename_candidates_pending', 0)}"
    )
    lines.append("")
    lines.extend(
        _fenced_list_section("Sources missing", integrity.get("sources_missing") or [])
    )
    lines.extend(
        _fenced_list_section(
            "Checkpoint invalid", integrity.get("checkpoint_invalid") or []
        )
    )

    lines.append("## Changes")
    lines.append("")
    changes = report.get("changes") or {}
    if not changes:
        lines.append("None recorded.")
        lines.append("")
    for source_name in sorted(changes):
        totals = changes[source_name]
        lines.append("Source:")
        lines.append(_fenced(source_name))
        lines.append(
            f"- added: {totals.get('added', 0)}, "
            f"modified: {totals.get('modified', 0)}, "
            f"removed: {totals.get('removed', 0)}"
        )
        lines.append("")

    lines.append("## Assessments")
    lines.append("")
    assessments = report.get("assessments") or {}
    if not assessments:
        lines.append("None recorded.")
    for key in sorted(assessments):
        lines.append(f"- {key}: {assessments[key]}")
    lines.append("")

    lines.append("## Proposals")
    lines.append("")
    proposals = report.get("proposals") or {}
    lines.append(f"- created this sweep: {proposals.get('created_this_sweep', 0)}")
    lines.append(f"- pending total: {proposals.get('pending_total', 0)}")
    by_action = proposals.get("by_action") or {}
    for action in sorted(by_action):
        lines.append(f"- {action}: {by_action[action]}")
    lines.append("")

    lines.append("## Observe")
    lines.append("")
    observe = report.get("observe") or {}
    lines.append(f"- changed during observe: {observe.get('changed_during_observe', 0)}")
    skipped_total = observe.get("skipped_total") or {}
    for reason in sorted(skipped_total):
        lines.append(f"- skipped.{reason}: {skipped_total[reason]}")
    lines.append("")

    lines.append("## Index")
    lines.append("")
    index_info = report.get("index") or {}
    lines.append("Indexed at:")
    lines.append(_fenced(str(index_info.get("indexed_at"))))
    lines.append("Index schema version:")
    lines.append(_fenced(str(index_info.get("schema_version"))))
    lines.append("")

    return "\n".join(lines) + "\n"


def sweep_registry(
    registry: SourceRegistry,
    state_root: Path,
    database: Path,
    *,
    report_format: str = "json",
    apply: bool = False,
    write_policy: Any = None,
) -> dict[str, Any]:
    """Run observe -> assess -> propose over every source, then report.

    A literal composition: each stage function is called exactly as an
    operator would call it by hand (``steward-observe``, then
    ``steward-assess``, then ``steward-propose``, each against the same
    ``state_root``), and each manages its own ``StateLock``. The only thing
    this function adds is reading back what those three calls left on disk
    (plus the transient "source missing this run" fact from the observe
    receipt) to assemble and write one ``stewardship_report`` document under
    ``reports/``.
    """
    if report_format not in _REPORT_FORMATS:
        raise ValueError(
            f"Unsupported report_format {report_format!r}; "
            f"expected one of {_REPORT_FORMATS}."
        )
    state_root = Path(state_root)
    database = Path(database)
    generated_at = _utc_now()
    ensure_state_root_outside_sources(
        state_root, [source.root for source in registry.sources]
    )

    if apply and write_policy is None:
        raise ValueError(
            "steward-sweep --apply requires an explicit --write-policy; "
            "there is no permissive default."
        )

    observe_receipt = observe_registry(registry, state_root)
    assess_latest(registry, state_root, database)
    propose_receipt = propose_latest(registry, state_root, database)

    dirs = ensure_state_layout(state_root)

    apply_summary: dict[str, Any] | None = None
    if apply:
        # The --apply leg executes ONLY proposals whose every edit resolves
        # to auto_apply; everything else stays pending for steward-apply.
        # Imported here, matching the CLI's isolation of the apply module.
        from .steward_apply import sweep_auto_apply

        with lock_state(state_root):
            apply_summary = sweep_auto_apply(
                registry, dirs, database, write_policy=write_policy
            )
    report = _assemble_report(
        registry,
        dirs,
        database,
        generated_at=generated_at,
        observe_receipt=observe_receipt,
        proposals_created_this_sweep=propose_receipt["proposals_created"],
        apply_summary=apply_summary,
    )

    timestamp = _file_timestamp(generated_at)
    atomic_write_json(
        dirs["reports"] / f"{timestamp}-sweep.json",
        report,
        within=dirs["reports"],
    )
    if report_format == "markdown":
        _atomic_write_text(
            dirs["reports"] / f"{timestamp}-sweep.md",
            render_sweep_markdown(report),
            within=dirs["reports"],
        )
    return report


# --- steward-status ---------------------------------------------------


def _dir_file_count(directory: Path) -> int:
    return sum(1 for entry in directory.iterdir() if entry.is_file())


def _pending_proposal_count(
    directory: Path, registry_sha256: str | None = None
) -> int:
    # Applied proposals remain on disk with status "applied" (their receipt
    # references them); a pending count must exclude them, matching
    # _aggregate_proposals. A proposal from a prior registry revision (foreign
    # digest) is also excluded -- it is not this registry's pending work. An
    # unreadable/malformed proposal file is counted as pending -- it still needs
    # operator attention and must not silently vanish.
    pending = 0
    for entry in sorted(directory.glob("*.json")):
        try:
            document = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending += 1
            continue
        if isinstance(document, dict) and document.get("status") == "applied":
            continue
        if (
            registry_sha256 is not None
            and isinstance(document, dict)
            and document.get("registry_sha256") != registry_sha256
        ):
            continue
        pending += 1
    return pending


def _dir_total_bytes(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _prune_dir(
    directory: Path,
    cutoff_epoch: float,
    prunable_names: set[str] | None = None,
) -> int:
    if is_link_like(directory):
        raise ValueError(
            f"Refusing to prune through a symlinked directory: {directory}"
        )
    deleted = 0
    for entry in directory.iterdir():
        if is_link_like(entry) or not entry.is_file():
            continue
        # When an allow-set is given, only artifacts whose downstream stage is
        # durably complete may be pruned -- so an unassessed change batch, or an
        # assessed-but-unproposed batch/assessment, is never deleted (which
        # would permanently lose changes the checkpoint has already advanced
        # past).
        if prunable_names is not None and entry.name not in prunable_names:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_epoch:
            try:
                entry.unlink()
                deleted += 1
            except OSError:
                pass
    return deleted


# Deterministic relations that cause propose to compile a proposal. An
# assessment with none of these produces nothing downstream and is complete
# once written; one that has them is complete only after a proposal exists.
_PROPOSAL_ELIGIBLE_RELATIONS = (
    "DELETED",
    "CITATION_BROKEN",
    "DUPLICATES_EXACT_BYTES",
)


def _fully_processed_artifact_names(dirs: dict[str, Path]) -> set[str]:
    """Names of change-batch/assessment artifacts safe to prune.

    An artifact ``<ts>-<source>.json`` (the batch and its same-named assessment)
    is safe to prune only once propose has consumed it: either a proposal cites
    it, or its assessment produced no proposal-eligible relations at all. An
    unassessed batch is never in this set."""

    proposed: set[str] = set()
    for path in dirs["proposals"].glob("*.json"):
        try:
            document = _load_json(path)
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue
        for ref in document.get("assessment_refs") or []:
            if isinstance(ref, dict) and isinstance(ref.get("assessment_file"), str):
                proposed.add(ref["assessment_file"])

    complete: set[str] = set()
    for path in dirs["assessments"].glob("*.json"):
        try:
            document = _load_json(path)
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue
        summary = document.get("summary") or {}
        eligible = any(
            int(summary.get(rel, 0) or 0) > 0 for rel in _PROPOSAL_ELIGIBLE_RELATIONS
        )
        if not eligible or path.name in proposed:
            complete.add(path.name)
    return complete


def _lock_state(state_root: Path) -> dict[str, Any]:
    lock_path = state_root / "steward.lock"
    if not lock_path.exists():
        return {"present": False, "pid": None, "acquired_at": None}
    pid: Any = None
    acquired_at: Any = None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = data.get("pid")
        acquired_at = data.get("acquired_at")
    except (OSError, ValueError):
        pass
    return {"present": True, "pid": pid, "acquired_at": acquired_at}


def _newest_report(reports_dir: Path) -> dict[str, Any] | None:
    files = sorted(reports_dir.glob("*-sweep.json"))
    if not files:
        return None
    try:
        document = _load_json(files[-1])
    except ValueError:
        return None
    return {
        "generated_at": document.get("generated_at"),
        "result": document.get("result"),
    }


def status_report(
    state_root: Path,
    *,
    prune_older_than_days: int | None = None,
    source_roots: list[Path] | None = None,
    registry_sha256: str | None = None,
) -> dict[str, Any]:
    """Report counts and lock state for ``state_root``; optionally prune.

    Pruning (only when ``prune_older_than_days`` is given) runs under the
    steward state lock and deletes files older than that many days, by mtime,
    from ``changes/``, ``assessments/`` and ``reports/`` only -- never
    ``proposals/``, ``receipts/`` or ``backups/``. Counts reflect the state
    after any pruning.
    """
    if source_roots:
        ensure_state_root_outside_sources(state_root, source_roots)
    state_root = Path(state_root)
    generated_at = _utc_now()
    dirs = ensure_state_layout(state_root)

    pruned: dict[str, int] | None = None
    if prune_older_than_days is not None:
        if (
            not isinstance(prune_older_than_days, int)
            or isinstance(prune_older_than_days, bool)
            or prune_older_than_days < 0
        ):
            raise ValueError(
                "prune_older_than_days must be a non-negative integer; got "
                f"{prune_older_than_days!r}."
            )
        cutoff_epoch = datetime.now(timezone.utc).timestamp() - (
            prune_older_than_days * 86400
        )
        pruned = {}
        # Destructive pruning is serialized by the same lock the pipeline
        # stages use, so a concurrent run cannot lose its selected inputs.
        with lock_state(state_root):
            # Reports are terminal output: prune purely by age. Change batches
            # and assessments are pipeline inputs: prune only those whose
            # downstream stage is durably complete, so an unprocessed backlog is
            # never deleted after the checkpoint has advanced past it.
            complete = _fully_processed_artifact_names(dirs)
            pruned["reports"] = _prune_dir(dirs["reports"], cutoff_epoch)
            pruned["changes"] = _prune_dir(
                dirs["changes"], cutoff_epoch, prunable_names=complete
            )
            pruned["assessments"] = _prune_dir(
                dirs["assessments"], cutoff_epoch, prunable_names=complete
            )
        pruned["total"] = pruned["reports"] + pruned["changes"] + pruned["assessments"]

    counts = {
        "change_batches": _dir_file_count(dirs["changes"]),
        "assessments": _dir_file_count(dirs["assessments"]),
        "proposals_pending": _pending_proposal_count(
            dirs["proposals"], registry_sha256
        ),
        "receipts": _dir_file_count(dirs["receipts"]),
        "reports": _dir_file_count(dirs["reports"]),
    }

    return {
        "schema_version": STEWARD_SCHEMA_VERSION,
        "kind": STATUS_KIND,
        "operation": "steward_status",
        "generated_at": generated_at,
        "subdirs": {name: dirs[name].is_dir() for name in STEWARD_SUBDIRS},
        "counts": counts,
        "newest_report": _newest_report(dirs["reports"]),
        "lock": _lock_state(state_root),
        "backups_total_bytes": _dir_total_bytes(dirs["backups"]),
        "pruned": pruned,
        "network_calls": 0,
        "vault_writes": 0,
    }
