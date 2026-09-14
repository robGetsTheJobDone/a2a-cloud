import { NextResponse } from "next/server";
import {
  adminAuthConfigured,
  adminRedirectUri,
  createOidcStateToken,
  keycloakAuthUrl,
  keycloakClientId,
  oidcStateCookie,
  pkceChallenge,
  randomVerifier,
} from "@/lib/auth";

export const runtime = "nodejs";

export async function GET() {
  if (!adminAuthConfigured()) {
    return new NextResponse(null, {
      status: 303,
      headers: { location: "/login?error=config" },
    });
  }
  const state = randomVerifier();
  const nonce = randomVerifier();
  const codeVerifier = randomVerifier();
  const params = new URLSearchParams({
    client_id: keycloakClientId(),
    redirect_uri: adminRedirectUri(),
    response_type: "code",
    scope: "openid email",
    state,
    nonce,
    code_challenge: pkceChallenge(codeVerifier),
    code_challenge_method: "S256",
  });
  const response = new NextResponse(null, {
    status: 303,
    headers: { location: `${keycloakAuthUrl()}?${params.toString()}` },
  });
  const cookie = oidcStateCookie(createOidcStateToken(state, nonce, codeVerifier));
  response.cookies.set(cookie.name, cookie.value, cookie.options);
  return response;
}

export async function POST() {
  return GET();
}
