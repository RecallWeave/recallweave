import assert from "node:assert/strict";
import test from "node:test";

import { VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import { buildColdTrails, exportSavedTrailsMarkdown, refusalMessage, trailTrustLabel } from "../app/cold-trails.ts";

function citedEvidence(source, target, extra = {}) {
  return {
    source_evidence: { citation: `${source}:10-12` },
    target_evidence: { citation: `${target}:20` },
    ...extra,
  };
}

function graph(overrides = {}) {
  return {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [],
    edges: [],
    privacy: {
      export_profile: "graph_metadata_and_note_derived_terms",
      metadata_only: false,
      includes_excerpts: false,
      includes_passage_text: false,
      includes_note_derived_terms: true,
      includes_paths_titles_tags: true,
    },
    import_diagnostics: {
      duplicate_nodes_dropped: 0,
      duplicate_edges_dropped: 0,
      dangling_edges_dropped: 0,
    },
    ...overrides,
  };
}

test("exports saved trail markdown for session handoff", () => {
  const hashA = "a".repeat(64);
  const graphDoc = {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "a.md", title: "Alpha", path: "a.md", content_hash: hashA },
      { id: "b.md", title: "Beta", path: "b.md", content_hash: hashA },
    ],
    edges: [
      {
        id: "candidate-1",
        source: "a.md",
        target: "b.md",
        verified: false,
        evidence: citedEvidence("a.md", "b.md", {
          signals: { lexical_terms: ["ripple", "phase"] },
        }),
      },
    ],
    privacy: { export_profile: "graph_metadata" },
    import_diagnostics: {
      duplicate_nodes_dropped: 0,
      duplicate_edges_dropped: 0,
      dangling_edges_dropped: 0,
    },
  };
  const markdown = exportSavedTrailsMarkdown(graphDoc, [
    {
      type: "unwritten_link",
      trust: "candidate",
      sourceId: "a.md",
      targetId: "b.md",
      edgeId: "candidate-1",
      surpriseTerms: ["ripple"],
      score: 0.5,
      scoreBreakdown: {
        novelty: 1,
        distance: 1,
        evidence: 0.3,
        centrality: 0.2,
        structure: 0,
        penalties: 0,
        total: 0.5,
      },
      headline: "Candidate only",
      structuralFacts: ["No authored path within three hops."],
    },
  ]);
  assert.match(markdown, /# Cold Trails session export/);
  assert.match(markdown, /CANDIDATE - NOT A FACT/);
  assert.match(markdown, /Alpha/);
  assert.match(markdown, /`a\.md:10-12`/);
  assert.match(markdown, /`b\.md:20`/);
});

test("escapes markdown-forging titles in saved exports", () => {
  const graphDoc = {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "x.md", title: "Safe\n## Verified findings", path: "x.md" }],
    edges: [],
    privacy: { export_profile: "graph_metadata" },
    import_diagnostics: {
      duplicate_nodes_dropped: 0,
      duplicate_edges_dropped: 0,
      dangling_edges_dropped: 0,
    },
  };
  const markdown = exportSavedTrailsMarkdown(graphDoc, [
    {
      type: "island",
      trust: "structural",
      sourceId: "x.md",
      targetId: "x.md",
      nodeId: "x.md",
      surpriseTerms: [],
      score: 0.75,
      scoreBreakdown: {
        novelty: 0,
        distance: 0,
        evidence: 0.25,
        centrality: 0,
        structure: 0.2,
        penalties: 0,
        total: 0.75,
      },
      headline: "Structural fact",
      structuralFacts: ["## forged fact"],
    },
  ]);
  assert.match(markdown, /`Safe ## Verified findings`/);
  assert.doesNotMatch(markdown, /^## Verified findings/m);
  assert.match(markdown, /STRUCTURAL FACT/);
  assert.equal(trailTrustLabel("structural"), "STRUCTURAL FACT");
});

