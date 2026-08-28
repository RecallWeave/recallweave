import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import { graphFromLoadedFileText } from "../app/graph-load.ts";

async function withViteGraphExplorer(run) {
  const reactPlugin = (await import("@vitejs/plugin-react")).default;
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    server: { middlewareMode: true },
    appType: "custom",
    configFile: false,
    plugins: [reactPlugin()],
  });
  try {
    const { GraphExplorer } = await server.ssrLoadModule(
      "/app/components/GraphExplorer.tsx",
    );
    return await run(GraphExplorer);
  } finally {
    await server.close();
  }
}

test("GraphExplorer renders provenance chrome after file-load parse", async () => {
  const fileText = JSON.stringify({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "file-load-export",
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  });
  const loaded = graphFromLoadedFileText(fileText);

  await withViteGraphExplorer((GraphExplorer) => {
    const html = renderToStaticMarkup(
      createElement(GraphExplorer, { initialGraph: loaded }),
    );
    assert.match(html, /app-shell/);
    assert.match(html, /export-privacy/);
    assert.match(html, /privacy-provenance-detail/);
    assert.match(html, /file-load-export/);
    assert.match(html, /first export claim/);
    assert.doesNotMatch(html, /export history conflicts with loaded graph/);
  });
});

test("GraphExplorer file-load path surfaces conflicted export history", async () => {
  const fileText = JSON.stringify({
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: "file-load-conflict",
      previous_content_hash: "b".repeat(64),
      node_content_hashes_changed: 9,
      node_content_hashes_unchanged: 0,
      nodes_added: 0,
      nodes_removed: 0,
    },
  });
  const loaded = graphFromLoadedFileText(fileText);
  assert.equal(loaded.export_history?.claim_conflict, true);

  await withViteGraphExplorer((GraphExplorer) => {
    const html = renderToStaticMarkup(
      createElement(GraphExplorer, { initialGraph: loaded }),
    );
    assert.match(html, /file-load-conflict/);
    assert.match(html, /export history conflicts with loaded graph/);
    assert.match(html, /privacy-provenance-detail/);
  });
});

test("assertGraphFileWithinLimit rejects oversized uploads", async () => {
  const { assertGraphFileWithinLimit } = await import("../app/graph-load.ts");
  const { MAX_FILE_BYTES } = await import("../app/graph-data.ts");
  assert.doesNotThrow(() => assertGraphFileWithinLimit(MAX_FILE_BYTES));
  assert.throws(
    () => assertGraphFileWithinLimit(MAX_FILE_BYTES + 1),
    /MB viewer limit/,
  );
});
