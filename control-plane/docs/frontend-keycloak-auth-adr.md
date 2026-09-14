# ADR: Frontend-Owned Keycloak Browser Auth

Date: 2026-05-31

Status: accepted for implementation

## Context

The platform now has a Keycloak realm at `https://auth.a2acloud.io/realms/a2acloud`
for external OAuth/MCP connectors. The control plane already accepts two token
families in `current_user`:

- short-lived control-plane HS256 browser-session cookies minted after
  successful Keycloak OIDC login
- Keycloak RS256 access tokens validated through the realm JWKS and linked to a
  `User` through `KeycloakIdentity`

The old dashboard-local password form, `localStorage` bearer session, and
CP-native SAML login callback are retired. `/v1/auth/login`,
`/v1/auth/signup`, and `/v1/auth/saml/*` are no longer registered routes.

The next auth experience should make browser auth feel owned by the a2a
dashboard and brand, while preserving Keycloak as the standards-compliant
authorization server and not regressing ChatGPT/Claude connector OAuth.

## Decision

Use a frontend-owned auth shell plus Keycloak-hosted branded auth pages.

`app.a2acloud.io` owns the entry, routing, callback, session, and logout
surfaces. `auth.a2acloud.io` owns credential handling, browser login,
registration, password reset, required actions, MFA/passkeys, consent, token
issuance, refresh, and revocation.

Do not make the SPA collect passwords and exchange them through Resource Owner
Password Credentials for normal browser login. There is no CP-password fallback:
the browser path is authorization-code + PKCE through Keycloak.

## Page Ownership

`app.a2acloud.io`:

- email-first auth entry screen
- signup/login/enterprise-SSO routing decisions
- `/auth/callback` or equivalent control-plane callback landing page
- session bootstrap and logout handoff
- product account entry points linking to Keycloak account actions

`auth.a2acloud.io`:

- login and registration forms
- reset password, verify email, update password, and required actions
- OTP/WebAuthn/passkey enrollment and challenges
- consent screens for dashboard clients and external connector clients
- account-console flows unless/until a product wrapper is built

The Keycloak pages must use an a2a theme packaged through GitOps, not manual
console edits.

## Browser Session Model

The new browser session should use an httpOnly, secure, same-site cookie or an
opaque server-side session cookie. JavaScript should not own refresh tokens or
long-lived access tokens.

Add control-plane browser-auth endpoints in the P2 implementation task:

- `GET /v1/auth/oidc/start`
- `GET /v1/auth/oidc/callback`
- `POST /v1/auth/logout`
- `GET /v1/auth/session`

Exact names can change during implementation, but the contract should hold:

- `start` creates state, nonce, PKCE verifier, redirect target, and a short TTL,
  then redirects to Keycloak.
- `callback` validates state and nonce, exchanges the code server-side,
  validates the Keycloak token, provisions or links the CP user through the
  existing Keycloak identity bridge, then sets the browser session cookie.
- `session` returns the current `UserOut` when the browser cookie is valid.
- `logout` clears the local browser session and redirects or calls Keycloak
  logout so the upstream SSO session is handled intentionally.

`current_user` resolves the browser session cookie and Keycloak RS256 bearer
tokens. CP HS256 bearer compatibility is limited to short-lived browser-session
cookies minted by the OIDC callback, not password login.

## Callback And Security Constraints

State, nonce, and PKCE verifier material must be server-side or sealed in
httpOnly cookies. Do not store them in `localStorage`.

Redirect targets must be same-origin relative paths from an allowlist or pass a
strict `safeRelativeRedirect` check. Never reflect arbitrary absolute return
URLs.

Logout and other state-changing auth endpoints need CSRF protection. A
same-site cookie helps, but implementation should still avoid cross-site
`GET` mutations and should use POST for local logout.

Public auth errors should be clear but avoid email/user enumeration. Analytics
must not log auth codes, tokens, full emails on failure paths, raw Keycloak
errors, or SAML assertions.

Tokens and secrets must not appear in URL fragments, query strings, logs,
deployment events, or dashboard-visible errors. This replaces the current SAML
fragment token pattern for the new browser path.

## Compatibility

