import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  importDiagnosticMessage,
  normalizeGraph,
  safeCitation,
  safeIdentifier,
  safeLabel,
  safeText,
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

test("ignores unsupported vault names and treats local generation as a source claim", () => {
  const normalized = normalizeGraph(
    graph({
      vault_name: "Unreviewed vault",
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
  assert.equal("vault_name" in normalized, false);
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
