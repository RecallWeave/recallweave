/** Cloudflare Worker entry point for the RecallWeave Atlas shell. */
import handler from "vinext/server/app-router-entry";
import { inlineScriptBodies } from "./html-scripts";

interface AssetBinding {
  fetch(request: Request): Promise<Response>;
}

interface Env {
  ASSETS: AssetBinding;
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

const SECURITY_HEADERS = {
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
  "Permissions-Policy":
    "camera=(), clipboard-write=(self), geolocation=(), microphone=()",
  "Referrer-Policy": "no-referrer",
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
} as const;

function base64(buffer: ArrayBuffer): string {
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function inlineScriptHashes(html: string): Promise<string[]> {
  const hashes = new Set<string>();
  for (const body of inlineScriptBodies(html)) {
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(body),
    );
    hashes.add(`'sha256-${base64(digest)}'`);
  }
  return [...hashes];
}

async function withSecurityHeaders(response: Response): Promise<Response> {
  const headers = new Headers(response.headers);
  let body: BodyInit | null = response.body;
  let scriptSources = "'self'";
  if ((headers.get("content-type") ?? "").toLowerCase().startsWith("text/html")) {
    const html = await response.text();
    const hashes = await inlineScriptHashes(html);
    if (hashes.length) scriptSources += ` ${hashes.join(" ")}`;
    body = html;
    headers.delete("content-encoding");
    headers.delete("content-length");
  }
  headers.set(
    "Content-Security-Policy",
    `default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; form-action 'none'; frame-ancestors 'none'; img-src 'self' data: blob:; object-src 'none'; script-src ${scriptSources}; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:`,
  );
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    headers.set(name, value);
  }
  return new Response(body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const response = await handler.fetch(request, env, ctx);
    return await withSecurityHeaders(response);
  },
};

export default worker;
