# Dynamic Capability Graph V0 Implementation Contract

Date: 2026-06-02

Status: implementation contract for epic
`83e22fce-2e8b-4123-a9f3-c342f6005bf2`.

## Decision

Build the first Dynamic Capability Graph implementation as a read-only Agent
Evidence DAG projection over existing control-plane and A2A evidence rows.

V0 does not add a graph database, authoritative graph tables, active rewrites,
autonomous routing, autonomous deletion, marketplace evolution, or protocol-pack
runtime behavior. The implementation starts with explainability and trust:
users should be able to inspect what an owned agent did, which authority it
used, which version was live, what reviewed it, what proved it, what changed,
and which risk remains.

## Files

Backend:

- `control_plane/evidence_dag.py`
- `control_plane/routes/agent_evidence.py`
- `control_plane/main.py`
- `tests/test_agent_evidence_dag.py`
- `tests/test_agent_dossier.py`

Frontend:

- existing dashboard agent detail, My Agents, or Agent Insights surface
- existing dashboard API client/types

The projection module must not depend on FastAPI request objects. Route handlers
resolve user/view context, validate filters, and call the projection service
with an `AsyncSession`.

## Existing Source Mapping

| Requirement | Current source | Join keys | V0 role |
|---|---|---|---|
| Agent identity and current card | `Agent` | `id`, `name`, `owner_id`, `public` | Root agent node, claims, ownership, current cached card. |
| Version and deploy evidence | `AgentDeployment`, `AgentDeploymentEvent` | `agent_id`, `agent_name`, `user_id`, `deploy_id`, `head_sha`, `image` | Version nodes, deployment nodes, deployment event timeline. |
| Review evidence | `AgentReviewRun` | `agent_id`, `agent_name`, `deploy_id`, `ref`, `review_id`, `user_id` | Quality nodes, finding counts, finding hashes, deploy-review edges. |
| Proof evidence | `AgentProofRun` | `agent_id`, `agent_name`, `grant_id`, `head_sha`, `card_hash`, `user_id` | Proof nodes, live invocation evidence, authority/version support. |
| Authority | `GrantAudit` | `grant_id`, `parent_grant_id`, `audience`, `user_id` | Capability/authority nodes and parent-chain edges. |
| Handoff execution | `SubagentRun`, `SubagentRunEvent` | `agent_name`, `grant_id`, `user_id`, `thread_id`, `run_id` | Process nodes, event nodes, handoff authority use. |
| DAG execution | `DagRun`, `DagRunNode` | `dag_run_id`, `thread_id`, `user_id`, node ids | Multi-agent process evidence where a workflow DAG exists. |
| Signed receipts | `AgentReceipt` | `agent_id`, `agent_name`, `receipt_id`, `task_id`, `skill_name` | Signed execution evidence nodes. |
| Replay sessions | `AgentSession` | `agent_id`, `agent_name`, `session_id`, `receipt_id` | Replay/session nodes linked to receipts without embedding raw logs. |
| Work ledger | `WorkJob`, `WorkEvent` | `job_id`, `source_type`, `source_id`, `subject_id`, `correlation_id`, `user_id` | Generic process/job/event evidence and source-push correlation. |
| LLM cost | `LLMUsageEvent` | `user_id`, `grant_id`, `thread_id`, `agent_name` | Cost/tokens signal nodes and dossier cost summary. |
| Agent memory | `AgentMemoryEntry` | `agent_name`, `user_id`, `namespace`, `key` | Future memory evidence; v0 should only surface redacted refs if used. |
| Code-editor mutation | `SubagentRun`/`WorkJob` payloads | `agent_name`, `thread_id`, `grant_id`, `push.head_sha`, `source_id` | Inferred mutation nodes until first-class mutation rows exist. |

## API Contract

Add three owner-scoped read endpoints under `control_plane/routes/agent_evidence.py`:

- `GET /v1/agents/{name}/evidence-dag`
- `GET /v1/agents/{name}/dossier`
- `GET /v1/agents/{name}/evidence-timeline`

Core query filters:

- `since`
- `until`
- `limit`
- `head_sha`
- `skill_name`
- `event_type`
- `status`
- `severity`
- `lane`
- `include_payloads`
- `include_inferred`
- `include_warnings`

Response models or typed serializers:

