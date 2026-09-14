# OAuth MCP Connector Security Review

Date: 2026-05-31
Updated: 2026-06-01

Scope: Keycloak-backed OAuth for remote MCP connectors, including deployed leaf
agents and the control-plane orchestrator MCP endpoints.

## Summary

The ChatGPT connector path is launchable with explicit accepted risks. The core
identity and resource-server path is in place: Keycloak RS256 tokens are verified
against JWKS, untrusted requests get MCP protected-resource metadata challenges,
the orchestrator MCP endpoint is private, connector jobs are user-scoped, and
Keycloak DCR is constrained to known connector hosts with consent required.
MCP resource servers now enforce method-level OAuth scopes for `tools/list`
(`agent:read`) and `tools/call` (`mcp:invoke`), and reject URL resource-audience
replay by default. The control-plane orchestrator MCP surface additionally
requires `orchestrator:run` for `tools/call`.

The remaining hardening items are not blockers for the current test launch, but
they should be closed before broader public availability: confirmed strict
audience enforcement for generic connector client audiences, PKCE S256
enforcement for anonymous DCR clients, and edge rate limiting on Keycloak
authorization/token/registration endpoints.

## Reviewed Surfaces

- Keycloak realm/client configuration (operator manifests)
- `a2a_pack/oauth.py`
- `a2a_pack/mcp/http.py`
- `control_plane/keycloak_auth.py`
- `control_plane/auth.py`
- `control_plane/orchestrator_mcp.py`

## Findings

### DCR Abuse

Status: controlled with accepted risk.

Controls:

- Anonymous client registration is limited by Keycloak client-registration
  policies.
- Trusted hosts are restricted to `chatgpt.com`, `openai.com`, `claude.ai`,
  and `anthropic.com`.
- `client-uris-must-match=true` requires registered client URIs to match the
  trusted host policy.
- A max-client policy caps anonymous registrations at 200.
- A consent-required policy forces DCR clients to show user consent.

Accepted risk:

- There is no initial access token requirement for DCR. That is intentional for
  ChatGPT/Claude compatibility during launch.
- `host-sending-registration-request-must-match=false` is accepted because
  hosted connector registration requests do not necessarily originate from the
  same host as their redirect URIs.

Follow-up:

- Add Keycloak/edge rate limits for anonymous registration.
- Revisit initial-access-token or tenant allowlist if abusive registration
  appears in logs.

### Redirect URI Validation

Status: controlled.

Controls:

- DCR trusted-host policy restricts connector redirect/client URIs to the known
  OpenAI and Anthropic connector hosts.
- Dashboard OIDC client uses explicit redirect URIs for production and local
  development.
- Wildcard post-logout redirects are limited to the dashboard origins.

Residual risk:

- Redirect validation depends on Keycloak's trusted-host policy being present in
  the live realm. Because native realm import is `IGNORE_EXISTING`, production
  migrations must verify live realm policy after changes.

### PKCE

Status: partial, accepted risk.

Controls:

- The dashboard OIDC client declares `pkce.code.challenge.method=S256`.
- Keycloak discovery advertises PKCE support.

Accepted risk:

- Anonymous DCR clients are not currently forced to S256 by a Keycloak client
  policy. Earlier enforcement attempts conflicted with DCR behavior. Connector
  clients are trusted-host constrained and consent-gated for this launch.

Follow-up:

- Reintroduce a PKCE-enforcer policy once it is verified against ChatGPT and
  Claude DCR flows.
- Document whether plain PKCE is disabled or accepted for each connector host.

### Audience And Scope Binding

Status: controlled.

Controls:

- MCP resource servers validate Keycloak RS256 signature, issuer, expiry, `iat`,
  and `sub`.
- `a2a_pack.oauth.validate_keycloak_token()` rejects replay when a token names a
  different URL resource audience. Tokens with generic client audiences remain
  tolerated unless `A2A_OAUTH_REQUIRE_AUDIENCE=1`.
- The MCP HTTP transport enforces `agent:read` for `tools/list` and
  `mcp:invoke` for `tools/call` on Keycloak bearer tokens. Enforcement is on by
  default and can be disabled only with `A2A_OAUTH_REQUIRE_SCOPES=0` for
  compatibility testing.
- The control-plane orchestrator agents declare an additional
  `orchestrator:run` requirement for `tools/call`, so hosted connector tokens
  need explicit orchestrator consent before starting, polling, or resuming
  orchestrator jobs.
- Protected-resource metadata advertises the resource URL.
- Keycloak scopes include audience mappers for MCP and orchestrator usage.

Accepted risk:

- `A2A_OAUTH_REQUIRE_AUDIENCE` remains off by default until resource-indicator
  minting is confirmed across ChatGPT and Claude. A token that only carries a
  generic client audience is not hard-rejected in the current launch posture.

Follow-up:

- Confirm connector resource-indicator behavior and enable strict audience
  enforcement per MCP host.

### Token Validation / User Linking

Status: controlled.

Controls:

- Control-plane auth routes by JWT algorithm: HS256 is limited to the
  short-lived browser session cookie minted by OIDC callback; RS* goes through
  Keycloak JWKS validation.
