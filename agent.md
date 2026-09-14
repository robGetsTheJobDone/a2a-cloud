# A2A Cloud Monorepo Agent Guide

## Purpose

This repository is the open-source A2A Cloud product: the developer SDK, the
control-plane API, the web surfaces, and first-party services. Operator
tooling (cluster manifests, provisioning scripts, CI/CD pipelines) is
intentionally not part of this tree; each deployable component ships a
`Dockerfile` and is configured through environment variables.

Start with the folder-level guides before editing a subsystem:

- `sdk/agent.md`: `a2a-pack` SDK/CLI and language sidecar SDKs.
- `control-plane/agent.md`: FastAPI control-plane and orchestration service.
- `web/agent.md`: pnpm/turbo web workspace (dashboard, docs, design system).
- `apps/agent.md`: first-party services, meta-agents, MCP gateway, e2e, sandbox runtime.

## System Shape

- `sdk/a2a-pack` is the SDK and CLI used to build, test, package, and deploy A2A agents. It defines `A2AAgent`, `@a2a.tool` (canonical namespaced form of the `tool` decorator), `RunContext`, the FastAPI runtime, packed frontends, DSL compilation, sidecars, grants, receipts, MCP, and OpenAPI support. Tools are published in the agent card's `skills` array (A2A spec vocabulary); `@skill` remains a permanent alias.
- `control-plane/` is the Python 3.11 FastAPI core for auth, agent registration/deployments, orchestration runs, files, organizations, receipts, schedules, managed databases, Gitea/Argo/Kubernetes integration, and admin/control-room APIs. It depends on the in-repo SDK through `[tool.uv.sources]`.
- `apps/` contains supporting products and services: admin console, local MCP gateway, agent builder/reviewer/studio, code-editor agent, graph agent, sandbox runtime, dev boxes, test helper, and e2e tests.
- `web/` is a separate pnpm/turbo workspace for the dashboard, docs, and shared analytics/design packages.

## Common Commands

```sh
cd sdk/a2a-pack   && uv sync --all-extras && uv run pytest -q
cd control-plane  && uv sync --all-extras && uv run pytest -q
cd web            && pnpm install && pnpm typecheck && pnpm test
cd web/apps/docs  && ../../../sdk/a2a-pack/.venv/bin/python scripts/check.py --repo-root ../../..
```

Language SDKs: `cd sdk/a2a-pack/typescript && npm ci && npm test`; Go and Rust
packages under `sdk/a2a-pack/go` and `sdk/a2a-pack/rust`.

## Conventions

- Python: `ruff` for lint/format; tests live next to each package under `tests/`.
- TypeScript: `pnpm lint` and `pnpm typecheck` must pass; dashboard tests use vitest, docs/landing use `node --test`.
- Generated files (`web/apps/docs/content/reference/*`, `sdk/a2a-pack/a2a_pack/typescript/*`) are produced by scripts and guarded by tests; regenerate rather than hand-edit.
- Keep secrets, private hostnames, and operator-specific configuration out of the tree.

## External Services

The control plane coordinates Gitea, Argo CD, Kubernetes/Knative, Redis,
Postgres, MinIO/S3, Keycloak, LiteLLM, Langfuse, Qdrant, the sandbox runtime,
and managed Neon-style databases. All endpoints are env-driven (`A2A_CP_*`,
`A2A_*`); defaults point at in-cluster service names.
