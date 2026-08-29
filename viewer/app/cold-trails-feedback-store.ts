import type { GraphDocument } from "./graph-data.ts";

const STORAGE_PREFIX = "recallweave.cold-trails.dismissed.v1:";
const MAX_STORED_KEYS = 500;

/** Stable localStorage key material for a loaded graph (hashed; no vault paths). */
export async function graphFeedbackFingerprint(
  graph: GraphDocument,
): Promise<string> {
  const exportId = graph.export_history?.export_id?.trim();
  if (exportId) return `export:${await sha256Hex(`export-id:${exportId}`)}`;
  const nodeParts = graph.nodes
    .map((node) => `${node.id}:${node.content_hash || ""}`)
    .sort();
  const edgeParts = graph.edges
    .map((edge) => `${edge.id}:${edge.source}:${edge.target}:${edge.verified ? 1 : 0}`)
    .sort();
  return `graph:${await sha256Hex([...nodeParts, ...edgeParts].join("\n"))}`;
}

/** Persist only digests of pair keys — never raw note paths or IDs. */
export async function hashDismissedPairKey(pairKey: string): Promise<string> {
  return sha256Hex(`dismissed-pair:${pairKey}`);
}

export async function loadDismissedPairDigests(
  fingerprint: string,
  storage: Pick<Storage, "getItem"> | null | undefined = globalThis.localStorage,
): Promise<string[]> {
  if (!fingerprint || !storage) return [];
  try {
    const raw = storage.getItem(STORAGE_PREFIX + fingerprint);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const digests = parsed
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim().toLowerCase())
      .filter((item) => /^[0-9a-f]{64}$/u.test(item))
      .slice(0, MAX_STORED_KEYS);
    return [...new Set(digests)].sort();
  } catch {
    return [];
  }
}

export async function saveDismissedPairDigests(
  fingerprint: string,
  digests: Iterable<string>,
  storage: Pick<Storage, "setItem"> | null | undefined = globalThis.localStorage,
): Promise<void> {
  if (!fingerprint || !storage) return;
  const unique = [
    ...new Set(
      [...digests]
        .map((item) => item.trim().toLowerCase())
        .filter((item) => /^[0-9a-f]{64}$/u.test(item)),
    ),
  ]
    .sort()
    .slice(0, MAX_STORED_KEYS);
  try {
    storage.setItem(STORAGE_PREFIX + fingerprint, JSON.stringify(unique));
  } catch {
    // Quota / private mode: persistence is optional.
  }
}

export async function clearDismissedPairDigests(
  fingerprint: string,
  storage: Pick<Storage, "removeItem"> | null | undefined = globalThis.localStorage,
): Promise<void> {
  if (!fingerprint || !storage) return;
  try {
    storage.removeItem(STORAGE_PREFIX + fingerprint);
  } catch {
    // ignore
  }
}

/**
 * Rebuild a feedback dismiss set that includes only keys whose digests were
 * previously stored for this graph. Keys are the in-memory pair identities;
 * storage never sees them.
 */
export async function filterDismissedPairsByStoredDigests(
  candidatePairKeys: Iterable<string>,
  storedDigests: Iterable<string>,
): Promise<Set<string>> {
  const allowed = new Set(storedDigests);
  const kept = new Set<string>();
  for (const key of candidatePairKeys) {
    const digest = await hashDismissedPairKey(key);
    if (allowed.has(digest)) kept.add(key);
  }
  return kept;
}

/** In-memory pair identities that may appear in dismiss feedback for this graph. */
export function candidateDismissKeys(graph: GraphDocument): string[] {
  const keys = new Set<string>();
  for (const node of graph.nodes) {
    keys.add(`node:${node.id}`);
  }
  for (const edge of graph.edges) {
    const a = edge.source;
    const b = edge.target;
    keys.add(a < b ? `${a}|${b}` : `${b}|${a}`);
  }
  return [...keys];
}

async function sha256Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error("Web Crypto SHA-256 is required for Cold Trails persistence.");
  }
  const digest = await subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
