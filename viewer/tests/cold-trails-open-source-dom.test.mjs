import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { Window } from "happy-dom";
import { createServer } from "vite";

import { VIEWER_SCHEMA_V2, normalizeGraph } from "../app/graph-data.ts";

function installDom() {
  const window = new Window({ url: "https://atlas.test/" });
  const { document } = window;
  const map = new Map();
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
  globalThis.localStorage = {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
  };
  window.HTMLCanvasElement.prototype.getContext = () => null;
  // Make the clipboard fallback deterministic so a copy produces a status.
  window.document.execCommand = () => true;
  return window;
}

async function withModule(exportName, run) {
  const reactPlugin = (await import("@vitejs/plugin-react")).default;
  const server = await createServer({
    root: fileURLToPath(new URL("..", import.meta.url)),
    server: { middlewareMode: true },
    appType: "custom",
    configFile: false,
    plugins: [reactPlugin()],
  });
  try {
    const componentPath =
      exportName === "GraphExplorer"
        ? "/app/components/GraphExplorer.tsx"
        : "/app/components/ColdTrailsTour.tsx";
    const mod = await server.ssrLoadModule(componentPath);
    return await run(mod[exportName]);
  } finally {
    await server.close();
  }
}

async function withColdTrailsTour(run) {
  return withModule("ColdTrailsTour", run);
}