test("refuses graphs that are too small or lack candidates", () => {
  const tiny = buildColdTrails(
    graph({
      nodes: Array.from({ length: 5 }, (_, index) => ({
        id: `n-${index}.md`,
        title: `Note ${index}`,
        path: `n-${index}.md`,
        domain: "Alpha",
      })),
      edges: [],
    }),
  );
  assert.equal(tiny.status, "refused");
  assert.equal(tiny.reason, "graph_too_small");
  assert.match(tiny.message, /small enough/i);

  const sparse = buildColdTrails(
    graph({
      nodes: Array.from({ length: 10 }, (_, index) => ({
        id: `n-${index}.md`,
        title: `Note ${index}`,
        path: `n-${index}.md`,
        domain: "Alpha",
      })),
      edges: [
        {
          id: "c1",
          source: "n-0.md",
          target: "n-1.md",
          verified: false,
          evidence: { shared_terms: ["alpha", "beta", "gamma"] },
        },
      ],
    }),
  );
  assert.equal(sparse.status, "refused");
  assert.equal(sparse.reason, "not_enough_candidates");
});

test("selects unwritten link and reinforced candidate trails deterministically", () => {
  const fixture = graph({
    nodes: [
      { id: "a.md", title: "Atlas map", path: "a.md", domain: "Systems" },
      { id: "b.md", title: "Garden log", path: "b.md", domain: "Garden" },
      { id: "c.md", title: "Bridge note", path: "c.md", domain: "Systems" },
      { id: "d.md", title: "Ledger", path: "d.md", domain: "Finance" },
      { id: "e.md", title: "Outline", path: "e.md", domain: "Garden" },
      { id: "f.md", title: "Memo", path: "f.md", domain: "Finance" },
      { id: "g.md", title: "Draft", path: "g.md", domain: "Systems" },
      { id: "h.md", title: "Archive", path: "h.md", domain: "Garden" },
    ],
    edges: [
      { id: "auth-1", source: "c.md", target: "a.md", verified: true },
      { id: "auth-2", source: "c.md", target: "d.md", verified: true },
      {
        id: "candidate-1",
        source: "a.md",
        target: "b.md",
        verified: false,
        evidence: {
          signals: {
            lexical_terms: ["reversible", "threshold", "canopy"],
            shared_tags: ["experiments"],
            mutual_neighbor_ids: ["c.md"],
          },
          source_evidence: { citation: "a.md:10-12", passage: "reversible threshold" },
          target_evidence: { citation: "b.md:4", passage: "canopy detail" },
        },
      },
      {
        id: "candidate-2",
        source: "e.md",
        target: "f.md",
        verified: false,
        evidence: citedEvidence("e.md", "f.md", {
          signals: {
            lexical_terms: ["ledger", "outline", "margin"],
            shared_tags: ["planning"],
          },
        }),
      },
      {
        id: "candidate-3",
        source: "g.md",
        target: "h.md",
        verified: false,
        evidence: citedEvidence("g.md", "h.md", {
          signals: {
            lexical_terms: ["draft", "archive", "margin"],
          },
        }),
      },
    ],
  });

  const first = buildColdTrails(fixture);
  assert.equal(first.status, "ok");
  assert.ok(first.trails.length >= 1);
  const types = new Set(first.trails.map((trail) => trail.type));
  assert.ok(types.has("bridge") || types.has("unwritten_link") || types.has("reinforced"));
  const unwritten = first.trails.find((trail) => trail.type === "unwritten_link");
  if (unwritten) {
    assert.equal(unwritten.trust, "candidate");
    assert.ok(unwritten.surpriseTerms.length >= 2);
    assert.ok(unwritten.scoreBreakdown.evidence >= 0.25);
  }

  const second = buildColdTrails(fixture);
  assert.deepEqual(
    second.status === "ok" ? second.trails.map((trail) => [trail.type, trail.sourceId, trail.targetId]) : [],
    first.status === "ok" ? first.trails.map((trail) => [trail.type, trail.sourceId, trail.targetId]) : [],
  );
});

