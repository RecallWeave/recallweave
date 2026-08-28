import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  citationPath,
  formatAtlasProvenanceClaims,
  formatExportHistoryDetail,
  importDiagnosticMessage,
  normalizeGraph,
  safeCitation,
  safeContentHash,
  safeIdentifier,
  safeLabel,
  safeText,
  safeVaultLabel,
  VIEWER_SCHEMA_V2,
} from "../app/graph-data.ts";

function graph(overrides = {}) {
  return {
    schema_version: "recallweave.viewer.v1",
    nodes: [
      { id: "a.md", title: "A", path: "a.md", domain: "One" },
      { id: "b.md", title: "B", path: "b.md", domain: "Two" },
    ],
    edges: [
      {
        id: "edge-1",
        source: "a.md",
        target: "b.md",
        kind: "wikilink",
        verified: true,
      },
    ],
    privacy: { includes_excerpts: false },
    ...overrides,
  };
}

test("deduplicates nodes and edges and reports dangling edges", () => {
  const normalized = normalizeGraph(
    graph({
      nodes: [
        { id: "a.md", title: "First", path: "a.md" },
        { id: "a.md", title: "Duplicate", path: "duplicate.md" },
        { id: "b.md", title: "B", path: "b.md" },
      ],
      edges: [
        { id: "edge-1", source: "a.md", target: "b.md", verified: true },
        { id: "edge-1", source: "b.md", target: "a.md", verified: true },
        { id: "missing", source: "a.md", target: "missing.md", verified: false },
      ],
    }),
  );

  assert.deepEqual(normalized.nodes.map((node) => node.title), ["First", "B"]);
  assert.equal(normalized.edges.length, 1);
  assert.deepEqual(normalized.import_diagnostics, {
    duplicate_nodes_dropped: 1,
    duplicate_edges_dropped: 1,
    dangling_edges_dropped: 1,
  });
  assert.equal(
    importDiagnosticMessage(normalized.import_diagnostics),
    "1 duplicate node, 1 duplicate edge, 1 dangling edge dropped while loading.",
  );
});

test("strips Unicode default-ignorables and unsafe controls", () => {
  const ignored = [
    "\u00AD",
    "\u034F",
    "\u061C",
    "\u180E",
    "\u200B",
    "\u200C",
    "\u200D",
    "\u200E",
    "\u200F",
    "\u202A",
    "\u202B",
    "\u202C",
    "\u202D",
    "\u202E",
    "\u2060",
    "\u2061",
    "\u2062",
    "\u2063",
    "\u2064",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\uFEFF",
  ];
  for (const character of ignored) {
    assert.equal(safeIdentifier(`safe${character}.md`), "safe.md");
  }
  assert.equal(safeText("line one\nline two\tvalue"), "line one\nline two\tvalue");
  assert.equal(safeLabel("line one\nline two\tvalue"), "line one line two value");
  assert.equal(safeIdentifier("line\nid\t.md"), "lineid.md");
  assert.equal(safeLabel("line\u2028separator\u2029label"), "line separator label");
  assert.equal(safeIdentifier("line\u2028separator\u2029id"), "lineseparatorid");
  assert.equal(safeText("safe\u0000\u0085.md"), "safe.md");
  const normalized = normalizeGraph(
    graph({
      nodes: [
        { id: "a.md", title: "Safe\u202Etxt.exe", path: "a.md" },
        { id: "b.md", title: "B", path: "b.md" },
      ],
    }),
  );
  assert.equal(normalized.nodes[0].title, "Safetxt.exe");
});

