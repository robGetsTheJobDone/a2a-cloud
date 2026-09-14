import crypto from "crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { platformDomain } from "./platform";

export type AdminSession = {
  sub: string;
  email: string;
  userId: number;
  iat: number;
  exp: number;
};

export type AdminOidcState = {
  state: string;
  nonce: string;
  codeVerifier: string;
  iat: number;
  exp: number;
};

const COOKIE_NAME = "a2a_admin_session";
const OIDC_COOKIE_NAME = "a2a_admin_oidc_state";
const SESSION_TTL_SECONDS = 60 * 60 * 12;
const OIDC_STATE_TTL_SECONDS = 10 * 60;

function base64url(input: Buffer | string): string {
  return Buffer.from(input)
    .toString("base64")
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function fromBase64url(input: string): Buffer {
  const padded = input.padEnd(input.length + ((4 - (input.length % 4)) % 4), "=");
  return Buffer.from(padded.replaceAll("-", "+").replaceAll("_", "/"), "base64");
}

function signedToken(payload: string): string {
  return `${payload}.${base64url(
    crypto.createHmac("sha256", sessionSecret()).update(payload).digest(),
  )}`;
}

function verifySignedToken(token: string | undefined): string | null {
  if (!token || !sessionSecret()) return null;
  const [payload, signature] = token.split(".");
  if (!payload || !signature) return null;
  const expected = signedToken(payload).split(".")[1];
  if (!expected || expected.length !== signature.length) return null;
  const actual = Buffer.from(signature);
  const want = Buffer.from(expected);
  if (!crypto.timingSafeEqual(actual, want)) return null;
  return payload;
}

function sessionSecret(): string {
  return process.env.ADMIN_SESSION_SECRET || "";
}

export function keycloakRealm(): string {
  return process.env.ADMIN_KEYCLOAK_REALM || "a2a";
}

export function keycloakIssuer(): string {
  return (
    process.env.ADMIN_KEYCLOAK_ISSUER || `https://auth.${platformDomain()}/realms/${keycloakRealm()}`
  ).replace(
    /\/$/,
    "",
  );
}

export function keycloakClientId(): string {
  return process.env.ADMIN_KEYCLOAK_CLIENT_ID || "a2a-admin";
}

export function adminPublicUrl(): string {
  return (process.env.ADMIN_PUBLIC_URL || `https://admin.${platformDomain()}`).replace(/\/$/, "");
}

export function adminRedirectUri(): string {
  return `${adminPublicUrl()}/api/admin/callback`;
}

export function keycloakAuthUrl(): string {
  return `${keycloakIssuer()}/protocol/openid-connect/auth`;
}

export function keycloakTokenUrl(): string {
  return `${keycloakIssuer()}/protocol/openid-connect/token`;
}

export function adminAuthConfigured(): boolean {
  return Boolean(sessionSecret() && process.env.A2A_CP_ADMIN_TOKEN);
}

export function randomVerifier(): string {
  return base64url(crypto.randomBytes(48));
}

export function pkceChallenge(verifier: string): string {
  return base64url(crypto.createHash("sha256").update(verifier).digest());
}

export function createOidcStateToken(
  state: string,
  nonce: string,
  codeVerifier: string,
): string {
  const now = Math.floor(Date.now() / 1000);
  const payload = base64url(
    JSON.stringify({
      state,
      nonce,
      codeVerifier,
      iat: now,
      exp: now + OIDC_STATE_TTL_SECONDS,
    } satisfies AdminOidcState),
  );
  return signedToken(payload);
}

export function readOidcStateToken(token: string | undefined): AdminOidcState | null {
  const payload = verifySignedToken(token);
  if (!payload) return null;
  try {
    const state = JSON.parse(fromBase64url(payload).toString("utf8")) as AdminOidcState;
    if (!state.state || !state.nonce || !state.codeVerifier) return null;
    if (!state.exp || state.exp < Math.floor(Date.now() / 1000)) return null;
    return state;
  } catch {
    return null;
  }
}

export function readJwtPayload(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length < 2 || !parts[1]) return null;
  try {
    const payload = JSON.parse(fromBase64url(parts[1]).toString("utf8"));
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

export function idTokenHasNonce(idToken: string, expectedNonce: string): boolean {
  const payload = readJwtPayload(idToken);
  const nonce = payload?.nonce;
  if (typeof nonce !== "string") return false;
  const actual = Buffer.from(nonce);
  const expected = Buffer.from(expectedNonce);
  return actual.length === expected.length && crypto.timingSafeEqual(actual, expected);
}

export function createSessionToken(user: { id: number; email: string }): string {
  const now = Math.floor(Date.now() / 1000);
  const payload = base64url(
    JSON.stringify({
      sub: user.email,
      email: user.email,
      userId: user.id,
      iat: now,
      exp: now + SESSION_TTL_SECONDS,
    } satisfies AdminSession),
  );
  return signedToken(payload);
}

export function readSessionToken(token: string | undefined): AdminSession | null {
  const payload = verifySignedToken(token);
  if (!payload) return null;
  try {
    const session = JSON.parse(fromBase64url(payload).toString("utf8")) as AdminSession;
    if (!session.sub || !session.email || !session.userId || !session.exp) return null;
    if (session.exp < Math.floor(Date.now() / 1000)) return null;
    return session;
  } catch {
    return null;
  }
}

export async function getAdminSession(): Promise<AdminSession | null> {
  const jar = await cookies();
  return readSessionToken(jar.get(COOKIE_NAME)?.value);
}

export async function requireAdmin(): Promise<AdminSession> {
  const session = await getAdminSession();
  if (!session) redirect("/login");
  return session;
}

function secureCookie() {
  return process.env.NODE_ENV === "production";
}

export function adminCookie(token: string) {
  return {
    name: COOKIE_NAME,
    value: token,
    options: {
      httpOnly: true,
      secure: secureCookie(),
      sameSite: "lax" as const,
      path: "/",
      maxAge: SESSION_TTL_SECONDS,
    },
  };
}

export function oidcStateCookie(token: string) {
  return {
    name: OIDC_COOKIE_NAME,
    value: token,
    options: {
      httpOnly: true,
      secure: secureCookie(),
      sameSite: "lax" as const,
      path: "/api/admin",
      maxAge: OIDC_STATE_TTL_SECONDS,
    },
  };
}

export const emptyAdminCookie = {
  name: COOKIE_NAME,
  value: "",
  options: {
    httpOnly: true,
    secure: secureCookie(),
    sameSite: "lax" as const,
    path: "/",
    maxAge: 0,
  },
};

export const emptyOidcStateCookie = {
  name: OIDC_COOKIE_NAME,
  value: "",
  options: {
    httpOnly: true,
    secure: secureCookie(),
    sameSite: "lax" as const,
    path: "/api/admin",
    maxAge: 0,
  },
};
