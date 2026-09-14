# Control-plane API guide

The control plane at `https://api.a2acloud.io` is the state API used by the
dashboard and CLI. Agent invocation traffic normally goes to the deployed
agent's A2A, `/invoke`, or `/mcp` endpoint instead.

## Choose the right interface

- Use the **CLI** for login, scaffolding, deployment, imports, MCP setup, and
  typed agent calls.
- Use an **agent endpoint** for A2A messages, direct tool invocation, streaming,
  agent cards, and MCP.
- Use the **control-plane API** for account state, registry state, workspaces,
  trials, schedules, proofs, setup, organizations, and governance.

## Authentication

`a2a login` stores and refreshes the account OAuth token. For direct HTTP
automation, send that access token:

```bash
export A2A_ACCESS_TOKEN="replace-with-a-short-lived-token"
curl --fail-with-body \
  -H "Authorization: Bearer ${A2A_ACCESS_TOKEN}" \
  -H "Accept: application/json" \
  https://api.a2acloud.io/v1/me
```

Do not reuse browser cookies in a server integration. Browser-session mutations
require CSRF protection in addition to the session.

## Canonical schemas

- [OpenAPI JSON](https://api.a2acloud.io/openapi.json)
- [Swagger UI](https://api.a2acloud.io/docs)
- [ReDoc](https://api.a2acloud.io/redoc)
- [Generated route inventory](/reference/control-plane-api)

The OpenAPI document is authoritative for bodies, query parameters, response
models, and current status codes. The generated inventory is pinned to this
repository revision and includes hidden platform/operator routes for audit
completeness.

## Main API families

| Family | Prefix | Purpose |
| --- | --- | --- |
| Account | `/v1/me` | Identity, onboarding, activity, files, threads, usage, runtime, and feature state |
| Agents | `/v1/agents` | Registry, deployment, imports, tools, proofs, evidence, secrets, auth, mailboxes, and Studio |
| Public discovery | `/v1/public` | Public agents and proofs |
| Installed setup | `/v1/installed-agents` | Consumer setup, imported auth, and links |
| Trials | `/v1/me/trial-rooms` | Candidate rooms, runs, ratings, and selection |
| Schedules | `/v1/me/schedules` | Recurring work and run-now |
| Workspace grants | `/v1/workspace-grants` | Scoped file delegation and file operations |
| Organizations | `/v1/me/organizations`, `/v1/scim` | Domains, roles, members, audit, and provisioning |
| Compliance | organization-scoped `/v1/.../compliance` routes | Policy, decision records, risk, retention, and export |

## Errors and retries

- Branch on HTTP status codes, not fragments of the human message.
- Read `detail`; when present, the platform also returns a structured `error`
  object.
- Retry idempotent reads and explicitly idempotent operations only.
- A `401` means the access token needs refresh or login.
- A `403` means the identity exists but lacks the required authority.
- A `409` means current state conflicts with the requested transition.
- A `422` means the request failed schema validation.
- A `429` means the caller exceeded the request-rate ceiling for that class of
  endpoint. Wait the number of seconds in `Retry-After` before retrying; do not
  retry immediately. Only sign-in, public discovery, and the endpoints that
  start builds or run agents carry a ceiling; the OpenAPI document marks every
  operation that can answer `429`. Ceilings that key on your account are keyed
  on the account, not on the token, so refreshing a token does not reset one.
- Preserve request, job, grant, proof, deployment, and receipt identifiers in
  automation logs.

## Versioning

`/v1` is the stable namespace. Additive response fields are expected. Clients
should ignore unknown fields, validate the fields they require, and refresh
generated clients when the OpenAPI document changes.
