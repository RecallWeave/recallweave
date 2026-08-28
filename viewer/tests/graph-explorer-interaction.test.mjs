import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { Window } from "happy-dom";
import { createServer } from "vite";

import { MAX_FILE_BYTES, VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import { loadGraphFromFile } from "../app/graph-load.ts";

function provenanceGraph(exportId) {
  return {
    schema_version: VIEWER_SCHEMA_V2,
    nodes: [{ id: "a.md", title: "A", path: "a.md", content_hash: "a".repeat(64) }],
    edges: [],
    export_history: {
      export_id: exportId,
      previous_content_hash: null,
      node_content_hashes_changed: 0,
      node_content_hashes_unchanged: 0,
      nodes_added: 1,
      nodes_removed: 0,
    },
  };
}

function installDom() {
  const window = new Window({ url: "https://atlas.test/" });
  const { document } = window;
  globalThis.window = window;
  globalThis.document = document;
  globalThis.HTMLElement = window.HTMLElement;
  globalThis.HTMLInputElement = window.HTMLInputElement;
  globalThis.Node = window.Node;
  globalThis.Text = window.Text;
  globalThis.DocumentFragment = window.DocumentFragment;
  globalThis.MutationObserver = window.MutationObserver;
  globalThis.getComputedStyle = window.getComputedStyle.bind(window);
  globalThis.requestAnimationFrame = (cb) => window.setTimeout(() => cb(0), 0);
  globalThis.cancelAnimationFrame = (id) => window.clearTimeout(id);
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // Canvas path no-ops under happy-dom; avoid getContext crashes.
  window.HTMLCanvasElement.prototype.getContext = () => null;
  return window;
}

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

async function flush() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

test("loadGraphFromFile rejects oversized and malformed without returning a graph", async () => {
  const oversized = await loadGraphFromFile({
    size: MAX_FILE_BYTES + 1,
    text: async () => {
      throw new Error("should not read oversized file");
    },
  });
  assert.equal(oversized.ok, false);
  if (!oversized.ok) assert.match(oversized.error, /MB viewer limit/);

  const malformed = await loadGraphFromFile({
    size: 12,
    text: async () => "{not-json",
  });
  assert.equal(malformed.ok, false);
  if (!malformed.ok) assert.match(malformed.error, /JSON|Unexpected|position/i);
});

test("file input change loads provenance into GraphExplorer", async () => {
  const window = installDom();
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);

  const prior = provenanceGraph("prior-export");
  const { graphFromLoadedFileText } = await import("../app/graph-load.ts");
  const initialGraph = graphFromLoadedFileText(JSON.stringify(prior));

  await withViteGraphExplorer(async (GraphExplorer) => {
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph }));
    });
    await flush();
    assert.match(container.innerHTML, /prior-export/);

    const input = container.querySelector('input[type="file"]');
    assert.ok(input, "file input must be mounted");

    const nextText = JSON.stringify(provenanceGraph("changed-export"));
    const file = new window.File([nextText], "next.json", {
      type: "application/json",
    });
    Object.defineProperty(input, "files", {
      configurable: true,
      value: {
        0: file,
        length: 1,
        item: (i) => (i === 0 ? file : null),
      },
    });

    await act(async () => {
      input.dispatchEvent(new window.Event("change", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await flush();

    assert.match(container.innerHTML, /changed-export/);
    assert.match(container.innerHTML, /privacy-provenance-detail/);
    assert.doesNotMatch(container.innerHTML, /prior-export/);

    await act(async () => {
      root.unmount();
    });
  });

  window.close();
});

test("rejected file change keeps prior provenance chrome", async () => {
  const window = installDom();
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);

  const { graphFromLoadedFileText } = await import("../app/graph-load.ts");
  const initialGraph = graphFromLoadedFileText(
    JSON.stringify(provenanceGraph("keep-export")),
  );

  await withViteGraphExplorer(async (GraphExplorer) => {
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph }));
    });
    await flush();
    assert.match(container.innerHTML, /keep-export/);

    const input = container.querySelector('input[type="file"]');
    assert.ok(input);

    async function rejectWith(file) {
      Object.defineProperty(input, "files", {
        configurable: true,
        value: {
          0: file,
          length: 1,
          item: (i) => (i === 0 ? file : null),
        },
      });
      await act(async () => {
        input.dispatchEvent(new window.Event("change", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await flush();
      assert.match(container.innerHTML, /keep-export/);
      assert.match(container.innerHTML, /privacy-provenance-detail/);
      assert.match(container.innerHTML, /import-notice/);
    }

    const oversized = new window.File(["x"], "huge.json", {
      type: "application/json",
    });
    Object.defineProperty(oversized, "size", {
      value: MAX_FILE_BYTES + 1,
    });
    await rejectWith(oversized);
    assert.match(container.innerHTML, /MB viewer limit/);

    const malformed = new window.File(["{bad"], "bad.json", {
      type: "application/json",
    });
    await rejectWith(malformed);

    await act(async () => {
      root.unmount();
    });
  });

  window.close();
});