test("detects island trails for low-degree nodes with multiple candidates", () => {
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Edge" },
      { id: "n2.md", title: "Two", path: "n2.md", domain: "Core" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Core" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Core" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Core" },
    ],
    edges: [
      { id: "auth", source: "hub.md", target: "n2.md", verified: true },
      {
        id: "c1",
        source: "leaf.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n3.md", {
          signals: {
            lexical_terms: ["orbit", "signal", "delta", "phase"],
            shared_tags: ["watchlist"],
          },
        }),
      },
      {
        id: "c2",
        source: "leaf.md",
        target: "n4.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n4.md", {
          signals: {
            lexical_terms: ["orbit", "signal", "theta", "phase"],
            shared_tags: ["watchlist"],
          },
        }),
      },
      {
        id: "c3",
        source: "n5.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("n5.md", "n6.md", {
          signals: {
            lexical_terms: ["vector", "signal", "theta", "phase"],
            shared_tags: ["watchlist"],
          },
        }),
      },
    ],
  });

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(result.trails.some((trail) => trail.type === "island" && trail.nodeId === "leaf.md"));
  const island = result.trails.find((trail) => trail.type === "island" && trail.nodeId === "leaf.md");
  assert.equal(island?.trust, "structural");
});

test("refuses candidate trails without valid citations", () => {
  const fixture = graph({
    nodes: Array.from({ length: 8 }, (_, index) => ({
      id: `note-${index}.md`,
      title: `Note ${index}`,
      path: `note-${index}.md`,
      domain: index % 2 ? "A" : "B",
    })),
    edges: [
      { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
      {
        id: "c1",
        source: "note-2.md",
        target: "note-3.md",
        verified: false,
        evidence: { signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] } },
      },
      {
        id: "c2",
        source: "note-4.md",
        target: "note-5.md",
        verified: false,
        evidence: { signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] } },
      },
      {
        id: "c3",
        source: "note-6.md",
        target: "note-7.md",
        verified: false,
        evidence: { signals: { lexical_terms: ["quasar", "ripple", "tensor", "phase"] } },
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "refused");
});

test("refuses when surprise terms collapse into titles and tags", () => {
  const fixture = graph({
    nodes: Array.from({ length: 8 }, (_, index) => ({
      id: `note-${index}.md`,
      title: `Shared title ${index}`,
      path: `note-${index}.md`,
      domain: "Shared",
      tags: ["shared"],
    })),
    edges: [
      {
        id: "c1",
        source: "note-0.md",
        target: "note-1.md",
        verified: false,
        evidence: { shared_terms: ["shared", "title"] },
      },
      {
        id: "c2",
        source: "note-2.md",
        target: "note-3.md",
        verified: false,
        evidence: { shared_terms: ["shared", "title"] },
      },
      {
        id: "c3",
        source: "note-4.md",
        target: "note-5.md",
        verified: false,
        evidence: { shared_terms: ["shared", "title"] },
      },
    ],
  });

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "refused");
  assert.equal(result.reason, "no_surprise_terms");
  assert.equal(result.message, refusalMessage("no_surprise_terms"));
});

test("notices single-domain graphs without refusing entirely", () => {
  const fixture = graph({
    nodes: Array.from({ length: 8 }, (_, index) => ({
      id: `note-${index}.md`,
      title: `Note ${index}`,
      path: `note-${index}.md`,
      domain: "Only",
    })),
    edges: [
      { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
      {
        id: "c1",
        source: "note-2.md",
        target: "note-3.md",
        verified: false,
        evidence: citedEvidence("note-2.md", "note-3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          shared_tags: ["experiments"],
        }),
      },
      {
        id: "c2",
        source: "note-4.md",
        target: "note-5.md",
        verified: false,
        evidence: citedEvidence("note-4.md", "note-5.md", {
          signals: {
            lexical_terms: ["quasar", "ripple", "matrix", "phase"],
            shared_tags: ["experiments"],
          },
        }),
      },
      {
        id: "c3",
        source: "note-6.md",
        target: "note-7.md",
        verified: false,
        evidence: citedEvidence("note-6.md", "note-7.md", {
          signals: {
            lexical_terms: ["quasar", "ripple", "tensor", "phase"],
            shared_tags: ["experiments"],
          },
        }),
      },
    ],
  });

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.match(result.notice ?? "", /only one domain/i);
});
