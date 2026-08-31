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

/**
 * Subscribe to local Obsidian-vault config changes. Returns an unsubscribe.
 *
 * Also listens for the browser `storage` event so a change made in ANOTHER
 * same-origin Atlas tab (including clearing all storage, key === null) updates
 * this tab too, instead of leaving it launching a stale vault until an unrelated
 * re-render.
 */
export function subscribeObsidianVault(listener: () => void): () => void {
  vaultListeners.add(listener);
  let storageHandler: ((event: StorageEvent) => void) | undefined;
  const canListen =
    typeof window !== "undefined" && typeof window.addEventListener === "function";
  if (canListen) {
    storageHandler = (event: StorageEvent) => {
      if (event.key === null || event.key === ATLAS_OBSIDIAN_VAULT_STORAGE_KEY) {
        listener();
      }
    };
    window.addEventListener("storage", storageHandler);
  }
  return () => {
    vaultListeners.delete(listener);
    if (storageHandler && canListen) {
      window.removeEventListener("storage", storageHandler);
    }
  };
}

function notifyVaultChange(): void {
  for (const listener of vaultListeners) listener();
}

const MAX_VAULT_LABEL_LENGTH = 120;

/**
 * True if the string carries any C0 or C1 control character (U+0000–U+001F,
 * U+007F–U+009F) or a line/paragraph separator — matching the graph import
 * sanitizer's control range so navigation labels fail closed consistently.
 */
function hasControlChars(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (
      code <= 0x1f ||
      (code >= 0x7f && code <= 0x9f) ||
      code === 0x2028 ||
      code === 0x2029
    ) {
      return true;
    }
  }
  return false;
}

/**
 * True if the string contains an unpaired UTF-16 surrogate. Such a value would
 * make encodeURIComponent throw at click time, so it must be rejected up front
 * rather than accepted, persisted, and shown as configured while every launch
 * silently fails.
 */
function hasLoneSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      index += 1; // valid pair; skip the low surrogate
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true; // low surrogate with no preceding high surrogate
    }
  }
  return false;
}

// Bidi controls, zero-width and other default-ignorable format characters can
// make a stored label display identically to (or reordered from) its real
// value, so every launch would target a nonexistent or unintended vault. Reject
// them up front, matching the graph import sanitizers for displayed identifiers.
const FORMAT_OR_IGNORABLE =
  /[\p{Cf}\p{Default_Ignorable_Code_Point}]/u;

/**
 * Validate a locally-configured Obsidian vault name as a navigation label,
 * INDEPENDENTLY of the export's provenance `vault_name`. Fail closed: return the
 * normalized label, or null for anything empty, path-shaped, scheme-shaped, or
 * carrying control characters — so a hostile value can never reach a URI.
 */
export function normalizeObsidianVaultLabel(value: unknown): string | null {
  if (typeof value !== "string") return null;
  // Trim only plain U+0020 from the ends (NOT String.trim, which would also
  // discard an edge tab/newline/NBSP that may be part of the real name). Internal
  // spaces are preserved EXACTLY — consecutive U+0020 are valid in real vault
  // names ("Research  Notes") — so collapsing/trimming other whitespace would
  // silently target a different or nonexistent vault; such whitespace is instead
  // rejected below.
  const trimmed = value.replace(/^ +/u, "").replace(/ +$/u, "");
  // Validate the COMPLETE value FIRST, before any length truncation: otherwise a
  // forbidden character beginning after the 120th code point (e.g. a separator
  // in "A"*120 + "/Other") would be sliced away and the prefix wrongly accepted.
  if (!trimmed) return null;
  // Reject any whitespace that is not a plain space (tab, newline, NBSP, other
  // Unicode spaces) rather than silently collapsing it — fail closed.
  if (/\s/u.test(trimmed.replace(/ /gu, ""))) return null;
  if (trimmed.startsWith(".")) return null;
  // Reject path separators and the scheme/host separator so the label cannot be
  // shaped into a path or a URI fragment.
  if (/[/\\:]/u.test(trimmed)) return null;
  if (hasControlChars(trimmed)) return null;
  if (hasLoneSurrogate(trimmed)) return null;
  if (FORMAT_OR_IGNORABLE.test(trimmed)) return null;
  // Reject an overlength label rather than truncating it: a vault name is a
  // navigation IDENTITY, so a silently truncated prefix could target a different
  // or nonexistent vault. Count by Unicode code points (Array.from), not UTF-16
  // units. Fail closed.
  if (Array.from(trimmed).length > MAX_VAULT_LABEL_LENGTH) return null;
  return trimmed;
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
  // Any Windows drive prefix (C:\, C:/, or the drive-RELATIVE C:secret.md,
  // which Windows resolves against the current dir on drive C:).
  if (/^[A-Za-z]:/u.test(value)) return false;
  // Any `..` traversal segment, under either separator.
  if (value.split(/[/\\]/u).some((segment) => segment === "..")) return false;
  if (hasLoneSurrogate(value)) return false;
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
  try {
    return (
      "obsidian://open?vault=" +
      encodeURIComponent(vault) +
      "&file=" +
      encodeURIComponent(relativePath)
    );
  } catch {
    // encodeURIComponent throws on a lone surrogate (e.g. a note path carrying
    // an unpaired surrogate). Fail closed rather than surface a click-time crash.
    return null;
  }
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

/**
 * Clear the locally-configured Obsidian vault name. Returns true when the value
 * was removed (or was already absent), false when storage threw and the value
 * may persist — so the caller can report a failure instead of a false success.
 * Never throws.
 */
export function clearObsidianVault(
  storage: Pick<Storage, "removeItem"> | null | undefined = safeLocalStorage(),
): boolean {
  // Unavailable storage (blocked, or its getter threw) is a FAILED clear, not a
  // success: a prior value could become active again if access is restored.
  if (!storage) return false;
  let removed = true;
  try {
    storage.removeItem(ATLAS_OBSIDIAN_VAULT_STORAGE_KEY);
  } catch {
    removed = false;
  }
  // Notify regardless so subscribers re-read the true current state (a failed
  // removal leaves the prior value in place).
  notifyVaultChange();
  return removed;
}

function safeLocalStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
