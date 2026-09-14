# Self-hosting A2A Cloud

This repository contains the product: SDK, control plane, dashboard, docs, admin console, meta-agents, and the
sandbox runtime. It does not contain a turnkey cluster installer. This page lists what each component needs so you
can run it on your own Kubernetes cluster, a single k3s box, or, for the control plane and web apps alone, plain
Docker.

## Components

| Component | Image / run | Talks to |
|---|---|---|
| Control plane | `control-plane/Dockerfile`, entrypoint `a2a-control-plane` (uvicorn) | Postgres, Redis, OIDC provider, Git server, Kubernetes API, S3 store, LiteLLM |
| Dashboard | `web/apps/dashboard/Dockerfile` (nginx, proxies `/v1/` to the control plane) | Control plane |
| Docs | `web/apps/docs/Dockerfile` (Next.js) | static |
| Admin console | `apps/admin/Dockerfile` (Next.js) | Control plane admin API, OIDC |
| Sandbox runtime | `apps/sandbox-runtime/Dockerfile` (privileged; microsandbox + FUSE) | S3 store |
| Dev box | `apps/devbox/Dockerfile` | Control plane |
| Meta-agents | `apps/agent-builder`, `agent-reviewer`, `code-editor-agent` Dockerfiles; `agent-studio` deploys as an agent | Control plane, Git server, sandbox runtime |
| Agents you deploy | Built by the control plane from `sdk/a2a-pack/Dockerfile` / the project template | Runtime cluster |

## Control plane configuration

Settings are read from environment variables prefixed `A2A_CP_` (or a `.env` file). The full list with defaults is
`control-plane/control_plane/config.py`.

Start with one variable. Every public hostname derives from it, and each derived value can still be overridden:

| Variable | Default | Purpose |
|---|---|---|
| `A2A_CP_PLATFORM_DOMAIN` | `example.com` | Root domain. Derives `api.`, `app.`, `docs.`, `registry.`, `auth.`, `langfuse.`, `mail.`, `agents.` hosts and the shared cookie domain |
| `A2A_CP_PUBLIC_CP_URL` | `https://api.<domain>` | Public URL of this control plane |
| `A2A_CP_DASHBOARD_URL` | `https://app.<domain>` | Dashboard URL, used in redirects and emails |
| `A2A_CP_DOCS_URL` | `https://docs.<domain>` | Docs links emitted into scaffolds and errors |
| `A2A_CP_INGRESS_HOST_TEMPLATE` | `{name}.<domain>` | Hostname template for deployed agents |
| `A2A_CP_IMAGE_REGISTRY` | `registry.<domain>` | Container registry for agent images (`<registry>/agents/<name>`) and base images |
| `A2A_CP_KEYCLOAK_REALM`, `A2A_CP_KEYCLOAK_ISSUER`, `A2A_CP_KEYCLOAK_JWKS_URL` | realm `a2a`, `https://auth.<domain>/realms/<realm>` | OIDC issuer; set `A2A_CP_KEYCLOAK_ENABLED=false` for password auth in local development |
| `A2A_CP_AGENT_MAIL_DOMAIN`, `A2A_CP_MAILU_API_URL` | `agents.<domain>`, `https://mail.<domain>/api/v1` | Per-agent inboxes (optional) |
| `A2A_CP_LANGFUSE_BASE_URL` | `https://langfuse.<domain>` | Optional tracing; `A2A_CP_LANGFUSE_PROVISIONING_ENABLED=false` to skip |

Then the infrastructure it needs:

| Variable | Purpose |
|---|---|
| `A2A_CP_DATABASE_URL` | Postgres, `postgresql+asyncpg://…` |
| `A2A_CP_REDIS_URL` | Redis for queues and rate limits |
| `A2A_CP_JWT_SECRET` | Session signing secret (the default is a placeholder) |
| `A2A_CP_KUBECONFIG` | Kubeconfig for the runtime cluster (in-cluster config when unset) |
| `A2A_LITELLM_KEY`, `A2A_LITELLM_MODEL` | LiteLLM gateway used for platform-provisioned models |
| `A2A_CP_GITEA_*` | Git server provisioning, webhooks, and OAuth linking |
| `A2A_CP_REGISTRY_ACTIONS_*` | Credentials the build pipeline uses to push agent images |

Search `config.py` for `sandbox`, `minio`, `neon`, `mail` for the remaining integrations.

## CLI and web configuration

| Component | Variable | Default |
|---|---|---|
| `a2a` CLI | `A2A_API_URL` | saved credentials, else the public hosted instance |
| `a2a` CLI | `A2A_PLATFORM_DOMAIN`, `A2A_DOCS_URL`, `A2A_DASHBOARD_URL`, `A2A_REGISTRY_HOST` | derived from the API URL host |
| Dashboard | `VITE_A2A_PLATFORM_DOMAIN` | derived from `window.location` (`app.<domain>` → `<domain>`) |
| Dashboard container | `POSTHOG_API_URL`, `POSTHOG_KEY` | unset (analytics off) |
| Admin console | `A2A_CP_URL`, `A2A_CP_ADMIN_TOKEN`, `A2A_PLATFORM_DOMAIN` | see `apps/admin/README.md` |
| `a2amcp` | `A2A_API_URL` | saved credentials, else the public hosted instance |
| e2e tests | `A2A_API_URL` | `http://127.0.0.1:8000` |

## Runtime cluster expectations

Deployed agents run as Knative services reconciled by Argo CD from a Git repository the control plane pushes to.
The control plane expects:

- Kubernetes with Knative Serving.
- Argo CD watching the agent source repositories.
- A container registry the cluster can pull from (`A2A_CP_IMAGE_REGISTRY`, credentials in `A2A_CP_REGISTRY_ACTIONS_*`).
- An S3-compatible object store for workspaces and files (MinIO works).
- A network policy that blocks agent pods from reaching cluster-internal and metadata addresses. The sandbox runtime
  ships an example egress list in its tests.
- Optional: a Neon-style scale-to-zero Postgres operator for per-agent databases (`A2A_CP_DATABASE_OPERATOR_*`).

## Local development without a cluster

```bash
# SDK: build and run an agent locally
cd sdk/a2a-pack && uv sync --all-extras
uv run a2a init demo && cd demo && uv run a2a dev --local

# Control plane against a local Postgres + Redis, password auth
cd control-plane && uv sync --all-extras
A2A_CP_PLATFORM_DOMAIN=localhost \
A2A_CP_PUBLIC_CP_URL=http://localhost:8000 \
A2A_CP_DASHBOARD_URL=http://localhost:5173 \
A2A_CP_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/a2a \
A2A_CP_REDIS_URL=redis://localhost:6379/0 \
A2A_CP_KEYCLOAK_ENABLED=false \
uv run a2a-control-plane

# Dashboard dev server, proxying to the control plane
cd web && pnpm install && pnpm dev:dashboard
```

Point the CLI at your control plane with `A2A_API_URL=http://localhost:8000` (default is the hosted service).

## Security notes

- Rotate `A2A_CP_JWT_SECRET` and every provider key before exposing anything publicly.
- Receipt and grant keys are Ed25519 keypairs generated by the control plane; back them up with the database.
- The sandbox runtime is privileged by design. Run it only on nodes dedicated to agent execution.
