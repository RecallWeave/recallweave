# RecallWeave

RecallWeave is a local-first discovery and resurfacing engine for Obsidian vaults.
It helps people and assistants answer three questions:

1. What does the vault actually say, and where?
2. Which notes may be meaningfully connected, and why?
3. Which older thoughts are relevant again now?

The vault remains the source of truth. RecallWeave creates a disposable SQLite
index in an external application-data directory by default and never edits a
note.

## Why this is different

RecallWeave keeps different kinds of evidence visibly separate:

- **Verified edges** come only from authored wikilinks and Markdown note links
  that resolve to exactly one note.
- **Discovery candidates** come from bounded, deterministic local similarity.
  They include the terms and cited passages that caused the match and must not
  be treated as facts.
- **Shared tags** are bounded supporting signals. A tag attached to two notes
  does not become a verified relationship or a quadratic table of note pairs.
- **Context packets** contain passages, physical line ranges, citations, an
  explicit truncation flag, and a hard character budget.
- **Resurfacing** favors relevant older notes that are easy to forget or poorly
  connected.

There is no telemetry, hosted service, model download, API key, or network call
in the core engine.

## Status

RecallWeave is an alpha. Its database is rebuildable output, not a backup and
not a new canonical knowledge store.

## Install from source

RecallWeave is not yet published on PyPI. Python 3.11 or newer is required.

Windows PowerShell:

