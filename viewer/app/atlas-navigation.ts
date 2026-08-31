/**
 * Opt-in Obsidian vault navigation — LOCAL PRESENTATION STATE ONLY.
 *
 * Nothing in this module touches the export: it neither reads nor writes any
 * export field (the frozen `recallweave.viewer.v2` `vault_name` stays
 * provenance-only), and nothing here affects export bytes, the export schema,
 * export hashes, provenance, the index, deterministic findings, task contracts,
 * or the Steward Truth plane. The export describes evidence; Atlas decides how
 * this local viewer navigates to it.
 *
 * The ONLY supported deep link is `obsidian://open`. There are deliberately no
 * arbitrary command templates, no shell execution, no generic external
 * handlers, and no configurable URI schemes. Copy-relative-path (in
 * GraphExplorer) remains the permanent, always-available floor and fallback.
 */

/** localStorage key for the one locally-configured Obsidian vault name. */
export const ATLAS_OBSIDIAN_VAULT_STORAGE_KEY =
  "recallweave.atlas.obsidian_vault.v1";

// Minimal subscription so the UI can read the configured vault via
// useSyncExternalStore (no effect-driven setState, no hydration mismatch:
// the server snapshot is always null, the client reads storage after mount).
const vaultListeners = new Set<() => void>();

/** Subscribe to local Obsidian-vault config changes. Returns an unsubscribe. */
export function subscribeObsidianVault(listener: () => void): () => void {
  vaultListeners.add(listener);
  return () => {
    vaultListeners.delete(listener);
  };
}

function notifyVaultChange(): void {
  for (const listener of vaultListeners) listener();
}

const MAX_VAULT_LABEL_LENGTH = 120;

/** True if the string carries any C0/DEL control char or a line/para separator. */
function hasControlChars(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 0x1f || code === 0x7f || code === 0x2028 || code === 0x2029) {
      return true;
    }
  }
  return false;
}

/**
 * Validate a locally-configured Obsidian vault name as a navigation label,
 * INDEPENDENTLY of the export's provenance `vault_name`. Fail closed: return the
 * normalized label, or null for anything empty, path-shaped, scheme-shaped, or
 * carrying control characters — so a hostile value can never reach a URI.
 */
export function normalizeObsidianVaultLabel(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const label = value.replace(/\s+/gu, " ").trim().slice(0, MAX_VAULT_LABEL_LENGTH);
  if (!label) return null;
  if (label.startsWith(".")) return null;
  // Reject path separators and the scheme/host separator so the label cannot be
  // shaped into a path or a URI fragment.
  if (/[/\\:]/u.test(label)) return null;
  if (hasControlChars(label)) return null;
  return label;
}

/**
 * True only when `path` is a safe RELATIVE note path usable in a navigation URI.
 * On load `safeIdentifier` strips control characters but does NOT reject
 * absolute or traversing paths; navigation must, so that no absolute source path
 * can ever enter a URI. Fail closed.
 */
export function isNavigableRelativePath(path: unknown): path is string {
  if (typeof path !== "string") return false;
  const value = path.trim();
  if (!value) return false;
  if (hasControlChars(value)) return false;
  // Absolute POSIX or leading-backslash / UNC.
  if (value.startsWith("/") || value.startsWith("\\")) return false;
  // Windows drive-qualified (C:\ or C:/).
  if (/^[A-Za-z]:[\\/]/u.test(value)) return false;
  // Any `..` traversal segment, under either separator.
  if (value.split(/[/\\]/u).some((segment) => segment === "..")) return false;
  return true;
}

/**
 * Build an `obsidian://open` URI from a locally-configured vault name and a
 * validated relative note path, URI-encoding both. Return null (no link) when
 * either input fails validation. Callers invoke this ONLY at click time; the
 * result is never stored, never pre-rendered into the DOM ahead of a click, and
 * never emitted into the export.
 */
export function buildObsidianOpenUri(
  vaultName: unknown,
  relativePath: unknown,
): string | null {
  const vault = normalizeObsidianVaultLabel(vaultName);
  if (vault === null) return null;
  if (!isNavigableRelativePath(relativePath)) return null;
  return (
    "obsidian://open?vault=" +
    encodeURIComponent(vault) +
    "&file=" +
    encodeURIComponent(relativePath)
  );
}

/** Load the locally-configured Obsidian vault name, or null. Never throws. */
export function loadObsidianVault(
  storage: Pick<Storage, "getItem"> | null | undefined = safeLocalStorage(),
): string | null {
  try {
    if (!storage) return null;
    return normalizeObsidianVaultLabel(
      storage.getItem(ATLAS_OBSIDIAN_VAULT_STORAGE_KEY),
    );
  } catch {
    return null;
  }
}

/**
 * Persist a locally-configured Obsidian vault name. Return the normalized value
 * actually stored, or null when the input is rejected (nothing is stored) or
 * storage is unavailable. Never throws.
 */
export function saveObsidianVault(
  value: unknown,
  storage: Pick<Storage, "setItem"> | null | undefined = safeLocalStorage(),
): string | null {
  const normalized = normalizeObsidianVaultLabel(value);
  if (normalized === null) return null;
  if (!storage) return null;
  try {
    storage.setItem(ATLAS_OBSIDIAN_VAULT_STORAGE_KEY, normalized);
  } catch {
    return null;
  }
  notifyVaultChange();
  return normalized;
}

/** Clear the locally-configured Obsidian vault name. Never throws. */
export function clearObsidianVault(
  storage: Pick<Storage, "removeItem"> | null | undefined = safeLocalStorage(),
): void {
  try {
    storage?.removeItem(ATLAS_OBSIDIAN_VAULT_STORAGE_KEY);
  } catch {
    // best-effort; a viewer with storage disabled simply keeps no configuration
  }
  notifyVaultChange();
}

function safeLocalStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