- Keycloak token validation requires RS256 signature, issuer, expiry, `iat`, and
  `sub`.
- Existing CP users are linked by email only when `email_verified=true`.
- Unverified email tokens are rejected with 403 before linking to an existing
  CP account.
- New email-less service identities use a synthetic
  `kc-<sub>@keycloak.a2acloud.io` address.

Residual risk:

- JWKS key cache TTL is controlled by PyJWT's client behavior rather than an
  explicit revocation-aware cache. This is acceptable for launch but must be
  documented in the token revocation task.

### Token Refresh / Revocation

Status: controlled for expiry and refresh; bounded residual risk for revocation.

Controls:

- Access-token expiry is enforced by both MCP resource servers and the
  control-plane identity bridge through PyJWT `exp` validation.
- Hosted connectors should refresh through Keycloak before retrying after an
  expired access token. The MCP resource server remains stateless and accepts
  the new RS256 token if signature, issuer, expiry, and configured audience
  checks pass.
- Agent `/v1/me` identity lookups are cached by bearer-token hash for 60
  seconds by default and can be shortened or disabled with
  `A2A_CP_ME_CACHE_TTL_SECONDS`.
- The identity cache is clamped to the JWT's own `exp`, so it cannot keep using
  an expired JWT after the token lifetime has passed.

Accepted risk:

- Keycloak access tokens are JWTs and are not introspected on each MCP call.
  Revoking a refresh token or logging out prevents future access-token minting,
  but an already-issued access token can remain valid until its `exp`.
- Admin/user revocation is therefore bounded by the shorter of the access
  token's remaining lifetime and the configured `/v1/me` identity-cache TTL for
  any cached CP identity decision. JWKS caching only affects signing-key
  discovery, not per-token revocation.

Follow-up:

- For stricter revocation, reduce Keycloak access-token lifespan and set
  `A2A_CP_ME_CACHE_TTL_SECONDS=0` during revocation E2E tests.
- Add token introspection or a server-side session denylist before relying on
  immediate admin revocation for high-risk scopes.

### Orchestrator Isolation

Status: controlled.

Controls:

- `mount_orchestrator_mcp()` forces `A2A_AGENT_PUBLIC=false`.
- The orchestrator MCP agent refuses to run without `ctx.cp_jwt`.
- The bearer token is resolved through `current_user`.
- `OrchestratorContext.for_user()` binds the run to `user-{id}-files`.
- Chat threads are looked up by both `thread_id` and `user_id`.
- Connector jobs stored in Redis include `user_id` and token hash; polling and
  resume calls require matching user or original token hash.
- Main-agent graph recursion limit is capped at 300.

Accepted risk:

- Temporary `auto_approve=True` is enabled for main-agent/subagent thread hooks
  to avoid ChatGPT host-side blocking of approval submission. The grant audit
  path remains active, but this should be removed once hosted approval resume is
  fully reliable.

### Tenant Isolation Tie-In

Status: tracked follow-up; not a connector launch blocker.

Controls:

- Orchestrator runs resolve the authenticated user through the control plane and
  bind workspace access to `user-{id}-files`.
- Connector jobs store `user_id` and token hash; polling and resume calls
  require the same user or original token hash.
- Agent-to-agent workspace grants carry an audience and bucket, and callee
  runtimes reject grants for the wrong agent audience.
- OAuth resource-audience binding now prevents a token minted for one URL
  resource from being replayed at another URL resource.

Follow-up:

- Move self-serve imported/deployed agent repositories out of the shared
  `gitea_admin` namespace into tenant or organization-owned namespaces before
  broader multi-tenant public launch.
- Tie tenant repository namespaces and stricter bucket policies to the OAuth
  scope/resource model, so platform identity, agent ownership, workspace grants,
  and repository access share the same authorization boundary.

### Secrets

Status: controlled.

Controls:

- Keycloak admin and Postgres credentials are Kubernetes Secrets:
  `keycloak-admin` and `keycloak-postgresql`.
- The realm JSON and theme ConfigMap contain no client secrets.
- DCR client secrets live in Keycloak's database, not in control-plane env.
- Control-plane grant signing material remains in platform Kubernetes Secrets.

Residual risk:

- Postgres credential rotation requires a planned maintenance procedure because
  the database role password and Kubernetes Secret must move together.

### Rate Limits

Status: accepted risk.

Controls:

- DCR is constrained by trusted hosts, max-client count, and consent.

Accepted risk:

- No explicit Keycloak/Traefik rate limits are currently configured for
  `/authorize`, `/token`, or `/clients-registrations/openid-connect`.

Follow-up:

- Add edge rate limits for Keycloak auth, token, and registration paths.
- Add alerts for high DCR volume, token errors, and repeated failed login or
  consent attempts.

## Launch Decision

Approved for the current ChatGPT connector launch with accepted risks.

Required before broader public launch:

- strict audience enforcement verified with ChatGPT and Claude
- PKCE S256 enforcement for DCR clients, or a documented per-client exception
- short-lived access tokens and/or introspection for immediate token revocation
- edge rate limits for Keycloak authorization/token/DCR endpoints
- removal of temporary thread auto-approve after hosted approval resume is
  stable
