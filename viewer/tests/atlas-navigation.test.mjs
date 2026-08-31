import assert from "node:assert/strict";
import test from "node:test";

import {
  ATLAS_OBSIDIAN_VAULT_STORAGE_KEY,
  buildObsidianOpenUri,
  clearObsidianVault,
  isNavigableRelativePath,
  loadObsidianVault,
  normalizeObsidianVaultLabel,
  saveObsidianVault,
  subscribeObsidianVault,
} from "../app/atlas-navigation.ts";

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => {
      map.set(key, String(value));
    },
    removeItem: (key) => {
      map.delete(key);
    },
    map,
  };
}

// Founder proof: hostile/invalid vault labels fail closed. Whitespace (tab,
// newline) is collapsed to spaces exactly as the export-side provenance
// normalizer does; only genuine non-whitespace control characters are rejected.
test("normalizeObsidianVaultLabel rejects hostile or invalid labels", () => {
  const rejected = [
    "",
    "   ",
    null,
    undefined,
    42,
    {},
    "../etc",
    ".hidden",
    "a/b",
    "a\\b",
    "vault:name",
    "obsidian://open",
    "\u0000null",
    "\u0007bell",
    "\u001funit",
    "del\u007fchar",
  ];
  for (const value of rejected) {
    assert.equal(
      normalizeObsidianVaultLabel(value),
      null,
      `should reject: ${JSON.stringify(value)}`,
    );
  }
  // Valid labels are stored VERBATIM — no trimming, no collapsing (a vault name
  // is a navigation identity). Edge and internal U+0020 are preserved; non-space
  // whitespace (tab, newline) is rejected; all-whitespace is rejected.
  assert.equal(normalizeObsidianVaultLabel("My Vault"), "My Vault");
  assert.equal(normalizeObsidianVaultLabel("  Research  Notes  "), "  Research  Notes  ");
  assert.equal(normalizeObsidianVaultLabel(" Research"), " Research");
  assert.equal(normalizeObsidianVaultLabel("   "), null);
  assert.equal(normalizeObsidianVaultLabel("with\nnewline"), null);
  assert.equal(normalizeObsidianVaultLabel("a\tb"), null);
  assert.equal(normalizeObsidianVaultLabel("nbsp here"), null);
  // Edge non-space whitespace is rejected, not silently trimmed (only plain
  // U+0020 is trimmed from the ends); C1 controls (U+0080–U+009F) fail closed.
  assert.equal(normalizeObsidianVaultLabel(String.fromCharCode(0x00a0) + "Vault"), null);
  assert.equal(normalizeObsidianVaultLabel("Vault" + String.fromCharCode(0x09)), null);
  // Edge U+0020 is preserved verbatim (identity-exact), not trimmed away.
  assert.equal(normalizeObsidianVaultLabel("  My Vault  "), "  My Vault  ");
  assert.equal(normalizeObsidianVaultLabel("Va" + String.fromCharCode(0x0085) + "ult"), null);
  assert.equal(normalizeObsidianVaultLabel("Va" + String.fromCharCode(0x009f) + "ult"), null);
  // Length is a HARD limit: a label at the cap is accepted verbatim, one over is
  // rejected (never truncated — truncation would change the navigation identity).
  assert.equal(normalizeObsidianVaultLabel("x".repeat(120)), "x".repeat(120));
  assert.equal(normalizeObsidianVaultLabel("x".repeat(121)), null);
  assert.equal(normalizeObsidianVaultLabel("x".repeat(200)), null);
  // A supplementary character (emoji) counts as ONE code point: 119 ASCII + 1
  // emoji = 120 is accepted whole (no split surrogate); 120 ASCII + 1 emoji =
  // 121 is over the cap and rejected (not truncated).
  const emojiAtBoundary = normalizeObsidianVaultLabel("a".repeat(119) + "😀");
  assert.equal([...emojiAtBoundary].length, 120);
  assert.doesNotThrow(() => encodeURIComponent(emojiAtBoundary));
  assert.ok(emojiAtBoundary.includes("😀"));
  assert.equal(normalizeObsidianVaultLabel("a".repeat(120) + "😀"), null);
  // A pre-existing unpaired surrogate in the input is rejected outright (it would
  // make encodeURIComponent throw at click time), not accepted and persisted.
  assert.equal(normalizeObsidianVaultLabel(String.fromCharCode(0xd800) + "vault"), null);
  assert.equal(normalizeObsidianVaultLabel("vault" + String.fromCharCode(0xdc00)), null);
  assert.equal(isNavigableRelativePath(String.fromCharCode(0xd800) + "note.md"), false);
  assert.equal(buildObsidianOpenUri("V", String.fromCharCode(0xd83d) + "note.md"), null);
  // The FULL collapsed value is validated before truncation: a forbidden
  // character beginning past the length cap must reject the whole input, not be
  // sliced away leaving an accepted prefix (which could name a different vault).
  assert.equal(normalizeObsidianVaultLabel("A".repeat(120) + "/Other"), null);
  assert.equal(normalizeObsidianVaultLabel("A".repeat(130) + ":smuggled"), null);
  // A wholly-valid but overlength label is rejected (never truncated to a prefix).
  assert.equal(normalizeObsidianVaultLabel("A".repeat(200)), null);
  // Bidi controls and zero-width / default-ignorable format characters are
  // rejected (they can spoof or reorder the displayed label vs. the real value).
  assert.equal(normalizeObsidianVaultLabel("Work" + String.fromCharCode(0x200b)), null);
  assert.equal(normalizeObsidianVaultLabel(String.fromCharCode(0x202e) + "Vault"), null);
  assert.equal(normalizeObsidianVaultLabel("Va" + String.fromCharCode(0x200e) + "ult"), null);
});