```powershell
git clone https://github.com/RecallWeave/recallweave.git
Set-Location recallweave
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

macOS or Linux:

```bash
git clone https://github.com/RecallWeave/recallweave.git
cd recallweave
python3.11 -m venv .venv
./.venv/bin/python -m pip install -e .
```

## Quick start

Indexing requires an explicit policy choice. Use `--config` for a real vault:

```console
recallweave index "/path/to/your/vault" --config local-config.json
recallweave query "What have I learned about reversible experiments?" --vault "/path/to/your/vault"
recallweave connections "Growth Atlas" --vault "/path/to/your/vault"
recallweave resurface "feedback loops and operating cadence" --vault "/path/to/your/vault"
recallweave path "Decision Memory" "Review Cadence" --vault "/path/to/your/vault"
recallweave doctor --vault "/path/to/your/vault"
```

`--no-policy` is an explicit opt-out for a vault whose every Markdown file you
have confirmed is safe to index. RecallWeave does not silently assume that
choice.

Every command returns versioned JSON. See
[docs/json-output.md](docs/json-output.md) for the output contract.

## Where the index lives

Unless `--database` is supplied, RecallWeave derives a stable filename from the
resolved vault path and stores it outside the vault:

- Windows: `%LOCALAPPDATA%\RecallWeave\indexes\`
- macOS: `~/Library/Application Support/RecallWeave/indexes/`
- Linux: `$XDG_DATA_HOME/recallweave/indexes/` or
  `~/.local/share/recallweave/indexes/`

Query commands use the index for `--vault`; if that option is omitted, they use
the current directory as the vault. The index receipt reports the exact database
path. RecallWeave refuses an in-vault database unless `--allow-in-vault` is
passed deliberately, in which case the receipt honestly reports one vault
write.

## Safety configuration

Use a local JSON policy to exclude folders or frontmatter classifications:

```json
{
  "include_paths": [
    "Projects/Growth Atlas.md",
    "Operations/Review Cadence.md"
  ],
  "exclude_globs": [".git/**", ".obsidian/**", "Private/**"],
  "deny_path_terms": ["credentials", "sealed"],
  "deny_frontmatter": {
    "sensitivity": ["sealed", "restricted"]
  },
  "max_file_bytes": 2000000
}
```

Those two allowlisted paths exist in `examples/synthetic-vault`, so the shipped
example can be exercised as written. Replace them with reviewed paths from your
own vault before indexing real notes.

Then index with:

```console
recallweave index "/path/to/vault" --config local-config.json
```

Unknown configuration keys and invalid types are rejected. When
`include_paths` is present, any Markdown file not listed is rejected before its
contents are parsed. This exact allowlist is the preferred boundary for
agent-facing indexes. If deny rules exist and frontmatter cannot be evaluated
safely, the note is rejected. The frontmatter parser intentionally supports
only top-level scalar values and one-dimensional flow or block lists of scalar
values. Nested collections and mappings are unsupported. With any
`deny_frontmatter` rule configured,
unsupported constructs anywhere in a note's frontmatter—including nested
mappings, block or multiline scalars, YAML tags, anchors, aliases, flow
mappings, template expressions, double-quoted escape sequences, and unsupported
keys—skip the whole note as `unparseable_frontmatter`. Scalar validation also
applies inside supported flow and block sequences. Validation is bounded and
rejects nested flow or block collections rather than recursively guessing. A
parser recursion failure skips only the affected note instead of aborting the
vault build. This conservative behavior can exclude otherwise non-sensitive
notes; inspect the index receipt and prefer an exact `include_paths` allowlist
for agent-facing indexes.

RecallWeave skips file and directory symlinks and file hardlinks, rejects
resolved files outside the vault, refuses to overwrite a non-RecallWeave
destination unless `--force` is explicit, and exposes unresolved or ambiguous
links through `doctor`.

## Commands

| Command | Purpose |
| --- | --- |
| `index` | Atomically rebuild an external local index |
| `query` | Return bounded passages, citations, and authored nearby edges |
| `connections` | Explain authored, discovery, and shared-tag signals |
| `resurface` | Rank dormant, relevant, underlinked notes |
| `path` | Find a short evidence-bearing path between two notes |
| `doctor` | Report unresolved links and why they were not trusted |
| `stats` | Report index counts, discovery diagnostics, and freshness |
| `export-viewer` | Create a local JSON file for the optional Atlas viewer |
| `contract` | Export a minimal, cited work packet for another agent |

Candidate edges are excluded from `query` and `path` unless explicitly
requested. `connections` includes candidates by default so a person can inspect
new possibilities.

## Atlas visual explorer

![RecallWeave Atlas — See the shape of what you know](viewer/public/og.png)

RecallWeave Atlas turns a graph export into an interactive, evidence-bearing map.
Search notes, filter domains, separate authored links from candidate connections,
and select any node to inspect why its connections exist.

The bundled Northstar Studio graph is a synthetic, hand-authored,
excerpt-rich demonstration fixture. It is deliberately richer than the default
structure-only export and declares that posture in its `privacy` object.

```console
recallweave export-viewer recallweave-graph.json --vault /path/to/vault
```

The default is a **structure-only** export, not an anonymous or metadata-free
one. It omits passage text, but includes relative paths, titles, tags, status,
domains, citations, and note-derived candidate terms. Add `--include-excerpts`
only when you deliberately want bounded note summaries and bilateral evidence
passages in the graph file. Open [viewer/README.md](viewer/README.md) for local
viewer setup. Atlas reads a selected graph in browser memory; its application
code does not upload the file.

[Cold Trails](docs/cold-trails.md) is a deterministic guided tour in Atlas for
high-value graph discoveries. It runs locally in the browser and never writes
back to your vault.

## Task contracts

A task contract turns one operator-authored spec plus an existing index into a
minimal, cited work packet for another AI agent: the objective, retrieved
context, constraints, prior decisions, acceptance criteria, and explicit
exclusions, in JSON for machines or Markdown for humans. Use one when you want
a reproducible, self-contained context for a bounded task instead of pointing
an agent at the whole vault.

```console
recallweave contract task-spec.json --vault "/path/to/your/vault" --output packet.json
```

The spec file is the single source of truth for what goes into a packet. A
broker stays responsible for identity, authorization, and redaction. See
[docs/task-contracts.md](docs/task-contracts.md) for the reference.

## Public core and private adapters

The public repository contains only the generic engine and a synthetic example
vault. Organization-specific access rules, private evaluation questions,
assistant permissions, and write workflows belong in a separate private
adapter. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Known limitations

- Indexing is a full rebuild; incremental indexing is planned.
- Frontmatter uses a conservative top-level scalar/list subset rather than a
  full YAML implementation.
- Similarity is lexical and deterministic; it can suggest a review, not prove a
  relationship.
- The included scale regression covers 1,000 notes. Larger vaults should inspect
  the discovery diagnostics returned by `index` and `stats`.
- Candidate discovery is currently in-memory and grows approximately linearly
  with note count; test representative hardware before indexing tens of
  thousands of notes.
- Co-tag signals ignore tags attached to more than 100 notes. Broad tags such as
  `#daily` remain stored on each note but do not produce connection suggestions.
- Windows can prevent the final atomic replacement while another program holds
  the database open; close that reader and retry.

## Development

```console
pip install -e ".[test]"
python -m compileall -q src
python -m unittest discover -s tests -v
cd viewer
npm ci
npm run lint
npm test
```

The core uses only the Python standard library and has no runtime dependencies.
The test suite has one optional dependency — the CommonMark parser `mistletoe`,
installed via `pip install -e ".[test]"` — used to prove the Markdown contract
artifact is inert. Future embedding or model integrations should be optional
providers, never a requirement for private local use.

## License

MIT
