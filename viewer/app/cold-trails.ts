import {
  citationPath,
  safeCitation,
  type GraphDocument,
  type GraphEdge,
  type GraphNode,
} from "./graph-data.ts";

export type TrailType =
  | "unwritten_link"
  | "distant_neighbors"
  | "bridge"
  | "island"
  | "reinforced";

export type TrailTrust = "authored" | "candidate" | "structural";

export type ScoreBreakdown = {
  novelty: number;
  distance: number;
  evidence: number;
  centrality: number;
  structure: number;
  penalties: number;
  total: number;
};

export type ColdTrail = {
  type: TrailType;
  trust: TrailTrust;
  sourceId: string;
  targetId: string;
  edgeId?: string;
  nodeId?: string;
  surpriseTerms: string[];
  score: number;
  scoreBreakdown: ScoreBreakdown;
  headline: string;
  structuralFacts: string[];
};

export type ColdTrailsRefusal =
  | "graph_too_small"
  | "not_enough_candidates"
  | "no_surprise_terms"
  | "single_domain"
  | "insufficient_eligible_trails";

export type ColdTrailsResult =
  | { status: "ok"; trails: ColdTrail[]; notice?: string }
  | { status: "refused"; reason: ColdTrailsRefusal; message: string };

export type ColdTrailsFeedback = {
  dismissedPairs: Set<string>;
  shownPairs: Set<string>;
  usedDomains: Set<string>;
  usedTypes: Map<TrailType, number>;
  usedNodeIds: Set<string>;
  usedSurpriseTerms: Set<string>;
  domainTouchCounts: Map<string, number>;
};

const EVIDENCE_FLOOR = 0.25;
const DEFAULT_TOUR_LENGTH = 6;

export function weightedTotal(breakdown: Omit<ScoreBreakdown, "total">): number {
  return Math.max(
    0,
    0.3 * breakdown.novelty +
      0.25 * breakdown.distance +
      0.25 * breakdown.evidence +
      0.1 * breakdown.centrality +
      0.1 * breakdown.structure -
      breakdown.penalties,
  );
}

function trailIdentity(trail: ColdTrail): string {
  if (trail.nodeId) return `node:${trail.nodeId}:${trail.type}`;
  return `${trail.type}:${pairKey(trail.sourceId, trail.targetId)}`;
}

function pairKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

export function trailPairKey(trail: ColdTrail): string {
  if (trail.nodeId) return `node:${trail.nodeId}`;
  return pairKey(trail.sourceId, trail.targetId);
}

export function trailTypeLabel(type: TrailType): string {
  switch (type) {
    case "unwritten_link":
      return "Unwritten link";
    case "distant_neighbors":
      return "Distant neighbors";
    case "bridge":
      return "Bridge";
    case "island":
      return "Island";
    case "reinforced":
      return "Reinforced";
    default:
      return type;
  }
}

export function trailTrustLabel(trust: TrailTrust): string {
  switch (trust) {
    case "candidate":
      return "CANDIDATE - NOT A FACT";
    case "structural":
      return "STRUCTURAL FACT";
    case "authored":
      return "AUTHORED LINK";
    default:
      return trust;
  }
}

function markdownScalar(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/`/g, "\\`")
    .replace(/\r/g, "")
    .replace(/\n/g, " ")
    .trim();
}

function markdownInline(value: string): string {
  return `\`${markdownScalar(value).replace(/`/g, "'")}\``;
}

function edgeForTrail(graph: GraphDocument, trail: ColdTrail): GraphEdge | undefined {
  if (!trail.edgeId) return undefined;
  return graph.edges.find((edge) => edge.id === trail.edgeId);
}