test("citations are single-line validated while bilateral passages remain multiline", () => {
  assert.equal(safeCitation("Folder/Note.md:12"), "Folder/Note.md:12");
  assert.equal(safeCitation("Folder/Note.md:12-18"), "Folder/Note.md:12-18");
  assert.equal(safeCitation("Folder/Note.md:18-12"), "");
  assert.equal(safeCitation("Folder/Note.md:0"), "");
  assert.equal(safeCitation("Folder/Note.md:not-a-line"), "");
  assert.equal(safeCitation("Folder/Note.md:12\nForged.md:4"), "");
  assert.equal(safeCitation("Folder/Note.md:12\u2028Forged.md:4"), "");
  assert.equal(safeCitation("Folder/Note.md:1\u202E2"), "");
  assert.equal(safeCitation("Folder/Note.md:12\u2060"), "");

  const sourcePassage = "Source line one\nSource line two\u2028Source paragraph";
  const targetPassage = "Target line one\tTarget detail\u2029Target paragraph";
  const normalized = normalizeGraph(
    graph({
      privacy: {
        export_profile: "graph_with_bounded_passage_text",
        metadata_only: false,
        includes_excerpts: true,
        includes_passage_text: true,
        includes_note_derived_terms: false,
        includes_paths_titles_tags: true,
        generated_locally: true,
      },
      edges: [
        {
          id: "ordinary",
          source: "a.md",
          target: "b.md",
          verified: true,
          evidence: {
            source_evidence: {
              citation: "a.md:10-12",
              passage: sourcePassage,
            },
            target_evidence: {
              citation: "b.md:20",
              passage: targetPassage,
            },
          },
        },
        {
          id: "forged",
          source: "a.md",
          target: "b.md",
          verified: false,
          evidence: {
            source_evidence: {
              citation: "a.md:10\nForged.md:99",
              passage: "Source passage remains.",
            },
            target_evidence: {
              citation: "b.md:20\u2029Forged.md:100",
              passage: "Target passage remains.",
            },
          },
        },
      ],
    }),
  );

  assert.deepEqual(normalized.edges[0].evidence.source_evidence, {
    citation: "a.md:10-12",
    passage: sourcePassage,
  });
  assert.deepEqual(normalized.edges[0].evidence.target_evidence, {
    citation: "b.md:20",
    passage: targetPassage,
  });
  assert.equal(normalized.edges[1].evidence.source_evidence.citation, "");
  assert.equal(normalized.edges[1].evidence.target_evidence.citation, "");
  assert.equal(normalized.edges[1].evidence.source_evidence.passage, "Source passage remains.");
  assert.equal(normalized.edges[1].evidence.target_evidence.passage, "Target passage remains.");
});

test("identifiers that collapse after sanitization deduplicate deterministically", () => {
  const normalized = normalizeGraph(
    graph({
      nodes: [
        { id: "same.md", title: "First", path: "same.md" },
        { id: "sa\u2061me.md", title: "Invisible duplicate", path: "duplicate.md" },
        { id: "other.md", title: "Other", path: "other.md" },
      ],
      edges: [
        { id: "edge", source: "same.md", target: "other.md", verified: true },
        { id: "ed\u00ADge", source: "same.md", target: "other.md", verified: false },
      ],
    }),
  );

  assert.deepEqual(normalized.nodes.map((node) => node.title), ["First", "Other"]);
  assert.equal(normalized.edges.length, 1);
  assert.equal(normalized.edges[0].verified, true);
  assert.equal(normalized.import_diagnostics.duplicate_nodes_dropped, 1);
  assert.equal(normalized.import_diagnostics.duplicate_edges_dropped, 1);
});

test("honors declared privacy and conservatively infers undeclared excerpts", () => {
  const structural = normalizeGraph(graph());
  assert.deepEqual(structural.privacy, {
    export_profile: "graph_metadata",
    declared_export_profile: "undeclared",
    metadata_only: true,
    includes_excerpts: false,
    includes_passage_text: false,
    includes_note_derived_terms: false,
    includes_paths_titles_tags: true,
    source_claims_generated_locally: false,
    declared: true,
    metadata_conflict: false,
  });

  const inferred = normalizeGraph(
    graph({
      privacy: undefined,
      nodes: [
        { id: "a.md", title: "A", path: "a.md", summary: "Note-derived text" },
        { id: "b.md", title: "B", path: "b.md" },
      ],
    }),
  );
  assert.deepEqual(inferred.privacy, {
    export_profile: "graph_with_bounded_passage_text",
    declared_export_profile: "undeclared",
    metadata_only: false,
    includes_excerpts: true,
    includes_passage_text: true,
    includes_note_derived_terms: false,
    includes_paths_titles_tags: true,
    source_claims_generated_locally: false,
    declared: false,
    metadata_conflict: false,
  });

  const conflict = normalizeGraph(
    graph({
      nodes: [
        { id: "a.md", title: "A", path: "a.md", summary: "Unexpected excerpt" },
        { id: "b.md", title: "B", path: "b.md" },
      ],
    }),
  );
  assert.deepEqual(conflict.privacy, {
    export_profile: "graph_with_bounded_passage_text",
    declared_export_profile: "undeclared",
    metadata_only: false,
    includes_excerpts: true,
    includes_passage_text: true,
    includes_note_derived_terms: false,
    includes_paths_titles_tags: true,
    source_claims_generated_locally: false,
    declared: true,
    metadata_conflict: true,
  });
});

