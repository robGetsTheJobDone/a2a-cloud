import crypto from "node:crypto";
import { promises as fs } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import type { AddressInfo } from "node:net";

import { ControlPlaneClient } from "./api.js";
import {
  DEFAULT_API_URL,
  type Credentials,
  loadCredentials,
  saveCredentials,
} from "./credentials.js";

export const DEFAULT_OAUTH_ISSUER = "https://auth.a2acloud.io/realms/a2acloud";
export const DEFAULT_OAUTH_CLIENT_ID = "a2acloud-cli";
export const DEFAULT_OAUTH_SCOPE = "openid email offline_access mcp:invoke agent:read";
export const DEFAULT_REDIRECT_PORT = 41873;
const CALLBACK_PATH = "/callback";
const REFRESH_LOCK_WAIT_MS = 10_000;
const REFRESH_LOCK_STALE_MS = 30_000;
const REFRESH_LOCK_POLL_MS = 100;

type OpenIdConfiguration = {
  authorization_endpoint: string;
  token_endpoint: string;
};

type TokenResponse = {
  access_token?: string;
  refresh_token?: string;
  expires_in?: number;
  scope?: string;
  error?: string;
  error_description?: string;
};

export type BrowserLoginOptions = {
  apiUrl?: string;
  issuer?: string;
  clientId?: string;
  scope?: string;
  port?: number;
  openBrowser?: boolean;
  timeoutMs?: number;
  onAuthorizationUrl?: (url: string) => void;
};

export async function loginWithBrowser(
  opts: BrowserLoginOptions = {},
): Promise<Credentials> {
  const apiUrl = (opts.apiUrl || DEFAULT_API_URL).replace(/\/+$/, "");
  const issuer = (opts.issuer || DEFAULT_OAUTH_ISSUER).replace(/\/+$/, "");
  const clientId = opts.clientId || DEFAULT_OAUTH_CLIENT_ID;
  const scope = opts.scope || DEFAULT_OAUTH_SCOPE;
  const cfg = await discoverOpenIdConfiguration(issuer);
  const pkce = createPkcePair();
  const state = randomUrlSafe(32);
  const callback = await listenForCallback({
    port: opts.port ?? DEFAULT_REDIRECT_PORT,
    state,
    timeoutMs: opts.timeoutMs ?? 180_000,
  });
  try {
    const redirectUri = callback.redirectUri;
    const authorizationUrl = buildAuthorizationUrl({
      authorizationEndpoint: cfg.authorization_endpoint,
      clientId,
      redirectUri,
      scope,
      state,
      codeChallenge: pkce.challenge,
    });
    opts.onAuthorizationUrl?.(authorizationUrl);
    if (opts.openBrowser !== false) openBrowser(authorizationUrl);
    const code = await callback.waitForCode;
    const token = await exchangeAuthorizationCode({
      tokenEndpoint: cfg.token_endpoint,
      clientId,
      redirectUri,
      code,
      codeVerifier: pkce.verifier,
    });
    const creds = await credentialsFromToken({
      apiUrl,
      token,
      issuer,
      clientId,
      scope,
    });
    await saveCredentials(creds);
    return creds;
  } finally {
    await callback.close();
  }
}

export async function loginWithAccessToken(
  token: string,
  opts: {
    apiUrl?: string;
    issuer?: string;
    clientId?: string;
    scope?: string;
  } = {},
): Promise<Credentials> {
  const apiUrl = (opts.apiUrl || DEFAULT_API_URL).replace(/\/+$/, "");
  const creds = await credentialsFromToken({
    apiUrl,
    token: { access_token: token },
    issuer: opts.issuer,
    clientId: opts.clientId,
    scope: opts.scope,
  });
  await saveCredentials(creds);
  return creds;
}

export async function refreshCredentialsIfNeeded(
  creds: Credentials,
  minTtlSeconds = 60,
): Promise<Credentials> {
  if (!creds.refreshToken || !creds.issuer || !creds.clientId) return creds;
  if (credentialsFresh(creds, minTtlSeconds)) return creds;

  return withRefreshLock(async () => {
    const latest = await loadCredentials();
    if (latest && sameOAuthClient(latest, creds)) {
      if (credentialsFresh(latest, minTtlSeconds)) return latest;
      if (latest.refreshToken && latest.issuer && latest.clientId) creds = latest;
    }

    return refreshCredentials(creds);
  });
}

