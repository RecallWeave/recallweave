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
import stat
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
    atomic_write_bytes,
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
# Per-array character budget for report evidence, so a handful of very long
# entries cannot blow the report size even under the element-count cap.
REPORT_EVIDENCE_CHAR_BUDGET = 200_000

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
    """Write text atomically via the shared descriptor-relative state writer.

    Using ``atomic_write_bytes`` (not a private pathname-based ``mkstemp``) means
    a Markdown report gets the same within-anchored, symlink-race-proof write as
    JSON state: the reports directory swapped for a symlink after ``guard_within``
    cannot redirect the temp file or the final report outside the state tree
    (e.g. into a vault)."""
    atomic_write_bytes(path, text.encode("utf-8"), within=within)


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
    # Durable current-state model. Assessments are INCREMENTAL (each is computed
    # from one change batch, not a full rescan), so a finding must PERSIST until
    # the affected path is reassessed -- a later, unrelated batch must not erase
    # an unresolved broken citation or duplicate. For each (source, path) the
    # latest assessment that MENTIONS it wins (that batch reassessed the path);
    # a path no later batch mentions keeps its finding. Each path also records
    # the batch index at which its state was set, so a DUPLICATES relationship
    # can be invalidated when a PARTICIPANT (not an unrelated note) is later
    # reassessed: a duplicate finding is dropped if any of its partner paths has
    # a newer state than the finding itself.
    current: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}

    for source in registry.sources:
        if exclude_sources and source.name in exclude_sources:
            continue
        for index, assessment_path in enumerate(
            _source_files(dirs["assessments"], source.name)
        ):
            assessment = _load_json(assessment_path)
            if not isinstance(assessment, dict):
                continue
            # Bind to the active registry: an assessment recorded under a
            # different (or, when one is active, a null) source registry -- e.g.
            # a same-named source repointed after a registry change -- must never
            # leak its paths/citations/relations into this registry's report.
            if registry.registry_sha256 is not None and (
                assessment.get("registry_sha256") != registry.registry_sha256
            ):
                continue
            for key, value in (assessment.get("summary") or {}).items():
                # Relation counts are recomputed from the durable state below;
                # only the informational bookkeeping stats are carried here.
                if key in DETERMINISTIC_RELATIONS:
                    continue
                if isinstance(value, int) and not isinstance(value, bool):
                    summary[key] = summary.get(key, 0) + value
            by_path: dict[str, list[dict[str, Any]]] = {}
            for item in assessment.get("assessments") or []:
                if not isinstance(item, dict):
                    continue
                path = item.get("relative_path")
                if isinstance(path, str):
                    by_path.setdefault(path, []).append(item)
            # Update state for EVERY path this batch reassessed -- both those
            # that produced relations and those that did not (covered_paths).
            # A covered path with no relations sets an empty item list, which
            # CLEARS any prior finding for it (a resolved citation/deletion is
            # no longer reported once its note is reassessed to nothing).
            covered = set(by_path)
            for path in assessment.get("covered_paths") or []:
                if isinstance(path, str):
                    covered.add(path)
            for path in covered:
                current[(source.name, path)] = (index, by_path.get(path, []))

    current_index = {key: idx for key, (idx, _items) in current.items()}

    def _duplicate_still_current(source_name: str, idx: int, item: dict) -> bool:
        # Valid only if no partner was reassessed AFTER this finding (which would
        # mean the participant changed and the pairing may no longer hold).
        inputs = item.get("inputs") or {}
        partners = list(inputs.get("duplicate_of") or []) + list(
            inputs.get("duplicate_in_batch") or []
        )
        for partner in partners:
            if not isinstance(partner, str):
                continue
            partner_idx = current_index.get((source_name, partner))
            if partner_idx is not None and partner_idx > idx:
                return False
        return True

    relation_counts: dict[str, set[tuple[str, str]]] = {
        relation: set() for relation in DETERMINISTIC_RELATIONS
    }
    for (source_name, path), (idx, items) in current.items():
        for item in items:
            relation = item.get("relation")
            if relation == "DUPLICATES_EXACT_BYTES":
                if not _duplicate_still_current(source_name, idx, item):
                    continue
                relation_counts["DUPLICATES_EXACT_BYTES"].add((source_name, path))
                duplicates.append(path)
            elif relation == "CITATION_BROKEN":
                relation_counts["CITATION_BROKEN"].add((source_name, path))
                for citation in (item.get("inputs") or {}).get("broken_citations") or []:
                    text = citation.get("citation") if isinstance(citation, dict) else None
                    if isinstance(text, str):
                        broken_citations.append(text)
            elif relation in relation_counts:
                relation_counts[relation].add((source_name, path))

    for relation, keys in relation_counts.items():
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
        # Enforce BOTH a element-count cap and a deterministic character budget:
        # a few filesystem-length paths or citations could otherwise blow the
        # report size even under the count cap. Keep whole entries in order until
        # either limit is reached; annotate the full total whenever anything is
        # omitted.
        kept: list[Any] = []
        used = 0
        for item in items:
            if len(kept) >= REPORT_EVIDENCE_LIMIT:
                break
            # Serialized (JSON, ensure_ascii) length -- what actually lands in
            # the report -- plus one for the separator. The budget applies to
            # EVERY entry including the first, so a single oversized entry is
            # omitted and flagged rather than admitted unbounded.
            length = len(json.dumps(item, ensure_ascii=True)) + 1
            if used + length > REPORT_EVIDENCE_CHAR_BUDGET:
                break
            kept.append(item)
            used += length
        if len(kept) < len(items):
            evidence_truncation[name] = {
                "reported": len(kept),
                "total": len(items),
            }
        return kept

    sources_missing = sorted(
        item["source"]
        for item in observe_receipt.get("sources", [])
        if isinstance(item, dict) and item.get("error") == "source_missing"
    )

    # Bound EVERY integrity evidence array (not just the assessment-derived
    # ones): a registry with very many missing or checkpoint-invalid sources
    # must not grow the report past the ceiling either.
    broken_citations = _bound("broken_citations", broken_citations)
    dangling_references = _bound("dangling_references", dangling_references)
    duplicates = _bound("duplicates", duplicates)
    sources_missing = _bound("sources_missing", sources_missing)
    checkpoint_invalid = _bound("checkpoint_invalid", batch_agg["checkpoint_invalid"])

    total_relations = sum(
        relation_summary.get(relation, 0) for relation in DETERMINISTIC_RELATIONS
    )
    # Only a failure that actually crossed the mutation boundary (a completed or
    # failed rollback, or a post-mutation persistence fault) is a
    # validation_failed_rolled_back. A pure preflight refusal mutated nothing and
    # rolled nothing back -- the proposal simply stays pending -- so it must not
    # claim a rollback that never happened (which would also repeat every run).
    mutation_failures = [
        failure
        for failure in (apply_summary.get("failures") or [])
        if not failure.get("preflight_refused")
    ] if apply_summary is not None else []
    if mutation_failures:
        result = "validation_failed_rolled_back"
    elif apply_summary is not None and apply_summary.get("applied"):
        result = "applied" if proposals_pending_total == 0 else "approval_required"
    else:
        result = _classify_result(
            pending_total=proposals_pending_total,
            total_relations=total_relations,
        )
    # A source that could not be observed (missing, symlinked, or identity-
    # changed root) means this sweep did NOT inspect it. That must not read as a
    # clean run: elevate a would-be no_change to findings (non-zero exit) so a
    # scheduled sweep cannot report false success while a source went unchecked.
    if sources_errored and result == "no_change":
        result = "findings"
    assert result in SWEEP_RESULTS

    # Bound the embedded apply detail lists the same way as the integrity
    # arrays: sweep_auto_apply records one `skipped` entry per non-auto proposal
    # and per proposal beyond the apply cap, so a large pending backlog could
    # otherwise grow the report unbounded with no truncation flag. Bound a COPY
    # for the report only -- result classification and vault_writes above used
    # the FULL summary, so truncating the display can never hide a mutation
    # failure or undercount mutations.
    apply_for_report = apply_summary
    if apply_summary is not None:
        apply_for_report = dict(apply_summary)
        for detail_key in ("applied", "skipped", "failures"):
            apply_for_report[detail_key] = _bound(
                f"apply.{detail_key}", apply_summary.get(detail_key) or []
            )

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
            "checkpoint_invalid": checkpoint_invalid,
            "evidence_truncated": evidence_truncation,
        },
        "changes": batch_agg["changes"],
        "assessments": relation_summary,
        "proposals": {
            "created_this_sweep": proposals_created_this_sweep,
            "pending_total": proposals_pending_total,
            "by_action": proposals_by_action,
        },
        "apply": apply_for_report,
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