function trailCitations(graph: GraphDocument, trail: ColdTrail): string[] {
  const edge = edgeForTrail(graph, trail);
  if (!edge) return [];
  const source = graph.nodes.find((node) => node.id === trail.sourceId);
  const target = graph.nodes.find((node) => node.id === trail.targetId);
  if (!source || !target) return [];
  const sourceCitation = safeCitation(edge.evidence?.source_evidence?.citation);
  const targetCitation = safeCitation(edge.evidence?.target_evidence?.citation);
  if (
    !sourceCitation ||
    !targetCitation ||
    !citationMatchesNode(sourceCitation, source) ||
    !citationMatchesNode(targetCitation, target)
  ) {
    return [];
  }
  return [sourceCitation, targetCitation];
}

function citationMatchesNode(citation: string, node: GraphNode): boolean {
  const path = citationPath(citation);
  if (!path) return false;
  return path === node.path || path === node.id;
}

function candidateHasValidCitation(edge: GraphEdge, source: GraphNode, target: GraphNode): boolean {
  const sourceCitation = safeCitation(edge.evidence?.source_evidence?.citation);
  const targetCitation = safeCitation(edge.evidence?.target_evidence?.citation);
  if (!sourceCitation || !targetCitation) return false;
  return citationMatchesNode(sourceCitation, source) && citationMatchesNode(targetCitation, target);
}

export function exportSavedTrailsMarkdown(graph: GraphDocument, trails: ColdTrail[]): string {
  const nodeTitle = (id: string) =>
    markdownInline(graph.nodes.find((node) => node.id === id)?.title || id);
  const lines = ["# Cold Trails session export", ""];
  trails.forEach((trail, index) => {
    lines.push(`## ${index + 1}. ${markdownScalar(trailTypeLabel(trail.type))}`);
    lines.push(`- Trust: ${trailTrustLabel(trail.trust)}`);
    if (trail.nodeId) {
      lines.push(`- Note: ${nodeTitle(trail.nodeId)}`);
    } else {
      lines.push(`- Notes: ${nodeTitle(trail.sourceId)} / ${nodeTitle(trail.targetId)}`);
    }
    trailCitations(graph, trail).forEach((citation) => {
      lines.push(`- Citation: ${markdownInline(citation)}`);
    });
    if (trail.trust === "candidate" && trail.edgeId && !trailCitations(graph, trail).length) {
      lines.push("- Provenance: incomplete bilateral citations omitted");
    }
    if (trail.surpriseTerms.length) {
      lines.push(`- Surprise terms: ${trail.surpriseTerms.map((term) => markdownInline(term)).join(", ")}`);
    }
    trail.structuralFacts.forEach((fact) => lines.push(`- Fact: ${markdownInline(fact)}`));
    lines.push("");
  });
  return lines.join("\n");
}

function tokenize(value: string): Set<string> {
  return new Set(
    value
      .toLowerCase()
      .split(/[^a-z0-9]+/u)
      .map((token) => token.trim())
      .filter((token) => token.length > 1),
  );
}

function nodeTokens(node: GraphNode): Set<string> {
  const tokens = tokenize(`${node.title} ${node.path}`);
  (node.tags || []).forEach((tag) => tokenize(tag).forEach((item) => tokens.add(item)));
  if (node.domain) tokenize(node.domain).forEach((item) => tokens.add(item));
  return tokens;
}

function sharedTerms(edge: GraphEdge): string[] {
  const signals = edge.evidence?.signals;
  if (signals?.lexical_terms?.length) return signals.lexical_terms;
  return edge.evidence?.shared_terms || [];
}

function surpriseTerms(source: GraphNode, target: GraphNode, edge: GraphEdge): string[] {
  const blocked = new Set<string>();
  nodeTokens(source).forEach((token) => blocked.add(token));
  nodeTokens(target).forEach((token) => blocked.add(token));
  return sharedTerms(edge).filter((term) => !blocked.has(term.toLowerCase()));
}

function authoredAdjacency(graph: GraphDocument): Map<string, Set<string>> {
  const adjacency = new Map<string, Set<string>>();
  graph.nodes.forEach((node) => adjacency.set(node.id, new Set()));
  graph.edges
    .filter((edge) => edge.verified)
    .forEach((edge) => {
      adjacency.get(edge.source)?.add(edge.target);
      adjacency.get(edge.target)?.add(edge.source);
    });
  return adjacency;
}

