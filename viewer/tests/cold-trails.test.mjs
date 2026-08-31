import assert from "node:assert/strict";
import test from "node:test";

import { VIEWER_SCHEMA_V2, normalizeGraph } from "../app/graph-data.ts";
import {
  buildColdTrails,
  classifyCandidateEdgeTypes,
  exportSavedTrailsMarkdown,
  refusalMessage,
  resolveTrailSourcePath,
  trailTrustLabel,
} from "../app/cold-trails.ts";

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
    generated_at: "2026-08-28T00:00:00Z",
    nodes: [],
    edges: [],
    privacy: {
      export_profile: "graph_metadata_and_note_derived_terms",
      metadata_only: false,
      includes_excerpts: false,
      includes_passage_text: true,
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
        ageBonus: 0,
        penalties: 0,
        total: 0.5,
      },
      headline: "Candidate only",
      structuralFacts: ["No authored path within three hops."],
    },
  ]);
  assert.equal(
    markdown,
    [
      "# Cold Trails session export",
      "",
      "## 1. Unwritten link",
      "- Trust: CANDIDATE - NOT A FACT",
      "- Notes: `Alpha` / `Beta`",
      "- Citation: `a.md:10-12`",
      "- Citation: `b.md:20`",
      "- Surprise terms: `ripple`",
      "- Fact: `No authored path within three hops.`",
      "",
    ].join("\n"),
  );
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
        ageBonus: 0,
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

