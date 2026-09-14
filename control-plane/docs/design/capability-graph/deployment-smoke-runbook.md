# Dynamic Capability Graph Deployment Smoke Runbook

Source epic: E16 Deployment Smoke and Production Kernel Verification.
Prerequisite design context: `docs/emergent-readiness-matrix.md`.

## What This Verifies

The smoke checks prove the deployed kernel creation surface is reachable without
granting live mutation authority:

- control-plane health endpoint responds
- authenticated user-created kernel templates are available
- a bounded user kernel simulation can run from a starter template
- the run can replay deterministically
- the new run is listed in durable run history
- optional agent protocol scenario, registry, runtime-readiness, and evidence
  timeline endpoints respond for an owned agent
- optional dashboard URL serves the kernel-capable app shell

The script never sends active apply grants. It also fails if a kernel run
response carries active apply material.

## Control-Plane Smoke

Public health-only smoke:

```bash
cd control-plane
.venv/bin/python scripts/kernel_deployment_smoke.py
```

Authenticated production smoke:

```bash
cd control-plane
A2A_SMOKE_BASE_URL="https://api.a2acloud.io" \
A2A_SMOKE_TOKEN="$TOKEN" \
A2A_SMOKE_REQUIRE_AUTH=1 \
.venv/bin/python scripts/kernel_deployment_smoke.py
```

Full smoke with an owned agent and dashboard shell check:

```bash
cd control-plane
A2A_SMOKE_BASE_URL="https://api.a2acloud.io" \
A2A_SMOKE_DASHBOARD_URL="https://a2acloud.io" \
A2A_SMOKE_TOKEN="$TOKEN" \
A2A_SMOKE_AGENT_NAME="my-agent" \
A2A_SMOKE_REQUIRE_AUTH=1 \
.venv/bin/python scripts/kernel_deployment_smoke.py
```

Environment variables:

- `A2A_SMOKE_BASE_URL`: control-plane base URL. Defaults to `http://localhost:8000`.
- `A2A_SMOKE_TOKEN`: bearer token for authenticated checks.
- `A2A_TOKEN`: fallback bearer token alias.
- `A2A_SMOKE_AGENT_NAME`: optional owned agent name for agent-scoped checks.
- `A2A_SMOKE_AGENT`: fallback agent name alias.
- `A2A_SMOKE_DASHBOARD_URL`: optional dashboard URL.
- `A2A_DASHBOARD_URL`: fallback dashboard URL alias.
- `A2A_SMOKE_TIMEOUT_SECONDS`: request timeout. Defaults to `20`.
- `A2A_SMOKE_REQUIRE_AUTH`: when true, missing auth is a failure instead of a skip.

Expected output is a JSON summary with `failed: 0`. Any failed check exits with
status code `1`.

## Dashboard Smoke

The dashboard smoke verifies the source wiring and production build artifacts:

```bash
cd web/apps/dashboard
npm run build
npm run smoke:kernel
```

Optional deployed dashboard check:

```bash
cd web/apps/dashboard
A2A_DASHBOARD_SMOKE_URL="https://a2acloud.io" npm run smoke:kernel
```

## Common Failures

- `user kernel simulations` skipped: no token was provided. Set `A2A_SMOKE_TOKEN`
  and `A2A_SMOKE_REQUIRE_AUTH=1` for production verification.
- `user kernel templates` failed: deployed control plane does not expose the
  expected starter templates or auth is pointed at the wrong tenant.
- `user kernel run` failed: the bounded simulation runner is unavailable, the
  token cannot create user work jobs, or the response carried forbidden active
  apply material.
- `user kernel replay` failed: trace replay drifted from the original summary.
- `agent runtime readiness denial` failed: incomplete gates unexpectedly allowed
  active runtime mutation. Treat this as a production blocker.
- Dashboard smoke cannot find kernel strings in `dist`: rebuild the dashboard
  and verify the deployed bundle includes the kernel simulation and chat evidence
  modules.

## Meta-Agent Production Smoke

Run the production smoke after any deploy that touches meta-agent orchestration,
source-push deployment, A2A Pack runtime packaging, deploy review, or platform
LLM grants:

```bash
cd control-plane
A2A_SMOKE_BEARER_TOKEN="$USER_TOKEN" \
A2A_SMOKE_OTHER_BEARER_TOKEN="$SECONDARY_USER_TOKEN" \
A2A_SMOKE_ADMIN_TOKEN="$ADMIN_TOKEN" \
KUBECONFIG="$HOME/.kube/a2a-remote" \
.venv/bin/python scripts/production_smoke.py --json
```

The default production smoke now includes a meta-agent contract section. It
checks:

- `agent-builder` card exposes `build`
- `agent-studio` card exposes `create_agent`
- `code-editor-agent` card exposes `turn` and `status`
- `agent-reviewer` card exposes `review`
- the latest retained deterministic reference agent
  `studio-final-fix-20260608102040` exposes `summarize_policy` and
  `validate_controls`

The same run also checks card availability, Knative readiness, DB convergence,
tenant isolation, external client integration links, admin billing/token
surfaces, and log redaction. A passing run means the first-class meta agents are
discoverable, serving their expected skill contracts, and reflected as healthy
in control-plane state.

Use `--skip-meta-agent-contracts` only when diagnosing unrelated API or tenant
isolation failures. Do not use that flag for release sign-off.

Focused meta-agent contract smoke:

```bash
cd control-plane
.venv/bin/python scripts/production_smoke.py \
  --only-meta-agent-contracts \
  --api-url "https://api.a2acloud.io" \
  --json
```

This focused mode is intentionally tokenless and non-mutating. It is the fast
release gate for card/skill drift in the first-class meta agents.

For a destructive full-chain proof, keep using a disposable generated agent:
build or update a small deterministic no-LLM agent, push one source commit,
then verify the resulting `AgentDeployment` reaches `live`, the deploy review
passes, the public card version matches `agents.version`, and direct no-LLM
invocation works without caller LLM credentials. Keep only the latest verified
reference agent and remove older smoke agents, repos, Argo apps, Knative
services, and registry tags after the run.

For spend tracking, run one tiny authenticated chat after a control-plane
rollout and compare the resulting `llm_usage_events` row with LiteLLM
`/spend/logs/v2`. The row should have non-zero `cost_usd` plus
`litellm_request_id` and `litellm_reconciled` metadata for the same thread.

## Rollback Guidance

If any authenticated kernel check fails after deployment, keep active runtime
mutation disabled and roll back the control-plane deployment first. Dashboard
failures can be rolled back independently unless the API smoke also fails.