Keycloak RS256 bearer tokens remain supported for external MCP connectors.
Dashboard browser traffic uses the httpOnly session cookie and
`/v1/auth/session`; the SPA must not use `localStorage` bearer tokens. CP-native
password and SAML auth endpoints are removed from the FastAPI routing table.

Existing CP user rows are not migrated through a password fallback. A user who
logs in through Keycloak with a verified email links to the existing CP user row
through `KeycloakIdentity`; otherwise a fresh CP user and personal organization
are provisioned.

## Connector OAuth Invariants

The browser-auth work must not weaken the connector OAuth work from the
Keycloak/MCP epic:

- keep anonymous DCR trusted-host policy for ChatGPT/Claude constrained to the
  approved connector hosts
- keep consent-required behavior for DCR clients
- preserve scopes `mcp:invoke`, `agent:read`, and `orchestrator:run`
- preserve refresh-token rotation
- preserve protected-resource metadata, JWKS validation, and audience/resource
  binding behavior on MCP resource servers
- rerun leaf-agent and orchestrator connector smoke flows after theme/session
  changes

Dashboard browser clients may have their own Keycloak client configuration, but
connector clients must not inherit dashboard-only redirect URIs, session
assumptions, or relaxed validation.

## MCP And Agent Card Declarations

There are two MCP surfaces:

- `/mcp` is the standard Streamable HTTP MCP endpoint for code, SDK, editor, and
  local gateway clients. It exposes the agent skills directly and keeps normal
  MCP request/response or SSE elicitation behavior.
- `/connector-mcp` is the hosted-connector endpoint for ChatGPT, Claude, and
  similar UIs. It exposes the same agent skills through a connector-safe wrapper
  that can return `queued`, `running`, `approval_required`, `input_required`,
  `auth_required`, or `failed` statuses, and it adds `job_result` plus
  `submit_interaction` tools for polling and resuming. There is no separate
  `/connect-mcp` path today; use `/connector-mcp`.

Both surfaces use the same OAuth resource-server discovery anchor:
`/.well-known/oauth-protected-resource`. A 401 from `/mcp` or
`/connector-mcp` should include `WWW-Authenticate: Bearer resource_metadata=...`
so connector clients can discover the Keycloak authorization server, scopes,
and token endpoint.

The Agent Card must continue to declare the MCP/auth contract in both card
forms:

- `/.well-known/agent-card` carries the platform card fields
  `mcp_endpoint`, `connector_mcp_endpoint`, and `capabilities.mcp`.
- `/.well-known/agent-card.json` carries the A2A protocol card
  `securitySchemes.oauth2.oauth2SecurityScheme` with the Keycloak
  authorization-code flow, metadata URL, token URL, refresh URL, and scopes.

For packed frontend agents, the card should also declare the browser auth flow
under `ui.auth`: auth mode, whether a browser session is required, the
`/auth/session` URL, the login URL, and that the browser session is
cookie-backed. This lets agent pages and marketplace/detail surfaces discover
how the frontend should start or resume the platform session without inspecting
runtime env vars.

## Rollout Phases

1. P1 packages and deploys the branded Keycloak theme through GitOps.
2. P2 adds browser OIDC start/callback/logout/session endpoints and cookie
   session resolution.
3. P3 refactors the dashboard login screen into the email-first entry flow and
   uses the P2 endpoints.
4. P4 turns on signup, recovery, verify-email, and MFA/passkey-ready required
   actions under the branded theme.
5. P5 themes consent and reruns connector OAuth regression checks.
6. P6 retires legacy CP password and CP-native SAML auth entry points. Verified
   Keycloak email is the only automatic account-linking mechanism.
7. P7 exposes account and org auth controls without cloning the Keycloak admin
   console.
8. P8 completes security review, browser E2E tests, connector regression tests,
   docs, and rollback runbooks.

## Open Questions

- Should the new browser cookie contain a short-lived CP HS256 token, or should
  it be an opaque session id backed by server-side storage? Prefer opaque if the
  implementation cost stays reasonable.
- Should Keycloak self-registration be enabled directly, or should signup start
  in the dashboard and create an invited/reset-first Keycloak account?
- Which Keycloak account-console or broker-management URLs should the product
  expose directly in P7?
