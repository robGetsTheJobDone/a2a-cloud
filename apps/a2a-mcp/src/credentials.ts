/**
 * Reader/writer for ~/.a2a/credentials.json — same file the Python `a2a` CLI
 * writes. New logins store Keycloak access/refresh tokens; older token-only
 * files are still readable.
 */
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * The hosted a2a cloud instance. This is the ONLY place the vendor domain
 * appears: `npx a2amcp` with no configuration talks to the hosted service.
 * Self-hosters set `A2A_PLATFORM_DOMAIN` (or `A2A_API_URL` directly).
 */
export const HOSTED_PLATFORM_DOMAIN = "a2acloud.io";

export function platformDomain(): string {
  return (process.env.A2A_PLATFORM_DOMAIN || HOSTED_PLATFORM_DOMAIN).trim();
}

export const DEFAULT_API_URL = (
  process.env.A2A_API_URL || `https://api.${platformDomain()}`
).replace(/\/+$/, "");

export interface Credentials {
  apiUrl: string;
  token: string;
  email: string;
  userId?: number;
  bucket?: string;
  refreshToken?: string;
  expiresAt?: number;
  issuer?: string;
  clientId?: string;
  scope?: string;
}

const credsDir = () => path.join(os.homedir(), ".a2a");
const credsFile = () => path.join(credsDir(), "credentials.json");

export async function loadCredentials(): Promise<Credentials | null> {
  try {
    const raw = await fs.readFile(credsFile(), "utf8");
    const data = JSON.parse(raw);
    if (!data?.token) return null;
    return {
      apiUrl: data.api_url ?? DEFAULT_API_URL,
      token: data.token,
      email: data.email ?? "",
      userId: typeof data.user_id === "number" ? data.user_id : undefined,
      bucket: typeof data.bucket === "string" ? data.bucket : undefined,
      refreshToken:
        typeof data.refresh_token === "string" ? data.refresh_token : undefined,
      expiresAt: typeof data.expires_at === "number" ? data.expires_at : undefined,
      issuer: typeof data.oauth_issuer === "string" ? data.oauth_issuer : undefined,
      clientId: typeof data.client_id === "string" ? data.client_id : undefined,
      scope: typeof data.scope === "string" ? data.scope : undefined,
    };
  } catch (err: any) {
    if (err?.code === "ENOENT") return null;
    throw err;
  }
}

export async function saveCredentials(c: Credentials): Promise<void> {
  await fs.mkdir(credsDir(), { recursive: true });
  const out: Record<string, unknown> = {
    api_url: c.apiUrl,
    token: c.token,
    email: c.email,
  };
  if (c.userId !== undefined) out.user_id = c.userId;
  if (c.bucket !== undefined) out.bucket = c.bucket;
  if (c.refreshToken !== undefined) out.refresh_token = c.refreshToken;
  if (c.expiresAt !== undefined) out.expires_at = c.expiresAt;
  if (c.issuer !== undefined) out.oauth_issuer = c.issuer;
  if (c.clientId !== undefined) out.client_id = c.clientId;
  if (c.scope !== undefined) out.scope = c.scope;
  await fs.writeFile(credsFile(), JSON.stringify(out), { mode: 0o600 });
}

export async function clearCredentials(): Promise<boolean> {
  try {
    await fs.unlink(credsFile());
    return true;
  } catch (err: any) {
    if (err?.code === "ENOENT") return false;
    throw err;
  }
}

export function resolveApiUrl(override?: string): string {
  if (override) return override;
  if (process.env.A2A_API_URL) return process.env.A2A_API_URL;
  return DEFAULT_API_URL;
}