function authoredPathWithinHops(
  adjacency: Map<string, Set<string>>,
  start: string,
  goal: string,
  maxHops: number,
): boolean {
  if (start === goal) return true;
  const queue: Array<{ id: string; depth: number }> = [{ id: start, depth: 0 }];
  const visited = new Set([start]);
  while (queue.length) {
    const current = queue.shift();
    if (!current) break;
    if (current.depth >= maxHops) continue;
    for (const neighbor of adjacency.get(current.id) || []) {
      if (neighbor === goal) return true;
      if (visited.has(neighbor)) continue;
      visited.add(neighbor);
      queue.push({ id: neighbor, depth: current.depth + 1 });
    }
  }
  return false;
}

function nodeMap(graph: GraphDocument): Map<string, GraphNode> {
  return new Map(graph.nodes.map((node) => [node.id, node]));
}

function candidateEdges(graph: GraphDocument): GraphEdge[] {
  return graph.edges.filter((edge) => !edge.verified);
}

function domainSet(graph: GraphDocument): Set<string> {
  return new Set(graph.nodes.map((node) => node.domain || "Unclassified"));
}

function interDomainAuthoredCounts(graph: GraphDocument): Map<string, number> {
  const counts = new Map<string, number>();
  graph.edges
    .filter((edge) => edge.verified)
    .forEach((edge) => {
      const source = graph.nodes.find((node) => node.id === edge.source);
      const target = graph.nodes.find((node) => node.id === edge.target);
      const sourceDomain = source?.domain || "Unclassified";
      const targetDomain = target?.domain || "Unclassified";
      if (sourceDomain === targetDomain) return;
      const key = sourceDomain < targetDomain
        ? `${sourceDomain}|${targetDomain}`
        : `${targetDomain}|${sourceDomain}`;
      counts.set(key, (counts.get(key) || 0) + 1);
    });
  return counts;
}

function degreeMap(graph: GraphDocument): Map<string, number> {
  const degrees = new Map<string, number>();
  graph.nodes.forEach((node) => degrees.set(node.id, 0));
  graph.edges.forEach((edge) => {
    degrees.set(edge.source, (degrees.get(edge.source) || 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) || 0) + 1);
  });
  return degrees;
}

function percentile(values: number[], ratio: number): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.floor(ratio * sorted.length));
  return sorted[index];
}

function evidenceScore(edge: GraphEdge): number {
  const terms = sharedTerms(edge);
  const tags = edge.evidence?.signals?.shared_tags?.length || 0;
  const neighbors = edge.evidence?.signals?.mutual_neighbor_ids?.length || 0;
  const passage =
    Boolean(edge.evidence?.source_evidence?.passage || edge.evidence?.source_text) &&
    Boolean(edge.evidence?.target_evidence?.passage);
  return (
    0.4 * Math.min(terms.length / 6, 1) +
    (passage ? 0.3 : 0) +
    (tags ? 0.2 : 0) +
    0.1 * Math.min(neighbors / 3, 1)
  );
}

function distanceScore(source: GraphNode, target: GraphNode, interDomainCounts: Map<string, number>): number {
  const sourceDomain = source.domain || "Unclassified";
  const targetDomain = target.domain || "Unclassified";
  if (sourceDomain === targetDomain) return 0;
  const key = sourceDomain < targetDomain
    ? `${sourceDomain}|${targetDomain}`
    : `${targetDomain}|${sourceDomain}`;
  const crossings = interDomainCounts.get(key) || 0;
  return crossings <= 1 ? 1 : 0.6;
}

function centralityScore(
  sourceId: string,
  targetId: string,
  degrees: Map<string, number>,
  p90: number,
): number {
  const maxDegree = Math.max(degrees.get(sourceId) || 0, degrees.get(targetId) || 0);
  if (!p90) return 0;
  return Math.min(maxDegree / p90, 1);
}