async function refreshCredentials(creds: Credentials): Promise<Credentials> {
  const cfg = await discoverOpenIdConfiguration(creds.issuer!);
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: creds.clientId!,
    refresh_token: creds.refreshToken!,
  });
  const resp = await fetch(cfg.token_endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  let token: TokenResponse;
  try {
    token = await parseTokenResponse(resp);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    throw new Error(
      `Login expired or revoked. Run \`a2amcp login\` and retry. (${detail})`,
    );
  }
  const next: Credentials = {
    ...creds,
    token: token.access_token!,
    refreshToken: token.refresh_token || creds.refreshToken,
    expiresAt: expiresAt(token.expires_in),
    scope: token.scope || creds.scope,
  };
  await saveCredentials(next);
  return next;
}

function credentialsFresh(creds: Credentials, minTtlSeconds: number): boolean {
  return Boolean(creds.expiresAt && creds.expiresAt - nowSeconds() > minTtlSeconds);
}

function sameOAuthClient(a: Credentials, b: Credentials): boolean {
  return (
    a.apiUrl === b.apiUrl &&
    a.issuer === b.issuer &&
    a.clientId === b.clientId
  );
}

async function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  const release = await acquireRefreshLock();
  try {
    return await fn();
  } finally {
    await release();
  }
}

async function acquireRefreshLock(): Promise<() => Promise<void>> {
  const file = refreshLockFile();
  await fs.mkdir(path.dirname(file), { recursive: true });
  const started = Date.now();

  while (true) {
    try {
      const handle = await fs.open(file, "wx", 0o600);
      try {
        await handle.writeFile(
          JSON.stringify({ pid: process.pid, created_at: new Date().toISOString() }),
        );
      } finally {
        await handle.close();
      }
      return async () => {
        try {
          await fs.unlink(file);
        } catch (err: any) {
          if (err?.code !== "ENOENT") throw err;
        }
      };
    } catch (err: any) {
      if (err?.code !== "EEXIST") throw err;
      await removeStaleRefreshLock(file);
      if (Date.now() - started > REFRESH_LOCK_WAIT_MS) {
        throw new Error("timed out waiting for credential refresh lock");
      }
      await sleep(REFRESH_LOCK_POLL_MS);
    }
  }
}

async function removeStaleRefreshLock(file: string): Promise<void> {
  try {
    const stat = await fs.stat(file);
    if (Date.now() - stat.mtimeMs <= REFRESH_LOCK_STALE_MS) return;
    await fs.unlink(file);
  } catch (err: any) {
    if (err?.code !== "ENOENT") throw err;
  }
}

function refreshLockFile(): string {
  return path.join(os.homedir(), ".a2a", "credentials-refresh.lock");
}

export async function discoverOpenIdConfiguration(
  issuer: string,
): Promise<OpenIdConfiguration> {
  const url = `${issuer.replace(/\/+$/, "")}/.well-known/openid-configuration`;
  const resp = await fetch(url, { headers: { accept: "application/json" } });
  if (!resp.ok) throw new Error(`OpenID discovery failed: ${resp.status}`);
  const data = (await resp.json()) as Partial<OpenIdConfiguration>;
  if (!data.authorization_endpoint || !data.token_endpoint) {
    throw new Error("OpenID discovery document is missing authorization/token endpoints");
  }
  return {
    authorization_endpoint: data.authorization_endpoint,
    token_endpoint: data.token_endpoint,
  };
}

export function createPkcePair(): { verifier: string; challenge: string } {
  const verifier = randomUrlSafe(64);
  const challenge = crypto
    .createHash("sha256")
    .update(verifier)
    .digest("base64url");
  return { verifier, challenge };
}

export function buildAuthorizationUrl(opts: {
  authorizationEndpoint: string;
  clientId: string;
  redirectUri: string;
  scope: string;
  state: string;
  codeChallenge: string;
}): string {
  const url = new URL(opts.authorizationEndpoint);
  url.searchParams.set("client_id", opts.clientId);
  url.searchParams.set("redirect_uri", opts.redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("scope", opts.scope);
  url.searchParams.set("state", opts.state);
  url.searchParams.set("code_challenge", opts.codeChallenge);
  url.searchParams.set("code_challenge_method", "S256");
  return url.toString();
}

async function exchangeAuthorizationCode(opts: {
  tokenEndpoint: string;
  clientId: string;
  redirectUri: string;
  code: string;
  codeVerifier: string;
}): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: opts.clientId,
    redirect_uri: opts.redirectUri,
    code: opts.code,
    code_verifier: opts.codeVerifier,
  });
  const resp = await fetch(opts.tokenEndpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  return parseTokenResponse(resp);
}

