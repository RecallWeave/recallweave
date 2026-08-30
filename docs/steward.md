# Steward — local knowledge stewardship

Steward watches the local sources you register, detects what changed since the
last sweep, assesses those changes against the RecallWeave index with
**deterministic, byte- and structure-level checks only**, and produces
reviewable reports and proposals. The pipeline stages — observe, assess,
propose, sweep, status — are **read-only**: they never write to a vault or
source file and every one of their receipts reports `vault_writes: 0` and
`network_calls: 0`.

**Recorded narrowing of the no-mutation invariant.** `steward-apply` is the
single, deliberate exception: a policy-gated, operator-approved executor for
compiled proposals, added as an explicit, scoped narrowing of the project's
"never edits a note" rule — not a silent exception. The engine keeps no write
path into notes (no engine module can even import the apply module; a test
proves it), writes exist only for a source registered `appliable`, only under
an explicit `--write-policy`, only with `--execute`, and only through
hash-pinned edits with journaled, verified rollback. Apply receipts report
their mutations honestly in `steward_vault_mutations`; `vault_writes: 0`
remains literally true for every other command.

## What Steward v1 is not

- It does not judge meaning, support, or truth. The interpretive relations
  (`CONFIRMS`, `EXTENDS`, `SUPERSEDES`, `CONTRADICTS`, `UNCERTAIN`) are
  **reserved** in the schema and cannot be emitted by any shipped code path.
  They are held for a future, explicitly opt-in **InterpretationProvider**
  layer with its own trust design. A provider may only **add** proposal-layer
  records that reference deterministic assessments — it can never rewrite,
  overwrite, or suppress a deterministic record, and its offline fallback is
  the **absence** of the claim, never a weaker version of the same claim.
  Nothing in this release fakes a semantic relation with a lexical heuristic.
- It does not edit notes. The standing rulings remain in force: candidates
  cannot establish truth, settle a contradiction, authorize an action, or
  change canonical notes; contradiction detection requires a model or human
  review and an explicit trust design. The write capability that now exists
  (`steward-apply`, above) is exactly the explicit, scoped, documented
  narrowing those rulings demanded — nothing else may write, and nothing may
  write silently.
- It is not a scheduler. `steward-sweep` is a one-shot local command with
  machine-readable results; recurring runs belong to your operating system's
  own scheduler (launchd, cron, Task Scheduler). No daemon, watch, or interval
  mode exists.

## Commands

```bash
recallweave steward-observe <sources.json>   # detect changes since the checkpoint
recallweave steward-assess  <sources.json>   # classify changes vs the index
recallweave steward-propose <sources.json>   # compile reviewable proposals
recallweave steward-sweep   <sources.json>   # observe -> assess -> propose -> report
recallweave steward-status  <sources.json>   # state-dir summary; optional pruning
recallweave steward-apply   <sources.json> --write-policy <p.json> \
    (--proposal-id ID | --approve-class CLASS | --recover J | --revert J) [--execute]
```

All commands accept `--state-dir`; the assess/propose/sweep stages accept the
standard `--database`/`--vault` locator. `steward-sweep` accepts
`--format json|markdown` for the report projection.

## Sweep results and exit codes

`steward-sweep` reports one machine-readable `result` in its JSON report and
returns the matching exit code. The enum is frozen:

| result | exit code | meaning |
|---|---|---|
| `no_change` | 0 | nothing changed; nothing awaits review |
| `findings` | 3 | assessments were recorded; no proposals await review |
| `approval_required` | 4 | proposals exist and await operator review |
| `applied` | 5 | reserved for sweep-integrated apply (not yet emitted) |
| `validation_failed_rolled_back` | 6 | reserved for sweep-integrated apply |
| `error` | 2 | hard error (standard JSON error envelope on stderr) |

## Source registry

Sources are registered in a JSON file (`recallweave.steward.sources.v1`).
Every source is admitted through a per-source **index policy** — the same
allowlist-first `IndexPolicy` that governs indexing; Steward has no admission
language of its own. The registry is structurally local: no field can name a
remote (any `://` value is rejected), roots must resolve to existing local
paths, overlapping roots are a validation error, and a source cannot be marked
`appliable` without an explicit `include_paths` allowlist.

```json
{
  "spec_version": "recallweave.steward.sources.v1",
  "sources": [
    {
      "name": "projects",
      "type": "folder",
      "root": "~/Vault",
      "mode": "read_only",
      "policy": {"include_paths": ["projects/plan.md"]}
    }
  ]
}
```

## Deterministic relations

Assessment records carry a pinned asserter and a standing caveat, and are a
distinct record type: they never enter the evidence-class enum and are never
written into the SQLite index (the index stays disposable; Steward state is
separately losable). The relations v1 can emit:

- `NEW` — new to the registry (not "new knowledge")
- `DELETED` — a file deletion (not a claim retraction)
- `MODIFIED` — content hash differs (decides *that*, never *why*)
- `DUPLICATES_EXACT_BYTES` — exact byte equality, nothing more
- `AUTHORED_REFERENCE_TOUCHED` — an authored edge endpoint changed
- `CITATION_BROKEN` — a previously resolving citation or authored link no
  longer holds (exact line range, passage, and heading re-verification)

Renames are never asserted: a move appears as `removed` + `added` with a
`rename_candidate` annotation carrying hash and inode evidence, and pairing is
left to the operator or a compiled proposal with hash preconditions.

## Proposals and the write-policy doctrine

Every v1 proposal carries `policy_level: "propose_only"` and edits nothing.
Compiled edits (single-line link fixes after an inode-proven, unique rename)
are hash-pinned: each names its target's current content hash and a predicted
post-hash, so any drift refuses cleanly at a future apply stage.

The write policy (`recallweave.steward.policy.v1`) defines four levels —
`disabled`, `propose_only` (the default for every class), `require_approval`,
and `auto_apply` — and enforces structurally, at load time, that **technical
determinism never implies authorization**: only append-only mutation classes
can ever be configured `auto_apply`, protected paths always resolve to
`disabled`, and no principal-naming or confidence-gating key can exist in a
policy file. `steward-apply` enforces the resolved level per edit at apply
time — including protected frontmatter against the file's current state —
and refuses anything this invocation cannot authorize.

## State, privacy, locality

Steward state (checkpoints, change batches, assessments, proposals, reports)
lives outside your sources in the platform application-data directory, keyed
by the registry path. Checkpoints store paths, hashes, and stat metadata —
never file content. Proposals are machine-local by construction: they carry no
identity or assignee fields, reference content only by relative path and local
hashes, and are not safe or meaningful to transport to another machine. A
single lock file serializes runs; a second concurrent run refuses with an
actionable error.

## Rollback is not undo

Every apply journals verified backups before it writes, and a failed apply —
including a failed post-apply validation gate — restores every target and
re-hashes each restore. `steward-apply --revert <journal>` restores an
already-applied journal the same way. But restoring bytes does not restore
the index (rebuild it), does not change artifacts you already exported, and
does not un-happen anything a reader saw. Steward reports exactly what it
restored and never claims a rollback it could not verify.
