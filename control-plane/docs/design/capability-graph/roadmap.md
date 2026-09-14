# Dynamic Capability Graph Roadmap

Status: exploratory roadmap, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/subgraph-templates.md`
- `docs/self-modification.md`
- `docs/deletion-revocation.md`
- `docs/ledger-lineage-economics.md`
- `docs/routing-dynamics.md`
- `docs/a2a-pack-mapping.md`
- `docs/protocol-packs.md`
- `docs/threat-model.md`

## Decision

The first concrete graph-kernel build should not be a new graph database, new
universal ledger, or active autonomous rewrite engine.

The first concrete wedge is a read-only Agent Evidence DAG projection over
existing tables and SDK evidence:

- `GrantAudit`
- `AgentReceipt`
- `AgentSession`
- `SubagentRun` and `SubagentRunEvent`
- `AgentDeployment` and `AgentDeploymentEvent`
- `AgentReviewRun`
- `AgentProofRun`
- `TrialRun`
- `WorkJob` and `WorkEvent`
- `LLMUsageEvent`
- control-room timeline aggregation
- agent-insights call logs
- code-editor turn output with `push.head_sha`

This wedge answers useful product questions now: what happened, under which
authority, against which version, with what review/proof evidence, and what
changed after failure.

Only after that projection proves useful should the platform add first-class
graph nodes, edges, rewrites, processes, route weights, revocations, tombstones,
or protocol-pack registry state.

## Relationship To Active Product Tracks

### Agent Studio

Agent Studio should proceed independently.

Role:

- practical deployed-agent build/review/edit/deploy/report loop
- concrete source mutation and remediation workflow
- place to emit stronger correlation ids and finding ids
- eventual rewrite-planner/coordinator surface

Do not block Agent Studio on graph-kernel tables. Instead, make its current
loops produce better evidence for the DAG.

### User-Composable Meta-Agents

Role:

- near-term composition substrate
- manifest-backed agents
- memory declarations
- raw-skill DAGs
- bounded `ctx.call` and `CompositionBudget`
- synthetic-host direction

This track proves that graph-like composition already exists. The graph kernel
should explain and secure it, not replace it.

### Self-Improvement Spine

Role:

- owner grant ceiling
- continuous review/proof
- autonomy tiers
- budgets and TTL
- kill-switch
- audited versions
- canary/rollback

This spine is prerequisite safety before active autonomous graph rewrites or
self-modification can run in production.

### Emergent Ecosystem

Role:

- later protocol packs
- marketplace/status/economics/taste/product dynamics
- expressive Life Engine product metaphors

This should remain gated behind evidence DAG, policy explanations, safety
spine, simulations, and operator controls.

## Existing Substrate Map

| Need | Current substrate | First action |
|---|---|---|
| authority chain | `GrantAudit`, SDK `Grant.parent_grant_id` | project grant nodes/edges |
| execution evidence | `AgentReceipt`, `AgentSession`, `SubagentRun/Event` | project invocation nodes/events |
| generic process timeline | `WorkJob`, `WorkEvent` | join by job/source/correlation ids |
| source/deploy version | `AgentDeployment/Event`, `head_sha`, image | project version/deployment nodes |
| quality gates | `AgentReviewRun`, `AgentProofRun`, `TrialRun` | project review/proof/trial nodes |
| cost/budget | `LLMUsageEvent`, `UserControlPolicy`, `CompositionBudget` | summarize cost/budget signals |
| mutation evidence | code-editor result, `push.head_sha`, source-push deploy | infer mutation node initially |
| discovery/status | `Agent`, Agent Card, search index | include as agent/current-state summary |
| deletion/revocation | current delete/cleanup routes | preview/project only at first |
| memory evidence | `AgentMemoryEntry` and memory routes | identify gaps before active mutation |

## Staged Roadmap

### Stage 0: Research And Safety Frame

Status: P0-P14.

Outputs:

- kernel framing and primitives
- capability algebra
- signal/evidence model
- rewrite/process/policy models
- subgraph templates
- self-modification and deletion models
- ledger/lineage/economics model
- routing dynamics
- A2A Pack mapping
- protocol pack boundary
- threat model

Gate to leave Stage 0:

- docs identify concrete existing rows
- no new active graph state required yet
- safety constraints and threat model are explicit

### Stage 1: Evidence Inventory

Task: P19.

Output:

- table-by-table inventory
- source-of-truth recommendation per evidence type
- join key map
- redaction and missing-field gaps

Exit criteria:

- every projected evidence type has a source row or explicit gap
- join keys are named: agent, user, grant, receipt, session, deploy, review,
  proof, head SHA, thread, job, correlation

### Stage 2: Agent Evidence DAG Schema

Task: P20.

Output:

- node schema
- edge schema
- provenance schema
- redaction rules
- example DAG from build -> deploy -> review/proof -> patch -> redeploy

Exit criteria:

- DAG is acyclic for evidence even if conceptual graph has cycles
- every node/edge has source table, row id, timestamp, and join rule
- no secret-bearing payload is duplicated

### Stage 3: Dossier And Timeline API Contract

Task: P21.

Output:

- `GET /v1/agents/{name}/evidence-dag`
- `GET /v1/agents/{name}/dossier`
- dossier summary schema
- UI timeline requirements
- authorization/redaction requirements

Exit criteria:

- one owned agent can show version, authority, quality, mutation, cost, and
  risk summaries
- missing receipts/sessions are tolerated

### Stage 4: Correlation Chains

Task: P22.

Output:

- deploy -> review -> proof chain
- grant -> handoff/proof/receipt chain
- receipt -> replay session chain
- code-editor mutation -> push SHA -> deployment chain
- failure -> patch -> deploy -> review/proof chain
- finding id/hash strategy

Exit criteria:

- a failure can be traced to later remediation evidence when join keys exist
- code-editor payload inference is documented until first-class mutation rows
  exist

### Stage 5: V0 Read-Only Build Plan

Task: P23.

Output:

- implementation plan
- minimal model/API additions, if any
- fixture and test plan across existing rows
- redaction tests

Exit criteria:

- implementation can start without re-litigating storage or graph primitives
- v0 remains read-only projection unless a narrow metadata field is justified

### Stage 6: Targeted Metadata Additions

Only after Stages 1-5 expose concrete gaps.

Likely additions:

- stable review finding id/hash
- rewrite/correlation id on Agent Studio/code-editor runs
- manifest version id on meta-runs
- template id/version on DAG/meta-run evidence
- route decision id where route explanation is shown
- memory operation id for write/summarize/purge

Rule: add metadata before adding broad graph tables.

### Stage 7: Explanation APIs

Add read APIs that answer:

- what can this agent do?
- why does it have this authority?
- what version is live?
- what evidence supports trust?
- why was this route selected?
- what would deletion affect?
- what changed between versions?
- which grants/calls/proofs/reviews connect these events?

These APIs should back UI surfaces before active graph mutation.

### Stage 8: Narrow Active State

Add first-class state only for proven active needs:

- grant revocation lookup
- graph tombstone/severance
- route decision/weight
- rewrite proposal
- graph process
- policy decision cache

Each active table must map to evidence refs and threat-model tests.

### Stage 9: Active Rewrite And Adaptive Routing

Prerequisites:

- threat model mitigations exist
- kill-switch exists
- policy-before-score route tests pass
- dangerous rewrite approval/review/proof/rollback lifecycle exists
- revocation race tests pass
- operator controls exist

Only then:

- agent-proposed rewrites
- adaptive route weight updates
- self-modification loops
- protocol-pack process activation

## Decision Gates

### Before New Graph Tables

Require:

- evidence inventory complete
- read-only projection cannot answer a required query
- table has a single active responsibility
- redaction and authorization behavior defined
- migration and backfill plan exists

### Before Active Revocation

Require:

- runtime/server-side revocation check
- process-local capability invalidation
- child revocation propagation
- audit evidence
- race test

### Before Self-Mutation

Require:

- rewrite proposal envelope
- owner/platform approval gate by risk
- source/manifest/prompt/memory versioning
- review/proof/canary
- rollback or compensation
- kill-switch

### Before Adaptive Routing

Require:

- route explanation schema
- capability/policy filter before scoring
- safety hard blocks
- trust/cost/taste/status score separation
- operator disable
- simulation tests

### Before Protocol Packs

Require:

- template id/version
- pack schema
- risk class
- policy attachment
- simulation fixture
- UI/audit language separation

## Open Questions

- Is `WorkJob`/`WorkEvent` enough as the generic event projection, or does v0
  need a small `EvidenceEvent` projection table?
- Which Agent Studio actions should emit rewrite/correlation ids first?
- Which graph rights need signed runtime tokens vs server-side checks?
- How much evidence graph state should agents be allowed to inspect?
- What is the smallest useful dossier that does not overfit the future kernel?
- Which active revocation check should land before autonomous repair loops?
- How much route explanation should be public, owner-visible, or internal only?

## Non-Goals

- no graph database first
- no universal ledger first
- no active autonomous self-modification first
- no protocol-pack runtime first
- no marketplace/status/taste authority
- no blocking Agent Studio or meta-agent shipping on speculative graph state

The roadmap is deliberately conservative: use the evidence substrate already in
production, make it explainable, prove the gaps, then add narrow active state
where the product and safety requirements demand it.
