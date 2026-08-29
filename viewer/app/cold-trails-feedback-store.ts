import type { GraphDocument } from "./graph-data.ts";

const STORAGE_PREFIX = "recallweave.cold-trails.dismissed.v1:";

/** Stable localStorage key material for a loaded graph (no vault paths). */
export function graphFeedbackFingerprint(graph: GraphDocument): string {
  const exportId = graph.export_history?.export_id?.trim();
  if (exportId) return `export:${exportId}`;
  const nodeParts = graph.nodes
    .map((node) => `${node.id}:${node.content_hash || ""}`)
    .sort();
  const edgeParts = graph.edges
    .map((edge) => `${edge.id}:${edge.source}:${edge.target}:${edge.verified ? 1 : 0}`)
    .sort();
  return `graph:${fnv1aHex([...nodeParts, ...edgeParts].join("\n"))}`;
}

export function loadDismissedPairKeys(
  fingerprint: string,
  storage: Pick<Storage, "getItem"> = globalThis.localStorage,
): string[] {
  if (!fingerprint || !storage) return [];
  try {
    const raw = storage.getItem(STORAGE_PREFIX + fingerprint);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return [
      ...new Set(
        parsed.filter((item): item is string => typeof item === "string" && item.length > 0),
      ),
    ].sort();
  } catch {
    return [];
  }
}

export function saveDismissedPairKeys(
  fingerprint: string,
  keys: Iterable<string>,
  storage: Pick<Storage, "setItem"> = globalThis.localStorage,
): void {
  if (!fingerprint || !storage) return;
  const unique = [...new Set([...keys].filter(Boolean))].sort();
  try {
    storage.setItem(STORAGE_PREFIX + fingerprint, JSON.stringify(unique));
  } catch {
    // Quota / private mode: persistence is optional.
  }
}

export function clearDismissedPairKeys(
  fingerprint: string,
  storage: Pick<Storage, "removeItem"> = globalThis.localStorage,
): void {
  if (!fingerprint || !storage) return;
  try {
    storage.removeItem(STORAGE_PREFIX + fingerprint);
  } catch {
    // ignore
  }
}

function fnv1aHex(input: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}
