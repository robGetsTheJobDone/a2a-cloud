import { NextRequest, NextResponse } from "next/server";
import {
  adminCookie,
  adminRedirectUri,
  createSessionToken,
  emptyOidcStateCookie,
  idTokenHasNonce,
  keycloakClientId,
  keycloakTokenUrl,
  readOidcStateToken,
} from "@/lib/auth";
import { authenticateKeycloakAdmin } from "@/lib/cp";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  const url = new URL(request.url);
  const error = url.searchParams.get("error");
  if (error) {
    return redirectToLogin("denied");
  }
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const stored = readOidcStateToken(request.cookies.get("a2a_admin_oidc_state")?.value);
  if (!code || !state || !stored || stored.state !== state) {
    return redirectToLogin("state");
  }

  try {
    const tokenResponse = await fetch(keycloakTokenUrl(), {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: keycloakClientId(),
        code,
        redirect_uri: adminRedirectUri(),
        code_verifier: stored.codeVerifier,
      }).toString(),
    });
    if (!tokenResponse.ok) {
      return redirectToLogin("token");
    }
    const tokens = (await tokenResponse.json()) as { id_token?: string };
    if (!tokens.id_token) {
      return redirectToLogin("token");
    }
    if (!idTokenHasNonce(tokens.id_token, stored.nonce)) {
      return redirectToLogin("state");
    }
    const admin = await authenticateKeycloakAdmin(tokens.id_token);
    const response = new NextResponse(null, {
      status: 303,
      headers: { location: "/" },
    });
    const sessionCookie = adminCookie(
      createSessionToken({ id: admin.id, email: admin.email }),
    );
    response.cookies.set(sessionCookie.name, sessionCookie.value, sessionCookie.options);
    response.cookies.set(
      emptyOidcStateCookie.name,
      emptyOidcStateCookie.value,
      emptyOidcStateCookie.options,
    );
    return response;
  } catch {
    return redirectToLogin("forbidden");
  }
}

function redirectToLogin(error: string) {
  const response = new NextResponse(null, {
    status: 303,
    headers: { location: `/login?error=${encodeURIComponent(error)}` },
  });
  response.cookies.set(
    emptyOidcStateCookie.name,
    emptyOidcStateCookie.value,
    emptyOidcStateCookie.options,
  );
  return response;
}
