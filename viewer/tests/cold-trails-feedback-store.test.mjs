import assert from "node:assert/strict";
import test from "node:test";
import { webcrypto } from "node:crypto";

import { VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import {
  clearDismissedPairDigests,
  filterDismissedPairsByStoredDigests,
  graphFeedbackFingerprint,
  hashDismissedPairKey,
  loadDismissedPairDigests,
  saveDismissedPairDigests,
} from "../app/cold-trails-feedback-store.ts";

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", {
    value: webcrypto,
    configurable: true,
  });
}

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
    entries: () => [...map.entries()],
  };
}

function demoGraph(exportId = "export-privacy-demo") {
  return {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [
      {
        id: "Health/Therapy Notes.md",
        title: "Therapy Notes",
        path: "Health/Therapy Notes.md",
        content_hash: "a".repeat(64),
      },
      {
        id: "Legal/Divorce Strategy.md",
        title: "Divorce Strategy",
        path: "Legal/Divorce Strategy.md",
        content_hash: "b".repeat(64),
      },
    ],
    edges: [
      {
        id: "c1",
        source: "Health/Therapy Notes.md",
        target: "Legal/Divorce Strategy.md",
        verified: false,
      },
    ],
    export_history: {
      export_id: exportId,
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 2,
      nodes_removed: 0,
      claim_conflict: false,
    },
  };
}

test("persisted dismissals never store vault-relative paths", async () => {
  const storage = memoryStorage();
  const graph = demoGraph();
  const pairKey = "Health/Therapy Notes.md|Legal/Divorce Strategy.md";
  const fingerprint = await graphFeedbackFingerprint(graph);
  const digest = await hashDismissedPairKey(pairKey);
  await saveDismissedPairDigests(fingerprint, [digest], storage);

  const serialized = JSON.stringify(storage.entries());
  assert.doesNotMatch(serialized, /Therapy Notes/);
  assert.doesNotMatch(serialized, /Divorce Strategy/);
  assert.doesNotMatch(serialized, /Health\//);
  assert.doesNotMatch(serialized, /Legal\//);
  assert.doesNotMatch(serialized, /export-privacy-demo/);
  assert.match(fingerprint, /^export:[0-9a-f]{64}$/);
  assert.match(digest, /^[0-9a-f]{64}$/);

  const restored = await filterDismissedPairsByStoredDigests(
    [pairKey, "other|pair"],
    await loadDismissedPairDigests(fingerprint, storage),
  );
  assert.deepEqual([...restored], [pairKey]);

  await clearDismissedPairDigests(fingerprint, storage);
  assert.deepEqual(await loadDismissedPairDigests(fingerprint, storage), []);
});

test("hostile export_id is hashed in the storage key", async () => {
  const storage = memoryStorage();
  const hostile = demoGraph("../../etc/passwd<script>");
  const fingerprint = await graphFeedbackFingerprint(hostile);
  assert.doesNotMatch(fingerprint, /\.\./);
  assert.doesNotMatch(fingerprint, /passwd/);
  assert.doesNotMatch(fingerprint, /script/);
  const digest = await hashDismissedPairKey("a.md|b.md");
  await saveDismissedPairDigests(fingerprint, [digest], storage);
  const serialized = JSON.stringify(storage.entries());
  assert.doesNotMatch(serialized, /passwd/);
  assert.doesNotMatch(serialized, /script/);
});

test("dismiss digests do not bleed across unrelated export fingerprints", async () => {
  const storage = memoryStorage();
  const first = await graphFeedbackFingerprint(demoGraph("export-one"));
  const second = await graphFeedbackFingerprint(demoGraph("export-two"));
  assert.notEqual(first, second);
  const digest = await hashDismissedPairKey("a.md|b.md");
  await saveDismissedPairDigests(first, [digest], storage);
  assert.deepEqual(await loadDismissedPairDigests(second, storage), []);
  assert.deepEqual(await loadDismissedPairDigests(first, storage), [digest]);
});
