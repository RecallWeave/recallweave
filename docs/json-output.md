# JSON output contract

RecallWeave writes one JSON object to standard output on success. Errors are
written as one JSON object to standard error and return exit code `2`.

Core command payloads include:

```json
{
  "schema_version": "2",
  "operation": "query"
}
```

Consumers should reject unknown major schema versions. Fields may be added
within a schema version; consumers should ignore fields they do not use.

`export-viewer` is an exception: both its stdout receipt and the graph document
use the purpose-specific schema version `recallweave.viewer.v2` (a documented
superset of `recallweave.viewer.v1`).

## Citations

A citation has the form:

```text
vault/relative/path.md:line_start-line_end
```

Lines are one-based physical Markdown lines. Only CRLF, CR, and LF count as line
boundaries. A passage shortened to fit a character budget has
`"truncated": true`; its citation identifies the full source section.

## `index`

Important fields:

- `database`: absolute path to the derived index;
- `notes_indexed`, `sections_indexed`, `note_tags`: indexed row counts;
- `verified_edges`: authored link edges;
- `candidate_edges`: unverified discovery edges;
- `unresolved_links`: links that were not trusted;
- `discovery`: scale/filter diagnostics and warnings;
- `skipped`: count by policy or safety reason;
- `network_calls`: always `0` in the core;
- `vault_writes`: `0` normally, `1` only for an explicitly allowed in-vault
  database;
- `policy_mode`: `config` when a JSON policy was supplied, or `none` when the
  operator explicitly passed `--no-policy`;
- `policy_config_sha256`: optional SHA-256 digest of the exact policy-file
  bytes. This field is present only for `policy_mode: "config"` and identifies
  the applied policy without disclosing its path or contents.

The database path is operationally sensitive because it can reveal a local
username or vault label. A private broker should redact it before forwarding an
index receipt to an assistant.

The CLI refuses to index until the operator chooses exactly one policy posture:
`--config <policy.json>` or `--no-policy`. The latter is an explicit
acknowledgment that every Markdown file in the selected vault is safe to index;
it does not apply sensitivity or path rules. A policy file is captured once,
then the same bytes are parsed and hashed. UTF-8 with or without a byte-order
mark is accepted; the digest covers the exact bytes supplied.

## `query`

`passages` contains ranked section objects:

```json
{
  "relative_path": "Projects/Example.md",
  "heading": "Decision",
  "line_start": 12,
  "line_end": 18,
  "citation": "Projects/Example.md:12-18",
  "passage": "Source text...",
  "truncated": false,
  "matched_terms": ["decision", "evidence"]
}
```

`connections` contains authored edges by default. Candidate edges appear only
when `--include-candidates` is explicit. `connections_total`,
`connections_returned`, and `connections_truncated` make the bounded edge list
explicit. At most 200 edges are returned in a query packet.
`characters_used` never exceeds `character_budget`.

## `connections`

Each item contains `note`, `title`, `direction`, `kind`, `verified`, `score`,
and `evidence`.

- `wikilink` and `markdown_link` are authored and verified;
- `discovery_candidate` is deterministic lexical inference and unverified;
- `co_tag` is an on-demand supporting signal and unverified.

`connections_total`, `connections_returned`, and `connections_truncated`
distinguish a complete list from one shortened by `--limit`. Co-tag signals are
not generated for tags attached to more than 100 notes; high-fanout tags such as
`#daily` are intentionally treated as too broad to be useful.

## `resurface`

`results` combines a cited search hit with `resurface_score`, `age_days`,
`verified_degree`, and a human-readable `why` list. The score is a suggestion
for review, not a claim of importance.

## `path`

`found` indicates whether a bounded path exists. `steps` contains the note pair,
evidence kind, verification class, score, and evidence for each hop. Candidate
edges are excluded unless `--include-candidates` is explicit. Path expansion
examines at most 1,000 connections per visited note. `search_truncated: true`
means that bound was reached, so `found: false` is not proof that no path exists.

## `doctor`

`unresolved_total` reports all unresolved links. `unresolved` is a bounded list
with source path, line, link kind, target text, and reason such as `not_found`,
`ambiguous`, `path_not_found`, or `ambiguous_path`.

## `stats`

Returns index freshness, row counts, and the same `discovery` diagnostics
recorded during indexing.

## `export-viewer` and `recallweave.viewer.v2`

`export-viewer` writes a local graph document and returns a stdout receipt. Both
use the current schema:

```json
{
  "schema_version": "recallweave.viewer.v2"
}
```