function scoreCandidateTrail(
  graph: GraphDocument,
  edge: GraphEdge,
  type: TrailType,
  feedback: ColdTrailsFeedback,
  adjacency: Map<string, Set<string>>,
  degrees: Map<string, number>,
  p90: number,
  interDomainCounts: Map<string, number>,
  nodes: Map<string, GraphNode>,
): ColdTrail | null {
  const source = nodes.get(edge.source);
  const target = nodes.get(edge.target);
  if (!source || !target) return null;

  const terms = surpriseTerms(source, target, edge);
  if (terms.length < 2) return null;

  const evidence = evidenceScore(edge);
  if (evidence < EVIDENCE_FLOOR) return null;
  if (!candidateHasValidCitation(edge, source, target)) return null;

  const novelty = authoredPathWithinHops(adjacency, edge.source, edge.target, 3) ? 0 : 1;
  const distance = distanceScore(source, target, interDomainCounts);
  const centrality = centralityScore(edge.source, edge.target, degrees, p90);
  const structure = type === "bridge" || type === "island" ? 0.2 : 0;
  let penalties = 0;
  if (feedback.usedNodeIds.has(edge.source) || feedback.usedNodeIds.has(edge.target)) {
    penalties += 0.3;
  }
  if (feedback.dismissedPairs.has(pairKey(edge.source, edge.target))) {
    penalties += 0.2;
  }
  const redundantSurprise = terms.some((term) =>
    feedback.usedSurpriseTerms.has(term.toLowerCase()),
  );
  if (redundantSurprise) {
    penalties += 0.15;
  }
  const breakdown = {
    novelty,
    distance,
    evidence,
    centrality,
    structure,
    penalties,
  };
  const total = weightedTotal(breakdown);

  return {
    type,
    trust: "candidate",
    sourceId: edge.source,
    targetId: edge.target,
    edgeId: edge.id,
    surpriseTerms: terms,
    score: total,
    scoreBreakdown: {
      ...breakdown,
      total,
    },
    headline: "Candidate only: overlapping signals are not proof of a factual relationship.",
    structuralFacts: [
      authoredPathWithinHops(adjacency, edge.source, edge.target, 3)
        ? "An authored path exists within three hops."
        : "No authored path within three hops.",
    ],
  };
}

function classifyCandidateEdge(
  graph: GraphDocument,
  edge: GraphEdge,
  adjacency: Map<string, Set<string>>,
  nodes: Map<string, GraphNode>,
  interDomainCounts: Map<string, number>,
): TrailType[] {
  const source = nodes.get(edge.source);
  const target = nodes.get(edge.target);
  if (!source || !target) return [];
  const types: TrailType[] = [];
  if (!authoredPathWithinHops(adjacency, edge.source, edge.target, 3)) {
    types.push("unwritten_link");
  }
  const sourceDomain = source.domain || "Unclassified";
  const targetDomain = target.domain || "Unclassified";
  if (sourceDomain !== targetDomain) {
    const key = sourceDomain < targetDomain
      ? `${sourceDomain}|${targetDomain}`
      : `${targetDomain}|${sourceDomain}`;
    if ((interDomainCounts.get(key) || 0) <= 1) {
      types.push("distant_neighbors");
    }
  }
  const signalCount = [
    sharedTerms(edge).length >= 2,
    Boolean(edge.evidence?.signals?.shared_tags?.length),
    Boolean(edge.evidence?.signals?.mutual_neighbor_ids?.length),
  ].filter(Boolean).length;
  if (signalCount >= 2) types.push("reinforced");
  return types;
}