// Founder proof: no absolute source path may enter a URI.
test("isNavigableRelativePath rejects absolute, drive, UNC, and traversal paths", () => {
  const bad = [
    "",
    "   ",
    null,
    undefined,
    "/abs/note.md",
    "\\unc\\note.md",
    "C:\\vault\\note.md",
    "c:/vault/note.md",
    "C:secret.md",
    "c:note.md",
    "../escape.md",
    "sub/../../escape.md",
    "sub/..",
    "with\nnewline.md",
  ];
  for (const value of bad) {
    assert.equal(
      isNavigableRelativePath(value),
      false,
      `should reject: ${JSON.stringify(value)}`,
    );
  }
  const good = ["note.md", "sub/note.md", "a/b/c.md", "Título con espacios.md"];
  for (const value of good) {
    assert.equal(isNavigableRelativePath(value), true, `should accept: ${value}`);
  }
});

// Founder proofs: relative paths correctly encoded; only the intended URI shape;
// no absolute path reaches a URI.
test("buildObsidianOpenUri emits only the encoded obsidian://open shape", () => {
  assert.equal(
    buildObsidianOpenUri("My Vault", "sub folder/my note.md"),
    "obsidian://open?vault=My%20Vault&file=sub%20folder%2Fmy%20note.md",
  );
  // Reserved characters in the path are percent-encoded.
  assert.equal(
    buildObsidianOpenUri("V", "a&b#c.md"),
    "obsidian://open?vault=V&file=a%26b%23c.md",
  );
  // Absolute / drive / drive-relative / traversal paths yield no URI.
  assert.equal(buildObsidianOpenUri("V", "/etc/passwd"), null);
  assert.equal(buildObsidianOpenUri("V", "C:\\secret.md"), null);
  assert.equal(buildObsidianOpenUri("V", "C:secret.md"), null);
  assert.equal(buildObsidianOpenUri("V", "../escape.md"), null);
  // Invalid vault labels yield no URI.
  assert.equal(buildObsidianOpenUri("bad/vault", "note.md"), null);
  assert.equal(buildObsidianOpenUri("", "note.md"), null);
});

