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

test("bundled sample is honest viewer.v2 with provenance and Cold Trails types", async () => {
  const { buildColdTrails } = await import("../app/cold-trails.ts");
  const sample = JSON.parse(
    await readFile(new URL("../public/sample-graph.json", import.meta.url), "utf8"),
  );
  assert.equal(sample.schema_version, VIEWER_SCHEMA_V2);
  assert.ok(sample.export_history?.export_id);
  assert.equal(typeof sample.export_history.previous_content_hash, "string");
  for (const node of sample.nodes) {
    assert.ok(node.created_at);
    assert.ok(node.modified_at);
    assert.match(node.content_hash, /^[a-f0-9]{64}$/u);
  }
  const candidates = sample.edges.filter((edge) => edge.verified === false);
  for (const candidate of candidates) {
    assert.ok(candidate.evidence?.signals?.lexical_terms?.length);
  }
  const normalized = normalizeGraph(sample);
  assert.equal(normalized.schema_version, VIEWER_SCHEMA_V2);
  assert.equal(normalized.export_history?.claim_conflict, false);
  assert.match(formatAtlasProvenanceClaims(normalized), /export history claim:/);
  const tour = buildColdTrails(normalized);
  assert.equal(tour.status, "ok");
  const types = new Set(tour.trails.map((trail) => trail.type));
  assert.ok(types.has("parallel_invention"), "sample tour should include Parallel invention");
  assert.ok(types.has("dormant"), "sample tour should include Dormant");
  const generatedMs = Date.parse(sample.generated_at);
  const candidateTouch = new Map();
  for (const edge of candidates) {
    candidateTouch.set(edge.source, (candidateTouch.get(edge.source) || 0) + 1);
    candidateTouch.set(edge.target, (candidateTouch.get(edge.target) || 0) + 1);
  }
  const driftEligible = sample.nodes.some((node) => {
    const spanDays =
      (Date.parse(node.modified_at) - Date.parse(node.created_at)) / 86_400_000;
    return spanDays >= 90 && (candidateTouch.get(node.id) || 0) >= 1;
  });
  assert.ok(driftEligible, "sample should keep Drift-eligible timestamped notes");
  assert.ok(Number.isFinite(generatedMs));
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
        tags: ["decisions"],
        created_at: "2026-01-02T03:04:05Z",
        modified_at: "2026-01-03T03:04:05Z",
        content_hash: hashA,
      },
      {
        id: "b.md",
        title: "B",
        path: "b.md",
        tags: ["decisions"],
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

test("AtlasProvenanceChrome renders conflicted and valid export history", async () => {
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { AtlasProvenanceChrome } = await import("../app/components/AtlasProvenanceChrome.ts");

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
  const validHtml = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: valid }),
  );
  assert.match(validHtml, /privacy-provenance-detail/);
  assert.match(validHtml, /export-ok/);
  assert.match(validHtml, /first export claim/);
  assert.doesNotMatch(validHtml, /export history conflicts with loaded graph/);

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
  const conflictHtml = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: conflicted }),
  );
  assert.match(conflictHtml, /export history conflicts with loaded graph/);

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
  const omittedHtml = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: omitted }),
  );
  assert.match(omittedHtml, /export history conflicts with loaded graph/);

  const emptyV2 = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
  });
  assert.equal(
    renderToStaticMarkup(createElement(AtlasProvenanceChrome, { graph: emptyV2 })),
    "",
  );

  const v1 = normalizeGraph({
    schema_version: "recallweave.viewer.v1",
    nodes: [{ id: "a.md", title: "A", path: "a.md" }],
    edges: [],
  });
  assert.equal(
    renderToStaticMarkup(createElement(AtlasProvenanceChrome, { graph: v1 })),
    "",
  );

  const hostile = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "x</span><script>alert(1)</script>",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  const hostileHtml = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: hostile }),
  );
  assert.doesNotMatch(hostileHtml, /<script>/i);
  assert.match(hostileHtml, /&lt;\/span&gt;&lt;script&gt;/i);
});