- `EvidenceDagResponse`
- `EvidenceNode`
- `EvidenceEdge`
- `EvidenceProvenanceRef`
- `EvidenceRedaction`
- `EvidenceWarning`
- `EvidenceChain`
- `DossierResponse`
- `TimelineResponse`
- `TimelineItem`

Required enums:

- node type
- edge type
- timeline lane
- confidence
- redaction class
- warning code

## Authorization

V0 supports three view modes:

- owner: full owner-scoped evidence with default redaction
- public: only public agent evidence and public-safe summaries
- operator: explicit operator/admin access, still redacted for secrets

The owner path starts from `Agent.name` and `Agent.owner_id == user.id`.
Expansion must use exact ids discovered from owned rows before using inferred
joins. Public callers never receive private payloads, private runs, signed
tokens, workspace paths, object-store keys, or raw logs by default.

## Redaction Rules

The default response must not serialize:

- signed grant tokens
- CP JWTs
- Gitea read/write tokens
- LLM provider credentials
- raw secret values
- signed receipt/session tokens
- raw replay event logs
- raw private user payloads
- unbounded stdout/stderr
- private object-store contents

Safe defaults include ids, timestamps, statuses, counts, redacted summaries,
bounded previews, source table refs, join rules, confidence, and warnings.

## Projection Order

1. Skeleton response and authorization.
2. Strict deployment/review/proof projection.
3. Authority, handoff, receipts, sessions, work ledger, and cost projection.
4. Deterministic ids, redaction, provenance refs, and finding hashes.
5. Code-editor/source-push/failure-remediation chains.
6. Dossier and timeline endpoints.
7. Dashboard evidence tab.
8. Agent Studio correlation hooks.
9. Simulation/proof harness.
10. Non-autonomous self-improvement proposal contract.

## Fixture Matrix

| Fixture | Rows | Assertions |
|---|---|---|
| Deploy review proof | `Agent`, `AgentDeployment`, `AgentDeploymentEvent`, `AgentReviewRun`, `AgentProofRun` | Strict deploy -> review -> proof chain, current version summary. |
| Authority handoff | `GrantAudit`, `SubagentRun`, `SubagentRunEvent`, optional `LLMUsageEvent` | Grant -> handoff/proof chain, parent grant edge, cost signal. |
| Receipt session | `AgentReceipt`, `AgentSession` | Receipt -> session edge, missing session warning path. |
| Work ledger process | `WorkJob`, `WorkEvent` | Job/event nodes, source/subject/correlation provenance. |
| Mutation source push | `SubagentRun` or `WorkJob` payload with `push.head_sha`, `AgentDeployment` with same `head_sha` | Code-editor mutation -> commit -> deployment chain. |
| Failure remediation | `AgentReviewRun` finding, code-editor payload, later deployment/review/proof | Finding hash links failure to patch and follow-up evidence. |
| Missing evidence | Sparse rows with no review, proof, session, or work events | DAG still returns agent/current rows plus warnings. |
| Authorization | Two users, private/public agents, operator view where available | Owner allowed, non-owner private denied, public redacted. |
| Redaction | Secret-like fields across payloads/tokens/logs | No forbidden values in serialized responses. |
| Determinism | Same rows projected twice | Same node ids, edge ids, finding hashes, and chain confidence. |

## Dashboard Target

The first frontend surface should be an Evidence or Dossier tab in the existing
agent detail / My Agents / Agent Insights area. It should reuse the dossier and
timeline endpoints instead of reimplementing graph logic client-side.

The UI should show:

- live version and deployment status
- latest review/proof status
- authority summary
- mutation/source-push summary
- residual risk summary
- lane-based timeline for version, authority, mutation, quality, process, cost,
  and control events

## Non-Goals

- No graph database.
- No materialized graph tables.
- No first-class mutation table unless v0 projection proves it is blocking.
- No active self-modification.
- No autonomous route-weight updates.
- No deletion/revocation execution engine.
- No collective-memory write propagation.
- No evolutionary breeding/culling.
- No autopoietic marketplace.

## Follow-Up Gate

Only after the read-only evidence surface and simulation/proof harness exist can
the platform safely advance to active graph rewrites. Any follow-up that mutates
source, manifests, prompts, memory, routing, pricing, deletion state, or
marketplace composition must prove:

- owner grant ceiling
- continuous review
- autonomy tier decision
- budget and kill-switch enforcement
- versioned/audited mutation evidence
- canary or proof gate
- rollback plan
- dossier visibility
