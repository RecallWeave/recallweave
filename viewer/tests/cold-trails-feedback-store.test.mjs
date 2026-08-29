import assert from "node:assert/strict";
import test from "node:test";

import { VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import {
  clearDismissedPairKeys,
  graphFeedbackFingerprint,
  loadDismissedPairKeys,
  saveDismissedPairKeys,
} from "../app/cold-trails-feedback-store.ts";

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
  };
}

test("graphFeedbackFingerprint prefers export_id when present", () => {
  const withExport = {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "export-alpha",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
      claim_conflict: false,
    },
  };
  assert.equal(graphFeedbackFingerprint(withExport), "export:export-alpha");
});

test("dismissed pair keys persist and clear under a fingerprint", () => {
  const storage = memoryStorage();
  const fingerprint = "export:test";
  saveDismissedPairKeys(fingerprint, ["b.md|a.md", "b.md|a.md", "node:x"], storage);
  assert.deepEqual(loadDismissedPairKeys(fingerprint, storage), ["b.md|a.md", "node:x"]);
  clearDismissedPairKeys(fingerprint, storage);
  assert.deepEqual(loadDismissedPairKeys(fingerprint, storage), []);
});
