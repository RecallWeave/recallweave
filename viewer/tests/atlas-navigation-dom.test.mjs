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
  // happy-dom exposes navigator.clipboard as a getter-only stub without
  // writeText, so copyToClipboard falls back to execCommand — make that path
  // deterministic AND capture the exact text it would place on the clipboard
  // (the transient <textarea> the fallback selects), so tests can assert the
  // copied value, not just the status line.
  window.__copiedText = [];
  window.document.execCommand = (command) => {
    if (command === "copy") {
      const textarea = window.document.querySelector("textarea");
      window.__copiedText.push(textarea ? textarea.value : null);
    }
    return true;
  };
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

// Render, select the node, and hand back the live drawer container plus a
// clicker so a test can inspect the sanitized-path caveat and the copy status
// that a Copy path click produces. The caller runs entirely inside the Vite
// server lifetime and unmounts before returning.
async function withSelectedDrawer(window, node, run) {
  const container = window.document.createElement("div");
  window.document.body.appendChild(container);
  await withGraphExplorer(async (GraphExplorer) => {
    const initialGraph = graphWith([node]);
    const root = createRoot(container);
    await act(async () => {
      root.render(createElement(GraphExplorer, { initialGraph }));
    });
    await flush();
    await selectNodeByTitle(window, container, node.title);
    const clickCopyPath = async () => {
      const button = [...container.querySelectorAll(".node-actions button")].find(
        (candidate) => candidate.textContent === "Copy path",
      );
      assert.ok(button, "Copy path button must exist");
      await act(async () => {
        button.dispatchEvent(new window.Event("click", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      await flush();
    };
    await run({ container, clickCopyPath });
    await act(async () => {
      root.unmount();
    });
  });
}

test("import-sanitized note shows the caveat and keeps Copy path", async () => {
  // A zero-width character in the raw path is stripped by import sanitization,
  // so node.path no longer equals the exported path (path_exact === false).
  // Copy path must remain (founder floor) and an inline caveat must appear.
  const window = installDom(memoryStorage());
  await withSelectedDrawer(
    window,
    {
      id: "zwsp",
      title: "ZeroWidthNote",
      path: "notes/plan" + String.fromCharCode(0x200b) + ".md",
      content_hash: "c".repeat(64),
    },
    async ({ container, clickCopyPath }) => {
      const caveat = container.querySelector(".node-path-sanitized");
      assert.ok(caveat, "sanitized-path caveat must be present for a non-exact path");
      assert.match(caveat.textContent, /adjusted on import/i);
      const copyButton = [...container.querySelectorAll(".node-actions button")].find(
        (candidate) => candidate.textContent === "Copy path",
      );
      assert.ok(copyButton, "Copy path must stay available for a non-exact note");
      await clickCopyPath();
      const status = container.querySelector(".detail-panel .copy-status");
      assert.ok(status, "a copy status must render after clicking Copy path");
      assert.match(status.textContent, /adjusted on import/i);
      // The copied value must be the sanitized stored path (zero-width stripped),
      // and it must match what the drawer displays — not the raw input, title,
      // or id. This is the mutation guard: passing the wrong value here fails.
      const copied = window.__copiedText.at(-1);
      assert.equal(copied, "notes/plan.md", "Copy path must copy the sanitized path");
      assert.ok(
        !copied.includes(String.fromCharCode(0x200b)),
        "the raw zero-width path must never be copied",
      );
      assert.equal(
        copied,
        container.querySelector(".node-path").textContent,
        "the copied value must equal the displayed sanitized path",
      );
    },
  );
  window.close();
});

test("exact note shows no caveat and the plain copy confirmation", async () => {
  const window = installDom(memoryStorage());
  await withSelectedDrawer(
    window,
    {
      id: "alpha",
      title: "AlphaNote",
      path: "notes/alpha.md",
      content_hash: "a".repeat(64),
    },
    async ({ container, clickCopyPath }) => {
      assert.equal(
        container.querySelector(".node-path-sanitized"),
        null,
        "an exact path must not show the sanitized caveat",
      );
      await clickCopyPath();
      const status = container.querySelector(".detail-panel .copy-status");
      assert.ok(status, "a copy status must render after clicking Copy path");
      assert.equal(status.textContent, "Path copied.");
      // Mutation guard for the exact path: the copied value is the path itself.
      assert.equal(window.__copiedText.at(-1), "notes/alpha.md");
    },
  );
  window.close();
});

test("Copy path copies the sanitized value via the Clipboard API too", async () => {
  // Exercise the primary navigator.clipboard.writeText branch (the execCommand
  // fallback is covered above): the value handed to the clipboard must be the
  // sanitized path, and the adjusted status must still render.
  const window = installDom(memoryStorage());
  const written = [];
  // The component reads the bare global `navigator`, so inject there (not on
  // window.navigator) and restore afterward so the fallback-path tests still see
  // no Clipboard API.
  const hadClipboard = Object.prototype.hasOwnProperty.call(globalThis.navigator, "clipboard");
  const priorClipboard = Object.getOwnPropertyDescriptor(globalThis.navigator, "clipboard");
  Object.defineProperty(globalThis.navigator, "clipboard", {
    configurable: true,
    value: { writeText: (text) => { written.push(text); return Promise.resolve(); } },
  });
  try {
    await withSelectedDrawer(
      window,
      {
        id: "zwsp",
        title: "ZeroWidthNote",
        path: "notes/plan" + String.fromCharCode(0x200b) + ".md",
        content_hash: "c".repeat(64),
      },
      async ({ container, clickCopyPath }) => {
        await clickCopyPath();
        assert.deepEqual(written, ["notes/plan.md"], "Clipboard API must receive the sanitized path");
        const status = container.querySelector(".detail-panel .copy-status");
        assert.match(status.textContent, /adjusted on import/i);
      },
    );
  } finally {
    if (hadClipboard && priorClipboard) {
      Object.defineProperty(globalThis.navigator, "clipboard", priorClipboard);
    } else {
      delete globalThis.navigator.clipboard;
    }
  }
  window.close();
});

test("a control-character path is also flagged and copied sanitized", async () => {
  // path_exact can become false for reasons other than zero-width characters;
  // a C0 control character is stripped by import sanitization too. The caveat
  // and the sanitized copied value must hold for that cause as well.
  const window = installDom(memoryStorage());
  await withSelectedDrawer(
    window,
    {
      id: "ctrl",
      title: "ControlCharNote",
      path: "notes/pl" + String.fromCharCode(0x07) + "an.md",
      content_hash: "d".repeat(64),
    },
    async ({ container, clickCopyPath }) => {
      assert.ok(
        container.querySelector(".node-path-sanitized"),
        "a control-character path must show the sanitized caveat",
      );
      await clickCopyPath();
      const copied = window.__copiedText.at(-1);
      assert.equal(copied, "notes/plan.md", "the control character must be stripped from the copied path");
      const status = container.querySelector(".detail-panel .copy-status");
      assert.match(status.textContent, /adjusted on import/i);
    },
  );
  window.close();
});

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

test("a cross-tab storage change clears stale footer config feedback", async () => {
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
    // Produce a footer status message (empty Save reliably sets one).
    const save = [...container.querySelectorAll(".obsidian-config-row button")].find(
      (button) => button.textContent === "Save",
    );
    await act(async () => {
      save.dispatchEvent(new window.Event("click", { bubbles: true }));
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await flush();
    assert.ok(
      container.querySelector(".obsidian-config .obsidian-config-status"),
      "a footer status should be present before the cross-tab event",
    );
    // Another same-origin tab changes the shared setting.
    await act(async () => {
      let event;
      try {
        event = new window.StorageEvent("storage", {
          key: ATLAS_OBSIDIAN_VAULT_STORAGE_KEY,
        });
      } catch {
        event = new window.Event("storage");
        Object.defineProperty(event, "key", {
          value: ATLAS_OBSIDIAN_VAULT_STORAGE_KEY,
        });
      }
      window.dispatchEvent(event);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    await flush();
    assert.equal(
      container.querySelector(".obsidian-config .obsidian-config-status"),
      null,
      "stale footer feedback must be cleared on a cross-tab storage change",
    );
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

test("configured viewer hides Open in Obsidian when import sanitized the path", async () => {
  // A path carrying a zero-width character is stripped by import sanitization,
  // so node.path no longer equals the exported path (path_exact === false). Open
  // in Obsidian must be withheld rather than silently opening a different note.
  const window = installDom(
    memoryStorage({ [ATLAS_OBSIDIAN_VAULT_STORAGE_KEY]: "My Vault" }),
  );
  const html = await renderAndSelect(window, "My Vault", {
    id: "zwsp",
    title: "ZeroWidthNote",
    path: "notes/plan" + String.fromCharCode(0x200b) + ".md",
    content_hash: "c".repeat(64),
  });
  assert.match(html, /Copy path/);
  assert.doesNotMatch(html, /Open in Obsidian/);
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