test("preserves bilateral evidence and exact export privacy profile", () => {
  const normalized = normalizeGraph(
    graph({
      privacy: {
        export_profile: "graph_metadata_and_note_derived_terms",
        metadata_only: false,
        includes_excerpts: false,
        includes_passage_text: false,
        includes_note_derived_terms: true,
        includes_paths_titles_tags: true,
        generated_locally: true,
      },
      edges: [
        {
          id: "candidate",
          source: "a.md",
          target: "b.md",
          verified: false,
          evidence: {
            source_evidence: { citation: "a.md:10-12" },
            target_evidence: { citation: "b.md:20-22" },
            shared_terms: ["system", "map"],
          },
        },
      ],
    }),
  );

  assert.deepEqual(normalized.edges[0].evidence.source_evidence, {
    citation: "a.md:10-12",
    passage: "",
  });
  assert.deepEqual(normalized.edges[0].evidence.target_evidence, {
    citation: "b.md:20-22",
    passage: "",
  });
  assert.deepEqual(normalized.privacy, {
    export_profile: "graph_metadata_and_note_derived_terms",
    declared_export_profile: "graph_metadata_and_note_derived_terms",
    metadata_only: false,
    includes_excerpts: false,
    includes_passage_text: false,
    includes_note_derived_terms: true,
    includes_paths_titles_tags: true,
    source_claims_generated_locally: true,
    declared: true,
    metadata_conflict: false,
  });
});

test("bundled candidates demonstrate bilateral cited evidence", async () => {
  const sample = JSON.parse(
    await readFile(new URL("../public/sample-graph.json", import.meta.url), "utf8"),
  );
  const candidates = sample.edges.filter((edge) => edge.verified === false);
  assert.equal(candidates.length, 6);
  for (const candidate of candidates) {
    assert.match(candidate.evidence?.source_evidence?.citation ?? "", /:[1-9]\d*(?:-[1-9]\d*)?$/u);
    assert.match(candidate.evidence?.target_evidence?.citation ?? "", /:[1-9]\d*(?:-[1-9]\d*)?$/u);
    assert.ok(candidate.evidence?.source_evidence?.passage);
    assert.ok(candidate.evidence?.target_evidence?.passage);
  }
});

test("bundled sample exercises the legend overflow path", async () => {
  const sample = JSON.parse(
    await readFile(new URL("../public/sample-graph.json", import.meta.url), "utf8"),
  );
  const domains = new Set(sample.nodes.map((node) => node.domain || "Unclassified"));
  assert.equal(sample.nodes.length, 16);
  assert.equal(domains.size, 7);
});

test("reconciles displayed explanation and shared terms against privacy claims", () => {
  const normalized = normalizeGraph(
    graph({
      privacy: {
        export_profile: "graph_metadata",
        metadata_only: true,
        includes_excerpts: false,
        includes_passage_text: false,
        includes_note_derived_terms: false,
        includes_paths_titles_tags: true,
        generated_locally: true,
      },
      edges: [
        {
          id: "candidate",
          source: "a.md",
          target: "b.md",
          verified: false,
          evidence: {
            explanation: "Both notes discuss a private operating constraint.",
            shared_terms: ["constraint"],
          },
        },
      ],
    }),
  );

  assert.equal(normalized.privacy.metadata_only, false);
  assert.equal(normalized.privacy.includes_note_derived_terms, true);
  assert.equal(normalized.privacy.export_profile, "graph_metadata_and_note_derived_terms");
  assert.equal(normalized.privacy.metadata_conflict, true);
});

test("a dangling edge does not reserve its id before a valid edge", () => {
  const normalized = normalizeGraph(
    graph({
      edges: [
        { id: "same-id", source: "a.md", target: "missing.md", verified: false },
        { id: "same-id", source: "a.md", target: "b.md", verified: true },
      ],
    }),
  );

  assert.equal(normalized.edges.length, 1);
  assert.equal(normalized.edges[0].verified, true);
  assert.deepEqual(normalized.import_diagnostics, {
    duplicate_nodes_dropped: 0,
    duplicate_edges_dropped: 0,
    dangling_edges_dropped: 1,
  });
});

