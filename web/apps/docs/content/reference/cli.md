# `a2a` CLI

Build, package, and deploy A2A agents.

This page is generated from the CLI command model. The structured
format is stable across terminal widths and Rich/Typer renderers.

## Commands

| Command | Purpose |
| --- | --- |
| [`a2a agents`](#a2a-agents) | List agents visible to the current user. |
| [`a2a auth`](#a2a-auth) | Manage imported agent auth. |
| [`a2a auth api-key`](#a2a-auth-api-key) | Connect API-key auth for an imported agent. |
| [`a2a auth bearer`](#a2a-auth-bearer) | Connect HTTP/Bearer auth for an imported agent. |
| [`a2a auth delete`](#a2a-auth-delete) | Remove an imported-agent auth connection. |
| [`a2a auth mtls`](#a2a-auth-mtls) | Connect mTLS client certificate auth for an imported agent. |
| [`a2a auth oauth-token`](#a2a-auth-oauth-token) | Connect OAuth/OIDC with an existing access token and optional refresh config. |
| [`a2a auth status`](#a2a-auth-status) | Show detected auth requirements and configured connections. |
| [`a2a build`](#a2a-build) | Build (and optionally push) the container image for the agent. |
| [`a2a call`](#a2a-call) | Invoke one tool without a typed stub — the schema-free escape hatch. |
| [`a2a card`](#a2a-card) | Print the Agent Card JSON for the project's agent. |
| [`a2a chat`](#a2a-chat) | Run one agent plus declared local Qdrant/Postgres resources. |
| [`a2a compile`](#a2a-compile) | Compile the project declaration to the sidecar Agent DSL. |
| [`a2a deploy`](#a2a-deploy) | Ship the agent. Tarballs your source, uploads to the control plane, and prints the URL when it's live. No local docker. No git. No knowledge of how the platform builds or deploys. |
| [`a2a dev`](#a2a-dev) | Edit locally, run in the cloud: sync this project to the agent's scale-to-zero dev box, hot-reload it there, and serve it on a public URL. Use --local to run on this machine instead. |
| [`a2a frontend`](#a2a-frontend) | Build and inspect packed frontend apps. |
| [`a2a frontend build`](#a2a-frontend-build) | Build the packed frontend declared in a2a.yaml. |
| [`a2a frontend info`](#a2a-frontend-info) | Print the packed frontend config resolved from a2a.yaml. |
| [`a2a import`](#a2a-import) | Import an already-running A2A agent into the registry. |
| [`a2a init`](#a2a-init) | Scaffold a new agent project. |
| [`a2a local-cleanup`](#a2a-local-cleanup) | Delete a local devcontainer agent and managed local resources. |
| [`a2a local-deploy`](#a2a-local-deploy) | Deploy an agent to the local devcontainer control plane. This is the non-interactive local harness for tests and generated agents: it mints an e2e bearer token from the local control-plane container when no token env var is present, then uses the same tarball upload path as `a2a deploy`. |
| [`a2a login`](#a2a-login) | Authenticate with Keycloak and cache OAuth tokens. |
| [`a2a logout`](#a2a-logout) | Forget the cached JWT. |
| [`a2a logs`](#a2a-logs) | Read the build and runtime logs behind a deploy. This is what to run when `a2a deploy` says a build failed: it prints the same Gitea Actions / pod / Argo output the dashboard's deployment timeline shows, for the deployment you name or the most recent one. |
| [`a2a mcp-url`](#a2a-mcp-url) | Print the MCP connect config for a deployed agent. Every shipped agent auto-exposes ``POST /mcp`` (Streamable HTTP). This command prints the JSON snippet you paste into Claude Code, Cursor, or any other MCP client to wire up the agent. |
| [`a2a openapi`](#a2a-openapi) | Generate editable agents from OpenAPI specs. |
| [`a2a openapi client`](#a2a-openapi-client) | Generate a TypeScript client for this agent's tool APIs. |
| [`a2a openapi generate`](#a2a-openapi-generate) | Generate an editable A2APack source repo and start deployment. |
| [`a2a openapi preview`](#a2a-openapi-preview) | Preview generated tools, setup fields, and source files. |
| [`a2a openapi spec`](#a2a-openapi-spec) | Generate an OpenAPI spec for this agent's direct tool APIs. |
| [`a2a receipt`](#a2a-receipt) | Inspect and verify signed execution receipts. |
| [`a2a receipt list`](#a2a-receipt-list) | List recent receipts for AGENT (or the ones cached by recent calls). |
| [`a2a receipt show`](#a2a-receipt-show) | Print what a receipt says: identity, call, authority, effects, outcome, timing. Exits 1 when the signature is bad, so `a2a receipt show` is safe in a script; a receipt nothing could verify (no key reachable) still exits 0. |
| [`a2a receipt verify`](#a2a-receipt-verify) | Check a receipt's Ed25519 signature. Exit 0 on PASS, 1 on FAIL. |
| [`a2a run`](#a2a-run) | Run the agent's HTTP server locally (used inside the container too). |
| [`a2a sidecar`](#a2a-sidecar) | Run the common sidecar runtime for a compiled Agent DSL. |
| [`a2a signup`](#a2a-signup) | Create or sign into a Keycloak account and cache OAuth tokens. |
| [`a2a ssh`](#a2a-ssh) | Open a shell in a throwaway dev box for AGENT. The box has node, python, a2a-pack, and the agent repo already loaded, and scales to zero when you disconnect. Also usable from VS Code Remote-SSH, Cursor, scp, and rsync via the host alias it writes to ~/.ssh/config. |
| [`a2a ssh-proxy`](#a2a-ssh-proxy) | Internal: stdio<->wss bridge used as an ssh ProxyCommand. |
| [`a2a test`](#a2a-test) | Run local preflight checks before deploying. |
| [`a2a unuse`](#a2a-unuse) | Remove AGENT's cached CLI stub (stored setup values are kept). |
| [`a2a use`](#a2a-use) | Make AGENT's tools available as typed `a2a <agent> <tool>` commands. Fetches the agent card (tools + input schemas + consumer setup), checks the platform-side setup status, then caches the typed stub. Re-run any time to refresh. |
| [`a2a validate`](#a2a-validate) | Load the agent and print its Card schema. Exits non-zero on errors. |
| [`a2a whoami`](#a2a-whoami) | Show the currently logged-in user. |

## `a2a agents`

List agents visible to the current user.

```text
a2a agents [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--api` | TEXT | no | — |  |

## `a2a auth`

Manage imported agent auth.

```text
a2a auth
```

## `a2a auth api-key`

Connect API-key auth for an imported agent.

```text
a2a auth api-key [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `--value` | TEXT | yes | — |  |
| `--name` | TEXT | yes | — | Header or query parameter name |
| `--location` | TEXT | no | header | header or query |
| `--scheme-name` | TEXT | no | — |  |
| `--api` | TEXT | no | — |  |

## `a2a auth bearer`

Connect HTTP/Bearer auth for an imported agent.

```text
a2a auth bearer [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `--token` | TEXT | yes | — |  |
| `--scheme-name` | TEXT | no | — |  |
| `--scheme` | TEXT | no | Bearer |  |
| `--api` | TEXT | no | — |  |

## `a2a auth delete`

Remove an imported-agent auth connection.

```text
a2a auth delete [OPTIONS] AGENT CONNECTION_ID
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `CONNECTION_ID` | INTEGER | yes | — | Auth connection id |
| `--api` | TEXT | no | — |  |

## `a2a auth mtls`

Connect mTLS client certificate auth for an imported agent.

```text
a2a auth mtls [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `--cert` | PATH | yes | — |  |
| `--key` | PATH | yes | — |  |
| `--ca` | PATH | no | — |  |
| `--scheme-name` | TEXT | no | — |  |
| `--api` | TEXT | no | — |  |

## `a2a auth oauth-token`

Connect OAuth/OIDC with an existing access token and optional refresh config.

```text
a2a auth oauth-token [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `--access-token` | TEXT | yes | — |  |
| `--scheme-type` | TEXT | no | oauth2 | oauth2 or oidc |
| `--scheme-name` | TEXT | no | — |  |
| `--refresh-token` | TEXT | no | — |  |
| `--token-url` | TEXT | no | — |  |
| `--client-id` | TEXT | no | — |  |
| `--client-secret` | TEXT | no | — |  |
| `--expires-in` | INTEGER | no | — |  |
| `--api` | TEXT | no | — |  |

## `a2a auth status`

Show detected auth requirements and configured connections.

```text
a2a auth status [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Imported agent name |
| `--api` | TEXT | no | — |  |

## `a2a build`

Build (and optionally push) the container image for the agent.

```text
a2a build [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--registry` | TEXT | no | registry.a2acloud.io |  |
| `--push` | BOOLEAN | no | False | Also push the built image |

## `a2a call`

Invoke one tool without a typed stub — the schema-free escape hatch.

```text
a2a call [OPTIONS] AGENT TOOL [ARGS...]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Agent name |
| `TOOL` | TEXT | yes | — | Tool id (see the agent card or `a2a use`) |
| `ARGS` | TEXT... | no | — | key=value pairs; values support @file and @- for stdin |
| `--json` | TEXT | no | — | Full arguments as JSON (inline or @file.json) |
| `--api` | TEXT | no | — |  |

## `a2a card`

Print the Agent Card JSON for the project's agent.

```text
a2a card [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |

## `a2a chat`

Run one agent plus declared local Qdrant/Postgres resources.

```text
a2a chat [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--host` | TEXT | no | 127.0.0.1 |  |
| `--port` | INTEGER | no | 8000 |  |
| `--reload`, `--no-reload` | BOOLEAN | no | True |  |
| `--env-file` | PATH | no | .env.local |  |
| `--workspace` | PATH | no | — |  |
| `--build`, `--no-build` | BOOLEAN | no | True | Build the local agent image before starting compose. |
| `--up`, `--no-up` | BOOLEAN | no | True | Start the compose harness after writing it. |
| `--detach`, `--foreground` | BOOLEAN | no | False | Run compose in the background. |
| `--down` | BOOLEAN | no | False | Stop the existing chat harness for this project. |
| `--volumes` | BOOLEAN | no | False | With --down, also remove the chat harness data volumes. |
| `--print-compose` | BOOLEAN | no | False | Print the generated compose file. |
| `--base-image` | TEXT | no | python:3.11-slim | Base image for the generated local Dockerfile. |

## `a2a compile`

Compile the project declaration to the sidecar Agent DSL.

```text
a2a compile [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--out`, `-o` | PATH | no | — | Write DSL JSON here (defaults to PROJECT/.a2a/agent.dsl.json) |
| `--stdout` | BOOLEAN | no | False | Print DSL JSON instead of writing a file |

## `a2a deploy`

Ship the agent. Tarballs your source, uploads to the control plane, and prints the URL when it's live. No local docker. No git. No knowledge of how the platform builds or deploys.

```text
a2a deploy [OPTIONS] [PROJECT]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `PROJECT` | PATH | no | — | Project directory containing a2a.yaml |
| `--project`, `-p` | PATH | no | . |  |
| `--public`, `--private` | BOOLEAN | no | — | List/unlist the agent in the public registry at a2acloud.io. Defaults to `expose.public` from a2a.yaml; with no such key the agent keeps its current listing, and a brand-new agent is unlisted. |
| `--wait`, `--no-wait` | BOOLEAN | no | True | Poll until URL is live |
| `--api` | TEXT | no | — | Override control plane URL |

## `a2a dev`

Edit locally, run in the cloud: sync this project to the agent's scale-to-zero dev box, hot-reload it there, and serve it on a public URL. Use --local to run on this machine instead.

```text
a2a dev [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--host` | TEXT | no | 127.0.0.1 |  |
| `--port` | INTEGER | no | 8000 |  |
| `--reload`, `--no-reload` | BOOLEAN | no | True |  |
| `--env-file` | PATH | no | .env.local |  |
| `--workspace` | PATH | no | — |  |
| `--docker`, `--host-runtime` | BOOLEAN | no | False | Run dev mode in the agent container image instead of this Python process. Defaults to the in-process runtime, which needs no Docker. |
| `--local` | BOOLEAN | no | False | Run on this machine instead of a cloud dev box (offline / air-gapped). |
| `--agent` | TEXT | no | — | Agent name for the cloud dev box (default: a2a.yaml name). |
| `--api` | TEXT | no | — |  |

## `a2a frontend`

Build and inspect packed frontend apps.

```text
a2a frontend
```

## `a2a frontend build`

Build the packed frontend declared in a2a.yaml.

```text
a2a frontend build [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |

## `a2a frontend info`

Print the packed frontend config resolved from a2a.yaml.

```text
a2a frontend info [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |

## `a2a import`

Import an already-running A2A agent into the registry.

```text
a2a import [OPTIONS] URL
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `URL` | TEXT | yes | — | Base URL of an existing A2A agent |
| `--name`, `-n` | TEXT | no | — | Registry slug to use |
| `--public`, `--private` | BOOLEAN | no | False |  |
| `--auth-bearer` | TEXT | no | — | Bearer token for the imported agent |
| `--auth-api-key` | TEXT | no | — | API key value for the imported agent |
| `--auth-name` | TEXT | no | — | API-key header or query parameter name |
| `--auth-location` | TEXT | no | header | API-key location: header or query |
| `--auth-scheme` | TEXT | no | Bearer | HTTP Authorization scheme |
| `--api` | TEXT | no | — |  |

## `a2a init`

Scaffold a new agent project.

```text
a2a init [OPTIONS] NAME
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `NAME` | TEXT | yes | — | Agent / project slug, e.g. research-agent |
| `--description`, `-d` | TEXT | no | A new A2A agent |  |
| `--target`, `-t` | PATH | no | . | Parent dir for the new project |
| `--language`, `-l` | TEXT | no | python | Template language. python and typescript/javascript are the full multi-tool SDKs. go and rust are single-skill demos of the sidecar worker protocol: they route only the built-in 'sum' skill. |
| `--frontend` | TEXT | no | — | Add a packed frontend scaffold: static, react, or nextjs |
| `--frontend-mode` | TEXT | no | static | Frontend deployment mode: static or server-rendered |
| `--auth` | TEXT | no | inherit | App auth mode for scaffolded agents/frontends: inherit, platform, or public |

## `a2a local-cleanup`

Delete a local devcontainer agent and managed local resources.

```text
a2a local-cleanup [OPTIONS] NAME
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `NAME` | TEXT | yes | — | Agent name to delete from the local control plane |
| `--api` | TEXT | no | — | Local control plane URL |
| `--token` | TEXT | no | — | Bearer token override |
| `--token-email` | TEXT | no | local@example.com | Local user email used when minting a local token with docker exec |
| `--docker-token`, `--no-docker-token` | BOOLEAN | no | True | Mint a local CP token from the a2a-control-plane container when none is supplied |
| `--wait-control-plane`, `--no-wait-control-plane` | BOOLEAN | no | True | Wait for the local control plane /healthz before deleting |
| `--ignore-missing` | BOOLEAN | no | False | Return success when the agent is already absent |
| `--json` | BOOLEAN | no | False | Print machine-readable JSON |

## `a2a local-deploy`

Deploy an agent to the local devcontainer control plane. This is the non-interactive local harness for tests and generated agents: it mints an e2e bearer token from the local control-plane container when no token env var is present, then uses the same tarball upload path as `a2a deploy`.

```text
a2a local-deploy [OPTIONS] [PROJECT]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `PROJECT` | PATH | no | — | Project directory containing a2a.yaml |
| `--project`, `-p` | PATH | no | . |  |
| `--public`, `--private` | BOOLEAN | no | — |  |
| `--api` | TEXT | no | — | Local control plane URL |
| `--token` | TEXT | no | — | Bearer token override |
| `--token-email` | TEXT | no | local@example.com | Local user email used when minting a local token with docker exec |
| `--docker-token`, `--no-docker-token` | BOOLEAN | no | True | Mint a local CP token from the a2a-control-plane container when none is supplied |
| `--wait-control-plane`, `--no-wait-control-plane` | BOOLEAN | no | True | Wait for the local control plane /healthz before uploading |
| `--wait-agent`, `--no-wait-agent` | BOOLEAN | no | False | Wait for the deployed agent /healthz URL after upload |
| `--json` | BOOLEAN | no | False | Print machine-readable JSON |

## `a2a login`

Authenticate with Keycloak and cache OAuth tokens.

```text
a2a login [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--api` | TEXT | no | https://api.a2acloud.io |  |
| `--issuer` | TEXT | no | https://auth.a2acloud.io/realms/a2acloud |  |
| `--client-id` | TEXT | no | a2acloud-cli |  |
| `--scope` | TEXT | no | openid email offline_access mcp:invoke agent:read |  |
| `--port` | INTEGER | no | 41873 |  |
| `--token` | TEXT | no | — | Use an existing Keycloak access token |
| `--open`, `--no-open` | BOOLEAN | no | True | Open the login URL |

## `a2a logout`

Forget the cached JWT.

```text
a2a logout
```

## `a2a logs`

Read the build and runtime logs behind a deploy. This is what to run when `a2a deploy` says a build failed: it prints the same Gitea Actions / pod / Argo output the dashboard's deployment timeline shows, for the deployment you name or the most recent one.

```text
a2a logs [OPTIONS] [AGENT]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | no | — | Agent name (defaults to a2a.yaml in --project) |
| `--deploy` | TEXT | no | — | Deployment id (default: the most recent deploy) |
| `--follow`, `-f` | BOOLEAN | no | False | Keep polling until the deploy reaches a terminal state |
| `--tail`, `-n` | INTEGER | no | 200 | Lines to show from the end of each log stream |
| `--project`, `-p` | PATH | no | . |  |
| `--api` | TEXT | no | — | Override control plane URL |

## `a2a mcp-url`

Print the MCP connect config for a deployed agent. Every shipped agent auto-exposes ``POST /mcp`` (Streamable HTTP). This command prints the JSON snippet you paste into Claude Code, Cursor, or any other MCP client to wire up the agent.

```text
a2a mcp-url [OPTIONS] [NAME]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `NAME` | TEXT | no | — | Agent name (defaults to a2a.yaml in --project) |
| `--project`, `-p` | PATH | no | . |  |
| `--api` | TEXT | no | — |  |

## `a2a openapi`

Generate editable agents from OpenAPI specs.

```text
a2a openapi
```

## `a2a openapi client`

Generate a TypeScript client for this agent's tool APIs.

```text
a2a openapi client [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--out`, `-o` | PATH | no | frontend/src/a2a-client | Directory where @hey-api/openapi-ts writes the generated client |
| `--base-url` | TEXT | no | — | Optional server URL to include in the generated OpenAPI spec |
| `--skill`, `-s` | TEXT | no | — | Tool to include. Repeat to export multiple tools. Defaults to all tools. |
| `--require-bearer`, `--public` | BOOLEAN | no | — | Override transport auth in the generated spec. By default this is derived from the agent auth_model. |
| `--spec-out` | PATH | no | — | Also write the intermediate OpenAPI JSON spec to this path after generation |

## `a2a openapi generate`

Generate an editable A2APack source repo and start deployment.

```text
a2a openapi generate [OPTIONS] [URL]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `URL` | TEXT | no | https://petstore3.swagger.io/api/v3/openapi.json | OpenAPI JSON/YAML URL |
| `--name`, `-n` | TEXT | no | — | Registry slug to generate |
| `--description`, `-d` | TEXT | no | — |  |
| `--base-url` | TEXT | no | — | Override the API server URL |
| `--public`, `--private` | BOOLEAN | no | — | List the generated agent in the public registry at a2acloud.io. Generated agents start unlisted. |
| `--api` | TEXT | no | — |  |

## `a2a openapi preview`

Preview generated tools, setup fields, and source files.

```text
a2a openapi preview [OPTIONS] [URL]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `URL` | TEXT | no | https://petstore3.swagger.io/api/v3/openapi.json | OpenAPI JSON/YAML URL |
| `--name`, `-n` | TEXT | no | — | Registry slug to generate |
| `--description`, `-d` | TEXT | no | — |  |
| `--base-url` | TEXT | no | — | Override the API server URL |
| `--public`, `--private` | BOOLEAN | no | — | Listing the matching `a2a openapi generate` would use. Generated agents start unlisted unless you pass --public. |
| `--api` | TEXT | no | — |  |

## `a2a openapi spec`

Generate an OpenAPI spec for this agent's direct tool APIs.

```text
a2a openapi spec [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--out`, `-o` | PATH | no | — | Write OpenAPI JSON here instead of stdout |
| `--base-url` | TEXT | no | — | Optional server URL to include in the OpenAPI servers list |
| `--require-bearer`, `--public` | BOOLEAN | no | — | Override transport auth in the generated spec. By default this is derived from the agent auth_model. |

## `a2a receipt`

Inspect and verify signed execution receipts.

```text
a2a receipt
```

## `a2a receipt list`

List recent receipts for AGENT (or the ones cached by recent calls).

```text
a2a receipt list [OPTIONS] [AGENT]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | no | — | Agent whose receipts to list (omit to list ones this CLI has seen) |
| `--limit`, `-n` | INTEGER | no | 20 | How many receipts to show |
| `--api` | TEXT | no | — |  |

## `a2a receipt show`

Print what a receipt says: identity, call, authority, effects, outcome, timing. Exits 1 when the signature is bad, so `a2a receipt show` is safe in a script; a receipt nothing could verify (no key reachable) still exits 0.

```text
a2a receipt show [OPTIONS] [RECEIPT_ID]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `RECEIPT_ID` | TEXT | no | — | Receipt id from a call, or - to read a signed token on stdin |
| `--token` | TEXT | no | — | Signed receipt token (inline, @file, or - for stdin) |
| `--agent` | TEXT | no | — | Agent that produced the receipt (needed to fetch it from the platform) |
| `--key` | TEXT | no | — | Base64 Ed25519 public key to verify with, offline (overrides A2A_RECEIPT_VERIFYING_KEY) |
| `--api` | TEXT | no | — |  |

## `a2a receipt verify`

Check a receipt's Ed25519 signature. Exit 0 on PASS, 1 on FAIL.

```text
a2a receipt verify [OPTIONS] [RECEIPT_ID]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `RECEIPT_ID` | TEXT | no | — | Receipt id from a call, or - to read a signed token on stdin |
| `--token` | TEXT | no | — | Signed receipt token (inline, @file, or - for stdin) |
| `--agent` | TEXT | no | — | Agent that produced the receipt (needed to fetch it from the platform) |
| `--key` | TEXT | no | — | Base64 Ed25519 public key to verify with, offline (overrides A2A_RECEIPT_VERIFYING_KEY) |
| `--api` | TEXT | no | — |  |

## `a2a run`

Run the agent's HTTP server locally (used inside the container too).

```text
a2a run [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--entrypoint`, `-e` | TEXT | yes | — | module:Class |
| `--host` | TEXT | no | 0.0.0.0 |  |
| `--port` | INTEGER | no | 8000 |  |
| `--project`, `-p` | PATH | no | . |  |

## `a2a sidecar`

Run the common sidecar runtime for a compiled Agent DSL.

```text
a2a sidecar [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--dsl` | PATH | no | .a2a/agent.dsl.json | Compiled Agent DSL JSON path |
| `--worker-url` | TEXT | no | http://127.0.0.1:9001 | Native language worker base URL |
| `--host` | TEXT | no | 0.0.0.0 |  |
| `--port` | INTEGER | no | 8000 |  |

## `a2a signup`

Create or sign into a Keycloak account and cache OAuth tokens.

```text
a2a signup [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--api` | TEXT | no | https://api.a2acloud.io |  |
| `--issuer` | TEXT | no | https://auth.a2acloud.io/realms/a2acloud |  |
| `--client-id` | TEXT | no | a2acloud-cli |  |
| `--scope` | TEXT | no | openid email offline_access mcp:invoke agent:read |  |
| `--port` | INTEGER | no | 41873 |  |
| `--token` | TEXT | no | — | Use an existing Keycloak access token |
| `--open`, `--no-open` | BOOLEAN | no | True | Open the login URL |

## `a2a ssh`

Open a shell in a throwaway dev box for AGENT. The box has node, python, a2a-pack, and the agent repo already loaded, and scales to zero when you disconnect. Also usable from VS Code Remote-SSH, Cursor, scp, and rsync via the host alias it writes to ~/.ssh/config.

```text
a2a ssh [OPTIONS] [AGENT]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | no | — | Agent name (defaults to a2a.yaml in the current repo) |
| `--print` | BOOLEAN | no | False | Print connection info instead of opening a shell |
| `--tunnel` | BOOLEAN | no | False | Serve the dev box on a local TCP port for SSH tools that can't use ~/.ssh/config (GUIs, IDEs, sftp clients) |
| `--port` | INTEGER | no | 0 | Local port for --tunnel (0 = pick a free one) |
| `--json` | BOOLEAN | no | False | Print connection details as JSON (implies --print) |
| `--api` | TEXT | no | — |  |

## `a2a ssh-proxy`

Internal: stdio<->wss bridge used as an ssh ProxyCommand.

```text
a2a ssh-proxy [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — |  |
| `--api` | TEXT | no | — |  |

## `a2a test`

Run local preflight checks before deploying.

```text
a2a test [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |
| `--env-file` | PATH | no | .env.local |  |
| `--workspace` | PATH | no | — |  |
| `--invoke`, `--no-invoke` | BOOLEAN | no | False | Run a local tool call |
| `--skill` | TEXT | no | — | Tool to invoke |
| `--args-json` | TEXT | no | — | JSON object args for --invoke |

## `a2a unuse`

Remove AGENT's cached CLI stub (stored setup values are kept).

```text
a2a unuse AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Agent stub to remove |

## `a2a use`

Make AGENT's tools available as typed `a2a <agent> <tool>` commands. Fetches the agent card (tools + input schemas + consumer setup), checks the platform-side setup status, then caches the typed stub. Re-run any time to refresh.

```text
a2a use [OPTIONS] AGENT
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `AGENT` | TEXT | yes | — | Agent name from the registry (`a2a agents`) |
| `--api` | TEXT | no | — |  |
| `--no-prompt` | BOOLEAN | no | False | Deprecated: the CLI never prompts for setup. |

## `a2a validate`

Load the agent and print its Card schema. Exits non-zero on errors.

```text
a2a validate [OPTIONS]
```

| Parameter | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `--project`, `-p` | PATH | no | . |  |

## `a2a whoami`

Show the currently logged-in user.

```text
a2a whoami
```
