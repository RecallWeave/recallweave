export type GraphNode = {
  id: string;
  title: string;
  path: string;
  status?: string;
  domain?: string;
  summary?: string;
  tags?: string[];
  section_count?: number;
};

export type GraphEvidence = {
  citation?: string;
  explanation?: string;
  source_text?: string;
  shared_terms?: string[];
  source_evidence?: GraphEvidenceSide;
  target_evidence?: GraphEvidenceSide;
};

export type GraphEvidenceSide = {
  citation?: string;
  passage?: string;
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: string;
  verified: boolean;
  score?: number;
  evidence?: GraphEvidence;
};

export type ImportDiagnostics = {
  duplicate_nodes_dropped: number;
  duplicate_edges_dropped: number;
  dangling_edges_dropped: number;
};

export type GraphDocument = {
  schema_version: "recallweave.viewer.v1";
  title?: string;
  generated_at?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  privacy: {
    export_profile: string;
    declared_export_profile: string;
    metadata_only: boolean;
    includes_excerpts: boolean;
    includes_passage_text: boolean;
    includes_note_derived_terms: boolean;
    includes_paths_titles_tags: boolean;
    source_claims_generated_locally: boolean;
    declared: boolean;
    metadata_conflict: boolean;
  };
  diagnostics?: {
    unresolved_links?: number;
  };
  import_diagnostics: ImportDiagnostics;
};

export const MAX_NODES = 5000;
export const MAX_EDGES = 12000;
export const MAX_FILE_BYTES = 15 * 1024 * 1024;

// Unicode default-ignorables include bidi controls, zero-width format
// characters, variation selectors, and historic invisible separators. Strip
// the property as a class instead of maintaining an incomplete blocklist.
// Newlines and tabs remain available in multiline evidence passages.
const C0_C1_CONTROLS =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F]/g;
const DEFAULT_IGNORABLES = /\p{Default_Ignorable_Code_Point}/gu;

export function safeText(value: unknown, fallback = ""): string {
  if (typeof value !== "string") return fallback;
  return value
    .replace(C0_C1_CONTROLS, "")
    .replace(DEFAULT_IGNORABLES, "")
    .slice(0, 1000);
}

export function safeLabel(value: unknown, fallback = ""): string {
  return safeText(value, fallback).replace(/[\t\r\n\u2028\u2029]+/gu, " ");
}

export function safeIdentifier(value: unknown, fallback = ""): string {
  return safeText(value, fallback).replace(/[\t\r\n\u2028\u2029]+/gu, "");
}

export function safeCitation(value: unknown): string {
  if (typeof value !== "string") return "";
  const cleaned = safeText(value);
  if (cleaned !== value) return "";
  if (/[\t\r\n\u2028\u2029]/u.test(cleaned)) return "";
  const match = /^(.+):([1-9]\d*)(?:-([1-9]\d*))?$/.exec(cleaned);
  if (!match || !match[1].trim()) return "";
  const start = Number(match[2]);
  const end = match[3] ? Number(match[3]) : start;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || end < start) {
    return "";
  }
  return cleaned;
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function evidenceSide(value: unknown): GraphEvidenceSide {
  if (!value || typeof value !== "object") return {};
  const raw = value as Record<string, unknown>;
  return {
    citation: safeCitation(raw.citation),
    passage: safeText(raw.passage),
  };
}

