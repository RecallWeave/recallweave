# Changelog

## 0.1.0 - Unreleased

- optional RecallWeave Atlas viewer with deterministic, in-browser graph
  exploration and a synthetic demonstration graph;
- `export-viewer` structure-only and bounded-passage profiles, bilateral
  candidate citations, explicit privacy content flags, verified-only filtering,
  non-replacing writes, and recoverable two-phase forced replacement;
- `contract` exports a minimal, cited work packet for another agent in JSON and
  Markdown under the `recallweave.contract.v1` schema, with enforced
  exclusions;
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
