# Agent Evidence DAG V0 Build Plan

Status: implementation plan, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/evidence-inventory.md`
- `docs/agent-evidence-dag.md`
- `docs/agent-dossier-api.md`
- `docs/correlation-chains.md`

## Decision

Build v0 as a read-only projection over existing tables before adding graph
kernel primitives.

V0 does not need:

- a graph database
- new authoritative graph tables
- active graph rewrites
- a universal event ledger
- autonomous routing or mutation

V0 needs:

- owner-scoped evidence collection
- redacted node/edge projection
- strict and inferred correlation chains
- dossier and timeline summaries
- fixture-backed tests across the existing evidence substrate

## Implementation Scope

### Add Projection Module

Suggested file:

- `control_plane/evidence_dag.py`

Responsibilities:

- authorize/resolve agent input already scoped by route
- collect source rows
- normalize source rows into evidence nodes
- normalize source relationships into evidence edges
- compute deterministic ids
- compute finding hashes
- redact payloads
- build correlation chains
- build dossier summary
- build timeline items
- return warnings for gaps and inferred joins

Keep this module independent of FastAPI request objects so tests can call it
directly with an `AsyncSession`.

### Add Routes

Suggested file:

- `control_plane/routes/agent_evidence.py`

Endpoints:

- `GET /v1/agents/{name}/evidence-dag`
- `GET /v1/agents/{name}/dossier`
- `GET /v1/agents/{name}/evidence-timeline`

Route responsibilities:

- authenticate user
- resolve owned or public agent
- enforce public/owner/operator view
- validate query filters
- call projection module
- serialize redacted response

### Add Router Registration

Suggested change:

- include `agent_evidence.router` in `control_plane/main.py`

### Add Tests

Suggested files:

- `tests/test_agent_evidence_dag.py`
- `tests/test_agent_dossier.py`

Keep tests fixture-driven and narrow. Do not require Kubernetes, Argo, Gitea,
LLM providers, object storage beyond fake object refs, or live agents.

## Build Sequence

### Phase 1: Skeleton And Authorization

Implement:

- route module
- response models or typed dict serializers
- owner-scoped agent resolution
- public view stub
- empty DAG response with agent node and watermark

Tests:

- owner can request own agent
- other user cannot read private agent
- public caller gets public redacted view only
- unknown agent returns 404

### Phase 2: Strict Deployment/Review/Proof Projection

Implement:

- deployment nodes
- deployment event nodes
- review nodes
- finding nodes with deterministic hashes
- proof nodes
- source/image/card version nodes
- strict edges:
  - agent -> deployment
  - deployment -> deployment event
  - deployment -> review
  - review -> finding
  - deployment/proof -> version

Tests:

- deploy -> review chain appears by `deploy_id`
- proof tests the same `head_sha`
- finding hash is stable
- warning/critical counts appear in dossier risk summary

### Phase 3: Authority And Handoff Projection

Implement:

- grant nodes from `GrantAudit`
- grant parent edges
- handoff nodes
- handoff event nodes
- strict edges:
  - grant -> handoff
  - handoff -> handoff event
  - grant parent -> child grant
  - grant -> proof

Tests:

- grant parent chain is returned in order
- denied grant appears as denied authority evidence
- handoff run is linked to grant
- proof run is linked to grant

### Phase 4: Receipt And Replay Projection

Implement:

- receipt nodes from `AgentReceipt`
- session nodes from `AgentSession`
- grant-use edges from receipt payload grant ids
- receipt -> session edge by `receipt_id`
- object-store replay refs without reading raw logs by default

Tests:

- receipt linked to session
- receipt linked to grant audit rows
- missing session produces warning, not failure
- signed token is never serialized

### Phase 5: Work Ledger And Cost Projection

Implement:

- WorkJob nodes
- WorkEvent nodes
- LLMUsageEvent nodes
- work job -> work event edges
- cost attribution edges by grant/thread/agent when possible
- process/correlation edges by `job_id`, `correlation_id`, source/subject refs

Tests:

- WorkJob and WorkEvent appear for owned user
- LLM cost appears redacted and attributed
- other user's work rows are not included
- raw WorkEvent payload is not included by default

### Phase 6: Mutation And Source-Push Correlation

Implement:

- inferred mutation nodes from code-editor payloads
- source-push WorkJob linkage
- deployment linkage by `head_sha`
- review/proof after deployment linkage
- confidence and warning metadata

Tests:

- code-editor `push.head_sha` links to deployment `head_sha`
- source-push job `source_sha` links to deployment
- missing source-push job falls back to SHA match with warning
- stdout/stderr are excluded by default

### Phase 7: Dossier Summary

Implement:

- claims summary
- current version summary
- trust profile
- authority summary
- quality summary
- mutation summary
- risk summary
- graph refs

Tests:

- latest deployment is selected
- latest review/proof status affects trust profile
- open critical finding creates risk
- later passing review/proof can close or reduce risk
- missing proof creates `unverified_live_version` or `proof_missing` warning

### Phase 8: Timeline Feed

Implement:

- flattened lane feed from DAG nodes/chains
- cursor or limit behavior
- lane filters
- status/severity filters
- version/head SHA filter

Tests:

- version lane includes deployment/review/proof
- authority lane includes grants
- mutation lane includes code-editor/source-push
- quality lane includes findings/proofs/receipts
- filters return expected subsets

### Phase 9: UI Integration

Suggested area:

- My Agents / Agent detail
- Agent Insights area

UI:

- Evidence tab
- dossier summary header
- lane timeline
- graph/detail drawer
- strict vs inferred badge
- warning list
- source refs for owner/operator

No new visual framework is required.

## Minimal API Models

Implement with Pydantic models or typed serializers matching P20/P21:

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
- lane
- confidence
- redaction class
- warning code

## Required Model Additions

None for v0.

The first implementation should query existing tables and compute projections
on request.

Optional later additions after v0 proves useful:

- `correlation_id` on `AgentDeployment`
- `correlation_id` on `AgentReviewRun`
- `correlation_id` on `AgentProofRun`
- deterministic `finding_hash` materialized on review findings
- first-class `AgentMutationRun`
- `EvidenceEvent`
- materialized `GraphNode` and `GraphEdge` projection tables

Do not add these before the read-only projection demonstrates the missing ids
are blocking real user value.

## Source Query Plan

Given an owned agent:

1. load `Agent`
2. load `AgentDeployment` by `agent_id`, `user_id`, and `agent_name`
3. load `AgentDeploymentEvent` by deployment ids
4. load `AgentReviewRun` by `agent_id`, `agent_name`, `deploy_id`, and `user_id`
5. load `AgentProofRun` by `agent_id`, `agent_name`, and `user_id`
6. load `SubagentRun` by `agent_name`, `user_id`, discovered grant ids, and
   thread ids
7. load `SubagentRunEvent` by run ids
8. load `AgentReceipt` by `agent_id` and `agent_name`
9. load `AgentSession` by `agent_id`, `agent_name`, and receipt ids
10. load `TrialRun`/`TrialRoom` by `agent_name` and `user_id`
11. load `WorkJob` by `user_id`, source/subject refs, worker name, thread ids,
    correlation ids, and source-push job kind
12. load `WorkEvent` by job ids
13. load `LLMUsageEvent` by `user_id`, grant ids, thread ids, and agent name
14. load `GrantAudit` by `user_id`, audience/agent name, and discovered grant
    ids, then recursively load parent/child chains

Query safety:

- start from owned agent and owner id
- expand by exact ids discovered from owned rows
- never start with unscoped `agent_name`
- cap result size by query limit and per-source safeguards

## Redaction Implementation

Create central helpers:

- `redact_grant`
- `redact_receipt`
- `redact_session`
- `redact_handoff_args`
- `redact_work_payload`
- `redact_review_finding`
- `redact_code_editor_result`
- `redact_secret_metadata`

Forbidden strings/fields in serialized default responses:

- `signed_token`
- `secret_ciphertext`
- `token`
- `access_token`
- `refresh_token`
- `api_key`
- `cp_jwt`
- raw `stdout`
- raw `stderr`
- raw replay events

Tests should serialize responses to JSON and assert these fields are absent.

## Fixture Test Plan

### Fixture A: Deploy Review Proof

Rows:

- owned Agent
- AgentDeployment with `deploy_id`, `head_sha`, image
- AgentDeploymentEvent rows for source/build/argo/review
- AgentReviewRun linked by `deploy_id`
- AgentProofRun linked by `head_sha` and `card_hash`

Asserts:

- DAG has deployment/review/proof/version nodes
- dossier current version matches `head_sha`
- trust profile reflects review/proof status

### Fixture B: Authority Handoff

Rows:

- parent and child GrantAudit rows
- SubagentRun with child `grant_id`
- SubagentRunEvent rows
- LLMUsageEvent with `grant_id`

Asserts:

- grant parent chain appears
- grant authorizes handoff
- events are ordered
- cost is attributed to grant

### Fixture C: Receipt Session

Rows:

- AgentReceipt with payload grant ids
- AgentSession with same `receipt_id`
- object-store key placeholder

Asserts:

- receipt -> session edge exists
- grant -> receipt edge exists
- signed token absent
- object key redacted or access-scoped

### Fixture D: Mutation Source Push

Rows:

- code-editor SubagentRun or WorkJob payload with `push.head_sha`
- source-push WorkJob with `input_payload.source_sha`
- WorkJob output with `deploy_id` and `review_id`
- AgentDeployment with same `head_sha`
- AgentReviewRun after deployment

Asserts:

- mutation node exists
- mutation produces source version
- source-push job triggers deployment
- deployment links to later review
- chain confidence is strict when all ids match

### Fixture E: Failure Remediation

Rows:

- old review with critical finding
- mutation/source-push to new SHA
- new deployment
- new review/proof pass

Asserts:

- finding hash is stable
- remediation chain links finding to mutation and new evidence
- risk summary marks old finding improved or resolved according to rules

### Fixture F: Missing Evidence

Rows:

- receipt without session
- deployment without proof
- mutation without source-push job but matching deployment SHA

Asserts:

- API returns warnings
- no crashes
- inferred confidence is marked

### Fixture G: Authorization And Redaction

Rows:

- private agent owned by user A
- unrelated rows for user B
- public agent
- secret/auth/token metadata

Asserts:

- user B cannot read user A private graph
- public view omits private lanes
- owner view redacts secrets
- operator view requires operator role if implemented

## Verification Commands

Initial narrow checks after implementation:

```bash
pytest tests/test_agent_evidence_dag.py tests/test_agent_dossier.py
```

Broader checks when route and UI integration land:

```bash
pytest tests/test_agent_evidence_dag.py tests/test_agent_dossier.py tests/test_control_room.py tests/test_agent_insights.py
```

## Exit Criteria

V0 is complete when:

- owned agent API returns redacted DAG with deployments, reviews, proofs, grants,
  handoffs, receipts, sessions, work events, and LLM cost when rows exist
- deploy -> review -> proof chain is strict for at least one fixture
- grant -> handoff/proof chain is strict for at least one fixture
- receipt -> session chain works and tolerates missing session
- code-editor mutation -> source-push -> deployment chain works when payloads
  exist
- dossier summarizes current version, authority, quality, mutation, and risks
- timeline lanes render from the same DAG source
- no signed tokens, credentials, CP JWTs, private payloads, raw replay logs, or
  raw stdout/stderr are serialized by default

## Deferred Until After V0

- materialized graph tables
- active graph rewrites
- graph DB
- autonomous mutation
- routing weight updates
- deletion/revocation execution
- protocol-pack registry
- first-class mutation table

## Recommendation

Implement v0 in the smallest useful cut:

1. projection module
2. redaction helpers
3. three read-only endpoints
4. strict chains first
5. inferred chains second
6. dossier summary
7. timeline feed
8. UI evidence tab

Only after users can inspect real agents through the DAG should the platform
add new graph-kernel tables or active graph behavior.
