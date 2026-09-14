# apps/

This directory contains the deployable app and agent projects that sit around
the A2A Cloud control plane and web frontend. Most projects are intentionally
self-contained because they are mirrored to per-app Gitea repos and built by
their own workflows.

## Projects

- `a2a-mcp`: TypeScript/npm package `a2amcp`, a local stdio MCP gateway for
  exposing deployed A2A agents to editor MCP clients. It stores credentials in
  `~/.a2a/credentials.json`, enabled-agent config in `~/.a2a/mcp.json`, talks to
  the control plane, and is distinct from hosted `/mcp` and `/connector-mcp`
  endpoints.
- `admin`: Next.js admin console (`admin.<platform-domain>`). Auth is Keycloak OIDC
  plus server-side control-plane verification; admin API calls are proxied with
  `A2A_CP_ADMIN_TOKEN` so the token never reaches the browser.
- `agent-builder`: Python `a2a-pack` meta-agent that uses DeepAgents/LangGraph
  to scaffold, sandbox-test, and deploy new agents from natural language. It
  requires an orchestrator-provided workspace grant, `ctx.llm`, and `ctx.cp_jwt`.
- `agent-reviewer`: Python read-only audit agent. It mints a short-lived
  read-scoped Gitea token through the control plane, reviews an agent repo with
  bundled skills, runs sandbox checks, returns a typed report, and releases the
  token in `finally`.
- `agent-studio`: Python coordinator/meta-agent for the build-review-improve
  flow. It delegates to `agent-builder.build`, `agent-reviewer.review`, and
  `code-editor-agent.turn`; generated agents are private by default.
- `code-editor-agent`: Python OpenHarness + CodeGraph editor agent. Default
  shared mode edits opted-in managed agent repos by minting short-lived
  write-scoped Gitea tokens, locking a workspace, refreshing CodeGraph, running
  OpenHarness, and pushing changes. Local wrapper mode is enabled with
  `A2A_CODE_EDITOR_MODE=local`.
- `graph-agent`: Public Python chart agent. It uses caller-provided LLM
  credentials, a workspace grant, DeepAgents/LangGraph, and the sandbox runtime
  to read data and write `outputs/*.png`.
- `test-helper`: Small public Python A2A agent used by e2e tests. Skills cover
  fast echo and deliberate `ctx.request_scope()` calls.
- `sandbox-runtime`: Host-side FastAPI wrapper around microsandbox, FUSE, and
  MinIO workspaces. Provides one-shot `/v1/run_shell` and `/v1/run_python` plus
  persistent `/v1/sandboxes` endpoints.
- `e2e`: Black-box pytest suite for the public platform API. It intentionally
  imports no SDK or control-plane internals.

## Runtimes and Commands

- Node apps use npm lockfiles. Common commands:
  - `cd apps/a2a-mcp && npm ci && npm test && npm run build`
  - `cd apps/admin && npm install && npm run dev` or `npm run build`
    or `npm run worker`; `npm test` builds first.
- Python services target Python 3.11+ or 3.12 images. Install their local deps
  from the app directory with a venv and the listed `requirements.txt` or
  `pyproject.toml` extras. If you need local SDK sources, this checkout has
  `sdk/a2a-pack`; some older README snippets still mention `../a2a`.
- Black-box e2e: `cd apps/e2e && A2A_E2E_API_URL=http://127.0.0.1:8000 uv run pytest -q`.
  Code-editor wrappers: `apps/code-editor-agent/scripts/generate-code-editor-agents.py --list`.
- App-specific entrypoints:
  - A2A Python agents run as `a2a run --entrypoint agent:<ClassName>` on port
    8000 in their Dockerfiles.
  - `sandbox-runtime` runs `sandbox_runtime.service:app` on `PORT` or 8000.
  - `a2amcp` with no command starts the stdio MCP gateway.

## Integration Points

- Control plane: admin proxies `/v1/admin/*`; builder deploys through CP;
  reviewer and code editor mint scoped Gitea tokens through CP; Agent Studio
  uses CP helper endpoints for opt-in, card refresh, deployment status, and
  freshness checks.
- Keycloak/OAuth: `a2a-mcp login` uses Keycloak Authorization Code + PKCE;
  admin uses Keycloak OIDC and creates a signed local admin session only after
  CP confirms admin status.
- Gitea: first-party agents are mirrored to per-app repos. Reviewer reads repos;
  code editor writes managed source repos; workflows push image tag bumps back
  to the app repo.
- Workspace and MinIO: builder, graph-agent, sandbox-runtime, and generated
  agents rely on scoped workspace grants. Do not bypass grant checks.
- LLM routing: platform agents should use forwarded `ctx.llm` credentials.
  Do not read provider keys such as `OPENAI_API_KEY` or `A2A_LITELLM_KEY`
  directly in new agent code unless you are editing infrastructure config.
- Sandbox: graph-agent, builder, reviewer, and the orchestrator call
  `sandbox.sandbox.svc.cluster.local:8000`; production sandbox-runtime is a
  privileged DaemonSet with `/dev/kvm` and `/dev/fuse` host mounts.

## Testing and Deployment

- `apps/e2e` targets `A2A_E2E_API_URL` (or `A2A_API_URL`), default
  `http://127.0.0.1:8000`. Use
  `-m "not slow"` to skip the chart-render flow.
- Python unit tests live beside the agents/services under `tests/`; common
  commands are `python -m pytest` after installing the package and deps.
  `agent-studio` also has `scripts/agent_studio_harness.py` for remote
  orchestrator checks.
- Each deployable app ships a `Dockerfile`; Kubernetes manifests and CI/CD
  pipelines are operator-specific and live outside this repository.
- `a2a-mcp` has an npm publish workflow on `v*` tags; the tag version must match
  `package.json`.
- `sandbox-runtime` deployment is security-sensitive because it is privileged
  and host-mounted. Treat auth (`A2A_SANDBOX_TOKEN`) and grant verification keys
  as mandatory in shared environments.

## Cautions

- Some agents only work through the platform orchestrator. Local MCP or direct
  `a2a run` calls will not provide workspace grants, `ctx.cp_jwt`, or token
  minting.
- `agent-builder` and `code-editor-agent` can write source or deploy changes;
  keep changes bounded to the target repo/workspace and preserve slug
  validation.
- `code-editor-agent` deliberately blocks dangerous OpenHarness operations such
  as force pushes and hard resets in its generated settings. Do not relax that
  casually.
- `.a2a/agent.dsl.json` files are generated
  artifacts. Regenerate them through the package commands instead of hand
  editing unless the change is explicitly about the generated output.
- E2E tests create real users and may hit production unless
  `A2A_E2E_API_URL` is set.