`recallweave.viewer.v1` remains readable by Atlas for legacy exports; new exports
use v2 (see [viewer.v2 contract](#export-viewer-and-recallweaveviewerv2-frozen-schema)
below).

Create a structure-only graph from an existing index:

```console
recallweave export-viewer graph.json --vault /path/to/vault
```

Options:

- `--verified-only` excludes deterministic discovery candidates;
- `--include-excerpts` adds bounded note summaries and evidence passages;
- `--vault-name` sets a vault label claim (not a filesystem path);
- `--force` permits replacing an existing regular output file;
- `--title` overrides the graph title.

The exporter refuses to replace its SQLite database, symlinks, junctions,
reparse points, and outputs beneath a symlinked or reparse-point parent. Writes
without `--force` are non-replacing. A forced replacement uses a recoverable
two-phase protocol, not one atomic replace:

1. rotate the expected target to a private same-filesystem backup;
2. verify that the rotated file is the target previously inspected;
3. install the new export without replacing another file;
4. restore the backup if installation fails.

On POSIX, the create and restore paths require hardlink support and fail closed
when it is unavailable. Windows uses non-replacing rename operations. On
success, a forced replacement deliberately retains the old approved output in
the private same-directory backup; RecallWeave never silently deletes it.

### Receipt

The stdout receipt identifies the destination and the content classes actually
written:

```json
{
  "schema_version": "recallweave.viewer.v2",
  "operation": "export_viewer",
  "output": "/absolute/path/graph.json",
  "notes": 6,
  "edges": 12,
  "candidate_edges_requested": true,
  "candidate_edges_included": true,
  "replacement_mode": "non_replacing",
  "replacement_backup": null,
  "export_profile": "graph_metadata_and_note_derived_terms",
  "requested_profile": "without_passage_text",
  "metadata_only": false,
  "excerpts_requested": false,
  "excerpts_included": false,
  "passage_text_included": false,
  "note_derived_terms_included": true,
  "paths_titles_tags_included": true
}
```

`output` can reveal local paths and should be redacted before sending a receipt
to an assistant. `replacement_mode` is `non_replacing` or
`two_phase_recoverable`. `replacement_backup` is `null` for a newly created
non-replacing output. A successful forced replacement always returns the
absolute retained-backup path, which is equally sensitive and gives the
operator an explicit recovery artifact to review or remove later.

### Graph document

Top-level fields:

- `title`: human-facing graph label;
- `generated_at`: UTC generation timestamp;
- `nodes`: note-derived graph nodes;
- `edges`: authored and, unless `--verified-only`, candidate connections;
- `diagnostics.unresolved_links`: count carried from the index;
- `privacy`: machine-readable export content flags.

A node contains:

```json
{
  "id": "Projects/Decision Memory.md",
  "title": "Decision Memory",
  "path": "Projects/Decision Memory.md",
  "status": "working",
  "domain": "Projects",
  "summary": "",
  "tags": ["decisions", "review"],
  "section_count": 4
}
```

`summary` is empty unless `--include-excerpts` is set, then contains at most 280
characters from the first indexed section.

An edge contains `id`, `source`, `target`, `kind`, `verified`, `score`, and an
`evidence` object. Candidate evidence can contain:

```json
{
  "citation": "Projects/Decision Memory.md:13-16",
  "source_evidence": {
    "citation": "Projects/Decision Memory.md:13-16"
  },
  "target_evidence": {
    "citation": "Ideas/Small Bets.md:9-12"
  },
  "shared_terms": ["reversible", "threshold", "owner"],
  "explanation": "Candidate only: lexical overlap is not proof of a factual relationship."
}
```

The flat `citation` field is retained for `viewer.v1` consumers. With
`--include-excerpts`, `source_evidence.passage` and
`target_evidence.passage` are added, each capped at 500 characters; the legacy
flat `source_text` is also retained. Citations identify indexed source
passages, but the graph document does not establish the meaning of a candidate
connection.

### Privacy flags and export profiles

The `privacy` object contains:

```json
{
  "export_profile": "graph_metadata_and_note_derived_terms",
  "requested_profile": "without_passage_text",
  "metadata_only": false,
  "includes_excerpts": false,
  "includes_passage_text": false,
  "includes_note_derived_terms": true,
  "includes_paths_titles_tags": true,
  "generated_locally": true
}
```

Export profiles:

- `empty_graph`: no nodes, paths, terms, or passage text were written;
- `graph_metadata`: paths, titles, tags, statuses, domains, citations, and graph
  structure, with no passage text or note-derived shared terms;
- `graph_metadata_and_note_derived_terms`: the default when candidates contain
  shared terms;
- `graph_with_bounded_passage_text`: selected by `--include-excerpts`.

`requested_profile` records the operator's request:
`without_passage_text` or `with_bounded_passage_text`. `export_profile` and all
`includes_*` flags describe content actually present. For example, an empty
graph exported with `--include-excerpts` has
`requested_profile: "with_bounded_passage_text"` but
`export_profile: "empty_graph"` and `includes_passage_text: false`.
`generated_locally` is an assertion written by the exporter, not a property a
consumer can independently prove from the JSON file. Viewers must label it as a
source claim rather than presenting it alongside recomputed content facts.

The default should be described as **structure-only**, not anonymous,
metadata-free, or automatically safe to share. Paths, titles, tags, domains,
citations, and candidate vocabulary can disclose the shape and subject matter
of a vault.

### Versioning boundary

`recallweave.viewer.v1` intentionally omits note timestamps, content hashes,
vault names, policy provenance, and export history. Consumers must not infer
dormancy, temporal drift, independent rediscovery, or direct Obsidian open
links from this schema. Those capabilities require a separately reviewed
`recallweave.viewer.v2`.

## `export-viewer` and `recallweave.viewer.v2` (frozen schema)

Status: **schema frozen for implementation**. Emitters and Atlas consumers may
implement against this contract; changing any required field below needs a new
reviewed minor schema bump (`viewer.v2.1+`) or a new major (`viewer.v3`).

`viewer.v2` is a **superset** of `viewer.v1`. A `viewer.v2` document MUST remain
readable by a `viewer.v1` consumer that ignores unknown fields. A consumer that
opts into `viewer.v2` MUST reject unknown major versions and MUST NOT invent
values for absent optional fields.

Both the graph document and the stdout receipt use:

```json
{
  "schema_version": "recallweave.viewer.v2"
}
```

### Node fields added in v2

Every node from v1 remains. v2 adds:

| Field | Required | Meaning |
| --- | --- | --- |
| `created_at` | yes when known from the index; otherwise JSON `null` | UTC ISO-8601 timestamp of note creation as recorded by the index. Prefer frontmatter `created` when present; otherwise the indexer may record filesystem birth time (`st_birthtime`, or Windows creation time) when the platform exposes it. Never invent a clock value; use JSON `null` when unknown. |
| `modified_at` | yes when known from the index; otherwise JSON `null` | UTC ISO-8601 timestamp of last indexed content change. |
| `content_hash` | yes | Hex SHA-256 of the exact note bytes the index hashed for this path. Empty-string hashes are forbidden; use `null` only if the index lacks a hash (legacy indexes). |

Consumers MUST treat timestamps and hashes as **claims about the exporting
index**, not as live filesystem facts, unless the consumer recomputes them.

### Graph-level fields added in v2

| Field | Required | Meaning |
| --- | --- | --- |
| `vault_name` | no | Optional vault label for constructing `obsidian://open` links. Never a filesystem path. Absent when the operator did not supply one. |
| `policy_config_sha256` | no | SHA-256 of the exact policy-file bytes applied at index time when `policy_mode` was `config`. MUST NOT include the policy path or policy contents. Absent for `--no-policy` indexes. |
| `export_history` | yes | Object describing this export relative to prior exports of the same graph destination. |

`export_history` shape:

```json
{
  "export_id": "uuid-or-stable-token",
  "previous_content_hash": null,
  "node_content_hashes_changed": 0,
  "node_content_hashes_unchanged": 0,
  "nodes_added": 0,
  "nodes_removed": 0
}
```

- `previous_content_hash` is the aggregate content digest of the prior approved
  export when a forced replacement rotates a previous file; otherwise `null`.
- Counts are derived by comparing per-node `content_hash` and node ids. They are
  structural facts about the export pair, not claims about author intent.

### Evidence signals (v2)

Candidate `evidence` objects MUST expose distinct signal bags instead of a
single flattened kind:

```json
{
  "citation": "Projects/Decision Memory.md:13-16",
  "signals": {
    "lexical_terms": ["reversible", "threshold"],
    "shared_tags": ["decisions"],
    "mutual_neighbor_ids": ["Projects/Related.md"]
  },
  "explanation": "Candidate only: overlapping signals are not proof of a factual relationship."
}
```

v1 fields `shared_terms` and flat `citation` remain for compatibility.
`signals.lexical_terms` SHOULD match `shared_terms` when both are present.
Absence of a signal key means “not computed,” not “zero.”

### Out of scope for v2

Contradiction, causality, embedding similarity without shared vocabulary, and
importance ranking remain out of scope. Dormant / Drift trail types may use
`created_at` / `modified_at` / `export_history` only after Cold Trails
implementation tasks land; the schema alone does not authorize those claims.

See also `docs/cold-trails.md` release gates.

Example:

```json
{
  "schema_version": "2",
  "error": "ValueError",
  "message": "RecallWeave database not found: ... Run 'recallweave index <vault>' first.",
  "operation": "stats"
}
```