function bridgeTrails(
  graph: GraphDocument,
  nodes: Map<string, GraphNode>,
  degrees: Map<string, number>,
  p90: number,
): ColdTrail[] {
  const adjacency = authoredAdjacency(graph);
  const trails: ColdTrail[] = [];
  graph.nodes.forEach((node) => {
    const neighborDomains = new Set<string>();
    for (const neighborId of adjacency.get(node.id) || []) {
      const neighbor = nodes.get(neighborId);
      neighborDomains.add(neighbor?.domain || "Unclassified");
    }
    if (neighborDomains.size >= 2) {
      const centrality = centralityScore(node.id, node.id, degrees, p90);
      const breakdown = {
        novelty: 0,
        distance: 1,
        evidence: 0,
        centrality,
        structure: 1,
        penalties: 0,
      };
      const total = weightedTotal(breakdown);
      trails.push({
        type: "bridge",
        trust: "structural",
        sourceId: node.id,
        targetId: node.id,
        nodeId: node.id,
        surpriseTerms: [],
        score: total,
        scoreBreakdown: {
          ...breakdown,
          total,
        },
        headline: "This note authored links into multiple domains.",
        structuralFacts: [
          `Authored edges reach ${neighborDomains.size} domains from one note.`,
        ],
      });
    }
  });
  return trails;
}

function authoredDegree(graph: GraphDocument, nodeId: string): number {
  return graph.edges.filter(
    (edge) => edge.verified && (edge.source === nodeId || edge.target === nodeId),
  ).length;
}

function islandTrails(
  graph: GraphDocument,
  degrees: Map<string, number>,
  p90: number,
): ColdTrail[] {
  const candidateCounts = new Map<string, number>();
  candidateEdges(graph).forEach((edge) => {
    candidateCounts.set(edge.source, (candidateCounts.get(edge.source) || 0) + 1);
    candidateCounts.set(edge.target, (candidateCounts.get(edge.target) || 0) + 1);
  });
  return graph.nodes
    .filter(
      (node) =>
        authoredDegree(graph, node.id) <= 1 &&
        (candidateCounts.get(node.id) || 0) >= 2,
    )
    .map((node) => {
      const centrality = centralityScore(node.id, node.id, degrees, p90);
      const breakdown = {
        novelty: 0.2,
        distance: 0,
        evidence: 0,
        centrality,
        structure: 1,
        penalties: 0,
      };
      const total = weightedTotal(breakdown);
      return {
        type: "island" as const,
        trust: "structural" as const,
        sourceId: node.id,
        targetId: node.id,
        nodeId: node.id,
        surpriseTerms: [],
        score: total,
        scoreBreakdown: {
          ...breakdown,
          total,
        },
        headline: "This note is structurally isolated but has multiple candidate edges.",
        structuralFacts: [
          `Authored degree ${authoredDegree(graph, node.id)}; ${candidateCounts.get(node.id) || 0} candidate edges.`,
        ],
      };
    });
}

export function refusalMessage(reason: ColdTrailsRefusal): string {
  switch (reason) {
    case "graph_too_small":
      return "Graph is small enough to explore directly.";
    case "not_enough_candidates":
      return "Not enough discovery candidates.";
    case "no_surprise_terms":
      return "Overlap mirrors existing labels; no surprising trails qualified.";
    case "single_domain":
      return "Only one domain is present; cross-domain trail types are omitted.";
    case "insufficient_eligible_trails":
      return "Fewer than three eligible trails; showing a shorter tour.";
    default:
      return "Cold Trails cannot build a useful tour for this graph.";
  }
}

function cloneFeedback(feedback: ColdTrailsFeedback): ColdTrailsFeedback {
  return {
    dismissedPairs: new Set(feedback.dismissedPairs),
    shownPairs: new Set(feedback.shownPairs),
    usedDomains: new Set(feedback.usedDomains),
    usedTypes: new Map(feedback.usedTypes),
    usedNodeIds: new Set(feedback.usedNodeIds),
    usedSurpriseTerms: new Set(feedback.usedSurpriseTerms),
    domainTouchCounts: new Map(feedback.domainTouchCounts),
  };
}

function endpointIdsForTrail(trail: ColdTrail): string[] {
  return trail.nodeId ? [trail.nodeId] : [trail.sourceId, trail.targetId];
}

function endpointDomainsForTrail(trail: ColdTrail, nodes: Map<string, GraphNode>): string[] {
  return endpointIdsForTrail(trail).map((id) => nodes.get(id)?.domain || "Unclassified");
}

