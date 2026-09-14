# Reference

Every `a2a` CLI command and one page per documented `a2a_pack` module,
auto-generated from the SDK source at this monorepo revision, plus
hand-written references for the project manifest and the control-plane API.

This is **not** an index of every symbol `a2a_pack` exports. Roughly half of
the public exports live in the modules below; the rest — the Agent DSL,
receipts, replay, memory, consumer setup, mail, and the meta-agent engine —
are not in the generated reference yet. Use the
[concept pages](/concepts/agents) or the source for those.

## Project manifest

- [`a2a.yaml`](/reference/a2a-yaml) — the manifest every deploy reads: identity,
  entrypoint, `expose.public`, packed frontend, runtime, and platform resources.

## SDK

- [`a2a_pack.agent`](/reference/agent) — `A2AAgent`, `@a2a.tool`, the agent model.
- [`a2a_pack.context`](/reference/context) — `RunContext`: progress, artifacts, auth, scoped access.
- [`a2a_pack.grants`](/reference/grants) — mint + verify scoped capability grants.
- [`a2a_pack.workspace`](/reference/workspace) — `WorkspaceClient`, `install_grant`.
- [`a2a_pack.runtime`](/reference/runtime) — runtime that hosts the agent.
- [`a2a_pack.card`](/reference/card) — Agent Card generation.
- [`a2a_pack.frontend`](/reference/frontend) — ship a packed frontend at `/app`.
- [`a2a_pack.a2a_client`](/reference/a2a_client) — call other agents.
- [`a2a_pack.discovery`](/reference/discovery) — marketplace discovery.
- [`a2a_pack.sandbox`](/reference/sandbox) — sandboxed microVM execution.
- [`a2a_pack.auth`](/reference/auth) — auth models.
- [`a2a_pack.deepagents`](/reference/deepagents) — DeepAgents integration.
- [`a2a_pack.serve.asgi`](/reference/serve-asgi) — the ASGI app the runtime serves.
- [`a2a_pack.mcp.http`](/reference/mcp-http) — MCP over HTTP.
- [`a2a_pack.mcp.server`](/reference/mcp-server) — MCP server implementation.

## CLI

- [`a2a`](/reference/cli) — build, package, and deploy agents.
- [`a2a_pack.cli.main`](/reference/cli-main) — the Typer app behind the CLI.
- [`a2a_pack.cli.local`](/reference/cli-local) — local project loading and workspace.
- [`a2a_pack.cli.dev_server`](/reference/cli-dev_server) — the `/_dev` console server.

## Control plane

- [Control-plane API](/reference/control-plane-api) — every FastAPI route,
  generated from the matching control-plane source revision.
- [API guide](/platform/api) — authentication, interface selection, errors,
  versioning, and the main endpoint families.
