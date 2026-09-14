# Hosted agent wake-up policy

Hosted user agents run as Knative Services with `min-scale: 0`, so an idle
agent has **zero pods**. Any HTTP call to `http://{name}.agents.svc.cluster.local`
(agent card, `/healthz`, `/invoke`, `/mcp`) cold-starts it. The platform must
only do that for real work — never as a side effect of a passive read.

## Allowed to wake an agent (execution + explicit intent)

| Path | Why |
|------|-----|
| Skill invocation / handoff `POST /invoke/{skill}` | The user is actually running the agent. |
| Proof / trial **runs** (the execution `POST`) | A run is real work. |
| Owner/operator explicit refresh — `GET /v1/agents/{name}?refresh=true` | Owner deliberately re-reads the live card (e.g. waiting out a redeploy). |
| Deploy verification, **once the runtime is reconciled** | A single bounded card/skills probe at the end of a deploy. See `deployments.verify_agent_deployment`. |

## Must NOT wake an agent (passive reads)

| Path | Source served |
|------|---------------|
| `GET /v1/agents`, `/v1/agents/mine` | stored `Agent.card` (DB) |
| `GET /v1/agents/{name}` (no `refresh`) | stored `Agent.card` |
| `GET /v1/public/agents` + public detail pages | stored `Agent.card` |
| Main-agent discovery (`discover_agent`, `list_my_agents`) | the registry endpoints above (cached) |
| Proof / trial **setup** (skill selection) | cached card via `_refresh_cards_inplace` (no-wake default) |
| Deployment status reads for a **live/terminal** deploy | stored Argo/Knative/runtime status, never `_safe_agent_http` |

## How it is enforced in code

- `routes/agents._refresh_cards_inplace(..., wake_hosted=False)` — default skips
  the live card GET for hosted agents and serves the stored card. External /
  imported agents are still refreshed (their card lives off-cluster, so the
  fetch never wakes one of our pods). Only `wake_hosted=True` (owner refresh)
  cold-starts a hosted agent.
- `deployments.verify_agent_deployment` — only calls `_safe_agent_http` (the
  card/skills probe) once Argo + the Knative runtime confirm the expected image
  is reconciled and `Ready`. A `Ready` Knative Service with zero idle pods is
  treated as healthy. Live deploys are not re-verified on read
  (`_deployment_out` gates on `ACTIVE_DEPLOY_STATUSES`).
- `card_cache` (Redis) — warmed on every version deploy and on explicit/external
  refresh, then read cache-first (e.g. the handoff `get_agent_card` hook) so the
  card is served without a DB hit or a pod wake.

## Card freshness

`Agent.card` (DB, durable) and the Redis cache are written at the moments an
agent is awake or changing — deploy, import, explicit refresh — never on a
passive read. The deploy-time write reflects what the pod actually serves, not
just the declared `a2a.yaml` card.

## Always-on opt-out

Platform-critical agents that must never scale to zero are listed in the
control-plane setting `always_on_agents` (comma-separated names); they are
stamped with `min-scale >= 1`. See `k8s.agent_min_scale`.
