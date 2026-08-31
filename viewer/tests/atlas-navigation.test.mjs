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
  // Valid labels normalize: whitespace collapsed, trimmed, length-capped.
  assert.equal(normalizeObsidianVaultLabel("My Vault"), "My Vault");
  assert.equal(normalizeObsidianVaultLabel("  Spaced   Out  "), "Spaced Out");
  assert.equal(normalizeObsidianVaultLabel("with\nnewline"), "with newline");
  assert.equal(normalizeObsidianVaultLabel("a\tb"), "a b");
  assert.equal(normalizeObsidianVaultLabel("x".repeat(200)).length, 120);
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
  // Valid input normalizes and persists.
  assert.equal(saveObsidianVault("  My Vault  ", store), "My Vault");
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
  assert.doesNotThrow(() => clearObsidianVault(throwing));
  assert.equal(loadObsidianVault(null), null);
  assert.equal(saveObsidianVault("V", null), null);
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