function touchedDomainsForTrail(trail: ColdTrail, nodes: Map<string, GraphNode>): string[] {
  return [...new Set(endpointDomainsForTrail(trail, nodes))];
}

function applyStructuralPenalties(
  trail: ColdTrail,
  feedback: ColdTrailsFeedback,
): ColdTrail {
  let penalties = 0;
  const endpoints = endpointIdsForTrail(trail);
  if (endpoints.some((id) => feedback.usedNodeIds.has(id))) {
    penalties += 0.3;
  }
  if (
    trail.sourceId &&
    trail.targetId &&
    trail.sourceId !== trail.targetId &&
    feedback.dismissedPairs.has(pairKey(trail.sourceId, trail.targetId))
  ) {
    penalties += 0.2;
  }
  const breakdown = {
    novelty: trail.scoreBreakdown.novelty,
    distance: trail.scoreBreakdown.distance,
    evidence: trail.scoreBreakdown.evidence,
    centrality: trail.scoreBreakdown.centrality,
    structure: trail.scoreBreakdown.structure,
    penalties,
  };
  const total = weightedTotal(breakdown);
  return {
    ...trail,
    score: total,
    scoreBreakdown: {
      ...breakdown,
      total,
    },
  };
}

function hubIneligible(
  trail: ColdTrail,
  degrees: Map<string, number>,
  p95: number,
): boolean {
  if (trail.type === "bridge") return false;
  return endpointIdsForTrail(trail).some((id) => (degrees.get(id) || 0) > p95);
}

function domainTouchOverflow(
  trail: ColdTrail,
  nodes: Map<string, GraphNode>,
  domainTouchCounts: Map<string, number>,
): boolean {
  return touchedDomainsForTrail(trail, nodes).some((domain) => {
    return (domainTouchCounts.get(domain) || 0) + 1 > 2;
  });
}

function isTrailEligible(
  trail: ColdTrail,
  feedback: ColdTrailsFeedback,
  nodes: Map<string, GraphNode>,
  degrees: Map<string, number>,
  p95: number,
): boolean {
  if (
    trail.sourceId &&
    trail.targetId &&
    trail.sourceId !== trail.targetId &&
    feedback.shownPairs.has(pairKey(trail.sourceId, trail.targetId))
  ) {
    return false;
  }
  if (trail.nodeId && feedback.shownPairs.has(`node:${trail.nodeId}`)) {
    return false;
  }
  const typeCount = feedback.usedTypes.get(trail.type) || 0;
  if (typeCount >= 2) return false;
  const endpoints = endpointIdsForTrail(trail);
  if (endpoints.some((id) => feedback.usedNodeIds.has(id))) return false;
  if (hubIneligible(trail, degrees, p95)) return false;
  if (domainTouchOverflow(trail, nodes, feedback.domainTouchCounts)) return false;
  return true;
}

function applyTrailSelection(
  trail: ColdTrail,
  feedback: ColdTrailsFeedback,
  nodes: Map<string, GraphNode>,
): void {
  const typeCount = feedback.usedTypes.get(trail.type) || 0;
  feedback.usedTypes.set(trail.type, typeCount + 1);
  endpointIdsForTrail(trail).forEach((id) => feedback.usedNodeIds.add(id));
  touchedDomainsForTrail(trail, nodes).forEach((domain) => {
    feedback.usedDomains.add(domain);
    feedback.domainTouchCounts.set(domain, (feedback.domainTouchCounts.get(domain) || 0) + 1);
  });
  trail.surpriseTerms.forEach((term) => feedback.usedSurpriseTerms.add(term.toLowerCase()));
  if (trail.sourceId && trail.targetId && trail.sourceId !== trail.targetId) {
    feedback.shownPairs.add(pairKey(trail.sourceId, trail.targetId));
  }
  if (trail.nodeId) {
    feedback.shownPairs.add(`node:${trail.nodeId}`);
  }
}

