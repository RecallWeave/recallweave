import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { startProdServer } from "vinext/server/prod-server";

import { inlineScriptBodies } from "../worker/html-scripts.ts";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

async function readRuntimeSources(directory) {
  const sources = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const child = new URL(
      `${entry.name}${entry.isDirectory() ? "/" : ""}`,
      directory,
    );
    if (entry.isDirectory()) {
      sources.push(...await readRuntimeSources(child));
    } else if (/\.(?:ts|tsx|js|mjs)$/u.test(entry.name)) {
      sources.push(await readFile(child, "utf8"));
    }
  }
  return sources;
}

test("server-renders the RecallWeave Atlas shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  assert.equal(response.headers.get("strict-transport-security"), "max-age=63072000; includeSubDomains");
  assert.equal(response.headers.get("cross-origin-opener-policy"), "same-origin");
  assert.equal(response.headers.get("cross-origin-resource-policy"), "same-origin");
  assert.equal(response.headers.get("content-encoding"), null);
  assert.match(response.headers.get("permissions-policy") ?? "", /clipboard-write=\(self\)/);
  const csp = response.headers.get("content-security-policy") ?? "";
  const scriptDirective = csp.split(";").find((directive) => directive.trim().startsWith("script-src"));
  assert.ok(scriptDirective);
  assert.doesNotMatch(scriptDirective, /unsafe-inline/);
  assert.match(scriptDirective, /'sha256-[A-Za-z0-9+/]+=*'/);

  const html = await response.text();
  assert.match(html, /<title>RecallWeave Atlas/);
  assert.match(html, /See the shape of/);
  assert.match(html, /Load your graph/);
  assert.match(html, /Private by design/);
  assert.match(html, /Candidate connections/);
  assert.match(html, /Reset Atlas/);
  assert.match(html, /Knowledge graph explorer/);
  assert.match(html, /Keyboard node navigator/);
  assert.match(html, /Excerpt status not declared/);
  assert.match(html, /Skip to graph explorer/);
  assert.match(
    html,
    /<header\b[\s\S]*<nav\b[\s\S]*<\/nav>[\s\S]*<\/header>[\s\S]*<main\b[\s\S]*<\/main>[\s\S]*<footer\b/,
  );
  assert.doesNotMatch(html, /private-preview|chatgpt\.site|obsidian:\/\/open/i);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton|Starter Project/i);
});

test("inline script parsing respects quoted greater-than characters", () => {
  assert.deepEqual(
    inlineScriptBodies(
      [
        '<script type="application/json" data-x="a>b">{"safe":true}</script>',
        '<script data-src="/still-inline.js">window.inline = true;</script>',
        '<script src = "/external.js">window.external = true;</script>',
        "<SCRIPT SRC=/external-two.js>window.externalTwo = true;</SCRIPT>",
        '<script type="module">window.module = true;</script>',
      ].join(""),
    ),
    [
      '{"safe":true}',
      "window.inline = true;",
      "window.module = true;",
    ],
  );
});