_DIR_FD_PRUNE = (
    os.open in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.listdir in getattr(os, "supports_fd", set())
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


def _delete_prunable_entry(
    dir_fd: int, name: str, expected: os.stat_result, cutoff_epoch: float
) -> bool:
    """Delete ``name`` in ``dir_fd`` ONLY if it still identifies the exact old,
    regular file we inspected -- closing the stat-to-unlink leaf race.

    POSIX cannot unlink by inode, so quarantine-and-verify: atomically rename the
    current target of ``name`` aside, fstat the moved inode, and unlink it only
    when its (dev, ino) still matches what we stat'd AND it is still a regular
    file older than the cutoff. If the name was repointed to a different inode
    between the stat and the rename, restore it (rename back, overwriting any
    plant -- pruning holds the exclusive StateLock, so the only writer that could
    have repointed it is not a legitimate one) and delete nothing."""

    quarantine = f".steward-prune-quarantine.{expected.st_dev}.{expected.st_ino}"
    try:
        os.rename(name, quarantine, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        return False
    try:
        moved = os.stat(quarantine, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if (
        moved.st_dev == expected.st_dev
        and moved.st_ino == expected.st_ino
        and stat.S_ISREG(moved.st_mode)
        and moved.st_mtime < cutoff_epoch
    ):
        try:
            os.unlink(quarantine, dir_fd=dir_fd)
            return True
        except OSError:
            return False
    # Not the inode we verified as prunable: put it back and delete nothing.
    try:
        os.rename(quarantine, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        pass
    return False


def _prune_in_dir(
    dir_fd: int, cutoff_epoch: float, prunable_names: set[str] | None
) -> int:
    """Prune old regular files in an ALREADY-OPEN directory descriptor. Every
    stat/rename/unlink is relative to ``dir_fd`` (which the caller pinned
    O_NOFOLLOW), and each deletion is inode-verified (see
    ``_delete_prunable_entry``)."""

    deleted = 0
    for name in os.listdir(dir_fd):
        if name.startswith(".steward-prune-quarantine."):
            continue  # never re-process a stale quarantine temp as an artifact
        if prunable_names is not None and name not in prunable_names:
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue  # skip symlinks, directories, and special files
        if info.st_mtime >= cutoff_epoch:
            continue
        if _delete_prunable_entry(dir_fd, name, info, cutoff_epoch):
            deleted += 1
    return deleted


def _prune_child_fd(
    state_root_fd: int,
    name: str,
    cutoff_epoch: float,
    prunable_names: set[str] | None = None,
) -> int:
    """Prune one state subdirectory RELATIVE to a held state-root descriptor, so
    the whole destructive prune operates on one pinned state-root identity."""
    try:
        dir_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=state_root_fd,
        )
    except FileNotFoundError:
        return 0
    except OSError as error:
        raise ValueError(
            f"Refusing to prune a symlinked or missing state subdirectory "
            f"{name} ({type(error).__name__})"
        ) from error
    try:
        return _prune_in_dir(dir_fd, cutoff_epoch, prunable_names)
    finally:
        os.close(dir_fd)


def _prune_dir(
    directory: Path,
    cutoff_epoch: float,
    prunable_names: set[str] | None = None,
) -> int:
    # When an allow-set is given, only artifacts whose downstream stage is
    # durably complete may be pruned -- so an unassessed change batch, or an
    # assessed-but-unproposed batch/assessment, is never deleted (which would
    # permanently lose changes the checkpoint has already advanced past).
    if is_link_like(directory):
        raise ValueError(
            f"Refusing to prune through a symlinked directory: {directory}"
        )

    if _DIR_FD_PRUNE:
        # Anchor at the PARENT (the state root) and open the target directory
        # RELATIVE to the parent's O_NOFOLLOW descriptor. Opening the target with
        # O_NOFOLLOW alone protects only its final component: a state root (the
        # parent) swapped for a symlink would still be followed, letting pruning
        # enumerate and unlink files in an external target (a vault, say). Opening
        # the parent O_NOFOLLOW refuses a swapped state root, and the target is
        # then opened relative to that pinned inode. (status_report's prune holds
        # ONE state-root descriptor across the whole operation via _prune_child_fd;
        # this path serves direct/standalone callers.)
        parent = directory.parent
        try:
            parent_fd = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as error:
            raise ValueError(
                f"Refusing to prune through a symlinked or missing state root "
                f"{parent} ({type(error).__name__})"
            ) from error
        try:
            return _prune_child_fd(parent_fd, directory.name, cutoff_epoch, prunable_names)
        finally:
            os.close(parent_fd)

    # No dir_fd primitives (e.g. Windows without them): unlike the create-only,
    # journaled WRITE fallbacks, pruning DELETES with no backup or rollback, so
    # the check-to-unlink race cannot be tolerated -- a directory swapped for a
    # symlink/junction after the check would let pathname iteration/unlink delete
    # files in its external target (a vault). Fail closed. (status_report gates on
    # _DIR_FD_PRUNE and never reaches this, but keep _prune_dir itself safe.)
    raise ValueError(
        f"Refusing to prune {directory}: descriptor-relative deletion is "
        "unavailable on this platform, so pruning by pathname could delete files "
        "outside the state tree through a directory swapped for a symlink or "
        "junction."
    )


# Deterministic relations that cause propose to compile a proposal. An
# assessment with none of these produces nothing downstream and is complete
# once written; one that has them is complete only after a proposal exists.
_PROPOSAL_ELIGIBLE_RELATIONS = (
    "DELETED",
    "CITATION_BROKEN",
    "DUPLICATES_EXACT_BYTES",
)


def _open_state_child_fd(state_root_fd: int, name: str) -> int | None:
    """Open a state subdirectory RELATIVE to a pinned state-root descriptor
    (O_NOFOLLOW). Returns None when absent; raises ValueError when it is a
    symlink or otherwise not an openable directory."""
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=state_root_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(
            f"Refusing to read a symlinked state subdirectory {name} "
            f"({type(error).__name__})"
        ) from error


def _read_json_at(dir_fd: int, name: str) -> Any:
    """Read and parse a JSON file RELATIVE to a pinned directory descriptor
    (O_NOFOLLOW). Returns None on any read/parse failure or a symlinked leaf."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _open_state_root_fd(state_root: Path) -> int:
    """Open the state root O_NOFOLLOW, refusing a symlinked root. The returned
    descriptor PINS the state-root inode: every read/deletion done relative to it
    targets that one inode for the whole prune, so a state root renamed away and
    replaced by another (even non-symlink) directory between operations cannot
    redirect them.

    Trust boundary (deliberate, and identical to the state WRITE path in
    steward_state.atomic_write_bytes): steward validates and anchors the state
    root and EVERYTHING BELOW it -- O_NOFOLLOW refuses a symlinked state root, the
    held descriptor pins its inode, and every subdirectory/leaf is opened
    relative to it O_NOFOLLOW with inode-verified deletion. The state root's OWN
    ancestor directories are treated as trusted: an attacker able to swap one of
    them for a symlink already has write access to the state tree's parent and
    can tamper with state directly, so validating the full ancestor chain from /
    would add no real protection -- and cannot be done in general anyway, since
    legitimate state-dir paths traverse OS symlinks (e.g. /tmp and /var on macOS)
    that an O_NOFOLLOW descent from / would wrongly reject. This matches the
    boundary every other steward state read and write already relies on."""
    try:
        return os.open(
            state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as error:
        raise ValueError(
            f"Refusing to prune through a symlinked or missing state root "
            f"{state_root} ({type(error).__name__})"
        ) from error


def _marked_assessment_names(docs: list[Any], registry_sha256: str | None) -> set[str]:
    marked: set[str] = set()
    for document in docs:
        if not isinstance(document, dict):
            continue
        if document.get("kind") != "propose_marker":
            continue
        if registry_sha256 is not None and (
            document.get("registry_sha256") != registry_sha256
        ):
            continue
        name = document.get("assessment")
        if isinstance(name, str):
            marked.add(name)
    return marked


def _classify_complete(
    marked: set[str],
    assessment_docs: list[tuple[str, Any]],
    registry_sha256: str | None,
) -> set[str]:
    complete: set[str] = set()
    for name, document in assessment_docs:
        if not isinstance(document, dict):
            continue
        if registry_sha256 is not None and (
            document.get("registry_sha256") != registry_sha256
        ):
            continue
        summary = document.get("summary") or {}
        eligible = any(
            int(summary.get(rel, 0) or 0) > 0 for rel in _PROPOSAL_ELIGIBLE_RELATIONS
        )
        if not eligible or name in marked:
            complete.add(name)
    return complete


def _allow_set_fd(state_root_fd: int, registry_sha256: str | None) -> set[str]:
    """Compute the pruning allow-set RELATIVE to a held state-root descriptor, so
    it is read from the same pinned identity the deletions operate on."""
    proposed_fd = _open_state_child_fd(state_root_fd, "proposed")
    marker_docs: list[Any] = []
    if proposed_fd is not None:
        try:
            for name in os.listdir(proposed_fd):
                if name.endswith(".json"):
                    marker_docs.append(_read_json_at(proposed_fd, name))
        finally:
            os.close(proposed_fd)
    marked = _marked_assessment_names(marker_docs, registry_sha256)

    assessments_fd = _open_state_child_fd(state_root_fd, "assessments")
    assessment_docs: list[tuple[str, Any]] = []
    if assessments_fd is not None:
        try:
            for name in os.listdir(assessments_fd):
                if name.endswith(".json"):
                    assessment_docs.append((name, _read_json_at(assessments_fd, name)))
        finally:
            os.close(assessments_fd)
    return _classify_complete(marked, assessment_docs, registry_sha256)


def _fully_processed_artifact_names(
    dirs: dict[str, Path], registry_sha256: str | None = None
) -> set[str]:
    """Names of change-batch/assessment artifacts safe to prune.

    An artifact ``<ts>-<source>.json`` (the batch and its same-named assessment)
    is safe to prune only once propose has fully consumed it: either its
    assessment produced no proposal-eligible relations at all, or propose wrote a
    durable completion marker for it (``proposed/<ts>-<source>.json``). An
    unassessed batch is never in this set.

    Pruning is destructive after the checkpoint has advanced, so completeness is
    proven by the marker -- written by propose ONLY after EVERY proposal for the
    assessment is on disk -- not by the mere presence of one proposal (a crash
    after the first of several proposals must not authorize pruning the rest).
    The marker must be a genuine current-registry marker; a foreign or malformed
    one does not authorize pruning.

    Reads are anchored to the pinned state root so a swapped state root cannot
    feed a forged allow-set."""

    assessments_dir = dirs["assessments"]
    state_root = assessments_dir.parent

    if _DIR_FD_PRUNE:
        state_root_fd = _open_state_root_fd(state_root)
        try:
            return _allow_set_fd(state_root_fd, registry_sha256)
        finally:
            os.close(state_root_fd)

    # Pathname fallback (no dir_fd): pruning itself fails closed on this platform,
    # so a pathname-read allow-set can never authorize an actual deletion.
    marker_docs: list[Any] = []
    proposed_dir = dirs.get("proposed")
    if proposed_dir is not None and proposed_dir.is_dir():
        for path in proposed_dir.glob("*.json"):
            try:
                marker_docs.append(_load_json(path))
            except ValueError:
                continue
    marked = _marked_assessment_names(marker_docs, registry_sha256)
    assessment_docs: list[tuple[str, Any]] = []
    for path in assessments_dir.glob("*.json"):
        try:
            assessment_docs.append((path.name, _load_json(path)))
        except ValueError:
            continue
    return _classify_complete(marked, assessment_docs, registry_sha256)


def _prune_orphan_markers_fd(state_root_fd: int) -> None:
    """Remove propose_marker files whose assessment is gone, relative to a held
    state-root descriptor (each unlink inode-verified via _delete_prunable_entry
    is unnecessary here -- markers carry no age criterion -- but the delete is
    still relative to the pinned proposed/ descriptor)."""
    proposed_fd = _open_state_child_fd(state_root_fd, "proposed")
    if proposed_fd is None:
        return
    try:
        assessments_fd = _open_state_child_fd(state_root_fd, "assessments")
        try:
            assessment_names = (
                set(os.listdir(assessments_fd))
                if assessments_fd is not None
                else set()
            )
        finally:
            if assessments_fd is not None:
                os.close(assessments_fd)
        for name in os.listdir(proposed_fd):
            if not name.endswith(".json"):
                continue
            if name not in assessment_names:
                try:
                    os.unlink(name, dir_fd=proposed_fd)
                except OSError:
                    pass
    finally:
        os.close(proposed_fd)


def _prune_state(
    state_root: Path, cutoff_epoch: float, registry_sha256: str | None
) -> dict[str, int]:
    """Run the ENTIRE destructive prune under ONE pinned state-root descriptor:
    the allow-set, all three subdirectory prunes, and marker cleanup share the
    same O_NOFOLLOW-pinned inode, so no window exists for a state-root swap
    between authorization and deletion. Each deletion is additionally
    inode-verified against a stat-to-unlink leaf swap."""
    state_root_fd = _open_state_root_fd(state_root)
    try:
        complete = _allow_set_fd(state_root_fd, registry_sha256)
        pruned = {
            "reports": _prune_child_fd(state_root_fd, "reports", cutoff_epoch),
            "changes": _prune_child_fd(
                state_root_fd, "changes", cutoff_epoch, complete
            ),
            "assessments": _prune_child_fd(
                state_root_fd, "assessments", cutoff_epoch, complete
            ),
        }
        _prune_orphan_markers_fd(state_root_fd)
        pruned["total"] = (
            pruned["reports"] + pruned["changes"] + pruned["assessments"]
        )
        return pruned
    finally:
        os.close(state_root_fd)


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


def _newest_report(
    reports_dir: Path, registry_sha256: str | None = None
) -> dict[str, Any] | None:
    # Newest report OF THE ACTIVE REGISTRY: after an in-place registry edit the
    # dir still holds the previous registry's reports, and returning the lex-
    # latest one unconditionally would present a foreign result as current until
    # a fresh sweep writes another report.
    for path in sorted(reports_dir.glob("*-sweep.json"), reverse=True):
        try:
            document = _load_json(path)
        except ValueError:
            continue
        if not isinstance(document, dict):
            continue
        if registry_sha256 is not None and (
            document.get("registry_sha256") != registry_sha256
        ):
            continue
        return {
            "generated_at": document.get("generated_at"),
            "result": document.get("result"),
        }
    return None


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
        # Acquire the state lock FIRST on every platform, so pruning is serialized
        # against the pipeline stages (and refuses while another run holds it)
        # whether or not descriptor-relative deletion is available.
        with lock_state(state_root):
            if not _DIR_FD_PRUNE:
                # Fail closed on platforms without descriptor-relative deletion:
                # prune nothing and report it unsupported rather than delete by
                # pathname through a possibly-swapped directory (see _prune_dir).
                pruned = {
                    "unsupported_platform": True,
                    "reports": 0,
                    "changes": 0,
                    "assessments": 0,
                    "total": 0,
                }
            else:
                # The whole prune -- allow-set, every subdirectory deletion, and
                # marker cleanup -- runs under ONE O_NOFOLLOW-pinned state-root
                # descriptor (see _prune_state), so a state-root swap between any
                # two of those steps cannot redirect them, and each deletion is
                # inode-verified against a leaf swap.
                pruned = _prune_state(state_root, cutoff_epoch, registry_sha256)

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
        "newest_report": _newest_report(dirs["reports"], registry_sha256),
        "lock": _lock_state(state_root),
        "backups_total_bytes": _dir_total_bytes(dirs["backups"]),
        "pruned": pruned,
        "network_calls": 0,
        "vault_writes": 0,
    }
