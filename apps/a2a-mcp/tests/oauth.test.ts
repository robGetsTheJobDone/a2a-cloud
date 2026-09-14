import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { loadCredentials, saveCredentials } from "../src/credentials.js";
import {
  buildAuthorizationUrl,
  createPkcePair,
  refreshCredentialsIfNeeded,
} from "../src/oauth.js";

let originalHome: string | undefined;
let tmpHome: string;
const originalFetch = globalThis.fetch;

beforeEach(async () => {
  originalHome = process.env.HOME;
  tmpHome = await fs.mkdtemp(path.join(os.tmpdir(), "a2a-mcp-oauth-"));
  process.env.HOME = tmpHome;
  globalThis.fetch = originalFetch;
});

afterEach(async () => {
  if (originalHome !== undefined) process.env.HOME = originalHome;
  else delete process.env.HOME;
  globalThis.fetch = originalFetch;
  await fs.rm(tmpHome, { recursive: true, force: true });
});

test("PKCE authorization URL requests Keycloak code flow", () => {
  const pkce = createPkcePair();
  assert.ok(pkce.verifier.length > 40);
  assert.ok(pkce.challenge.length > 40);

  const url = new URL(
    buildAuthorizationUrl({
      authorizationEndpoint: "https://auth.example.test/auth",
      clientId: "a2acloud-cli",
      redirectUri: "http://127.0.0.1:41873/callback",
      scope: "openid email mcp:invoke",
      state: "state-123",
      codeChallenge: pkce.challenge,
    }),
  );

  assert.equal(url.searchParams.get("client_id"), "a2acloud-cli");
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("scope"), "openid email mcp:invoke");
});

test("refreshCredentialsIfNeeded refreshes expired Keycloak access token", async () => {
  const calls: Array<{ url: string; body?: string }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body?.toString() });
    if (url.endsWith("/.well-known/openid-configuration")) {
      return new Response(
        JSON.stringify({
          authorization_endpoint: "https://auth.example.test/auth",
          token_endpoint: "https://auth.example.test/token",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        access_token: "fresh-token",
        refresh_token: "fresh-refresh",
        expires_in: 3600,
        scope: "openid email mcp:invoke",
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }) as typeof fetch;

  await saveCredentials({
    apiUrl: "https://api.example.test",
    token: "old-token",
    refreshToken: "refresh-token",
    expiresAt: 1,
    email: "dev@example.test",
    issuer: "https://auth.example.test",
    clientId: "a2acloud-cli",
  });

  const refreshed = await refreshCredentialsIfNeeded((await loadCredentials())!);

  assert.equal(refreshed.token, "fresh-token");
  assert.equal(refreshed.refreshToken, "fresh-refresh");
  assert.equal((await loadCredentials())!.token, "fresh-token");
  assert.ok(calls.some((call) => call.body?.includes("grant_type=refresh_token")));
});

test("refreshCredentialsIfNeeded serializes concurrent refreshes", async () => {
  let tokenCalls = 0;
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/.well-known/openid-configuration")) {
      return new Response(
        JSON.stringify({
          authorization_endpoint: "https://auth.example.test/auth",
          token_endpoint: "https://auth.example.test/token",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    tokenCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 25));
    return new Response(
      JSON.stringify({
        access_token: `fresh-token-${tokenCalls}`,
        refresh_token: `fresh-refresh-${tokenCalls}`,
        expires_in: 3600,
        scope: "openid email mcp:invoke",
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }) as typeof fetch;

  await saveCredentials({
    apiUrl: "https://api.example.test",
    token: "old-token",
    refreshToken: "refresh-token",
    expiresAt: 1,
    email: "dev@example.test",
    issuer: "https://auth.example.test",
    clientId: "a2acloud-cli",
  });

  const stale = (await loadCredentials())!;
  const [first, second] = await Promise.all([
    refreshCredentialsIfNeeded(stale),
    refreshCredentialsIfNeeded({ ...stale }),
  ]);

  assert.equal(tokenCalls, 1);
  assert.equal(first.token, "fresh-token-1");
  assert.equal(second.token, "fresh-token-1");
  assert.equal((await loadCredentials())!.refreshToken, "fresh-refresh-1");
});

test("refreshCredentialsIfNeeded reports stale Keycloak sessions clearly", async () => {
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/.well-known/openid-configuration")) {
      return new Response(
        JSON.stringify({
          authorization_endpoint: "https://auth.example.test/auth",
          token_endpoint: "https://auth.example.test/token",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        error: "invalid_grant",
        error_description: "Session doesn't have required client",
      }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }) as typeof fetch;

  await saveCredentials({
    apiUrl: "https://api.example.test",
    token: "old-token",
    refreshToken: "refresh-token",
    expiresAt: 1,
    email: "dev@example.test",
    issuer: "https://auth.example.test",
    clientId: "a2acloud-cli",
  });

  await assert.rejects(
    refreshCredentialsIfNeeded((await loadCredentials())!),
    /Login expired or revoked\. Run `a2amcp login` and retry\. .*Session doesn't have required client/,
  );
  assert.equal((await loadCredentials())!.token, "old-token");
});