async function flush(window) {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

async function click(window, button) {
  await act(async () => {
    button.dispatchEvent(new window.Event("click", { bubbles: true }));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
  await flush(window);
}

function buttonByText(container, text) {
  return [...container.querySelectorAll("button")].find(
    (candidate) => candidate.textContent === text,
  );
}

// The island fixture reliably yields an island trail whose nodeId is "leaf.md"
// (mirrors the pure buildColdTrails test above). Leaf's path carries a
// zero-width character, so import sanitization strips it: path_exact === false
// and the stored path becomes notes/leaf.md.
function islandGraph() {
  return normalizeGraph({
    schema_version: VIEWER_SCHEMA_V2,
    generated_at: "2026-08-28T00:00:00Z",
    nodes: [
      { id: "hub.md", title: "Hub", path: "hub.md", domain: "Core" },
      { id: "leaf.md", title: "Leaf", path: "notes/leaf" + String.fromCharCode(0x200b) + ".md", domain: "Edge" },
      { id: "n2.md", title: "Two", path: "n2.md", domain: "Core" },
      { id: "n3.md", title: "Three", path: "n3.md", domain: "Core" },
      { id: "n4.md", title: "Four", path: "n4.md", domain: "Core" },
      { id: "n5.md", title: "Five", path: "n5.md", domain: "Core" },
      { id: "n6.md", title: "Six", path: "n6.md", domain: "Core" },
      { id: "n7.md", title: "Seven", path: "n7.md", domain: "Core" },
    ],
    edges: [
      { id: "auth", source: "hub.md", target: "n2.md", verified: true },
      {
        id: "c1", source: "leaf.md", target: "n3.md", verified: false,
        evidence: {
          source_evidence: { citation: "notes/leaf.md:10-12" },
          target_evidence: { citation: "n3.md:20" },
          signals: { lexical_terms: ["orbit", "signal", "delta", "phase"], shared_tags: ["watchlist"] },
        },
      },
      {
        id: "c2", source: "leaf.md", target: "n4.md", verified: false,
        evidence: {
          source_evidence: { citation: "notes/leaf.md:14-16" },
          target_evidence: { citation: "n4.md:20" },
          signals: { lexical_terms: ["orbit", "signal", "theta", "phase"], shared_tags: ["watchlist"] },
        },
      },
      {
        id: "c3", source: "n5.md", target: "n6.md", verified: false,
        evidence: {
          source_evidence: { citation: "n5.md:10-12" },
          target_evidence: { citation: "n6.md:20" },
          signals: { lexical_terms: ["vector", "signal", "theta", "phase"], shared_tags: ["watchlist"] },
        },
      },
    ],
    privacy: { export_profile: "graph_metadata" },
    import_diagnostics: { duplicate_nodes_dropped: 0, duplicate_edges_dropped: 0, dangling_edges_dropped: 0 },
  });
}

test("Cold Trails Open source flags an import-adjusted node path end-to-end", async () => {
  const window = installDom();
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  const copyCalls = [];
  await withColdTrailsTour(async (ColdTrailsTour) => {
    const root = createRoot(container);
    await act(async () => {
      root.render(
        createElement(ColdTrailsTour, {
          graph: islandGraph(),
          open: true,
          onClose: () => {},
          onShowOnMap: () => {},
          onCopyPath: (path, pathExact) => copyCalls.push({ path, pathExact }),
          onCopyCitation: () => {},
          onStatus: () => {},
        }),
      );
    });
    // Let the tour's mount effects (feedback load + trail build) settle.
    for (let i = 0; i < 5; i += 1) await flush(window);

    // Walk the tour to the leaf island trail, then copy its source path. The
    // leaf island is one of the trails buildColdTrails produces for this graph.
    let found = false;
    for (let i = 0; i < 12 && !found; i += 1) {
      const endpoint = container.querySelector(".cold-trails-endpoint-title");
      if (endpoint && endpoint.textContent === "Leaf") {
        await click(window, buttonByText(container, "Open source"));
        found = true;
        break;
      }
      const next = buttonByText(container, "Next");
      if (!next || next.disabled) break;
      await click(window, next);
    }
    assert.ok(found, "the leaf island trail must be reachable in the tour");

    await act(async () => { root.unmount(); });
  });

  // The wiring must pass BOTH the sanitized path and its exactness. Reverting
  // openSource() to onCopyPath(path) would drop pathExact and fail this.
  assert.equal(copyCalls.length, 1, "Open source must invoke the copy handler once");
  assert.deepEqual(copyCalls[0], { path: "notes/leaf.md", pathExact: false });
  window.close();
});

test("the tour renders the host copy confirmation inside its own dialog", async () => {
  // The confirmation must be visible while the modal is open, not only in the
  // note drawer behind it. Passing statusMessage must render it in the dialog.
  const window = installDom();
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  await withColdTrailsTour(async (ColdTrailsTour) => {
    const root = createRoot(container);
    await act(async () => {
      root.render(
        createElement(ColdTrailsTour, {
          graph: islandGraph(),
          open: true,
          onClose: () => {},
          onShowOnMap: () => {},
          onCopyPath: () => {},
          onCopyCitation: () => {},
          onStatus: () => {},
          statusMessage: "Path copied (adjusted on import — may not match your note exactly).",
        }),
      );
    });
    for (let i = 0; i < 5; i += 1) await flush(window);
    const status = container.querySelector(".cold-trails-dialog .cold-trails-status");
    assert.ok(status, "the dialog must render the host status message");
    assert.match(status.textContent, /adjusted on import/i);
    await act(async () => { root.unmount(); });
  });
  window.close();
});

test("Open source surfaces the adjusted-path warning in the tour with no note selected", async () => {
  // End-to-end through GraphExplorer: open Cold Trails without selecting a note,
  // reach the sanitized leaf island, click Open source, and assert the adjusted
  // warning is visible inside the dialog (not only in the hidden note drawer).
  const window = installDom();
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  await withModule("GraphExplorer", async (GraphExplorer) => {
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph: islandGraph() }));
    });
    for (let i = 0; i < 5; i += 1) await flush(window);

    // No note selected: the drawer status line is absent.
    assert.equal(container.querySelector(".detail-panel .copy-status"), null);

    await click(window, buttonByText(container, "Cold Trails"));
    for (let i = 0; i < 5; i += 1) await flush(window);

    let found = false;
    for (let i = 0; i < 12 && !found; i += 1) {
      const endpoint = container.querySelector(".cold-trails-endpoint-title");
      if (endpoint && endpoint.textContent === "Leaf") {
        await click(window, buttonByText(container, "Open source"));
        found = true;
        break;
      }
      const next = buttonByText(container, "Next");
      if (!next || next.disabled) break;
      await click(window, next);
    }
    assert.ok(found, "the leaf island trail must be reachable in the tour");

    const status = container.querySelector(".cold-trails-dialog .cold-trails-status");
    assert.ok(status, "the adjusted-path confirmation must be visible inside the tour dialog");
    assert.match(status.textContent, /adjusted on import/i);

    await act(async () => { root.unmount(); });
  });
  window.close();
});
