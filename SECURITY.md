# Security policy

## Reporting

Please open a private security advisory in the GitHub repository rather than a
public issue when a vulnerability could expose vault content or bypass an
indexing policy.

## Security properties

- Markdown input is treated as data, not executed code.
- Frontmatter parsing supports a small data-only subset and does not construct
  Python objects.
- The core does not execute Obsidian plugins, shell commands, or note content.
- The core does not make network requests.
- Indexing does not follow symlinks, skips file hardlinks, and checks resolved
  input paths remain inside the vault.
- Indexing reads Markdown files and writes only the selected database path.
- The default database is outside the vault; in-vault output requires an
  explicit override.
- Existing non-RecallWeave destination files are not overwritten implicitly.
- Query operations open the database read-only.

## Viewer export boundary

- `export-viewer` requires the same explicit index policy choice as the core.
- Viewer output refuses symlinked destinations and symlinked parent paths,
  refuses the active database as a destination, and does not replace an
  existing file unless `--force` is explicit.
- Forced replacement uses a recoverable, non-overwriting two-phase protocol.
  The prior approved output is retained beside the destination and its path is
  reported in the receipt.
- Export privacy flags describe the content actually written. Structure-only
  exports can still disclose paths, titles, tags, citations, and note-derived
  candidate terms.

## Browser viewer boundary

- Graph JSON is untrusted input. Atlas validates the schema, enforces byte and
  record caps before rendering, strips unsafe controls and invisible format
  characters, and drops duplicate or dangling graph records.
- Displayed privacy profiles are recomputed from normalized content. Values
  asserted by the source file are labeled as claims rather than verified facts.
- `recallweave.viewer.v1` does not authorize direct file-system or Obsidian
  navigation. Atlas does not render links derived from unreviewed vault names
  or note paths.
- Selected graph files are parsed in browser memory and are not uploaded by the
  Atlas application. A hosting provider can still log ordinary requests for the
  application shell and static assets.
- Hosted responses use same-origin isolation headers and a content security
  policy that avoids `unsafe-inline` by hashing the inline scripts present in
  each HTML response. Because those hashes are derived from that response,
  they are not a separate defense against server-side HTML injection.

## Dependency posture

- The Python package has no runtime dependencies.
- The viewer lockfile is audited in full and with development dependencies
  omitted. Release candidates require both audits to report zero known
  vulnerabilities.

## Non-properties

RecallWeave is not an authentication system, a secret scanner, an encryption
tool, or a sandbox. A private assistant integration needs its own identity,
authorization, redaction, and audit controls.