function rescorePool(
  pool: ColdTrail[],
  selected: ColdTrail[],
  graph: GraphDocument,
  feedback: ColdTrailsFeedback,
  adjacency: Map<string, Set<string>>,
  degrees: Map<string, number>,
  p90: number,
  interDomainCounts: Map<string, number>,
  nodes: Map<string, GraphNode>,
): ColdTrail[] {
  const selectedKeys = new Set(selected.map((trail) => trailIdentity(trail)));
  const rescored: ColdTrail[] = [];
  for (const trail of pool) {
    if (selectedKeys.has(trailIdentity(trail))) continue;
    if (trail.trust === "candidate" && trail.edgeId) {
      const edge = graph.edges.find((item) => item.id === trail.edgeId);
      if (!edge) continue;
      const rescoredTrail = scoreCandidateTrail(
        graph,
        edge,
        trail.type,
        feedback,
        adjacency,
        degrees,
        p90,
        interDomainCounts,
        nodes,
      );
      if (rescoredTrail) rescored.push(rescoredTrail);
    } else {
      rescored.push(applyStructuralPenalties(trail, feedback));
    }
  }
  return rescored.sort((a, b) => b.score - a.score);
}

function pickTrail(
  candidates: ColdTrail[],
  prefer?: (trail: ColdTrail) => boolean,
): ColdTrail | undefined {
  if (!candidates.length) return undefined;
  if (prefer) {
    const preferred = candidates.filter(prefer);
    if (preferred.length) return preferred[0];
  }
  return candidates[0];
}

function isStructuralTrail(trail: ColdTrail): boolean {
  return trail.type === "bridge" || trail.type === "island";
}

function selectTourTrails(
  pool: ColdTrail[],
  graph: GraphDocument,
  feedback: ColdTrailsFeedback,
  nodes: Map<string, GraphNode>,
  degrees: Map<string, number>,
  p90: number,
  p95: number,
  interDomainCounts: Map<string, number>,
  domains: Set<string>,
): ColdTrail[] {
  const selected: ColdTrail[] = [];
  const localFeedback = cloneFeedback(feedback);
  const adjacency = authoredAdjacency(graph);

  const phases: Array<(trail: ColdTrail) => boolean> = [
    (trail) => trail.type === "bridge" || trail.type === "island",
    (trail) => trail.type === "unwritten_link" || trail.type === "distant_neighbors",
    (trail) => trail.type === "unwritten_link" || trail.type === "distant_neighbors",
    (trail) => {
      const selectedTypes = new Set(selected.map((item) => item.type));
      return !selectedTypes.has(trail.type);
    },
    () => true,
    (trail) => trail.type === "reinforced",
  ];

  for (let slot = 0; slot < phases.length; slot += 1) {
    if (selected.length >= DEFAULT_TOUR_LENGTH) break;
    const phase = phases[slot];
    const rescored = rescorePool(
      pool,
      selected,
      graph,
      localFeedback,
      adjacency,
      degrees,
      p90,
      interDomainCounts,
      nodes,
    );
    const eligible = rescored.filter((trail) => phase(trail) && isTrailEligible(trail, localFeedback, nodes, degrees, p95));
    const chosen =
      slot === 4
        ? pickTrail(
            eligible,
            (trail) =>
              endpointDomainsForTrail(trail, nodes).some(
                (domain) => (localFeedback.domainTouchCounts.get(domain) || 0) === 0,
              ),
          )
        : pickTrail(eligible);
    if (!chosen) continue;
    selected.push(chosen);
    applyTrailSelection(chosen, localFeedback, nodes);
  }

  while (selected.length < DEFAULT_TOUR_LENGTH) {
    const rescored = rescorePool(
      pool,
      selected,
      graph,
      localFeedback,
      adjacency,
      degrees,
      p90,
      interDomainCounts,
      nodes,
    );
    const eligible = rescored.filter((trail) => isTrailEligible(trail, localFeedback, nodes, degrees, p95));
    if (!eligible.length) break;
    const fillEligible = eligible.filter((trail) => !isStructuralTrail(trail));
    if (!fillEligible.length) break;
    let chosen = fillEligible[0];
    if (domains.size >= 3) {
      const covered = new Set(
        selected.flatMap((trail) => touchedDomainsForTrail(trail, nodes)),
      );
      if (covered.size < 3) {
        chosen =
          fillEligible.find((trail) =>
            touchedDomainsForTrail(trail, nodes).some((domain) => !covered.has(domain)),
          ) || fillEligible[0];
      }
    }
    selected.push(chosen);
    applyTrailSelection(chosen, localFeedback, nodes);
  }

  return selected;
}

