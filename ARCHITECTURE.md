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

RecallWeave OSS is **local and single-user by construction**. Every capability in
this repository runs on one machine, for one operator, against local files, with
no server and no other person's data or credentials involved. This is a design
boundary, not a temporary limitation.

The following belong **outside** the OSS core, in a separate commercial control
plane — never in this repository:

- hosted scheduling or execution (running work on a server, for someone else,
  on a timer they do not control locally);
- cross-machine or multi-agent **fleet orchestration** and routing;
- multi-user accounts, roles, or **RBAC**;
- **centralized approvals** (an approval service other people submit to);
- **managed secrets or connectors** (holding third-party credentials, brokering
  access to external systems on a user's behalf);
- **enterprise audit/admin**, billing, or usage metering;
- any **proprietary control-plane** behavior.

A useful test when adding a feature: *does it need a server, another user, or
someone else's credentials to work?* If yes, it does not belong here. Local
policy, local Git/checkpoint workflows, and local diff/reconcile/steward
primitives are in scope precisely because they run entirely on the operator's own
machine; the moment such a mechanism is hosted, shared, or centralized, it
crosses into the commercial layer.

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

## Extension points

Planned optional providers may add local embeddings, hosted embeddings, entity
extraction, or MCP transport. They must preserve:

1. offline deterministic core behavior;
2. evidence-class separation;
3. no note mutation;
4. inspectable provider and privacy configuration;
5. versioned JSON output.

Task contracts are a projection over the same evidence classes, so any provider
that keeps the versioned JSON output stable remains compatible with the
`recallweave contract` command.
