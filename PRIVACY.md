# Privacy

RecallWeave's core runs locally and performs no network calls.

The derived database contains full note passages. Protect it like the vault
itself:

- keep it outside synchronized or public folders unless that is intentional;
- do not commit it to Git;
- exclude sensitive folders before indexing;
- delete and rebuild it when access rules change;
- do not expose the CLI through a network service without authentication and
  an allowlisting broker.

The repository's ignore rules cover common database names, `.env` files, local
configuration, and the `.recallweave` directory. Ignore rules reduce accidents;
they do not replace reviewing staged files before a public commit.

Optional future model providers must disclose exactly what content leaves the
machine. They will not be enabled by default.

## Atlas graph exports

`export-viewer` creates a separate local JSON file. By default it requests no
passage text. A typical graph with candidates reports the actual profile
`graph_metadata_and_note_derived_terms`; a graph without candidate terms may
report `graph_metadata`, and an empty graph reports `empty_graph`. A
structure-only graph is **not metadata-free** and may still be sensitive. It
can contain:

- relative note paths and titles;
- tags, status, domain, and section counts;
- graph endpoints, edge kinds, scores, and citations;
- up to 12 note-derived shared terms for a discovery candidate.

These fields can reveal a person's folder taxonomy, projects, organizations,
people, and working vocabulary. Treat a structure-only graph as confidential
unless you have reviewed it. `--include-excerpts` changes the profile to
`graph_with_bounded_passage_text` and additionally includes bounded note
summaries and bilateral evidence passages.

The export embeds machine-readable `privacy` flags describing both the
requested posture and content classes actually present. Consumers should
display actual-content flags rather than infer privacy from a command option or
empty field.

Atlas parses a selected graph in browser memory. The application has no graph
upload endpoint, analytics, telemetry, or third-party fonts, and its client code
does not transmit the selected file. Hosting providers can still log ordinary
requests for the application shell; that operational logging is separate from
the locally selected graph contents.

Keep graph exports outside Git and synchronized or shared folders unless
sharing is intentional. Review the node list and `privacy` object before
screen-sharing or sending an export. `--verified-only` removes discovery
candidates but does not remove note paths, titles, or tags.

`--force` uses a recoverable two-phase replacement and retains the previous
approved output as a private same-directory backup. The receipt returns that
backup path. Protect and review both files; RecallWeave does not silently delete
the recovery artifact.

By default, indexes are stored outside the vault in a platform application-data
directory. `--database` may select another location. An in-vault destination is
refused unless `--allow-in-vault` is explicit, and then the index receipt reports
the write rather than claiming zero vault writes.

The CLI requires one explicit indexing posture: `--config <policy.json>` or
`--no-policy`. The latter is an acknowledgment that every Markdown file in the
selected vault is safe to index; it does not apply sensitivity or path rules.
For private and agent-facing indexes, prefer a reviewed `include_paths`
allowlist.

File and directory symlinks are skipped. When a frontmatter deny rule is
configured, malformed or unsupported frontmatter values cause the entire note
to be skipped rather than guessed at. This applies when an unsupported
construct appears under any frontmatter key, not only a configured deny key.
Common examples include nested mappings, block or multiline scalars, YAML tags,
anchors, aliases, flow mappings, template expressions, double-quoted escape
sequences, unsupported keys, and nested flow or block collections. Supported
lists are one-dimensional and contain scalar values only. Validation is
bounded and applies to every supported list item; a parser recursion failure
skips only the affected note. These notes are counted as
`unparseable_frontmatter` in the index receipt. Exact `include_paths` allowlists
remain the strongest boundary for an agent-facing index.
