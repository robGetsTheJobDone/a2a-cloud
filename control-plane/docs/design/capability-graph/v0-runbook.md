# Dynamic Capability Graph V0 Runbook

Date: 2026-06-02

Status: production rollout and operator verification for epic
`83e22fce-2e8b-4123-a9f3-c342f6005bf2`.

## What Shipped

V0 is a read-only Agent Evidence DAG projection over existing control-plane
rows. It explains an agent through deploys, reviews, proofs, grants, handoffs,
DAG runs, receipts, sessions, work jobs, work events, LLM usage, code-editor
correlation, dossier summaries, and evidence timeline lanes.

Public callers only receive the public-safe root view. Owners receive
owner-scoped evidence with secrets still redacted. Operator view remains
credential-redacted.

V0 did not ship a graph database, authoritative graph tables, autonomous
rewrites, autonomous routing, deletion engines, collective-memory propagation,
evolutionary breeding/culling, or marketplace autopoiesis.

## API Checks

Use `https://api.a2acloud.io` in production. Replace `<agent>` with an owned
fixture/demo agent when checking owner evidence. Use a bearer token from the
dashboard login or `/v1/auth/login`.

Health:

```bash
curl -fsS https://api.a2acloud.io/healthz
```

Public-safe root evidence for a public agent:

```bash
curl -fsS "https://api.a2acloud.io/v1/agents/<public-agent>/evidence-dag?include_warnings=true"
```

Owner dossier:

```bash
curl -fsS \
  -H "Authorization: Bearer $A2A_JWT" \
  "https://api.a2acloud.io/v1/agents/<agent>/dossier?include_warnings=true"
```

Owner timeline filtered to quality events:

```bash
curl -fsS \
  -H "Authorization: Bearer $A2A_JWT" \
  "https://api.a2acloud.io/v1/agents/<agent>/evidence-timeline?lane=quality&limit=25"
```

Owner timeline filtered to a source version:

```bash
curl -fsS \
  -H "Authorization: Bearer $A2A_JWT" \
  "https://api.a2acloud.io/v1/agents/<agent>/evidence-timeline?head_sha=<sha>&limit=25"
```

Expected response shape:

- `schema_version` is `1`.
- `redaction.omitted_classes` includes secret-bearing and private-payload
  classes.
- `watermark.projection` is `read_only_v0`.
- Owner dossiers contain `current_version`, `authority_summary`,
  `quality_summary`, `mutation_summary`, `risk_summary`, and `graph_refs`.
- Timeline items have one of these lanes: `version`, `authority`, `mutation`,
  `quality`, `process`, `cost`, or `control`.

## Dashboard Check

Open `https://app.a2acloud.io`, sign in, then go to My Agents. Each agent card
has an evidence action that opens the dossier/timeline panel.

Expected UI behavior:

- Dossier header shows live version, latest deploy/review/proof, warning count,
  authority count, proof count, and mutation count.
- Lane filter changes the timeline without exposing raw payloads.
- Empty agents show a valid evidence panel with warnings instead of a broken
  state.
- Public-only evidence remains sparse and redacted.

## Deployment Flow

Commit and push the control-plane and dashboard changes. Argo CD reconciles the
applications from the source repository. Check remote state:

```bash
KUBECONFIG=$HOME/.kube/a2a-remote kubectl -n control-plane rollout status deploy/control-plane --timeout=180s
KUBECONFIG=$HOME/.kube/a2a-remote kubectl -n dashboard rollout status deploy/dashboard --timeout=180s
```

If Argo needs a nudge, trigger a sync of the affected Argo CD applications.

## Local Verification

Control-plane:

```bash
cd apps/control-plane
.venv/bin/python -m pytest tests/test_agent_evidence_dag.py tests/test_dynamic_graph_sim.py tests/test_self_improvement_proposals.py
PYTHONPATH=../a2a .venv/bin/python - <<'PY'
import os
os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
from control_plane.main import app
paths = {getattr(route, "path", "") for route in app.routes}
required = {
    "/v1/agents/{name}/evidence-dag",
    "/v1/agents/{name}/dossier",
    "/v1/agents/{name}/evidence-timeline",
}
missing = required - paths
raise SystemExit(f"missing routes: {sorted(missing)}" if missing else 0)
PY
```

Dashboard:

```bash
cd apps/dashboard
npm run build
```

Agent Studio correlation:

```bash
cd apps/agent-studio
pytest tests/test_agent.py
```

## Redaction Guarantees

Default responses must not serialize:

- signed grants or receipts
- CP JWTs
- Gitea tokens
- LLM provider keys
- raw secret values
- signed session tokens
- raw replay logs
- private payloads
- unbounded stdout/stderr
- private object-store keys

Safe output is limited to ids, refs, statuses, timestamps, bounded summaries,
counts, source table names, row ids, join rules, confidence levels, warning
codes, and redacted payload metadata.

## Known Degraded Behavior

V0 prefers strict joins. When a strict key is missing, it may emit inferred or
warning-only evidence rather than inventing causality.

Common warning paths:

- `missing_review_runs`: deployment exists without review rows.
- `missing_proof_runs`: deployment exists without proof rows.
- `receipt_session_missing`: receipt has no matching session.
- `missing_code_editor_correlation_fields`: patch job lacks finding/source refs.
- `projection_skeleton`: owned agent has no evidence rows yet.
- `public_projection_limited`: anonymous public view is intentionally sparse.

## Rollback

Rollback is normal Gitea/Argo rollback:

```bash
cd apps/control-plane
git log --oneline -5
git revert <bad-commit>
git push

cd ../dashboard
git log --oneline -5
git revert <bad-commit>
git push
```

Then verify:

```bash
curl -fsS https://api.a2acloud.io/healthz
curl -fsSI https://app.a2acloud.io/
```

## Follow-Up Gaps

These are intentionally outside v0 and are captured in the emergent follow-up
epic:

- materialized graph projections and first-class graph tables
- first-class mutation ids and mutation run persistence
- protocol-pack inheritance/runtime behavior
- crystallization and evaporation
- collective memory/stigmergy
- evolutionary breeding/culling
- adversarial reviewer loops
- autopoietic marketplace

Every follow-up must keep owner grant ceilings, review/proof gates, canary,
rollback, kill-switch, budget/TTL/depth limits, dossier visibility, and
operator disable controls before any active behavior ships.