test("uses the empty graph privacy profile when no content is displayed", () => {
  const normalized = normalizeGraph(
    graph({
      nodes: [],
      edges: [],
      privacy: {
        export_profile: "empty_graph",
        metadata_only: true,
        includes_excerpts: false,
        includes_passage_text: false,
        includes_note_derived_terms: false,
        includes_paths_titles_tags: false,
        generated_locally: true,
      },
    }),
  );
  assert.equal(normalized.privacy.export_profile, "empty_graph");
  assert.equal(normalized.privacy.metadata_conflict, false);
});

test("accepts validated vault label claims and rejects path-like vault names", () => {
  const accepted = normalizeGraph(
    graph({
      schema_version: VIEWER_SCHEMA_V2,
      vault_name: "Research Vault",
      export_history: {
        export_id: "export-1",
        previous_content_hash: null,
        node_content_hashes_changed: 0,
        node_content_hashes_unchanged: 0,
        nodes_added: 2,
        nodes_removed: 0,
      },
    }),
  );
  assert.equal(accepted.vault_label_claim, "Research Vault");
  assert.equal("vault_name" in accepted, false);

  const rejected = normalizeGraph(
    graph({
      schema_version: VIEWER_SCHEMA_V2,
      vault_name: "../secrets",
      export_history: {
        export_id: "export-2",
        previous_content_hash: null,
        node_content_hashes_changed: 0,
        node_content_hashes_unchanged: 0,
        nodes_added: 2,
        nodes_removed: 0,
      },
    }),
  );
  assert.equal(rejected.vault_label_claim, undefined);
});

test("normalizes viewer.v2 provenance claims and reconciles export history", () => {
  const hashA = "a".repeat(64);
  const hashB = "b".repeat(64);
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      {
        id: "a.md",
        title: "A",
        path: "a.md",
        created_at: "2026-01-02T03:04:05Z",
        modified_at: "2026-01-03T03:04:05Z",
        content_hash: hashA,
      },
      {
        id: "b.md",
        title: "B",
        path: "b.md",
        created_at: null,
        modified_at: null,
        content_hash: hashB,
      },
    ],
    edges: [
      {
        id: "candidate",
        source: "a.md",
        target: "b.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "a.md:10-12" },
          target_evidence: { citation: "b.md:20" },
          signals: {
            lexical_terms: ["system", "map"],
            shared_tags: ["decisions"],
            mutual_neighbor_ids: ["Projects/Related.md"],
          },
        },
      },
    ],
    policy_config_sha256: "c".repeat(64),
    export_history: {
      export_id: "export-v2",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 2,
      nodes_removed: 0,
    },
    privacy: {
      export_profile: "graph_metadata_and_note_derived_terms",
      metadata_only: false,
      includes_excerpts: false,
      includes_passage_text: false,
      includes_note_derived_terms: true,
      includes_paths_titles_tags: true,
    },
  });

  assert.equal(normalized.schema_version, VIEWER_SCHEMA_V2);
  assert.equal(normalized.nodes[0].content_hash, hashA);
  assert.equal(normalized.nodes[0].created_at, "2026-01-02T03:04:05Z");
  assert.equal(normalized.policy_config_sha256_claim, "c".repeat(64));
  assert.equal(normalized.export_history?.claim_conflict, false);
  assert.deepEqual(normalized.edges[0].evidence.signals, {
    lexical_terms: ["system", "map"],
    shared_tags: ["decisions"],
    mutual_neighbor_ids: ["Projects/Related.md"],
  });

  const conflict = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: hashA }],
    edges: [],
    export_history: {
      export_id: "export-conflict",
      previous_content_hash: "b".repeat(64),
      node_content_hashes_changed: 3,
      node_content_hashes_unchanged: 0,
      nodes_added: 0,
      nodes_removed: 0,
    },
  });
  assert.equal(conflict.export_history?.claim_conflict, true);

  const omittedPrior = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: hashA }],
    edges: [],
    export_history: {
      export_id: "export-omitted-prior",
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  assert.equal(omittedPrior.export_history?.previous_content_hash, null);
  assert.equal(omittedPrior.export_history?.claim_conflict, true);
});

