import assert from "node:assert/strict";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import { Window } from "happy-dom";
import { createServer } from "vite";

import { VIEWER_SCHEMA_V2 } from "../app/graph-data.ts";
import { graphFromLoadedFileText } from "../app/graph-load.ts";
import { ATLAS_OBSIDIAN_VAULT_STORAGE_KEY } from "../app/atlas-navigation.ts";

function graphWith(nodes) {
  return graphFromLoadedFileText(
    JSON.stringify({
      schema_version: VIEWER_SCHEMA_V2,
      nodes,
      edges: [],
      export_history: {
        export_id: "nav-demo",
        previous_content_hash: null,
        node_content_hashes_changed: 0,
        node_content_hashes_unchanged: 0,
        nodes_added: nodes.length,
        nodes_removed: 0,
      },
    }),
  );
}

function memoryStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
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

function installDom(localStorage) {
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
  globalThis.localStorage = localStorage;
  window.HTMLCanvasElement.prototype.getContext = () => null;
  return window;
}

async function withGraphExplorer(run) {
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

async function selectNodeByTitle(window, container, title) {
  const buttons = [...container.querySelectorAll(".node-browser-list button")];
  const target = buttons.find(
    (button) => button.textContent && button.textContent.includes(title),
  );
  assert.ok(target, `node button for ${title} must exist`);
  await act(async () => {
    target.dispatchEvent(new window.Event("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  await flush();
}

async function renderAndSelect(window, localStorageState, node) {
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  let capturedHtml = "";
  await withGraphExplorer(async (GraphExplorer) => {
    const initialGraph = graphWith([node]);
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph }));
    });
    await flush();
    await selectNodeByTitle(window, container, node.title);
    // Scope to the note's action row so descriptive copy elsewhere (e.g. the
    // footer config status) cannot be mistaken for the actual affordance.
    const actions = container.querySelector(".node-actions");
    capturedHtml = actions ? actions.innerHTML : "";
    await act(async () => {
      root.unmount();
    });
  });
  return capturedHtml;
}

test("vault-config feedback renders in the footer form with no note selected", async () => {
  // Regression for the bug where config feedback was written only to the
  // selected-note drawer's status line: with no note selected, a failed Save
  // must still surface feedback in the footer form itself.
  const window = installDom(memoryStorage());
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  await withGraphExplorer(async (GraphExplorer) => {
    const initialGraph = graphWith([
      { id: "alpha", title: "AlphaNote", path: "notes/alpha.md", content_hash: "a".repeat(64) },
    ]);
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph }));
    });
    await flush();
    // No note is selected, so the note drawer's status line is not rendered.
    assert.equal(container.querySelector(".detail-panel .copy-status"), null);
    const save = [...container.querySelectorAll(".obsidian-config-row button")].find(
      (button) => button.textContent === "Save",
    );
    assert.ok(save, "Save button must be present");
    await act(async () => {
      save.dispatchEvent(new window.Event("click", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await flush();
    const status = container.querySelector(".obsidian-config .obsidian-config-status");
    assert.ok(status, "footer config status must render feedback without a selection");
    assert.match(status.textContent, /vault name/i);
    await act(async () => {
      root.unmount();
    });
  });
  window.close();
});

test("unconfigured viewer exposes Copy path but no Open in Obsidian", async () => {
  const window = installDom(memoryStorage());
  const html = await renderAndSelect(window, null, {
    id: "alpha",
    title: "AlphaNote",
    path: "notes/alpha.md",
    content_hash: "a".repeat(64),
  });
  assert.match(html, /Copy path/);
  assert.doesNotMatch(html, /Open in Obsidian/);
  window.close();
});

test("configured viewer exposes Open in Obsidian for a relative-path note", async () => {
  const window = installDom(
    memoryStorage({ [ATLAS_OBSIDIAN_VAULT_STORAGE_KEY]: "My Vault" }),
  );
  const html = await renderAndSelect(window, "My Vault", {
    id: "alpha",
    title: "AlphaNote",
    path: "notes/alpha.md",
    content_hash: "a".repeat(64),
  });
  assert.match(html, /Copy path/);
  assert.match(html, /Open in Obsidian/);
  window.close();
});

test("configured viewer still hides Open in Obsidian for a non-relative path", async () => {
  const window = installDom(
    memoryStorage({ [ATLAS_OBSIDIAN_VAULT_STORAGE_KEY]: "My Vault" }),
  );
  const html = await renderAndSelect(window, "My Vault", {
    id: "beta",
    title: "AbsoluteNote",
    path: "/abs/beta.md",
    content_hash: "b".repeat(64),
  });
  assert.match(html, /Copy path/);
  assert.doesNotMatch(html, /Open in Obsidian/);
  window.close();
});