test("Cold Trails tour dialog meets accessibility and trust boundaries", async () => {
  const tour = await readFile(
    new URL("../app/components/ColdTrailsTour.tsx", import.meta.url),
    "utf8",
  );
  assert.match(tour, /role="dialog"/);
  assert.match(tour, /aria-modal="true"/);
  assert.match(tour, /aria-live="polite"/);
  assert.match(tour, /trailTrustLabel\(current\.trust\)/);
  assert.match(tour, /label="Source evidence"/);
  assert.match(tour, /label="Target evidence"/);
  assert.match(tour, /Show on map/);
  assert.match(tour, /Clear history/);
  assert.match(tour, /saveDismissedPairDigests/);
  assert.match(tour, /clearDismissedPairDigests/);
  assert.match(tour, /hashDismissedPairKey/);
  assert.match(
    tour,
    /if \(event\.key === "Escape"\) \{\s*event\.preventDefault\(\);\s*endTour\(\);/s,
  );
  assert.doesNotMatch(tour, /obsidian:\/\//);
  assert.doesNotMatch(tour, /obsidianOpenHref/);

  const explorer = await readFile(
    new URL("../app/components/GraphExplorer.tsx", import.meta.url),
    "utf8",
  );
  assert.match(explorer, /Cold Trails/);
  assert.match(explorer, /ColdTrailsTour/);
  // The Obsidian affordance is opt-in and assembled at click time via the
  // navigation module; the component itself must hardcode no actionable URI and
  // pre-render no obsidian href.
  assert.doesNotMatch(explorer, /obsidianOpenHref|obsidian:\/\//);
});

test("client import and keyboard focus guards remain wired", async () => {
  const source = await readFile(
    new URL("../app/components/GraphExplorer.tsx", import.meta.url),
    "utf8",
  );
  assert.ok(
    source.indexOf("loadGraphFromFile") <
      source.indexOf("userLoadedRef.current = true"),
    "a rejected import must not suppress the bundled sample",
  );
  assert.match(source, /loadGraphFromFile/);
  assert.match(source, /sampleAbortRef\.current\?\.abort\(\)/);
  assert.match(source, /tabIndex=\{-1\}/);
  assert.match(source, /role="group" aria-label="Connection filters"/);
  assert.match(source, /aria-live="polite" aria-atomic="true"/);
  assert.match(source, /moveNodeNavigatorFocus/);
  assert.match(source, /document\.execCommand\("copy"\)/);
  assert.match(source, /Copy path/);
  assert.match(source, /AtlasExportPrivacyChrome/);
  assert.match(source, /<AtlasExportPrivacyChrome\b/);
  assert.match(source, /graph=\{graph\}/);
  const privacyChrome = await readFile(
    new URL("../app/components/AtlasExportPrivacyChrome.ts", import.meta.url),
    "utf8",
  );
  assert.match(privacyChrome, /AtlasProvenanceChrome/);
  assert.match(privacyChrome, /source file claims local generation/);
  assert.match(privacyChrome, /Declared profile:.*inspected content:/);
  const provenanceChrome = await readFile(
    new URL("../app/components/AtlasProvenanceChrome.ts", import.meta.url),
    "utf8",
  );
  assert.match(provenanceChrome, /Index claims:/);
  assert.match(provenanceChrome, /privacy-provenance-detail/);
  assert.match(provenanceChrome, /formatAtlasProvenanceClaims/);
  // Opt-in Obsidian navigation is wired through the click-time builder; the
  // component hardcodes no actionable URI and pre-renders no obsidian href.
  assert.doesNotMatch(source, /obsidianOpenHref|obsidian:\/\//);
  assert.match(source, /buildObsidianOpenUri\(/);
  assert.match(source, /resetExplorer\(true\)/);
  assert.match(source, /searchRef\.current\?\.focus\(\)/);
});

test("metadata uses a configurable neutral origin", async () => {
  const source = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.match(source, /NEXT_PUBLIC_RECALLWEAVE_ORIGIN/);
  assert.match(source, /https:\/\/recallweave\.example/);
  assert.doesNotMatch(source, /private-preview|chatgpt\.site/i);
});

test("Obsidian navigation exists only as a click-time builder, never pre-rendered or hardcoded", async () => {
  // Opt-in Obsidian navigation (founder ruling on recallweave-fkd) is permitted,
  // but the obsidian scheme may live ONLY in the sanctioned click-time builder,
  // and no actionable URI or DOM href may be pre-rendered or persisted anywhere.
  const navText = await readFile(
    new URL("../app/atlas-navigation.ts", import.meta.url),
    "utf8",
  );
  const runtimeOthers = [
    ...(await readRuntimeSources(new URL("../app/", import.meta.url))),
    ...(await readRuntimeSources(new URL("../worker/", import.meta.url))),
  ].filter((text) => text !== navText);
  // Only atlas-navigation.ts references the scheme.
  assert.doesNotMatch(
    runtimeOthers.join("\n"),
    /obsidian:\/\//iu,
    "only atlas-navigation.ts may reference the obsidian scheme",
  );
  // That module assembles the URI at call time from percent-encoded parts.
  assert.match(navText, /obsidian:\/\/open\?vault=/);
  assert.match(navText, /encodeURIComponent/);
  // Nowhere — source or built bundle — is the scheme pre-rendered as a DOM href
  // or otherwise persisted into markup as an actionable link.
  const everything = [
    navText,
    ...runtimeOthers,
    ...(await readRuntimeSources(new URL("../dist/client/", import.meta.url))),
  ].join("\n");
  assert.doesNotMatch(everything, /href\s*=\s*["'`{]?\s*obsidian:/iu);
});

test("production server delivers every rendered client asset", async (t) => {
  const { server, port } = await startProdServer({
    port: 0,
    host: "127.0.0.1",
    outDir: fileURLToPath(new URL("../dist", import.meta.url)),
  });
  t.after(
    () =>
      new Promise((resolve, reject) => {
        server.closeAllConnections?.();
        server.close((error) => (error ? reject(error) : resolve()));
      }),
  );

  const baseUrl = `http://127.0.0.1:${port}`;
  const page = await fetch(baseUrl);
  assert.equal(page.status, 200);
  const html = await page.text();
  const assetPaths = [
    ...html.matchAll(
      /(?:src|href)="(\/(?:assets|_next\/static)\/[^"]+)"/gu,
    ),
  ].map((match) => match[1]);
  assert.ok(
    assetPaths.length > 0,
    "rendered shell must reference client assets",
  );

  for (const assetPath of new Set(assetPaths)) {
    const response = await fetch(`${baseUrl}${assetPath}`);
    assert.equal(
      response.status,
      200,
      `${assetPath} must be served by the production server`,
    );
  }
});