async function parseTokenResponse(resp: Response): Promise<TokenResponse> {
  const text = await resp.text();
  const data = parseJsonObject(text) as TokenResponse;
  if (!resp.ok || !data.access_token) {
    const message = data.error_description || data.error || text.trim() || resp.statusText;
    throw new Error(`Keycloak token exchange failed: ${message}`);
  }
  return data;
}

async function credentialsFromToken(opts: {
  apiUrl: string;
  token: TokenResponse;
  issuer?: string;
  clientId?: string;
  scope?: string;
}): Promise<Credentials> {
  const me = await new ControlPlaneClient(opts.apiUrl, opts.token.access_token!).me();
  return {
    apiUrl: opts.apiUrl,
    token: opts.token.access_token!,
    email: me.email,
    userId: me.id,
    bucket: `user-${me.id}-files`,
    refreshToken: opts.token.refresh_token,
    expiresAt: expiresAt(opts.token.expires_in),
    issuer: opts.issuer,
    clientId: opts.clientId,
    scope: opts.token.scope || opts.scope,
  };
}

function listenForCallback(opts: {
  port: number;
  state: string;
  timeoutMs: number;
}): Promise<{
  redirectUri: string;
  waitForCode: Promise<string>;
  close: () => Promise<void>;
}> {
  let resolveCode!: (code: string) => void;
  let rejectCode!: (err: Error) => void;
  const waitForCode = new Promise<string>((resolve, reject) => {
    resolveCode = resolve;
    rejectCode = reject;
  });

  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", `http://${req.headers.host}`);
    if (url.pathname !== CALLBACK_PATH) {
      res.statusCode = 404;
      res.end("not found");
      return;
    }
    const error = url.searchParams.get("error");
    const errorDescription = url.searchParams.get("error_description");
    const code = url.searchParams.get("code");
    const state = url.searchParams.get("state");
    if (error) {
      rejectCode(new Error(errorDescription || error));
      finishBrowser(res, false);
      return;
    }
    if (!code || state !== opts.state) {
      rejectCode(new Error("invalid OAuth callback"));
      finishBrowser(res, false);
      return;
    }
    resolveCode(code);
    finishBrowser(res, true);
  });

  const timer = setTimeout(() => {
    rejectCode(new Error("timed out waiting for Keycloak login"));
  }, opts.timeoutMs);

  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(opts.port, "127.0.0.1", () => {
      const { port } = server.address() as AddressInfo;
      resolve({
        redirectUri: `http://127.0.0.1:${port}${CALLBACK_PATH}`,
        waitForCode,
        close: () =>
          new Promise((done) => {
            clearTimeout(timer);
            server.close(() => done());
          }),
      });
    });
  });
}

function finishBrowser(res: http.ServerResponse, ok: boolean): void {
  res.statusCode = ok ? 200 : 400;
  res.setHeader("content-type", "text/html; charset=utf-8");
  res.end(
    `<html><body><h1>${ok ? "a2amcp is connected" : "a2amcp login failed"}</h1>` +
      "<p>You can close this tab and return to your terminal.</p></body></html>",
  );
}

function openBrowser(url: string): void {
  const cmd =
    process.platform === "darwin"
      ? "open"
      : process.platform === "win32"
        ? "cmd"
        : "xdg-open";
  const args =
    process.platform === "win32"
      ? ["/c", "start", "", url]
      : [url];
  const child = spawn(cmd, args, { detached: true, stdio: "ignore" });
  child.on("error", () => {});
  child.unref();
}

function randomUrlSafe(bytes: number): string {
  return crypto.randomBytes(bytes).toString("base64url");
}

function expiresAt(expiresIn: number | undefined): number | undefined {
  return typeof expiresIn === "number" ? nowSeconds() + expiresIn : undefined;
}

function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseJsonObject(text: string): Record<string, unknown> {
  if (!text.trim()) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}