// Founder proof: no arbitrary schemes or command execution are producible.
test("buildObsidianOpenUri never yields a non-obsidian scheme", () => {
  for (const [vault, path] of [
    ["V", "note.md"],
    ["Vault Name", "deep/sub/note.md"],
  ]) {
    const uri = buildObsidianOpenUri(vault, path);
    assert.ok(uri && uri.startsWith("obsidian://open?vault="), `bad shape: ${uri}`);
  }
  // A filename that looks like another scheme is just an encoded filename: its
  // colon is percent-encoded, so no second scheme can ever appear.
  const uri = buildObsidianOpenUri("V", "javascript:alert(1).md");
  assert.equal(uri, "obsidian://open?vault=V&file=javascript%3Aalert(1).md");
  assert.ok(!uri.includes("javascript:"));
});

// Founder proof (partial): save fails closed; roundtrip works.
test("save/load/clear roundtrip and reject-and-store-nothing on invalid input", () => {
  const store = memoryStorage();
  assert.equal(loadObsidianVault(store), null);
  // Invalid input is rejected and NOTHING is stored (no fabricated value).
  assert.equal(saveObsidianVault("bad/vault", store), null);
  assert.equal(store.map.has(ATLAS_OBSIDIAN_VAULT_STORAGE_KEY), false);
  // Valid input persists verbatim (identity-exact; no trimming).
  assert.equal(saveObsidianVault("My Vault", store), "My Vault");
  assert.equal(loadObsidianVault(store), "My Vault");
  clearObsidianVault(store);
  assert.equal(loadObsidianVault(store), null);
});

test("storage helpers never throw when storage is unavailable or throwing", () => {
  const throwing = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {
      throw new Error("blocked");
    },
  };
  assert.equal(loadObsidianVault(throwing), null);
  assert.equal(saveObsidianVault("V", throwing), null);
  // clear reports failure rather than a false success when storage throws.
  assert.equal(clearObsidianVault(throwing), false);
  assert.equal(loadObsidianVault(null), null);
  assert.equal(saveObsidianVault("V", null), null);
});

test("clearObsidianVault reports success when the value is removed", () => {
  const store = memoryStorage();
  saveObsidianVault("V", store);
  assert.equal(clearObsidianVault(store), true);
  assert.equal(loadObsidianVault(store), null);
});

test("clearObsidianVault reports failure when storage is unavailable", () => {
  // Unavailable storage is a failed clear, not a false success.
  assert.equal(clearObsidianVault(null), false);
});

test("subscribeObsidianVault also reacts to cross-tab storage events", () => {
  // Simulate a same-origin sibling tab changing storage: the browser dispatches
  // a `storage` event, and our subscription must fire so useSyncExternalStore
  // re-reads instead of launching a stale vault.
  const handlers = new Set();
  const fakeWindow = {
    addEventListener: (type, handler) => {
      if (type === "storage") handlers.add(handler);
    },
    removeEventListener: (type, handler) => {
      if (type === "storage") handlers.delete(handler);
    },
  };
  const priorWindow = globalThis.window;
  globalThis.window = fakeWindow;
  try {
    let count = 0;
    const unsubscribe = subscribeObsidianVault(() => {
      count += 1;
    });
    // A change to our key notifies; an unrelated key does not; clear-all (null) does.
    for (const handler of handlers) {
      handler({ key: ATLAS_OBSIDIAN_VAULT_STORAGE_KEY });
      handler({ key: "some.other.key" });
      handler({ key: null });
    }
    unsubscribe();
    for (const handler of handlers) handler({ key: ATLAS_OBSIDIAN_VAULT_STORAGE_KEY });
    assert.equal(count, 2);
    assert.equal(handlers.size, 0, "unsubscribe must remove the storage listener");
  } finally {
    if (priorWindow === undefined) delete globalThis.window;
    else globalThis.window = priorWindow;
  }
});

test("subscribeObsidianVault notifies on successful save and on clear only", () => {
  const store = memoryStorage();
  let count = 0;
  const unsubscribe = subscribeObsidianVault(() => {
    count += 1;
  });
  saveObsidianVault("V", store); // notify
  saveObsidianVault("bad/v", store); // rejected -> no notify
  clearObsidianVault(store); // notify
  unsubscribe();
  saveObsidianVault("W", store); // after unsubscribe -> no notify
  assert.equal(count, 2);
});