test("GraphExplorer mounts AtlasExportPrivacyChrome for provenance chrome", async () => {
  const { readFile } = await import("node:fs/promises");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");
  const sourcePath = path.join(
    path.dirname(fileURLToPath(import.meta.url)),
    "../app/components/GraphExplorer.tsx",
  );
  const source = await readFile(sourcePath, "utf8");
  assert.match(source, /AtlasExportPrivacyChrome/);
  assert.match(source, /<AtlasExportPrivacyChrome\b/);
  assert.match(source, /graph=\{graph\}/);
});

test("AtlasExportPrivacyChrome renders GraphExplorer provenance chrome", async () => {
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { AtlasExportPrivacyChrome } = await import(
    "../app/components/AtlasExportPrivacyChrome.ts"
  );
  const { AtlasProvenanceChrome } = await import("../app/components/AtlasProvenanceChrome.ts");

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
  const expectedValidChrome = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: valid }),
  );
  assert.equal(
    expectedValidChrome,
    '<span class="privacy-provenance-detail"> Index claims: export history claim: export export-ok · first export claim · 1 added · 0 removed · 0 hash changes · 0 unchanged.</span>',
  );
  const validBanner = renderToStaticMarkup(
    createElement(AtlasExportPrivacyChrome, { graph: valid }),
  );
  assert.ok(
    validBanner.includes(expectedValidChrome),
    "valid provenance chrome must appear inside GraphExplorer privacy banner",
  );
  assert.match(validBanner, /export-privacy/);
  assert.match(validBanner, /structure-only/);
  assert.doesNotMatch(validBanner, /export history conflicts with loaded graph/);

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
  const expectedConflictChrome = renderToStaticMarkup(
    createElement(AtlasProvenanceChrome, { graph: conflicted }),
  );
  assert.equal(
    expectedConflictChrome,
    '<span class="privacy-provenance-detail"> Index claims: export history claim: export export-bad · follows prior export · 0 added · 0 removed · 9 hash changes · 0 unchanged · export history conflicts with loaded graph.</span>',
  );
  const conflictBanner = renderToStaticMarkup(
    createElement(AtlasExportPrivacyChrome, { graph: conflicted }),
  );
  assert.ok(
    conflictBanner.includes(expectedConflictChrome),
    "conflict provenance chrome must appear inside GraphExplorer privacy banner",
  );

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
  const omittedBanner = renderToStaticMarkup(
    createElement(AtlasExportPrivacyChrome, { graph: omitted }),
  );
  assert.match(omittedBanner, /export history conflicts with loaded graph/);
  assert.match(omittedBanner, /privacy-provenance-detail/);

  const emptyBanner = renderToStaticMarkup(
    createElement(AtlasExportPrivacyChrome, { graph: null }),
  );
  assert.match(emptyBanner, /Load an export to inspect its privacy profile/);
  assert.doesNotMatch(emptyBanner, /privacy-provenance-detail/);
});

