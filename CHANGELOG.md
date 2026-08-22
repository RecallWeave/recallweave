# Changelog

## 0.1.0 - Unreleased

- task contract Markdown renders one fenced block per field with a trusted
  label, preserving the evidence boundary between an operator's statement and
  the citation attached to it, and keeping the projection injective over a
  documented projected field set (omitted canonical fields, such as connection
  score and evidence, are intentionally not projected; the JSON contract
  remains canonical and complete);
- the task contract Markdown's projected and omitted field sets are now an
  exhaustive partition of the canonical document, enforced against a document
  the public builder produced, and the value-invariance proof that an omitted
  field cannot influence the rendering runs over all thirty-one omitted fields
  rather than the ten retrieved-context leaves alone;
- `contract` fails closed on malformed persisted connection evidence: the
  builder enforces its own well-formedness predicate on every connection before
  the budget is consulted and raises rather than exporting a document its
  validator rejects, so nothing malformed is silently shown and nothing is
  silently dropped;
- task contract Markdown signals an absent field structurally — its trusted
  label followed by the marker as a bare chrome line, with no fenced block —
  so a field whose value is literally that marker text can no longer imitate
  absence; the marker previously rendered inside the fence, in the same channel
  as untrusted operator and vault text;
- optional RecallWeave Atlas viewer with deterministic, in-browser graph
  exploration and a synthetic demonstration graph;
- `export-viewer` structure-only and bounded-passage profiles, bilateral
  candidate citations, explicit privacy content flags, verified-only filtering,
  non-replacing writes, and recoverable two-phase forced replacement;
- `contract` exports a minimal, cited work packet for another agent in JSON and
  Markdown under the `recallweave.contract.v1` schema, with enforced
  exclusions;
- the Markdown contract artifact renders every operator-controlled or
  vault-derived string inside a fenced code block so it is an inert,
  human-readable projection of the canonical JSON contract, never Markdown
  syntax;
- explicit CLI policy choice for indexing: a JSON policy or an acknowledged
  `--no-policy` opt-out;
- clean-clone Atlas builds and dedicated Node CI for install, lint, build, and
  type checking, rendered-shell tests, and graph-import regressions;
- patched Atlas production dependencies with a zero-advisory production audit;
- local deployment metadata excluded from source archives, unused storage
  bindings removed, and security response headers added to the hosted shell;
- Atlas public-release hardening: unreviewed Obsidian deep links removed from
  the v1 viewer contract, source privacy assertions labeled as claims, and
  declared-versus-inspected privacy conflicts made explicit;
- configurable neutral social metadata, same-origin isolation headers,
  SHA-256-hashed inline scripts, and a zero-advisory ESLint 10 toolchain;
- Windows production previews serve their hashed Atlas assets through Vinext
  0.0.53's normalized static-file paths, guarded by an end-to-end asset test;
- corrected landmarks, skip navigation, bounded live announcements, visible
  programmatic focus, roving keyboard navigation, and a clipboard fallback;
- V3 review follow-up adds an ESLint 10 accessibility gate, rejects citations
  changed by Unicode sanitization, clears stale response encodings after HTML
  inspection, accurately scopes response-derived CSP hashes, parses quoted
  script attributes correctly, and exercises legend overflow in the sample;
- complete source distributions including policy examples and contributor
  documentation, plus a synthetic bilateral-evidence demonstration;
- Cold Trails guided discovery documented as a design-only roadmap, pending a
  reviewed `recallweave.viewer.v2` schema;
- local, atomic SQLite indexing for Markdown and Obsidian vaults;
- safe data-only frontmatter parsing;
- verified authored wikilink and Markdown-link edges;
- bounded, explicitly unverified shared-tag supporting signals;
- separately labeled deterministic discovery candidates with bilateral cited
  passages;
- bounded cited query packets;
- dormant-thought resurfacing;
- evidence-path traversal;
- exact include-path and exclusion policy controls;
- external-by-default index storage, symlink/hardlink rejection, strict
  frontmatter privacy evaluation, and safe destination handling;
- fail-closed handling for unsupported YAML value syntax and YAML comments;
- explicit totals and truncation flags for bounded connection lists;
- resolved-path deduplication for intra-vault aliases and junctions;
- versioned JSON output and unresolved-link diagnostics;
- synthetic fixtures and cross-platform CI.