test("flags subsequent export with missing overlap counters", () => {
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) },
      { id: "b.md", title: "B", path: "b.md", content_hash: "b".repeat(64) },
    ],
    edges: [],
    export_history: {
      export_id: "second-export",
      previous_content_hash: "c".repeat(64),
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 0,
      nodes_removed: 0,
    },
  });
  assert.equal(normalized.export_history?.claim_conflict, true);
});

test("flags first-export history that claims removals", () => {
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) },
      { id: "b.md", title: "B", path: "b.md", content_hash: "b".repeat(64) },
    ],
    edges: [],
    export_history: {
      export_id: "first-export",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 2,
      nodes_removed: 1,
    },
  });
  assert.equal(normalized.export_history?.claim_conflict, true);
});

test("accepts producer-shaped first export history", () => {
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) },
      { id: "b.md", title: "B", path: "b.md", content_hash: "b".repeat(64) },
    ],
    edges: [],
    export_history: {
      export_id: "first-export",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 2,
      nodes_removed: 0,
    },
  });
  assert.equal(normalized.export_history?.claim_conflict, false);
});

test("flags malformed export-history counters instead of coercing them", () => {
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) },
      { id: "b.md", title: "B", path: "b.md", content_hash: "b".repeat(64) },
    ],
    edges: [],
    export_history: {
      export_id: "malformed-export",
      previous_content_hash: null,
      node_content_hashes_changed: "0",
      node_content_hashes_unchanged: -1,
      nodes_added: 2,
      nodes_removed: 0,
    },
  });
  assert.equal(normalized.export_history?.claim_conflict, true);
});

test("omitting any required export_history counter claims conflict", () => {
  const baseHistory = {
    export_id: "first-export",
    previous_content_hash: null,
    node_content_hashes_changed: 0,
    node_content_hashes_unchanged: 0,
    nodes_added: 1,
    nodes_removed: 0,
  };
  for (const omitted of [
    "node_content_hashes_changed",
    "node_content_hashes_unchanged",
    "nodes_added",
    "nodes_removed",
  ]) {
    const history = { ...baseHistory };
    delete history[omitted];
    const normalized = normalizeGraph({
      schema_version: VIEWER_SCHEMA_V2,
      nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
      edges: [],
      export_history: history,
    });
    assert.equal(
      normalized.export_history?.claim_conflict,
      true,
      `expected claim_conflict when omitting ${omitted}`,
    );
  }
});

test("formatExportHistoryDetail includes conflict wording only when claims disagree", () => {
  const valid = formatExportHistoryDetail({
    export_id: "export-ok",
    previous_content_hash: null,
    node_content_hashes_changed: 0,
    node_content_hashes_unchanged: 0,
    nodes_added: 2,
    nodes_removed: 0,
    claim_conflict: false,
  });
  assert.match(valid, /first export claim/);
  assert.doesNotMatch(valid, /export history conflicts with loaded graph/);

  const conflicted = formatExportHistoryDetail({
    export_id: "export-bad",
    previous_content_hash: "a".repeat(64),
    node_content_hashes_changed: 9,
    node_content_hashes_unchanged: 0,
    nodes_added: 0,
    nodes_removed: 0,
    claim_conflict: true,
  });
  assert.match(conflicted, /follows prior export/);
  assert.match(conflicted, /export history conflicts with loaded graph/);
});

test("formatAtlasProvenanceClaims surfaces conflicted and valid export history", () => {
  const valid = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "export-ok",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  const validClaims = formatAtlasProvenanceClaims(valid);
  assert.match(validClaims, /export history claim:/);
  assert.match(validClaims, /export-ok/);
  assert.match(validClaims, /first export claim/);
  assert.doesNotMatch(validClaims, /export history conflicts with loaded graph/);

  const conflicted = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "export-bad",
      previous_content_hash: "b".repeat(64),
      node_content_hashes_changed: 9,
      node_content_hashes_unchanged: 0,
      nodes_added: 0,
      nodes_removed: 0,
    },
  });
  assert.equal(conflicted.export_history?.claim_conflict, true);
  const conflictClaims = formatAtlasProvenanceClaims(conflicted);
  assert.match(conflictClaims, /export history conflicts with loaded graph/);

  const omitted = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "omitted-prior",
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  assert.match(
    formatAtlasProvenanceClaims(omitted),
    /export history conflicts with loaded graph/,
  );
});