export function buildColdTrails(
  graph: GraphDocument,
  feedback: ColdTrailsFeedback = {
    dismissedPairs: new Set(),
    shownPairs: new Set(),
    usedDomains: new Set(),
    usedTypes: new Map(),
    usedNodeIds: new Set(),
    usedSurpriseTerms: new Set(),
    domainTouchCounts: new Map(),
  },
): ColdTrailsResult {
  if (graph.nodes.length < 8) {
    return { status: "refused", reason: "graph_too_small", message: refusalMessage("graph_too_small") };
  }
  const candidates = candidateEdges(graph);
  if (candidates.length < 3) {
    return {
      status: "refused",
      reason: "not_enough_candidates",
      message: refusalMessage("not_enough_candidates"),
    };
  }

  const nodes = nodeMap(graph);
  const adjacency = authoredAdjacency(graph);
  const degrees = degreeMap(graph);
  const p90 = percentile([...degrees.values()], 0.9);
  const p95 = percentile([...degrees.values()], 0.95);
  const interDomainCounts = interDomainAuthoredCounts(graph);
  const domains = domainSet(graph);
  const notice = domains.size === 1 ? refusalMessage("single_domain") : undefined;

  const pool: ColdTrail[] = [
    ...bridgeTrails(graph, nodes, degrees, p90),
    ...islandTrails(graph, degrees, p90),
  ];

  candidates.forEach((edge) => {
    const types = classifyCandidateEdge(graph, edge, adjacency, nodes, interDomainCounts);
    types.forEach((type) => {
      const trail = scoreCandidateTrail(
        graph,
        edge,
        type,
        feedback,
        adjacency,
        degrees,
        p90,
        interDomainCounts,
        nodes,
      );
      if (trail) pool.push(trail);
    });
  });

  if (
    !pool.some((trail) => trail.surpriseTerms.length >= 2) &&
    !pool.some((trail) => trail.type === "bridge" || trail.type === "island")
  ) {
    return {
      status: "refused",
      reason: "no_surprise_terms",
      message: refusalMessage("no_surprise_terms"),
    };
  }

  const selected = selectTourTrails(
    pool,
    graph,
    feedback,
    nodes,
    degrees,
    p90,
    p95,
    interDomainCounts,
    domains,
  );

  if (!selected.length) {
    return {
      status: "refused",
      reason: "insufficient_eligible_trails",
      message: refusalMessage("insufficient_eligible_trails"),
    };
  }

  const structuralSelected = selected.some((trail) => isStructuralTrail(trail));
  if (!structuralSelected || !isStructuralTrail(selected[0])) {
    return {
      status: "refused",
      reason: "insufficient_eligible_trails",
      message: refusalMessage("insufficient_eligible_trails"),
    };
  }

  const poolDomains = new Set<string>();
  pool.forEach((trail) => {
    touchedDomainsForTrail(trail, nodes).forEach((domain) => poolDomains.add(domain));
  });
  const coveredDomains = new Set(
    selected.flatMap((trail) => touchedDomainsForTrail(trail, nodes)),
  );
  if (poolDomains.size >= 3 && coveredDomains.size < 3) {
    return {
      status: "refused",
      reason: "insufficient_eligible_trails",
      message: refusalMessage("insufficient_eligible_trails"),
    };
  }

  return {
    status: "ok",
    trails: selected,
    notice,
  };
}