test("AtlasExportPrivacyChrome exact SSR for passage and metadata-conflict banners", async () => {
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { AtlasExportPrivacyChrome } = await import(
    "../app/components/AtlasExportPrivacyChrome.ts"
  );

  const passage = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    privacy: undefined,
    nodes: [
      {
        id: "a.md",
        title: "A",
        path: "a.md",
        content_hash: "a".repeat(64),
        summary: "Note-derived passage text",
      },
    ],
    edges: [],
    export_history: {
      export_id: "export-passage",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  assert.equal(passage.privacy.includes_passage_text, true);
  assert.equal(passage.privacy.metadata_conflict, false);
  assert.equal(
    renderToStaticMarkup(createElement(AtlasExportPrivacyChrome, { graph: passage })),
    '<div class="export-privacy contains-excerpts" role="status"><span class="export-privacy-icon" aria-hidden="true"></span><span><strong>Possible excerpts detected</strong><span class="privacy-detail">paths, titles, tags · passages · profile: graph_with_bounded_passage_text</span> Review before screen sharing or sending this file.<span class="privacy-provenance-detail"> Index claims: export history claim: export export-passage · first export claim · 1 added · 0 removed · 0 hash changes · 0 unchanged.</span></span></div>',
  );

  const conflict = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    privacy: { includes_excerpts: false },
    nodes: [
      {
        id: "a.md",
        title: "A",
        path: "a.md",
        content_hash: "a".repeat(64),
        summary: "Unexpected excerpt",
      },
    ],
    edges: [],
    export_history: {
      export_id: "export-conflict-banner",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  assert.equal(conflict.privacy.metadata_conflict, true);
  assert.equal(
    renderToStaticMarkup(createElement(AtlasExportPrivacyChrome, { graph: conflict })),
    '<div class="export-privacy contains-excerpts" role="status"><span class="export-privacy-icon" aria-hidden="true"></span><span><strong>Export privacy flags conflict with displayed content</strong><span class="privacy-detail">paths, titles, tags · passages · profile: graph_with_bounded_passage_text</span> Review before screen sharing or sending this file.<span class="privacy-conflict-detail"> Declared profile: undeclared; inspected content: graph_with_bounded_passage_text.</span><span class="privacy-provenance-detail"> Index claims: export history claim: export export-conflict-banner · first export claim · 1 added · 0 removed · 0 hash changes · 0 unchanged.</span></span></div>',
  );
});

test("normalizeGraph load path feeds AtlasExportPrivacyChrome the same way GraphExplorer does", async () => {
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { readFile } = await import("node:fs/promises");
  const { fileURLToPath } = await import("node:url");
  const path = await import("node:path");
  const { AtlasExportPrivacyChrome } = await import(
    "../app/components/AtlasExportPrivacyChrome.ts"
  );

  const explorerSource = await readFile(
    path.join(path.dirname(fileURLToPath(import.meta.url)), "../app/components/GraphExplorer.tsx"),
    "utf8",
  );
  assert.ok(
    explorerSource.indexOf("loadGraphFromFile") <
      explorerSource.indexOf("setGraph(result.graph)"),
    "loadFile must parse via loadGraphFromFile before setting graph state",
  );
  assert.match(explorerSource, /<AtlasExportPrivacyChrome\s+graph=\{graph\}\s*\/>/);

  // Mirror the post-loadFile UI: parse file text, then render the banner GraphExplorer mounts.
  const raw = {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "loaded-export",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  };
  const { graphFromLoadedFileText } = await import("../app/graph-load.ts");
  const loaded = graphFromLoadedFileText(JSON.stringify(raw));
  const html = renderToStaticMarkup(
    createElement(AtlasExportPrivacyChrome, { graph: loaded }),
  );
  assert.match(html, /loaded-export/);
  assert.match(html, /privacy-provenance-detail/);
  assert.match(html, /first export claim/);
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

test("drops fabricated shared_tags that are not on both endpoint nodes", () => {
  const hash = "a".repeat(64);
  const normalized = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      {
        id: "a.md",
        title: "A",
        path: "a.md",
        tags: ["shared", "only-a"],
        created_at: "2026-01-02T03:04:05Z",
        modified_at: "2026-01-03T03:04:05Z",
        content_hash: hash,
      },
      {
        id: "b.md",
        title: "B",
        path: "b.md",
        tags: ["shared", "only-b"],
        created_at: "2026-01-02T03:04:05Z",
        modified_at: "2026-01-03T03:04:05Z",
        content_hash: hash,
      },
    ],
    edges: [
      {
        id: "candidate",
        source: "a.md",
        target: "b.md",
        verified: false,
        evidence: {
          signals: {
            shared_tags: ["shared", "fabricated", "only-a"],
          },
        },
      },
    ],
  });
  assert.deepEqual(normalized.edges[0].evidence?.signals?.shared_tags, ["shared"]);
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