test("surprise qualification ignores locale-specific casing rules", () => {
  const fixture = graph({
    nodes: Array.from({ length: 8 }, (_, index) => ({
      id: `note-${index}.md`,
      title: index === 2 ? "TITLE" : `Note ${index}`,
      path: `note-${index}.md`,
      domain: "Shared",
    })),
    edges: [
      { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
      {
        id: "c1",
        source: "note-2.md",
        target: "note-3.md",
        verified: false,
        evidence: citedEvidence("note-2.md", "note-3.md", {
          signals: { lexical_terms: ["title", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c2",
        source: "note-4.md",
        target: "note-5.md",
        verified: false,
        evidence: citedEvidence("note-4.md", "note-5.md", {
          signals: { lexical_terms: ["phase", "ripple", "matrix", "tensor"] },
        }),
      },
      {
        id: "c3",
        source: "note-6.md",
        target: "note-7.md",
        verified: false,
        evidence: citedEvidence("note-6.md", "note-7.md", {
          signals: { lexical_terms: ["phase", "ripple", "theta", "tensor"] },
        }),
      },
    ],
  });
  const english = buildColdTrails(fixture);
  const prior = Intl.DateTimeFormat.prototype.resolvedOptions;
  Intl.DateTimeFormat.prototype.resolvedOptions = function resolvedOptions() {
    return { ...prior.call(this), locale: "tr-TR" };
  };
  const turkish = buildColdTrails(fixture);
  Intl.DateTimeFormat.prototype.resolvedOptions = prior;
  assert.deepEqual(
    english.status === "ok" ? english.trails.map((trail) => trail.edgeId) : english,
    turkish.status === "ok" ? turkish.trails.map((trail) => trail.edgeId) : turkish,
  );
});

test("excludes candidates with one-sided or mismatched citations", () => {
  const nodes = Array.from({ length: 8 }, (_, index) => ({
    id: `note-${index}.md`,
    title: `Note ${index}`,
    path: `note-${index}.md`,
    domain: index % 2 ? "A" : "B",
  }));
  const mixed = buildColdTrails(
    graph({
      nodes,
      edges: [
        { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
        { id: "auth-bridge", source: "note-0.md", target: "note-2.md", verified: true },
        {
          id: "c1",
          source: "note-2.md",
          target: "note-3.md",
          verified: false,
          evidence: {
            source_evidence: { citation: "note-2.md:10" },
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          },
        },
        {
          id: "c2",
          source: "note-4.md",
          target: "note-5.md",
          verified: false,
          evidence: citedEvidence("note-4.md", "note-5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "note-6.md",
          target: "note-7.md",
          verified: false,
          evidence: citedEvidence("note-6.md", "note-7.md", {
            signals: { lexical_terms: ["quasar", "ripple", "tensor", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(mixed.status, "ok");
  assert.ok(!mixed.trails.some((trail) => trail.edgeId === "c1"));

  const mismatched = buildColdTrails(
    graph({
      nodes,
      edges: [
        { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
        { id: "auth-bridge", source: "note-0.md", target: "note-2.md", verified: true },
        {
          id: "bad",
          source: "note-2.md",
          target: "note-3.md",
          verified: false,
          evidence: citedEvidence("note-2.md", "note-99.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c2",
          source: "note-4.md",
          target: "note-5.md",
          verified: false,
          evidence: citedEvidence("note-4.md", "note-5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "note-6.md",
          target: "note-7.md",
          verified: false,
          evidence: citedEvidence("note-6.md", "note-7.md", {
            signals: { lexical_terms: ["quasar", "ripple", "tensor", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(mismatched.status, "ok");
  assert.ok(!mismatched.trails.some((trail) => trail.edgeId === "bad"));
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
        id: "c0",
        source: "note-2.md",
        target: "note-4.md",
        verified: false,
        evidence: citedEvidence("note-2.md", "note-4.md", {
          signals: { lexical_terms: ["orbit", "signal", "delta", "phase"] },
        }),
      },
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
  assert.ok(result.trails.some((trail) => trail.type === "island"));
  assert.ok(isStructuralTrail(result.trails[0]));
});

test("refuses candidate-only tours when no structural trail exists", () => {
  const result = buildColdTrails(
    graph({
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
          }),
        },
        {
          id: "c2",
          source: "note-4.md",
          target: "note-5.md",
          verified: false,
          evidence: citedEvidence("note-4.md", "note-5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "note-6.md",
          target: "note-7.md",
          verified: false,
          evidence: citedEvidence("note-6.md", "note-7.md", {
            signals: { lexical_terms: ["quasar", "ripple", "tensor", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(result.status, "refused");
});

test("allows two same-domain trails before exhausting the domain touch limit", () => {
  const result = buildColdTrails(
    graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
        { id: "core-b.md", title: "Core B", path: "core-b.md", domain: "Core" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
        { id: "only-b.md", title: "Only B", path: "only-b.md", domain: "Only" },
        { id: "only-c.md", title: "Only C", path: "only-c.md", domain: "Only" },
        { id: "only-d.md", title: "Only D", path: "only-d.md", domain: "Only" },
        { id: "only-e.md", title: "Only E", path: "only-e.md", domain: "Only" },
        { id: "only-f.md", title: "Only F", path: "only-f.md", domain: "Only" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        { id: "auth-2", source: "hub.md", target: "core-b.md", verified: true },
        {
          id: "c1",
          source: "only-b.md",
          target: "only-c.md",
          verified: false,
          evidence: citedEvidence("only-b.md", "only-c.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c2",
          source: "only-d.md",
          target: "only-e.md",
          verified: false,
          evidence: citedEvidence("only-d.md", "only-e.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "only-f.md",
          target: "edge.md",
          verified: false,
          evidence: {
            source_evidence: { citation: "only-f.md:10" },
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          },
        },
      ],
    }),
  );
  assert.equal(result.status, "ok");
  assert.ok(isOpeningStructuralTrail(result.trails[0]));
  const onlyDomainCandidates = result.trails.filter(
    (trail) =>
      trail.trust === "candidate" &&
      [trail.sourceId, trail.targetId].every((id) => id.startsWith("only-")),
  );
  assert.ok(onlyDomainCandidates.length >= 2);
});

function isOpeningStructuralTrail(trail) {
  return trail.type === "bridge" || trail.type === "island";
}

function isStructuralTrail(trail) {
  return (
    isOpeningStructuralTrail(trail) ||
    trail.type === "dormant" ||
    trail.type === "drift"
  );
}

test("requires three covered domains when the eligible pool spans three", () => {
  const result = buildColdTrails(
    graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
        { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
        { id: "only-b.md", title: "Only B", path: "only-b.md", domain: "Only" },
        { id: "only-c.md", title: "Only C", path: "only-c.md", domain: "Only" },
        { id: "only-d.md", title: "Only D", path: "only-d.md", domain: "Only" },
        { id: "only-e.md", title: "Only E", path: "only-e.md", domain: "Only" },
        { id: "only-f.md", title: "Only F", path: "only-f.md", domain: "Only" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        { id: "auth-2", source: "hub.md", target: "leaf.md", verified: true },
        {
          id: "c1",
          source: "only-b.md",
          target: "only-c.md",
          verified: false,
          evidence: citedEvidence("only-b.md", "only-c.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c2",
          source: "only-d.md",
          target: "only-e.md",
          verified: false,
          evidence: citedEvidence("only-d.md", "only-e.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "only-f.md",
          target: "edge.md",
          verified: false,
          evidence: citedEvidence("only-f.md", "edge.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(result.status, "ok");
  const domainById = new Map(
    [
      ["hub.md", "Core"],
      ["edge.md", "Edge"],
      ["leaf.md", "Garden"],
      ["only-b.md", "Only"],
      ["only-c.md", "Only"],
      ["only-d.md", "Only"],
      ["only-e.md", "Only"],
      ["only-f.md", "Only"],
    ],
  );
  const domains = new Set(
    result.trails.flatMap((trail) => {
      const ids = trail.nodeId ? [trail.nodeId] : [trail.sourceId, trail.targetId];
      return ids.map((id) => domainById.get(id) || "Unclassified");
    }),
  );
  assert.ok(domains.size >= 3);
});

test("detects dormant trails from unmodified notes with candidate edges", () => {
  const oldStamp = "2024-01-01T00:00:00Z";
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
      { id: "sleep.md", title: "Sleep", path: "sleep.md", domain: "Archive", modified_at: oldStamp },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      {
        id: "c1",
        source: "sleep.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "sleep.md:10-12", passage: "dormant signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(isOpeningStructuralTrail(result.trails[0]));
  const dormant = result.trails.find((trail) => trail.type === "dormant");
  assert.ok(dormant);
  assert.equal(dormant?.nodeId, "sleep.md");
  assert.equal(dormant?.trust, "structural");
  assert.match(dormant?.structuralFacts.join(" ") ?? "", /modified_at/);
});

test("detects parallel invention from near-simultaneous cross-domain candidates", () => {
  const stampA = "2026-06-01T00:00:00Z";
  const stampB = "2026-06-08T00:00:00Z";
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: stampA },
      { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: stampB },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
      { id: "n8.md", title: "Eight", path: "n8.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
      // Authored path through hub so Parallel still qualifies but Unwritten does not.
      { id: "auth-3", source: "hub.md", target: "alpha.md", verified: true },
      { id: "auth-4", source: "hub.md", target: "beta.md", verified: true },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "parallel",
        source: "alpha.md",
        target: "beta.md",
        verified: false,
        evidence: citedEvidence("alpha.md", "beta.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n8.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n8.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  const parallel = result.trails.find((trail) => trail.type === "parallel_invention");
  assert.ok(parallel);
  assert.equal(parallel?.trust, "candidate");
  assert.match(parallel?.structuralFacts.join(" ") ?? "", /created_at/);
});

test("refuses parallel invention for future timestamps years apart", () => {
  const fixture = graph({
    generated_at: "2026-08-28T00:00:00Z",
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: "2030-01-01T00:00:00Z" },
      { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: "2040-01-01T00:00:00Z" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
      {
        id: "parallel",
        source: "alpha.md",
        target: "beta.md",
        verified: false,
        evidence: citedEvidence("alpha.md", "beta.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c2",
        source: "n5.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("n5.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n4.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n4.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(!result.trails.some((trail) => trail.type === "parallel_invention"));
  assert.ok(
    !result.trails.some((trail) => (trail.structuralFacts || []).join(" ").includes("within 14 days")),
  );
});

test("detects drift trails from long-lived notes with candidate edges", () => {
  const fixture = graph({
    nodes: [
      {
        id: "hub.md",
        title: "Hub",
        path: "hub.md",
        domain: "Core",
        created_at: "2026-07-01T00:00:00Z",
        modified_at: "2026-07-02T00:00:00Z",
      },
      {
        id: "core-b.md",
        title: "Core B",
        path: "core-b.md",
        domain: "Core",
        created_at: "2026-07-01T00:00:00Z",
        modified_at: "2026-07-02T00:00:00Z",
      },
      {
        id: "drift.md",
        title: "Drift note",
        path: "drift.md",
        domain: "Archive",
        created_at: "2024-01-01T00:00:00Z",
        modified_at: "2026-07-01T00:00:00Z",
      },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "core-b.md", verified: true },
      {
        id: "c1",
        source: "drift.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "drift.md:10-12", passage: "drift signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
    export_history: {
      export_id: "export-2",
      previous_content_hash: "a".repeat(64),
      node_content_hashes_changed: 2,
      node_content_hashes_unchanged: 6,
      nodes_added: 0,
      nodes_removed: 0,
      claim_conflict: false,
    },
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(isOpeningStructuralTrail(result.trails[0]));
  const drift = result.trails.find((trail) => trail.type === "drift");
  assert.ok(drift);
  assert.equal(drift?.nodeId, "drift.md");
  assert.match(drift?.structuralFacts.join(" ") ?? "", /created_at/);
  assert.match(drift?.structuralFacts.join(" ") ?? "", /export history/i);
});

test("refuses drift when modified_at precedes created_at", () => {
  const fixture = graph({
    nodes: [
      {
        id: "hub.md",
        title: "Hub",
        path: "hub.md",
        domain: "Core",
        created_at: "2026-07-01T00:00:00Z",
        modified_at: "2026-07-02T00:00:00Z",
      },
      {
        id: "core-b.md",
        title: "Core B",
        path: "core-b.md",
        domain: "Core",
        created_at: "2026-07-01T00:00:00Z",
        modified_at: "2026-07-02T00:00:00Z",
      },
      {
        id: "reversed.md",
        title: "Reversed",
        path: "reversed.md",
        domain: "Archive",
        created_at: "2026-06-01T00:00:00Z",
        modified_at: "2024-01-01T00:00:00Z",
      },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "core-b.md", verified: true },
      {
        id: "c1",
        source: "reversed.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "reversed.md:10-12", passage: "reversed signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(!result.trails.some((trail) => trail.type === "drift"));
});

test("buildColdTrails is deterministic for the same graph and reference clock", () => {
  const fixture = graph({
    generated_at: "2026-08-28T12:00:00Z",
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "only-b.md", title: "Only B", path: "only-b.md", domain: "Only" },
      { id: "only-c.md", title: "Only C", path: "only-c.md", domain: "Only" },
      { id: "only-d.md", title: "Only D", path: "only-d.md", domain: "Only" },
      { id: "only-e.md", title: "Only E", path: "only-e.md", domain: "Only" },
      { id: "only-f.md", title: "Only F", path: "only-f.md", domain: "Only" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "leaf.md", verified: true },
      {
        id: "c1",
        source: "only-b.md",
        target: "only-c.md",
        verified: false,
        evidence: citedEvidence("only-b.md", "only-c.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c2",
        source: "only-d.md",
        target: "only-e.md",
        verified: false,
        evidence: citedEvidence("only-d.md", "only-e.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "only-f.md",
        target: "edge.md",
        verified: false,
        evidence: citedEvidence("only-f.md", "edge.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const first = buildColdTrails(fixture);
  const second = buildColdTrails(fixture);
  assert.deepEqual(first, second);
});

test("reported scores match the weighted scoring formula", () => {
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

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  for (const trail of result.trails) {
    const { novelty, distance, evidence, centrality, structure, ageBonus, penalties, total } =
      trail.scoreBreakdown;
    const expected = Math.max(
      0,
      0.3 * novelty +
        0.25 * distance +
        0.25 * evidence +
        0.1 * centrality +
        0.1 * structure -
        penalties +
        ageBonus,
    );
    assert.equal(total, expected);
    assert.equal(trail.score, expected);
  }
});

test("successful tours satisfy aggregate selection invariants", () => {
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "only-b.md", title: "Only B", path: "only-b.md", domain: "Only" },
      { id: "only-c.md", title: "Only C", path: "only-c.md", domain: "Only" },
      { id: "only-d.md", title: "Only D", path: "only-d.md", domain: "Only" },
      { id: "only-e.md", title: "Only E", path: "only-e.md", domain: "Only" },
      { id: "only-f.md", title: "Only F", path: "only-f.md", domain: "Only" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "leaf.md", verified: true },
      {
        id: "c1",
        source: "only-b.md",
        target: "only-c.md",
        verified: false,
        evidence: citedEvidence("only-b.md", "only-c.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c2",
        source: "only-d.md",
        target: "only-e.md",
        verified: false,
        evidence: citedEvidence("only-d.md", "only-e.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "only-f.md",
        target: "edge.md",
        verified: false,
        evidence: citedEvidence("only-f.md", "edge.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(isStructuralTrail(result.trails[0]));
  const typeCounts = new Map();
  const domainTouches = new Map();
  const usedNodes = new Set();
  for (const trail of result.trails) {
    typeCounts.set(trail.type, (typeCounts.get(trail.type) || 0) + 1);
    assert.ok((typeCounts.get(trail.type) || 0) <= 2);
    const endpoints = trail.nodeId ? [trail.nodeId] : [trail.sourceId, trail.targetId];
    for (const id of endpoints) {
      assert.ok(!usedNodes.has(id), `node ${id} reused`);
      usedNodes.add(id);
    }
    const domains = new Set(
      endpoints.map((id) => {
        const node = fixture.nodes.find((item) => item.id === id);
        return node?.domain || "Unclassified";
      }),
    );
    for (const domain of domains) {
      domainTouches.set(domain, (domainTouches.get(domain) || 0) + 1);
      assert.ok(domainTouches.get(domain) <= 2);
    }
  }
});

test("keeps citation-backed candidate trails when the export has no passage text", () => {
  const fixture = graph({
    privacy: {
      export_profile: "graph_metadata_and_note_derived_terms",
      metadata_only: false,
      includes_excerpts: false,
      includes_passage_text: false,
      includes_note_derived_terms: true,
      includes_paths_titles_tags: true,
    },
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Edge" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Core" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Edge" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "leaf.md", verified: true },
      {
        id: "c1",
        source: "n3.md",
        target: "n4.md",
        verified: false,
        evidence: citedEvidence("n3.md", "n4.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c2",
        source: "n5.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("n5.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "leaf.md",
        verified: false,
        evidence: citedEvidence("n7.md", "leaf.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(result.trails.some((trail) => trail.trust === "candidate"));
  assert.match(result.notice ?? "", /citations and signals only/i);
});

test("counts same-domain endpoint trails once against the domain touch limit", () => {
  const result = buildColdTrails(
    graph({
      nodes: Array.from({ length: 8 }, (_, index) => ({
        id: `note-${index}.md`,
        title: `Note ${index}`,
        path: `note-${index}.md`,
        domain: "Only",
      })),
      edges: [
        { id: "auth", source: "note-0.md", target: "note-1.md", verified: true },
        {
          id: "c0",
          source: "note-2.md",
          target: "note-4.md",
          verified: false,
          evidence: citedEvidence("note-2.md", "note-4.md", {
            signals: { lexical_terms: ["orbit", "signal", "delta", "phase"] },
          }),
        },
        {
          id: "c1",
          source: "note-2.md",
          target: "note-3.md",
          verified: false,
          evidence: citedEvidence("note-2.md", "note-3.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c2",
          source: "note-5.md",
          target: "note-6.md",
          verified: false,
          evidence: citedEvidence("note-5.md", "note-6.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "note-6.md",
          target: "note-7.md",
          verified: false,
          evidence: citedEvidence("note-6.md", "note-7.md", {
            signals: { lexical_terms: ["quasar", "ripple", "tensor", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(result.status, "ok");
  assert.ok(result.trails.length >= 2);
});

test("omits dormant trails when generated_at is invalid instead of using node clocks", () => {
  const oldStamp = "2024-01-01T00:00:00Z";
  const fixture = graph({
    generated_at: "not-a-date",
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
      { id: "sleep.md", title: "Sleep", path: "sleep.md", domain: "Archive", modified_at: oldStamp },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      {
        id: "c1",
        source: "sleep.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "sleep.md:10-12", passage: "dormant signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const withoutClock = buildColdTrails(fixture);
  assert.ok(!withoutClock.trails?.some((trail) => trail.type === "dormant"));

  const withClock = buildColdTrails({ ...fixture, generated_at: "2026-08-28T00:00:00Z" });
  assert.ok(withClock.trails.some((trail) => trail.type === "dormant"));
});

test("parallel invention includes the exact 14-day boundary and excludes one millisecond beyond", () => {
  const stampA = "2026-06-01T00:00:00.000Z";
  const atBoundary = "2026-06-15T00:00:00.000Z";
  const beyond = "2026-06-15T00:00:00.001Z";

  function parallelFixture(stampB) {
    return graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
        { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: stampA },
        { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: stampB },
        { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
        { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
        { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
        { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
        { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
        { id: "n8.md", title: "Eight", path: "n8.md", domain: "Finance" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
        { id: "auth-3", source: "hub.md", target: "alpha.md", verified: true },
        { id: "auth-4", source: "hub.md", target: "beta.md", verified: true },
        {
          id: "c-leaf-1",
          source: "leaf.md",
          target: "n5.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c-leaf-2",
          source: "leaf.md",
          target: "n6.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n6.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
        {
          id: "parallel",
          source: "alpha.md",
          target: "beta.md",
          verified: false,
          evidence: citedEvidence("alpha.md", "beta.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c3",
          source: "n7.md",
          target: "n8.md",
          verified: false,
          evidence: citedEvidence("n7.md", "n8.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
      ],
    });
  }

  const inside = buildColdTrails(parallelFixture(atBoundary));
  assert.ok(inside.trails.some((trail) => trail.type === "parallel_invention"));

  const outside = buildColdTrails(parallelFixture(beyond));
  assert.ok(!outside.trails.some((trail) => trail.type === "parallel_invention"));
});

test("dormant 180-day boundary is identical across timezones for normalized graphs", async () => {
  const { spawnSync } = await import("node:child_process");
  const { fileURLToPath, pathToFileURL } = await import("node:url");
  const path = await import("node:path");
  const root = path.dirname(fileURLToPath(import.meta.url));
  const graphDataUrl = pathToFileURL(path.join(root, "../app/graph-data.ts")).href;
  const coldTrailsUrl = pathToFileURL(path.join(root, "../app/cold-trails.ts")).href;
  const script = `
import { normalizeGraph } from ${JSON.stringify(graphDataUrl)};
import { buildColdTrails } from ${JSON.stringify(coldTrailsUrl)};

const generatedAt = "2026-08-28T00:00:00Z";
const atBoundary = "2026-03-01T00:00:00Z";
const raw = {
  schema_version: "recallweave.viewer.v2",
  generated_at: generatedAt,
  nodes: [
    { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
    { id: "edge.md", title: "Edge", path: "edge.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
    { id: "sleep.md", title: "Sleep", path: "sleep.md", domain: "Archive", modified_at: atBoundary },
    { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
    { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
    { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
    { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
    { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
    { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
  ],
  edges: [
    { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
    {
      id: "c1",
      source: "sleep.md",
      target: "n4.md",
      verified: false,
      evidence: {
        source_evidence: { citation: "sleep.md:10-12", passage: "dormant signal" },
        signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
      },
    },
    {
      id: "c-leaf-1",
      source: "leaf.md",
      target: "n5.md",
      verified: false,
      evidence: {
        source_evidence: { citation: "leaf.md:10-12" },
        target_evidence: { citation: "n5.md:20" },
        signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
      },
    },
    {
      id: "c-leaf-2",
      source: "leaf.md",
      target: "n6.md",
      verified: false,
      evidence: {
        source_evidence: { citation: "leaf.md:10-12" },
        target_evidence: { citation: "n6.md:20" },
        signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
      },
    },
    {
      id: "c3",
      source: "n7.md",
      target: "n3.md",
      verified: false,
      evidence: {
        source_evidence: { citation: "n7.md:10-12" },
        target_evidence: { citation: "n3.md:20" },
        signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
      },
    },
  ],
  privacy: {
    export_profile: "graph_metadata_and_note_derived_terms",
    metadata_only: false,
    includes_excerpts: false,
    includes_passage_text: true,
    includes_note_derived_terms: true,
    includes_paths_titles_tags: true,
  },
};
const fixture = normalizeGraph(raw);
const result = buildColdTrails(fixture);
const dormant = result.trails.filter((trail) => trail.type === "dormant").map((trail) => trail.nodeId);
process.stdout.write(JSON.stringify({ generated_at: fixture.generated_at, dormant }));
`;

  function runUnder(tz) {
    const proc = spawnSync(process.execPath, ["--experimental-strip-types", "-e", script], {
      encoding: "utf8",
      env: { ...process.env, TZ: tz },
    });
    assert.equal(proc.status, 0, proc.stderr || proc.stdout);
    return JSON.parse(proc.stdout);
  }

  const utc = runUnder("UTC");
  const east = runUnder("America/New_York");
  assert.deepEqual(utc, east);
  assert.equal(utc.generated_at, "2026-08-28T00:00:00Z");
  assert.deepEqual(utc.dormant, ["sleep.md"]);
});

test("refuses tours that lack a Bridge or Island opening trail", () => {
  const oldStamp = "2024-01-01T00:00:00Z";
  const result = buildColdTrails(
    graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Core", modified_at: "2026-08-01T00:00:00Z" },
        { id: "sleep.md", title: "Sleep", path: "sleep.md", domain: "Archive", modified_at: oldStamp },
        { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
        { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
        { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
        { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
        { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        {
          id: "c1",
          source: "sleep.md",
          target: "n4.md",
          verified: false,
          evidence: citedEvidence("sleep.md", "n4.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c2",
          source: "n5.md",
          target: "n6.md",
          verified: false,
          evidence: citedEvidence("n5.md", "n6.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "n7.md",
          target: "n3.md",
          verified: false,
          evidence: citedEvidence("n7.md", "n3.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(result.status, "refused");
});

test("ignores conflicted export history when scoring Drift trails", () => {
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "core-b.md", title: "Core B", path: "core-b.md", domain: "Core" },
      {
        id: "drift.md",
        title: "Drift note",
        path: "drift.md",
        domain: "Archive",
        created_at: "2024-01-01T00:00:00Z",
        modified_at: "2026-07-01T00:00:00Z",
      },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "core-b.md", verified: true },
      {
        id: "c1",
        source: "drift.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "drift.md:10-12", passage: "drift signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
    export_history: {
      export_id: "export-conflict",
      previous_content_hash: "a".repeat(64),
      node_content_hashes_changed: 9,
      node_content_hashes_unchanged: 0,
      nodes_added: 0,
      nodes_removed: 0,
      claim_conflict: true,
    },
  });
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  const drift = result.trails.find((trail) => trail.type === "drift");
  assert.ok(drift);
  assert.doesNotMatch(drift?.structuralFacts.join(" ") ?? "", /export history/i);
});

test("omitted previous_content_hash after normalize cannot trust Drift history", () => {
  const raw = {
    schema_version: VIEWER_SCHEMA_V2,
    generated_at: "2026-08-28T00:00:00Z",
    privacy: {
      export_profile: "graph_metadata_and_note_derived_terms",
      metadata_only: false,
      includes_excerpts: false,
      includes_passage_text: true,
      includes_note_derived_terms: true,
      includes_paths_titles_tags: true,
    },
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "core-b.md", title: "Core B", path: "core-b.md", domain: "Core" },
      {
        id: "drift.md",
        title: "Drift note",
        path: "drift.md",
        domain: "Archive",
        created_at: "2024-01-01T00:00:00Z",
        modified_at: "2026-07-01T00:00:00Z",
      },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Archive" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "core-b.md", verified: true },
      {
        id: "c1",
        source: "drift.md",
        target: "n4.md",
        verified: false,
        evidence: {
          source_evidence: { citation: "drift.md:10-12", passage: "drift signal" },
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        },
      },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n3.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n3.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
    export_history: {
      export_id: "omitted-prior",
      node_content_hashes_changed: 2,
      node_content_hashes_unchanged: 6,
      nodes_added: 0,
      nodes_removed: 0,
    },
  };
  const fixture = normalizeGraph(raw);
  assert.equal(fixture.export_history?.claim_conflict, true);
  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  const drift = result.trails.find((trail) => trail.type === "drift");
  assert.ok(drift);
  assert.doesNotMatch(drift?.structuralFacts.join(" ") ?? "", /export history/i);
});

test("preserves fractional seconds through normalizeGraph for parallel invention bounds", async () => {
  const { normalizeGraph } = await import("../app/graph-data.ts");
  const stampA = "2026-06-01T00:00:00.000Z";
  const beyond = "2026-06-15T00:00:00.001Z";
  const microBeyond = "2026-06-15T00:00:00.000001Z";
  const normalized = normalizeGraph(
    graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
        { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: stampA },
        { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: beyond },
        { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
        { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
        { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
        { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
        { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
        {
          id: "parallel",
          source: "alpha.md",
          target: "beta.md",
          verified: false,
          evidence: citedEvidence("alpha.md", "beta.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c-leaf-1",
          source: "leaf.md",
          target: "n5.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c-leaf-2",
          source: "leaf.md",
          target: "n6.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n6.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "n7.md",
          target: "n4.md",
          verified: false,
          evidence: citedEvidence("n7.md", "n4.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(normalized.nodes.find((node) => node.id === "beta.md")?.created_at, beyond);
  const result = buildColdTrails(normalized);
  assert.ok(!result.trails?.some((trail) => trail.type === "parallel_invention"));

  const microNormalized = normalizeGraph(
    graph({
      nodes: [
        { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
        { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
        { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: stampA },
        { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: microBeyond },
        { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
        { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
        { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
        { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
        { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
      ],
      edges: [
        { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
        { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
        {
          id: "parallel",
          source: "alpha.md",
          target: "beta.md",
          verified: false,
          evidence: citedEvidence("alpha.md", "beta.md", {
            signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
          }),
        },
        {
          id: "c-leaf-1",
          source: "leaf.md",
          target: "n5.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n5.md", {
            signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
          }),
        },
        {
          id: "c-leaf-2",
          source: "leaf.md",
          target: "n6.md",
          verified: false,
          evidence: citedEvidence("leaf.md", "n6.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
        {
          id: "c3",
          source: "n7.md",
          target: "n4.md",
          verified: false,
          evidence: citedEvidence("n7.md", "n4.md", {
            signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
          }),
        },
      ],
    }),
  );
  assert.equal(
    microNormalized.nodes.find((node) => node.id === "beta.md")?.created_at,
    microBeyond,
  );
  assert.ok(!buildColdTrails(microNormalized).trails?.some((trail) => trail.type === "parallel_invention"));
});

test("emits Parallel invention without suppressing Unwritten or Distant classifications", () => {
  const stampA = "2026-06-01T00:00:00Z";
  const stampB = "2026-06-08T00:00:00Z";
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      { id: "alpha.md", title: "Alpha", path: "alpha.md", domain: "Labs", created_at: stampA },
      { id: "beta.md", title: "Beta", path: "beta.md", domain: "Field", created_at: stampB },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "parallel",
        source: "alpha.md",
        target: "beta.md",
        verified: false,
        evidence: citedEvidence("alpha.md", "beta.md", {
          signals: { lexical_terms: ["quasar", "ripple", "vector", "tensor"] },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n4.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n4.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const parallelEdge = fixture.edges.find((edge) => edge.id === "parallel");
  assert.ok(parallelEdge);
  const classified = new Set(classifyCandidateEdgeTypes(fixture, parallelEdge));
  assert.ok(classified.has("parallel_invention"));
  assert.ok(classified.has("unwritten_link"));
  assert.ok(classified.has("distant_neighbors"));

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  const types = new Set(result.trails.map((trail) => trail.type));
  assert.ok(types.has("island"));
  // Reservation holds Parallel pairs out of early Unwritten/Distant slots.
  assert.ok(result.trails.some((trail) => trail.type === "parallel_invention"));
  assert.ok(
    result.trails.some(
      (trail) =>
        trail.type === "unwritten_link" || trail.type === "distant_neighbors",
    ),
  );
});

test("selects Parallel invention when the same pair is also the reserved Reinforced candidate", () => {
  const stampA = "2026-06-01T00:00:00Z";
  const stampB = "2026-06-08T00:00:00Z";
  const fixture = graph({
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "edge.md", title: "Edge", path: "edge.md", domain: "Edge" },
      {
        id: "alpha.md",
        title: "Alpha",
        path: "alpha.md",
        domain: "Labs",
        created_at: stampA,
        tags: ["experiments"],
      },
      {
        id: "beta.md",
        title: "Beta",
        path: "beta.md",
        domain: "Field",
        created_at: stampB,
        tags: ["experiments"],
      },
      { id: "leaf.md", title: "Leaf", path: "leaf.md", domain: "Garden" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Garden" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Garden" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Finance" },
      { id: "n8.md", title: "Eight", path: "n8.md", domain: "Finance" },
    ],
    edges: [
      { id: "auth-1", source: "hub.md", target: "edge.md", verified: true },
      { id: "auth-2", source: "hub.md", target: "n4.md", verified: true },
      {
        id: "c-leaf-1",
        source: "leaf.md",
        target: "n5.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n5.md", {
          signals: { lexical_terms: ["quasar", "ripple", "matrix", "phase"] },
        }),
      },
      {
        id: "c-leaf-2",
        source: "leaf.md",
        target: "n6.md",
        verified: false,
        evidence: citedEvidence("leaf.md", "n6.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
      {
        id: "parallel",
        source: "alpha.md",
        target: "beta.md",
        verified: false,
        evidence: citedEvidence("alpha.md", "beta.md", {
          signals: {
            lexical_terms: ["quasar", "ripple", "vector", "tensor"],
            shared_tags: ["experiments"],
          },
        }),
      },
      {
        id: "c3",
        source: "n7.md",
        target: "n8.md",
        verified: false,
        evidence: citedEvidence("n7.md", "n8.md", {
          signals: { lexical_terms: ["quasar", "ripple", "theta", "phase"] },
        }),
      },
    ],
  });
  const parallelEdge = fixture.edges.find((edge) => edge.id === "parallel");
  assert.ok(parallelEdge);
  const classified = new Set(classifyCandidateEdgeTypes(fixture, parallelEdge));
  assert.ok(classified.has("parallel_invention"));
  assert.ok(classified.has("reinforced"));

  const result = buildColdTrails(fixture);
  assert.equal(result.status, "ok");
  assert.ok(
    result.trails.some(
      (trail) => trail.type === "parallel_invention" && trail.edgeId === "parallel",
    ),
  );
});

test("resolveTrailSourcePath propagates node path exactness to Open source copies", () => {
  const hash = "a".repeat(64);
  const graphDoc = normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      { id: "clean", title: "Clean", path: "notes/clean.md", content_hash: hash },
      // Zero-width char is stripped by import sanitization: path_exact === false,
      // stored path sanitizes to notes/plan.md.
      { id: "zwsp", title: "Zero", path: "notes/plan" + String.fromCharCode(0x200b) + ".md", content_hash: hash },
      { id: "other", title: "Other", path: "notes/other.md", content_hash: hash },
    ],
    edges: [
      {
        id: "cited",
        source: "clean",
        target: "other",
        verified: false,
        evidence: { source_evidence: { citation: "notes/clean.md:10-12" } },
      },
      {
        id: "uncited",
        source: "zwsp",
        target: "other",
        verified: false,
        evidence: {},
      },
    ],
    privacy: { export_profile: "graph_metadata" },
    import_diagnostics: { duplicate_nodes_dropped: 0, duplicate_edges_dropped: 0, dangling_edges_dropped: 0 },
  });

  // A node-sourced trail on a sanitized node must report the sanitized path AND
  // pathExact false, so the tour flags the copy like the note drawer does.
  const sanitized = resolveTrailSourcePath(graphDoc, { nodeId: "zwsp", sourceId: "zwsp", targetId: "other" });
  assert.equal(sanitized.path, "notes/plan.md");
  assert.equal(sanitized.pathExact, false);

  // A node-sourced trail on an exact node keeps pathExact true.
  const exact = resolveTrailSourcePath(graphDoc, { nodeId: "clean", sourceId: "clean", targetId: "other" });
  assert.equal(exact.path, "notes/clean.md");
  assert.equal(exact.pathExact, true);

  // A citation-derived path is not a node path, so it stays exact.
  const cited = resolveTrailSourcePath(graphDoc, { sourceId: "clean", targetId: "other", edgeId: "cited" });
  assert.equal(cited.path, "notes/clean.md");
  assert.equal(cited.pathExact, true);

  // With no usable citation, the edge trail falls back to the source node — and
  // that node's sanitized exactness must carry through (here, false).
  const fallback = resolveTrailSourcePath(graphDoc, { sourceId: "zwsp", targetId: "other", edgeId: "uncited" });
  assert.equal(fallback.path, "notes/plan.md");
  assert.equal(fallback.pathExact, false);
});
