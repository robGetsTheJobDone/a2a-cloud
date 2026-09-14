# Control-Plane Rollout Drain Contract

Date: 2026-06-02

Epic: `19965c7a-e580-46aa-b02f-fb7257259b32`

Status: Implemented through P6.

## Decision

Control-plane request serving must be separated from singleton background work
before Knative or any overlapping rollout model is used for the API. The API
process may own web dependencies and request-scoped work. It must not own
platform singleton loops whose side effects duplicate across revisions.

This contract distinguishes three categories:

- active request: safe to drain through pod termination grace
- durable job: must continue outside the accepting API pod
- singleton worker: must run once per platform, not once per API revision

## Path Inventory

| Path | Current failure mode | Target behavior | Budget |
|---|---|---|---|
| Dashboard `/v1/chat` SSE | Stream is tied to the serving pod. A rollout can cut the browser stream. | Keep as active request until Knative migration; old revision drains within request timeout. Client can reload the persisted thread if stream drops. | 15 minutes initial, aligned with orchestrator and agent runtime limits. |
| Connector MCP `chat` | Initial response can detach. | Connector chat jobs are enqueued to Redis and executed by `connector-mcp-worker`, outside the accepting API pod. | 15 minutes. |
| Connector MCP `chat_result` / `resume_interaction` | Poll/resume must survive API revision changes. | Redis-backed job and pending-action state let any API revision poll/resume while the worker continues execution. | 15 minutes. |
| Agent schedules | `main.py` starts the schedule loop inside the API lifespan. Overlapping API revisions duplicate schedule claims and termination cancels active schedule tasks. | P1 moves the schedule loop to `control-plane-workers`; API sets scheduler disabled. DB claim logic remains the singleton guard. | 1900 seconds. |
| Gitea token sweeper | `main.py` starts the sweeper inside the API lifespan. Overlapping API revisions duplicate sweeps. | P1 moves sweeper to `control-plane-workers`; API sets sweeper disabled. Revoke operation remains idempotent. | 30 second sweep interval. |
| Langfuse provisioner | Runs as a sidecar in the API pod. API overlap duplicates provisioner side effects. | P1 moves it to `control-plane-workers`. Existing work-job/idempotent provisioning remains the guard. | Continuous loop, 10 second interval. |
| Gitea provisioner | Runs as a sidecar in the API pod. API overlap duplicates repository/user provisioning work. | P1 moves it to `control-plane-workers`. Existing idempotent Gitea operations remain the guard. | Continuous loop, 10 second interval. |
| Source-push deploy worker | Runs as a sidecar in the API pod. API overlap can duplicate deployment worker attempts. | P1 moves it to `control-plane-workers`; `WorkJob` claiming remains the singleton guard. | Continuous loop, 5 second interval. |
| Proof/trial requests | Request-scoped HTTP work can be cut by API rollout. | Keep as active request until Knative migration; later smoke tests must cover reload/status behavior. | Bounded by route/runtime timeouts. |
| Agent-builder handoff | Builds can wait 900-1200 seconds. | `agent-builder` runs as an always-on Knative Service with 1800 second request and response-start timeouts. | 900-1200 seconds. |

## P1 Worker Ownership

`control-plane` Deployment:

- serves HTTP API traffic
- owns checkpointer and pending-action stores
- disables API-local scheduler and Gitea token sweeper
- may overlap during RollingUpdate without duplicating platform loops

`control-plane-workers` Deployment:

- `schedule-worker`
- `gitea-token-sweeper`
- `langfuse-provisioner`
- `gitea-provisioner`
- `source-push-deploy-worker`
- `connector-mcp-worker`

The worker Deployment is intentionally singleton and uses `Recreate`; detached
connector execution is durable because the accepted job is queued and persisted
before the worker starts it.

## Recovery Model

- Dashboard chat: reload the thread if the stream is severed.
- Connector chat: poll by job id; execution is owned by `connector-mcp-worker`.
- Approval/input interrupts: resume by job id through Redis-backed
  pending-action state.
- Schedules: work jobs record completion/error and can be inspected from the
  control room.
- Source-push deployments: work jobs and deployment timeline expose queued,
  blocked, failed, and complete states.

## Verification

Verification:

```bash
.venv/bin/python -m py_compile control_plane/schedule_worker.py control_plane/gitea_token_sweeper.py
KUBECONFIG=$HOME/.kube/a2a-remote kubectl apply --dry-run=server -f deploy/
python -m scripts.rollout_durability_smoke --dry-run --skip-connector-call
```

Production verification after rollout:

```bash
KUBECONFIG=$HOME/.kube/a2a-remote kubectl -n control-plane get deploy,pods
curl -fsS https://api.a2acloud.io/healthz
```

See `control-plane-rollout-durability-runbook.md` for the full operator smoke,
rollback, and log queries.
