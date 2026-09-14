# control-plane agent notes

Purpose: `control-plane/` is the A2A Cloud control-plane API and orchestration service. It is a Python 3.11 FastAPI app (`a2a-control-plane`) that owns auth/session APIs, agent registration and deployment, user files, organizations, evidence/proofs/receipts, chat/orchestrator runs, schedules, work ledgers, managed databases, and admin/control-room surfaces.

Important areas:

- `control_plane/main.py`: app entrypoint. Mounts all `/v1` routers, `/healthz`, `/metrics`, the orchestrator MCP bridge, and lifespan background loops.
- `control_plane/routes/`: FastAPI routers. Large surfaces include `agents.py`, `chat.py`, `admin.py`, `control_room.py`, `organizations.py`, `workspace_grants.py`, `agent_evidence.py`, `agent_receipts.py`, and protocol/kernel simulation routes.
- `control_plane/models.py` and `db.py`: SQLAlchemy async models/session. Startup calls `Base.metadata.create_all()` plus many Postgres `ALTER TABLE IF NOT EXISTS` statements; Alembic is a dependency but this tree currently relies on app-managed schema creation/patching.
- `control_plane/k8s.py`, `deployments.py`, `argo.py`, `scaffold.py`, `source_push_deployments.py`: agent runtime rendering, Argo/Kubernetes interactions, source deploy tracking, and generated agent scaffold content.
- `control_plane/graph_kernel/`: protocol/graph-kernel simulation primitives.
- `main_agent/`: LangGraph/deepagents-based user orchestrator. Tools cover MinIO/Gitea workspace files, sandbox shell/Python, agent discovery, handoff, and DAG execution.
- `scripts/`: `kernel_deployment_smoke.py` (simulation-only smoke used by tests).
- `docs/`: design notes and ADRs (Keycloak/OAuth, rollout drain contract, agent studio); `docs/design/capability-graph/` holds the dynamic capability graph design series (see its README).

Common commands:

- Install locally from this folder: `python -m pip install -e ".[dev]"`.
- Run tests: `python -m pytest -q` from `control-plane/`. Targeted tests are usually fastest, for example `python -m pytest -q tests/test_agent_evidence_dag.py`.
- Pytest config adds `../a2a` to `PYTHONPATH`; this service depends on the sibling `a2a` package during tests.
- Run the API after install: `a2a-control-plane` or `uvicorn control_plane.main:app --reload --port 8000`.
- Build/test in Docker: `docker build --target test -t control-plane-test .`; runtime image command is `a2a-control-plane`.
- Useful operator scripts run as modules or files from this folder, for example `python -m scripts.rollout_durability_smoke --dry-run --skip-connector-call` and `python scripts/production_receipt_smoke.py --api-url https://api.a2acloud.io --email ... --json`.

Runtime and integration points:

- Configuration is env-driven through `A2A_CP_*` in `control_plane/config.py`, plus related `A2A_*` vars for LiteLLM, MinIO, sandbox, grants, and receipts. Defaults often point at in-cluster services, so set local envs before importing/running the app.
- Primary external services: Postgres/asyncpg, Redis, Kubernetes/Knative, ArgoCD, Gitea, Keycloak/OIDC, LiteLLM, Langfuse, MinIO/S3, Qdrant, sandbox service, and managed Neon-style Postgres provisioning.
- The deployed API is a Knative Service. The control-plane service account manages agent resources in the `agents` namespace and Argo CD app/repo secrets in the infra namespace (`A2A_CP_*` settings).
- `main.py` starts background loops when enabled: agent scheduler, Gitea token sweeper, database provisioner, Redis-backed pending actions, and chat checkpointer setup.
- Orchestrator MCP lives in `control_plane/orchestrator_mcp.py`; connector jobs may be stored/queued in Redis and processed by `orchestrator_mcp_worker.py`.

Testing/deployment notes:

- `Dockerfile` has a `test` target that runs the suite inside the image; CI (`.github/workflows/ci.yml`) runs `uv sync --all-extras && uv run pytest -q`.
- Production settings worth knowing when writing manifests: `A2A_CP_UVICORN_WORKERS`, long (1800s) graceful shutdown/Knative timeouts, a Prometheus multiprocess dir, and `A2A_CP_ENABLE_SCHEDULER`/token-sweeper flags disabled in API pods (background loops run in a separate singleton).

Cautions:

- Do not hand-edit generated scaffold strings or deploy image tags casually; CI and scaffold helpers expect specific formats.
- Be careful importing app modules in scripts/tests without setting env: `settings` and SQLAlchemy engine are created at import time and may point to cluster Postgres by default.
- Avoid logging or serializing CP JWTs, Gitea tokens, LLM keys, OAuth tokens, runtime secrets, signed grants/receipts, replay logs, or private object-store keys. Several evidence/dossier paths are explicitly redaction-sensitive.
- `main_agent` file tools route `agents/<name>/...` and `repos/<name>/...` to Gitea-backed repos; other workspace paths use MinIO buckets like `user-<id>-files`.
- Screenshots, raw frames, JSONL histories, and MinIO/Qdrant mirrors are generated or evidence data. Do not bulk-format, delete, or regenerate them unless the task is specifically about those artifacts.