export function normalizeGraph(value: unknown): GraphDocument {
  if (!value || typeof value !== "object") {
    throw new Error("That file is not a RecallWeave graph.");
  }
  const raw = value as Record<string, unknown>;
  if (raw.schema_version !== "recallweave.viewer.v1") {
    throw new Error("Unsupported graph format. Expected recallweave.viewer.v1.");
  }
  if (!Array.isArray(raw.nodes) || !Array.isArray(raw.edges)) {
    throw new Error("The graph must contain nodes and edges.");
  }
  if (raw.nodes.length > MAX_NODES || raw.edges.length > MAX_EDGES) {
    throw new Error(`Graph exceeds the viewer limit of ${MAX_NODES} nodes and ${MAX_EDGES} edges.`);
  }

  const importDiagnostics: ImportDiagnostics = {
    duplicate_nodes_dropped: 0,
    duplicate_edges_dropped: 0,
    dangling_edges_dropped: 0,
  };
  const nodeIds = new Set<string>();
  const nodes: GraphNode[] = [];

  raw.nodes.forEach((item, index) => {
    if (!item || typeof item !== "object") {
      throw new Error(`Node ${index + 1} is not an object.`);
    }
    const node = item as Record<string, unknown>;
    const id = safeIdentifier(node.id);
    if (!id) throw new Error(`Node ${index + 1} has no id.`);
    if (nodeIds.has(id)) {
      importDiagnostics.duplicate_nodes_dropped += 1;
      return;
    }
    nodeIds.add(id);
    nodes.push({
      id,
      title: safeLabel(node.title, id),
      path: safeIdentifier(node.path, id),
      status: safeLabel(node.status),
      domain: safeLabel(node.domain, "Unclassified"),
      summary: safeText(node.summary),
      tags: Array.isArray(node.tags)
        ? node.tags.map((tag) => safeLabel(tag)).filter(Boolean).slice(0, 24)
        : [],
      section_count: finiteNumber(node.section_count),
    });
  });

  const edgeIds = new Set<string>();
  const edges: GraphEdge[] = [];
  raw.edges.forEach((item, index) => {
    if (!item || typeof item !== "object") {
      importDiagnostics.dangling_edges_dropped += 1;
      return;
    }
    const edge = item as Record<string, unknown>;
    const evidence =
      edge.evidence && typeof edge.evidence === "object"
        ? (edge.evidence as Record<string, unknown>)
        : {};
    const nestedSource = evidenceSide(evidence.source_evidence);
    const nestedTarget = evidenceSide(evidence.target_evidence);
    const flatCitation = safeCitation(evidence.citation);
    const flatSourceText = safeText(evidence.source_text);
    const sourceEvidence: GraphEvidenceSide = {
      citation: nestedSource.citation || flatCitation,
      passage: nestedSource.passage || flatSourceText,
    };
    const source = safeIdentifier(edge.source);
    const target = safeIdentifier(edge.target);
    if (!nodeIds.has(source) || !nodeIds.has(target)) {
      importDiagnostics.dangling_edges_dropped += 1;
      return;
    }
    const id = safeIdentifier(edge.id, `edge-${index + 1}`);
    if (edgeIds.has(id)) {
      importDiagnostics.duplicate_edges_dropped += 1;
      return;
    }
    edgeIds.add(id);
    edges.push({
      id,
      source,
      target,
      kind: safeLabel(edge.kind, "connection"),
      verified: edge.verified === true,
      score: finiteNumber(edge.score),
      evidence: {
        citation: flatCitation,
        explanation: safeText(evidence.explanation),
        source_text: flatSourceText,
        source_evidence: sourceEvidence,
        target_evidence: nestedTarget,
        shared_terms: Array.isArray(evidence.shared_terms)
          ? evidence.shared_terms.map((term) => safeLabel(term)).filter(Boolean).slice(0, 12)
          : [],
      },
    });
  });

  const privacyRaw =
    raw.privacy && typeof raw.privacy === "object"
      ? (raw.privacy as Record<string, unknown>)
      : null;
  const privacyDeclared = privacyRaw !== null;
  const includesPassageText =
    nodes.some((node) => Boolean(node.summary)) ||
    edges.some(
      (edge) =>
        Boolean(edge.evidence?.source_text) ||
        Boolean(edge.evidence?.source_evidence?.passage) ||
        Boolean(edge.evidence?.target_evidence?.passage),
    );
  // Candidate explanations may summarize note content even when they look like
  // product copy. Classify them conservatively alongside shared terms.
  const includesNoteDerivedTerms = edges.some(
    (edge) =>
      Boolean(edge.evidence?.shared_terms?.length) ||
      Boolean(edge.evidence?.explanation),
  );
  const includesPathsTitlesTags = nodes.some(
    (node) =>
      Boolean(node.path) ||
      Boolean(node.title) ||
      Boolean(node.tags?.length),
  );
  const metadataOnly = !includesPassageText && !includesNoteDerivedTerms;
  const actualExportProfile = nodes.length === 0
    ? "empty_graph"
    : includesPassageText
      ? "graph_with_bounded_passage_text"
      : includesNoteDerivedTerms
        ? "graph_metadata_and_note_derived_terms"
        : "graph_metadata";
  const declaredExportProfile = safeLabel(privacyRaw?.export_profile, "undeclared");
  const privacyFlagConflict =
    privacyDeclared &&
    (
      (typeof privacyRaw?.includes_excerpts === "boolean" &&
        privacyRaw.includes_excerpts !== includesPassageText) ||
      (typeof privacyRaw?.includes_passage_text === "boolean" &&
        privacyRaw.includes_passage_text !== includesPassageText) ||
      (typeof privacyRaw?.includes_note_derived_terms === "boolean" &&
        privacyRaw.includes_note_derived_terms !== includesNoteDerivedTerms) ||
      (typeof privacyRaw?.includes_paths_titles_tags === "boolean" &&
        privacyRaw.includes_paths_titles_tags !== includesPathsTitlesTags) ||
      (typeof privacyRaw?.metadata_only === "boolean" &&
        privacyRaw.metadata_only !== metadataOnly) ||
      (declaredExportProfile !== "undeclared" && declaredExportProfile !== actualExportProfile)
    );
  const diagnosticsRaw =
    raw.diagnostics && typeof raw.diagnostics === "object"
      ? (raw.diagnostics as Record<string, unknown>)
      : null;

  return {
    schema_version: "recallweave.viewer.v1",
    title: safeLabel(raw.title, "Loaded knowledge graph"),
    generated_at: safeLabel(raw.generated_at),
    nodes,
    edges,
    privacy: {
      export_profile: actualExportProfile,
      declared_export_profile: declaredExportProfile,
      metadata_only: metadataOnly,
      includes_excerpts: includesPassageText,
      includes_passage_text: includesPassageText,
      includes_note_derived_terms: includesNoteDerivedTerms,
      includes_paths_titles_tags: includesPathsTitlesTags,
      source_claims_generated_locally: privacyRaw?.generated_locally === true,
      declared: privacyDeclared,
      metadata_conflict: privacyFlagConflict,
    },
    diagnostics: diagnosticsRaw
      ? { unresolved_links: finiteNumber(diagnosticsRaw.unresolved_links) }
      : undefined,
    import_diagnostics: importDiagnostics,
  };
}

export function importDiagnosticMessage(diagnostics: ImportDiagnostics): string {
  const parts: string[] = [];
  if (diagnostics.duplicate_nodes_dropped) {
    parts.push(`${diagnostics.duplicate_nodes_dropped} duplicate node${diagnostics.duplicate_nodes_dropped === 1 ? "" : "s"}`);
  }
  if (diagnostics.duplicate_edges_dropped) {
    parts.push(`${diagnostics.duplicate_edges_dropped} duplicate edge${diagnostics.duplicate_edges_dropped === 1 ? "" : "s"}`);
  }
  if (diagnostics.dangling_edges_dropped) {
    parts.push(`${diagnostics.dangling_edges_dropped} dangling edge${diagnostics.dangling_edges_dropped === 1 ? "" : "s"}`);
  }
  return parts.length ? `${parts.join(", ")} dropped while loading.` : "";
}
