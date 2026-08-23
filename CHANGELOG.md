# Changelog

## 0.1.0 - Unreleased

- task contract Markdown renders one fenced block per field with a trusted
  label, preserving the evidence boundary between an operator's statement and
  the citation attached to it, and keeping the projection injective over a
  documented projected field set (omitted canonical fields, such as connection
  score and evidence, are intentionally not projected; the JSON contract
  remains canonical and complete);
- the task contract Markdown's projected and omitted field sets are an
  exhaustive partition of the canonical document, enforced against a document
  the public builder produced across several shapes, and the value-invariance
  proof that an omitted field cannot influence the rendering runs over all
  thirty-one omitted fields, driving each to several values that vary
  cardinality and falsiness so a renderer reading only a length, a truthiness
  or an emptiness is caught;
- task contract evidence classes name the **origin** of a statement, not the
  presence of a citation: an operator's own wording stays
  `authored_by_operator` even when the operator cited a note, and only a
  statement that IS the cited passage is `cited_passage`. The citation and
  passage travel beside an operator statement as support, the Markdown
  projection now shows that supporting passage under its own label instead of
  omitting it, and `truncated` and `passage_truncated` say separately which
  text was shortened. Nothing infers whether a passage supports a statement;
  both are shown and each is attributed;
- `contract` authenticates the persisted edge record, not only its evidence: a
  candidate must carry the candidate kind, an unset verification flag and a
  cosine score in range, and an authored link must carry a real link kind, a
  set verification flag, a unit score, and a link that re-derives from the
  index. For a link in a section body that means the exact physical line read
  back from the indexed section, parsed with the indexer's own link extractor
  and resolved with its own resolver, uniqueness included. A link on a heading
  line is bound the same way: the index records every heading's own physical
  line and `#` level — including a heading with no body beneath it, which
  produces no section but can still carry a link — the exporter reconstructs
  the heading line from indexed data and requires the quoted text to equal it
  at the claimed coordinate, and an index written before those were recorded is
  refused with a request to re-index. So a hand-written row can no longer
  export as an authored, verified relationship. Candidate existence, ranking
  and `score` are deliberately not recomputed: a candidate's score is persisted
  and range-checked, not authenticated, and the docs say so;
- `contract` authenticates a discovery candidate's own evidence, not only its
  passages: `shared_terms` must be at least two non-empty strings that both
  endpoint notes actually carry in the index, and `method` and `explanation`
  must be the indexer's own, so a persisted edge can no longer assert a
  relationship the index does not support or rewrite the standing warning that
  lexical overlap is not proof;
- `contract` attributes every connection-evidence side against the indexed
  snapshot before admitting its connection: the citation must name a section
  the index contains, and the quoted passage and heading must be the ones that
  section holds, so neither a fabricated citation nor a fabricated passage
  behind a real citation can reach the artifact. Attributed citations join
  `provenance.citations` in document order, making the inventory complete. A
  connection evidence side quoting a passage must also carry the citation
  attributing it, and each present side must carry the complete leaf set the
  indexer emits, so a shortened passage can never arrive without the
  `truncated` flag that declares it shortened. Verification reads the index,
  never the vault, so evidence is attributed to the snapshot the index recorded
  rather than to the vault's current bytes;
- `contract` failure receipts carry no vault content: a refused export names
  the offending edge by its database id rather than by its endpoint note paths;
- `contract` fails closed on malformed persisted connection evidence: the
  builder enforces its own well-formedness predicate on each connection it
  considers, before that connection's budget check, and raises rather than
  exporting a document its validator rejects, so nothing malformed is silently
  shown and nothing is silently dropped. This validates every connection the
  export RETURNS; it is not a whole-index scan, because the loop stops once the
  character budget is exhausted and edges ordered after that point are never
  examined;
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
