# RecallWeave Atlas

RecallWeave Atlas is the optional visual explorer for RecallWeave graph exports.
It makes authored links, candidate connections, domains, and their supporting
evidence legible without turning the graph into a source of truth.

The hosted viewer is an application shell. A graph selected with **Load your
graph** is parsed in browser memory. The application has no graph upload
endpoint and its client code does not transmit the selected file. A hosting
provider can still log ordinary shell requests. The included sample is entirely
synthetic and deliberately excerpt-rich so the evidence interface can be
demonstrated. It is a hand-authored product fixture, not an `export-viewer`
output, and its `privacy` block makes that passage-bearing posture explicit.

## Create a graph

The default structure-only profile omits passage text:

```console
recallweave export-viewer recallweave-graph.json --vault /path/to/vault
```

To include bounded note summaries and evidence excerpts:

```console
recallweave export-viewer recallweave-graph.json --vault /path/to/vault --include-excerpts
```

Use `--verified-only` to omit discovery candidates. Existing files are not
replaced unless `--force` is explicit. Forced replacement uses a recoverable
two-phase backup protocol; see the JSON contract for receipt and recovery
details.

Structure-only does not mean anonymous or metadata-free. The default file still
contains relative paths, titles, tags, status, domains, citations, and
note-derived candidate terms. Inspect the top-level `privacy` object before
sharing a graph:

```json
{
  "export_profile": "graph_metadata_and_note_derived_terms",
  "requested_profile": "without_passage_text",
  "metadata_only": false,
  "includes_passage_text": false,
  "includes_note_derived_terms": true,
  "includes_paths_titles_tags": true
}
```

See [the JSON contract](../docs/json-output.md#export-viewer-and-recallweaveviewerv2)
for the complete `recallweave.viewer.v2` schema (`viewer.v1` remains readable).

## Run locally

Requires Node.js 22.13 or newer.

```console
npm install
npm run dev
```

Open the displayed local URL, then load the exported JSON file. For a release
check:

```console
npm test
```

Hosted builds can set canonical social metadata without modifying tracked
source:

```console
NEXT_PUBLIC_RECALLWEAVE_ORIGIN=https://atlas.example.org npm run build
```

The value must be HTTPS, except that HTTP is accepted for loopback development.
When it is absent or invalid, Atlas uses the neutral reserved origin
`https://recallweave.example`.

## Trust boundary

- The Obsidian vault remains canonical.
- Authored links and discovery candidates stay visually distinct.
- Candidate edges are prompts for review, never facts.
- Exported graph files may contain private metadata. Keep them outside Git and
  do not share them unless you have reviewed their contents.
- Note and evidence excerpts are opt-in at export time.
- Copy-relative-path is the permanent, always-available way to reach a source.
  Atlas also offers an **opt-in "Open in Obsidian"** deep link (`obsidian://open`)
  that appears only when you locally configure an Obsidian vault name in the
  viewer. That configuration is local presentation state only: it never touches
  export bytes, the export schema, hashes, or provenance, the frozen
  `viewer.v2` `vault_name` field stays provenance-only, and the URI is assembled
  in the browser at click time from the configured name and the note's validated
  relative path. Do not invent vault labels in fixtures. See
  [docs/cold-trails.md](../docs/cold-trails.md) ("Vault navigation").

## Cold Trails

[Cold Trails](../docs/cold-trails.md) is the shipped deterministic guided tour for
high-value graph discoveries. It runs in browser memory against a loaded
`recallweave.viewer.v2` graph (timestamps, `content_hash`, `export_history`, and
evidence `signals`). Dismissals persist under a graph fingerprint in
localStorage, with Clear history and Escape/Close ending the tour. The bundled
sample is a hand-authored v2 fixture so the demo can exercise provenance chrome
and timestamp trail types without claiming to be an exporter output.
