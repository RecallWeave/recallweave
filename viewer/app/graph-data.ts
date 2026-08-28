export type GraphEvidenceSignals = {
  lexical_terms?: string[];
  shared_tags?: string[];
  mutual_neighbor_ids?: string[];
};

export type GraphEvidence = {
  citation?: string;
  explanation?: string;
  source_text?: string;
  shared_terms?: string[];
  signals?: GraphEvidenceSignals;
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

export type GraphNode = {
  id: string;
  title: string;
  path: string;
  status?: string;
  domain?: string;
  summary?: string;
  tags?: string[];
  section_count?: number;
  created_at?: string | null;
  modified_at?: string | null;
  content_hash?: string | null;
};

export type ExportHistoryClaims = {
  export_id: string;
  previous_content_hash: string | null;
  node_content_hashes_changed: number;
  node_content_hashes_unchanged: number;
  nodes_added: number;
  nodes_removed: number;
  claim_conflict: boolean;
};

export type ImportDiagnostics = {
  duplicate_nodes_dropped: number;
  duplicate_edges_dropped: number;
  dangling_edges_dropped: number;
};

export const VIEWER_SCHEMA_V1 = "recallweave.viewer.v1";
export const VIEWER_SCHEMA_V2 = "recallweave.viewer.v2";
export const SUPPORTED_VIEWER_SCHEMAS = new Set([VIEWER_SCHEMA_V1, VIEWER_SCHEMA_V2]);

export type GraphDocument = {
  schema_version: typeof VIEWER_SCHEMA_V1 | typeof VIEWER_SCHEMA_V2;
  title?: string;
  generated_at?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  vault_label_claim?: string;
  policy_config_sha256_claim?: string;
  export_history?: ExportHistoryClaims;
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

export function citationPath(citation: string): string {
  const validated = safeCitation(citation);
  if (!validated) return "";
  return validated.replace(/:[1-9]\d*(?:-[1-9]\d*)?$/u, "");
}

const SHA256_HEX = /^[a-f0-9]{64}$/u;

export function safeContentHash(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (!normalized || !SHA256_HEX.test(normalized)) return null;
  return normalized;
}

const UTC_ISO_TIMESTAMP =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/u;

/** Absolute UTC microseconds since Unix epoch, preserving up to microsecond precision. */
export function utcEpochMicros(value: string): bigint | null {
  const match = UTC_ISO_TIMESTAMP.exec(value);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  // Date.UTC remaps years 0–99 into 1900–1999; reject those and other out-of-range years.
  if (year < 1000 || year > 9999) return null;
  if (month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) {
    return null;
  }
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  if (day > daysInMonth) return null;
  const tz = match[8];
  let offsetMinutes = 0;
  if (tz !== "Z") {
    const offsetHour = Number(tz.slice(1, 3));
    const offsetMinute = Number(tz.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return null;
    const sign = tz.startsWith("-") ? -1 : 1;
    offsetMinutes = sign * (offsetHour * 60 + offsetMinute);
  }
  const secondMs = Date.UTC(year, month - 1, day, hour, minute, second) - offsetMinutes * 60_000;
  if (!Number.isFinite(secondMs)) return null;
  const fracDigits = match[7] || "";
  // Accept at most microsecond precision; do not silently truncate nanoseconds.
  if (fracDigits.length > 6) return null;
  const microsPart = BigInt(fracDigits.padEnd(6, "0").slice(0, 6) || "0");
  return BigInt(secondMs) * BigInt(1000) + microsPart;
}

function formatUtcMicros(epochMicros: bigint): string {
  const million = BigInt(1_000_000);
  let microsInSecond = epochMicros % million;
  let secondEpochMicros = epochMicros - microsInSecond;
  if (microsInSecond < BigInt(0)) {
    microsInSecond += million;
    secondEpochMicros -= million;
  }
  const secondMs = Number(secondEpochMicros / BigInt(1000));
  const asUtc = new Date(secondMs);
  const base = asUtc.toISOString().replace(/\.\d{3}Z$/u, "Z").replace(/Z$/u, "");
  if (microsInSecond === BigInt(0)) return `${base}Z`;
  const frac = microsInSecond.toString().padStart(6, "0").replace(/0+$/u, "");
  return `${base}.${frac}Z`;
}

export function safeIsoTimestamp(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;
  if (value !== value.trim()) return null;
  const cleaned = value.trim();
  const epoch = utcEpochMicros(cleaned);
  if (epoch === null) return null;
  const formatted = formatUtcMicros(epoch);
  // Reject conversions that cannot round-trip (e.g. offset crossing year 1000).
  if (utcEpochMicros(formatted) !== epoch) return null;
  return formatted;
}

export function safeVaultLabel(value: unknown): string {
  if (typeof value !== "string") return "";
  const label = safeLabel(value).slice(0, 120);
  if (!label) return "";
  if (/[/\\:]|obsidian|^\./iu.test(label)) return "";
  if (/\.\./u.test(label)) return "";
  return label;
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

function evidenceSignals(value: unknown): GraphEvidenceSignals | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  const lexical_terms: string[] = [];
  if (Array.isArray(raw.lexical_terms)) {
    const seen = new Set<string>();
    for (const term of raw.lexical_terms) {
      const safe = safeLabel(term);
      if (!safe) continue;
      const key = safe.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      lexical_terms.push(safe);
      if (lexical_terms.length >= 24) break;
    }
  }
  const shared_tags = Array.isArray(raw.shared_tags)
    ? raw.shared_tags.map((tag) => safeLabel(tag)).filter(Boolean).slice(0, 24)
    : [];
  const mutual_neighbor_ids = Array.isArray(raw.mutual_neighbor_ids)
    ? raw.mutual_neighbor_ids.map((id) => safeIdentifier(id)).filter(Boolean).slice(0, 24)
    : [];
  if (!lexical_terms.length && !shared_tags.length && !mutual_neighbor_ids.length) {
    return undefined;
  }
  return {
    lexical_terms: lexical_terms.length ? lexical_terms : undefined,
    shared_tags: shared_tags.length ? shared_tags : undefined,
    mutual_neighbor_ids: mutual_neighbor_ids.length ? mutual_neighbor_ids : undefined,
  };
}

function optionalNonNegativeInteger(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const number = finiteNumber(value);
  if (number === undefined || !Number.isSafeInteger(number) || number < 0) {
    return undefined;
  }
  return number;
}

function parseExportHistory(
  value: unknown,
  nodes: GraphNode[],
): ExportHistoryClaims | undefined {
  if (!value || typeof value !== "object") return undefined;
  const raw = value as Record<string, unknown>;
  const export_id = safeIdentifier(raw.export_id);
  if (!export_id) return undefined;
  const priorAbsent = !Object.prototype.hasOwnProperty.call(raw, "previous_content_hash");
  const priorRaw = raw.previous_content_hash;
  const previous_content_hash = safeContentHash(priorRaw);
  const priorMalformed =
    priorAbsent ||
    (priorRaw !== null &&
      priorRaw !== undefined &&
      previous_content_hash === null);
  const node_content_hashes_changed = optionalNonNegativeInteger(
    raw.node_content_hashes_changed,
  );
  const node_content_hashes_unchanged = optionalNonNegativeInteger(
    raw.node_content_hashes_unchanged,
  );
  const nodes_added = optionalNonNegativeInteger(raw.nodes_added);
  const nodes_removed = optionalNonNegativeInteger(raw.nodes_removed);
  const malformedCounters =
    node_content_hashes_changed === undefined ||
    node_content_hashes_unchanged === undefined ||
    nodes_added === undefined ||
    nodes_removed === undefined;
  const changed = node_content_hashes_changed ?? 0;
  const unchanged = node_content_hashes_unchanged ?? 0;
  const added = nodes_added ?? 0;
  const removed = nodes_removed ?? 0;
  const overlapHashes = changed + unchanged;
  const totalNodes = nodes.length;
  const claim_conflict =
    malformedCounters ||
    priorMalformed ||
    (previous_content_hash === null
      ? added !== totalNodes || overlapHashes !== 0 || removed !== 0
      : overlapHashes + added !== totalNodes);
  return {
    export_id,
    previous_content_hash: priorMalformed ? null : previous_content_hash,
    node_content_hashes_changed: changed,
    node_content_hashes_unchanged: unchanged,
    nodes_added: added,
    nodes_removed: removed,
    claim_conflict,
  };
}

export function normalizeGraph(value: unknown): GraphDocument {
  if (!value || typeof value !== "object") {
    throw new Error("That file is not a RecallWeave graph.");
  }
  const raw = value as Record<string, unknown>;
  const schemaVersion = raw.schema_version;
  if (!SUPPORTED_VIEWER_SCHEMAS.has(schemaVersion as typeof VIEWER_SCHEMA_V1)) {
    throw new Error(
      "Unsupported graph format. Expected recallweave.viewer.v1 or recallweave.viewer.v2.",
    );
  }
  const isV2 = schemaVersion === VIEWER_SCHEMA_V2;
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
    const normalized: GraphNode = {
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
    };
    if (isV2) {
      normalized.created_at = safeIsoTimestamp(node.created_at);
      normalized.modified_at = safeIsoTimestamp(node.modified_at);
      normalized.content_hash = safeContentHash(node.content_hash);
    }
    nodes.push(normalized);
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
    const signals = isV2 ? evidenceSignals(evidence.signals) : undefined;
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
        signals,
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
      Boolean(edge.evidence?.signals?.lexical_terms?.length) ||
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

  const vault_label_claim = isV2 ? safeVaultLabel(raw.vault_name) : undefined;
  const policy_config_sha256_claim = isV2
    ? safeContentHash(raw.policy_config_sha256) ?? undefined
    : undefined;
  const export_history = isV2 ? parseExportHistory(raw.export_history, nodes) : undefined;

  return {
    schema_version: isV2 ? VIEWER_SCHEMA_V2 : VIEWER_SCHEMA_V1,
    title: safeLabel(raw.title, "Loaded knowledge graph"),
    generated_at: safeIsoTimestamp(raw.generated_at) ?? undefined,
    nodes,
    edges,
    vault_label_claim: vault_label_claim || undefined,
    policy_config_sha256_claim,
    export_history,
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