test("GraphExplorer wires formatAtlasProvenanceClaims into provenance chrome", async () => {
  const { readFile } = await import("node:fs/promises");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");
  const sourcePath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "../app/components/GraphExplorer.tsx",
  );
  const source = await readFile(sourcePath, "utf8");
  assert.match(source, /formatAtlasProvenanceClaims\s*\(/);
  assert.match(source, /privacy-provenance-detail/);
});

test("citationPath extracts the note path from validated citations", () => {
  assert.equal(citationPath("Folder/Note.md:12"), "Folder/Note.md");
  assert.equal(citationPath("Folder/Note.md:12-18"), "Folder/Note.md");
  assert.equal(citationPath("Folder/Note.md:18-12"), "");
  assert.equal(safeContentHash(""), null);
  assert.equal(safeContentHash("not-a-hash"), null);
  assert.equal(safeContentHash("A".repeat(64)), "a".repeat(64));
  assert.equal(safeVaultLabel("obsidian vault"), "");
});

test("rejects non-UTC and malformed graph generated_at instead of preserving labels", () => {
  const cases = [
    "2026-08-28",
    "08/28/2026",
    " 2026-08-28T00:00:00Z ",
    "not-a-date",
    "2026-08-28T00:00:00",
    "2026-02-30T00:00:00Z",
  ];
  for (const generated_at of [
    ...cases,
    "2026-01-01T00:00:00+99:99",
    "2026-01-01T00:00:00+24:00",
    "0099-01-01T00:00:00Z",
    "0000-01-01T00:00:00Z",
  ]) {
    const normalized = normalizeGraph(graph({ generated_at }));
    assert.equal(
      normalized.generated_at,
      undefined,
      `expected rejection for ${JSON.stringify(generated_at)}`,
    );
  }

  const accepted = normalizeGraph(graph({ generated_at: "2026-08-28T00:00:00Z" }));
  assert.equal(accepted.generated_at, "2026-08-28T00:00:00Z");

  const offset = normalizeGraph(graph({ generated_at: "2026-08-28T00:00:00+00:00" }));
  assert.equal(offset.generated_at, "2026-08-28T00:00:00Z");

  const fractional = normalizeGraph(graph({ generated_at: "2026-08-28T00:00:00.001Z" }));
  assert.equal(fractional.generated_at, "2026-08-28T00:00:00.001Z");

  const micro = normalizeGraph(graph({ generated_at: "2026-06-15T00:00:00.000001Z" }));
  assert.equal(micro.generated_at, "2026-06-15T00:00:00.000001Z");

  const offsetNodes = normalizeGraph(
    graph({
      schema_version: VIEWER_SCHEMA_V2,
      nodes: [
        {
          id: "a.md",
          title: "A",
          path: "a.md",
          created_at: "2026-01-02T03:04:05+02:00",
          modified_at: "2026-01-02T04:04:05+02:00",
        },
        { id: "b.md", title: "B", path: "b.md" },
      ],
    }),
  );
  assert.equal(offsetNodes.nodes[0].created_at, "2026-01-02T01:04:05Z");
  assert.equal(offsetNodes.nodes[0].modified_at, "2026-01-02T02:04:05Z");
});

test("ignores unsupported vault names and treats local generation as a source claim", () => {
  const normalized = normalizeGraph(
    graph({
      privacy: {
        export_profile: "graph_metadata",
        metadata_only: true,
        includes_excerpts: false,
        includes_passage_text: false,
        includes_note_derived_terms: false,
        includes_paths_titles_tags: true,
        generated_locally: true,
      },
    }),
  );
  assert.equal(normalized.vault_label_claim, undefined);
  assert.equal(normalized.privacy.source_claims_generated_locally, true);
});

test("rejects malformed nodes and count caps", () => {
  assert.throws(
    () => normalizeGraph(graph({ nodes: [null] })),
    /Node 1 is not an object/,
  );
  assert.throws(
    () => normalizeGraph(graph({ nodes: Array.from({ length: 5001 }, (_, id) => ({ id: `${id}` })) })),
    /Graph exceeds the viewer limit/,
  );
});
