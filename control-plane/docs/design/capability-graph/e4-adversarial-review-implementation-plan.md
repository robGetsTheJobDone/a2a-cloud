# Dynamic Capability Graph E4 Adversarial Review Implementation Plan

Date: 2026-06-03

Status: implemented and under verification for
`c5f467fa-7cef-4d33-9401-d5e40dfa5244`.

Source anchor: `docs/emergent-readiness-matrix.md`,
section `E4 Adversarial Reviewer Loop`.

## Decision

Build E4 as a bounded, owner-started reviewer-loop process over existing
control-plane rails. Do not add a graph database, autonomous mutation runtime, or
direct-apply path.

The implementation uses:

- `apps/agent-reviewer/agent.py` as the read-only reviewer.
- `control_plane/self_improvement_proposals.py` for proposal-only policy gates.
- `WorkJob` and `WorkEvent` for loop process evidence.
- `AgentReviewRun` and stable finding hashes for quality evidence.
- `control_plane/evidence_dag.py` for dossier and timeline visibility.
- dashboard evidence, Control Room, and chat graph surfaces for owner-visible
  inspection.

## Gate Mapping

| Matrix gate | Implementation surface |
|---|---|
| reviewer emits findings only | `apps/agent-reviewer/agent.py`, `apps/agent-reviewer/tests/test_agent.py` |
| no direct apply authority | `control_plane/self_improvement_proposals.py`, reviewer-loop event validation |
| narrowed code-editor authority | `SelfImprovementProposal` refs only in v0; no code-editor call from reviewer loop |
| critical finding freezes promotion | `promotion_frozen` WorkEvent, dossier risk summary, control timeline |
| loop budget, TTL, max iterations | reviewer-loop `WorkJob.metadata_json`, event validation, simulator scenarios |
| kill-switch | `POST /v1/agents/{name}/review-loops/{job_id}/stop` emits `loop_killed` |
| proposed fixes enter SelfImprovementProposal | `fix_proposed` events store proposal refs, never applied mutations |
| dossier/timeline visibility | `evidence_dag.py`, `MyAgents.tsx`, `ControlRoom.tsx`, `Chat.tsx` |

## Initial API

- `POST /v1/agents/{name}/review-loops`
- `GET /v1/agents/{name}/review-loops`
- `GET /v1/agents/{name}/review-loops/{job_id}`
- `POST /v1/agents/{name}/review-loops/{job_id}/events`
- `POST /v1/agents/{name}/review-loops/{job_id}/stop`

The process row uses `WorkJob.kind = "adversarial_review_loop"` and
`metadata_json.template_ref = "adversarial_review_loop@v1"`.

Allowed loop events are:

- `reviewer_started`
- `finding_emitted`
- `fix_proposed`
- `loop_completed`
- `loop_failed`

The route rejects truthy direct-apply or mutation-authority fields, rejects
direct mutation grants, requires `finding_emitted` to include findings, requires
`fix_proposed` to include a `SelfImprovementProposal` ref, kills loops that
exceed budget/TTL/max iterations, and emits `promotion_frozen` automatically
when a critical finding is recorded.

## Files

- `control_plane/routes/adversarial_review_loops.py`
- `control_plane/main.py`
- `control_plane/routes/control_room.py`
- `control_plane/evidence_dag.py`
- `control_plane/dynamic_graph_sim.py`
- `tests/test_adversarial_review_loops.py`
- `tests/test_control_room.py`
- `tests/test_agent_evidence_dag.py`
- `tests/test_dynamic_graph_sim.py`
- `apps/dashboard/src/api.ts`
- `apps/dashboard/src/components/MyAgents.tsx`
- `apps/dashboard/src/components/ControlRoom.tsx`
- `apps/dashboard/src/components/Chat.tsx`

## Non-Goals

- no autonomous source editing
- no direct source, manifest, policy, budget, route, or memory mutation
- no marketplace promotion
- no evolutionary breeding/culling
- no new graph database or generic graph runtime

## Verification

```bash
.venv/bin/python -m pytest tests/test_adversarial_review_loops.py tests/test_self_improvement_proposals.py
.venv/bin/python -m pytest tests/test_dynamic_graph_sim.py tests/test_agent_evidence_dag.py
.venv/bin/python -m pytest
cd ../dashboard && npm run build
cd ../../agent-reviewer && .venv/bin/python -m pytest
```
