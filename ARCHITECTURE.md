# Architecture

## Trust model

RecallWeave treats the Markdown vault as canonical and its own index as
disposable. The core has no write path back into notes.

```text
Obsidian vault (canonical, read only)
              |
              v
Parser -> external local SQLite index -> query / connection / resurfacing JSON
              |
              +-- verified edges: authored note links
              +-- candidate edges: deterministic inference
              +-- supporting signals: explicit tags
```

An assistant integration should sit outside this repository:

```text
Assistant -> private policy broker -> RecallWeave JSON
                                  -> canonical retrieval/citation route
```

The broker is responsible for identity, domain allowlists, refusals, redaction,
audit receipts, and human approval. RecallWeave does not grant an assistant
permission to read a vault merely because it can index one.

For agent-facing indexes, the private broker should generate an exact
`include_paths` allowlist from its approved retrieval corpus. Denylists remain
defense in depth; they are not the primary authorization boundary.

## Product boundary

RecallWeave OSS is **local and single-user by construction**. The engine and its
tools run on one machine, for one operator, against local files, with no server
processing that operator's data and no other person's data or credentials
involved. This is a design boundary, not a temporary limitation. Serving the
**inert Atlas application shell** — a static build that parses a graph the viewer
selects locally in their own browser, with no upload endpoint and no transmission
of the selected file (see [viewer/README.md](viewer/README.md)) — is a static
asset, not hosted execution over user data, and stays in scope.

The following belong **outside** the OSS core, in a separate commercial control
plane — never in this repository:

- hosted scheduling or execution **over user data** (running the engine or work
  on a server, processing another person's vault or graph, on a timer they do not
  control locally) — as distinct from serving the inert, data-less Atlas shell;
- cross-machine or multi-agent **fleet orchestration** and routing;
- multi-user accounts, roles, or **RBAC**;
- **centralized approvals** (an approval service other people submit to);
- **managed secrets or connectors** (holding third-party credentials, brokering
  access to external systems on a user's behalf);
- **enterprise audit/admin**, billing, or usage metering;
- any **proprietary control-plane** behavior.

A useful test when adding a feature: *does it need a server to process someone's
data, another user, or someone else's credentials to work?* If yes, it does not
belong here. (Serving a static, inert client that does its work in the viewer's
own browser is not that.) Local
policy, local Git/checkpoint workflows, and local diff/reconcile/steward
primitives are in scope precisely because they run entirely on the operator's own
machine; the moment such a mechanism is hosted, shared, or centralized, it
crosses into the commercial layer.

The line is drawn at *who runs it and whose data flows through it*, not at whether
a network call is ever made. An optional provider that runs locally, is invoked by
the single operator, and uses that operator's own credentials to reach an external
API they chose (see **Extension points**) stays on the local side of the line —
it needs an offline fallback and inspectable configuration, but it is the
operator's own client. What crosses the line is *hosted execution* on someone
else's server, *managed* credentials or connectors held on a user's behalf, and
anything shared across users. "No network calls" describes the default core;
it is not a blanket ban on operator-configured optional providers.

The trust model above already assumes this split: identity, authorization,
redaction, audit receipts, and human approval live in the **private broker /
control plane**, and RecallWeave hands it bounded, cited, read-only evidence.

## Task contracts

A task contract is a bounded projection of the index for one task: the
operator asserts the objective, constraints, prior decisions, and acceptance
criteria; RecallWeave attaches provenance and verifies every cited passage
against the **indexed snapshot** — the citation must name a section the index
contains, and the quoted passage and heading must be the ones that section
holds. The exporter reads the index, never the vault, so `network_calls` and
`vault_writes` stay `0`; `provenance.index.indexed_at` is what tells a reader
how old that snapshot is. A contract inherits the existing evidence
classes (`authored_by_operator`, `cited_passage`, `lexical_match`,
`authored_link`, `discovery_candidate`) rather than inventing new ones, and
RecallWeave never infers that a passage is a constraint or a decision.

An evidence class names the **origin** of the text it labels, never the
strength of the support attached to it. A statement the operator wrote is
`authored_by_operator` even when the operator also cited a note; the citation
and passage then travel beside it as support, in their own fields, and the
human projection shows both, each attributed. Only a statement that IS the
source-derived passage is `cited_passage`. RecallWeave does not judge whether a
cited passage supports an operator's assertion — that is not decidable here,
and a model that claimed to decide it would assert something it cannot check. The
private broker remains responsible for identity, authorization, and redaction;
exclusions in a spec are a second boundary behind the index policy's
`include_paths` allowlist, never an authorization boundary on their own.

## Index

The SQLite database stores vault-relative paths, note metadata, section
passages, term counts, authored edges, explicit per-note tags, discovery
candidates, and unresolved link diagnostics. It does not store the absolute
vault path. The command receipt does report the absolute database destination
so an operator can find and protect it.

The safe default destination is a platform application-data directory outside
the vault. Indexing builds a unique temporary database and replaces an old
RecallWeave database only after a successful build. A non-RecallWeave
destination is refused unless the operator deliberately uses `--force`.

Traversal does not follow file or directory symlinks and skips file hardlinks.
Every candidate file is resolved and checked to remain inside the vault before
it is read.

## Evidence classes

Verified evidence:

- authored `[[wikilinks]]` resolving to exactly one note;
- authored Markdown note links resolving to exactly one note.

Supporting evidence:

- an explicit tag on a note;
- a bounded on-demand co-tag signal, always labeled unverified;
- no co-tag expansion for tags attached to more than 100 notes.

Discovery candidates:

- local TF-IDF cosine similarity;
- at least two informative shared terms;
- relative document-frequency filtering;
- bounded posting comparisons and candidates per note;
- cited source and target passages attached to every edge.

Candidates and shared tags can suggest review. They cannot establish truth,
settle a contradiction, authorize an action, or change canonical notes.

## Citation contract

Line numbers are one-based physical Markdown lines. RecallWeave recognizes only
CRLF, CR, and LF as line boundaries, matching common editors. Leading and
trailing blank lines omitted from a section are also removed from its reported
range. If a returned passage is shortened to fit a character budget, its
`truncated` field is `true`; the citation continues to identify the source
section from which the excerpt came.

## Query packet

A query packet includes:

- matching section passages;
- vault-relative path and physical line range;
- matched terms;
- note status/domain metadata when present;
- nearby authored connections by default;
- an explicit character budget and truncation flag.

This gives an assistant a bounded map plus cited evidence rather than the whole
vault. See [docs/json-output.md](docs/json-output.md) for the versioned API.
Every bounded connection list reports its total, returned count, and truncation
state.

## Steward

Steward is the local stewardship pipeline over registered sources:
checkpointed change detection (`steward-observe`), deterministic assessment
against the indexed snapshot (`steward-assess`), reviewable read-only
proposals (`steward-propose`), and a one-shot sweep with a stewardship report
(`steward-sweep`). See [docs/steward.md](docs/steward.md).

### Three planes

Steward is organized around three conceptual planes, and the boundaries
between them are load-bearing. They are frozen here so no later change blurs
them silently.

- **Truth plane** — what is deterministically the case: the source registry,
  content hashes and checkpoints, citations and provenance, authored
  references, and the deterministic change/integrity findings
  (`NEW`, `DELETED`, `MODIFIED`, `DUPLICATES_EXACT_BYTES`,
  `AUTHORED_REFERENCE_TOUCHED`, `CITATION_BROKEN`). Every fact here is a
  byte- or structure-level observation, reproducible without a model, and
  never depends on inference.
- **Interpretation plane** — reserved, and **not implemented in v1**. This is
  where a future opt-in provider would assert *semantic* relations
  (`CONFIRMS`, `EXTENDS`, `SUPERSEDES`, `CONTRADICTS`, `UNCERTAIN`). Such
  claims are inferential and must be attributable to their asserter; they may
  only be *added* as a layer that references Truth-plane records, and may
  never overwrite, rewrite, or suppress a deterministic finding. No shipped
  code path emits them; they exist in v1 only as schema reservation and this
  documented boundary.
- **Action plane** — what may change the estate: proposals, the write policy,
  apply, post-apply validation, rollback, and the optional local Git
  lifecycle. Every action is gated, hash-pinned, journaled, and reversible,
  and authorization is always explicit — determinism in the Truth plane
  never grants authority in the Action plane.

The planes flow one way: Truth informs Interpretation and Action;
Interpretation (when it exists) may inform Action only through reviewable
proposals; neither Interpretation nor Action ever rewrites Truth. The rules
below enforce these boundaries in code.

Three structural rules keep Steward inside the trust model above:

1. **Admission is IndexPolicy, only.** Every registered source is admitted
   through a per-source index policy; the registry adds no admission language,
   cannot name a remote, and rejects overlapping roots.
2. **Relations are not evidence classes.** Assessment records label a
   *relation between states* (`NEW`, `DELETED`, `MODIFIED`,
   `DUPLICATES_EXACT_BYTES`, `AUTHORED_REFERENCE_TOUCHED`,
   `CITATION_BROKEN`), carry a pinned asserter and a standing caveat, and are
   never written into the index or the evidence-class enum. The interpretive
   relations (`CONFIRMS`, `EXTENDS`, `SUPERSEDES`, `CONTRADICTS`,
   `UNCERTAIN`) are schema-reserved for a future opt-in
   InterpretationProvider and cannot be emitted by shipped code; the prior
   rulings that candidates cannot settle a contradiction or change canonical
   notes remain binding, and any future change to that posture must be
   recorded here explicitly.
3. **Determinism never implies authorization.** Proposals are read-only
   documents; the write policy assigns every mutation class an explicit
   level with non-auto defaults, only append-only classes are structurally
   eligible for `auto_apply`, and the apply stage re-resolves the policy per
   edit at execution time.

The pipeline stages are read-only over sources and index alike; every one
of their receipts reports `network_calls: 0` and `vault_writes: 0`. The
single sanctioned writer is `steward-apply` — a pure executor over
operator-approved, hash-pinned edit scripts, import-isolated from the engine
(no engine module can reach it, pinned by test), gated by an explicit write
policy and `--execute`, journaled with verified backups and rollback, and
validated post-write (parse, admissibility, structure, whole-source
manifest, index-rebuild bounds) inside the same transaction. This is the
explicit, scoped narrowing of the no-mutation invariant that this document
previously required of any future write capability; apply receipts count
their mutations in `steward_vault_mutations` and never claim zero writes
when a write occurred.

Every mutation — write, create, delete, and each rollback restore — is
anchored to a verified source-root directory descriptor and traverses to its
target through an `O_NOFOLLOW` `openat` chain, so a parent directory swapped
for a symlink between validation and the syscall cannot redirect the
operation outside the admitted source. On a platform whose runtime lacks the
required descriptor-relative primitives (`os.supports_dir_fd`) — notably
Windows — apply falls back to pathname operations that recheck the parent
chain immediately before each syscall and open final components
`O_NOFOLLOW`; this is a documented, weaker best-effort boundary, not the
descriptor-anchored guarantee. Rollback is itself hash-pinned: it restores a
target only when the live bytes still match what the transaction wrote (or
already match the pre-apply bytes), and refuses — retaining the backup and
reporting — rather than overwrite content another process changed after the
apply.

## Extension points

Planned optional providers may add local embeddings, a client for a hosted
embedding API (run locally by the operator, using the operator's own credentials),
entity extraction, or MCP transport. These stay within the local, single-user
**Product boundary** above: each is an opt-in client the operator configures and
runs on their own machine, not hosted infrastructure the project operates on a
user's behalf. Every provider must preserve:

1. offline deterministic core behavior (the default core stays fully offline, and
   every provider ships a working offline fallback);
2. evidence-class separation;
3. no note mutation;
4. inspectable provider and privacy configuration, with any external credentials
   supplied and held by the operator alone;
5. versioned JSON output;
6. an offline fallback that is the **absence** of the provider's claim, never a
   weaker imitation of the same claim — the honest fallback for a semantic
   relation such as `CONTRADICTS` is not emitting it, not a lexical heuristic
   wearing the same label.

Task contracts are a projection over the same evidence classes, so any provider
that keeps the versioned JSON output stable remains compatible with the
`recallweave contract` command.
