# Dynamic Capability Graph Storage And Query Surface

Status: storage decision, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/signals-evidence.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/deletion-revocation.md`
- `docs/ledger-lineage-economics.md`
- `docs/roadmap.md`

## Decision

Do not add a separate graph database or greenfield kernel store yet.

The first storage model is a hybrid inside the existing control-plane
Postgres:

- existing durable tables remain the authoritative source for their domain
- append-only event rows remain the causal history where they already exist
- graph nodes and edges are read-only projections over existing evidence
- projected graph state is rebuildable and not authoritative in v0
- heavy payloads stay in object storage or existing signed-token payload fields
- first-class graph tables are added only after the evidence DAG proves useful

In short: the kernel store starts as a logical query layer, not a new database.

This keeps the platform aligned with the current reality. The codebase already
has durable evidence for grants, receipts, sessions, handoffs, deployments,
reviews, proofs, trials, work jobs, work events, LLM usage, control-room
timelines, agent insights, and code-editor source-push results. P16 should
organize that substrate before inventing another ledger.

## Storage Options

### Option 1: Greenfield Graph Database

Examples: Neo4j, Memgraph, ArangoDB, or a property graph service.

Strengths:

- native multi-hop graph traversal
- visual graph exploration can be straightforward
- graph algorithms may be easier later

Problems:

- creates a second source of truth immediately
- forces dual-write or sync logic before the graph has product value
- complicates authorization and redaction across stores
- makes deploy, backup, migration, and restore harder
- does not match the current evidence rows, which are relational and temporal

Decision:

- reject for v0
- reconsider only if materialized Postgres projections cannot answer concrete
  multi-hop product queries with reasonable indexes

### Option 2: Event-Sourced Kernel From Scratch

All graph changes would be written to a new append-only event table, and every
current-state view would be rebuilt from those events.

Strengths:

- strong causal history
- clean replay model
- easy to reason about graph rewrites after the kernel exists

Problems:

- ignores existing event sources
- requires backfilling or shadow-writing before the first useful query
- risks a fake ledger that duplicates `WorkEvent`, `SubagentRunEvent`,
  `AgentDeploymentEvent`, signed receipts, signed sessions, and review/proof
  rows
- pushes implementation too far ahead of product proof

Decision:

- reject for v0
- later, add `GraphEvent` or `EvidenceEvent` only if existing rows cannot carry
  a required graph mutation or correlation record

### Option 3: Current-State Relational Graph Tables

Create tables like `graph_nodes`, `graph_edges`, `graph_capabilities`,
`graph_processes`, and `graph_signals`, then query them directly.

Strengths:

- fits the current SQLAlchemy/Postgres stack
- easy to authorize by owner and organization
- easy to index common query shapes
- can support a future graph explorer

Problems:

- current-state rows can hide causality if not tied to source events
- premature table design may overfit future protocol-pack metaphors
- active graph rewrites are not safe until the policy and safety spine are
  implemented

Decision:

- acceptable as a later materialized projection
- not authoritative in v0
- every row must include provenance refs back to source tables/events

### Option 4: Hybrid Relational Evidence Store With Projections

Use existing tables as sources of truth, treat event/timeline rows as the
ledger where available, and build read-only projections for graph queries.

Strengths:

- uses the evidence that already exists
- avoids dual-write risk
- keeps authorization near current user-owned rows
- lets the platform ship useful dossier/timeline queries first
- leaves room for first-class graph/event tables later

Problems:

- projection code must normalize uneven row shapes
- correlation heuristics are needed until stronger ids exist
- some graph concepts will be absent until later implementation phases

Decision:

- recommended path
- P19 through P23 should implement or specify this path before new kernel
  tables are added

## Source Of Truth By Primitive

| Graph primitive | v0 source of truth | Projection rule |
|---|---|---|
| agent node | `Agent` and Agent Card/cache | one durable node per owned or visible agent |
| source version | `AgentDeployment.head_sha`, code-editor `push.head_sha` | one version node per SHA |
| image version | `AgentDeployment.image`, `AgentProofRun.image` | one version node per image ref or digest |
| card version | Agent Card hash, `AgentProofRun.card_hash` | one version node per card hash |
| grant/capability | `GrantAudit`, SDK grant ids in receipts/proofs/runs | capability edge with grant provenance |
| credential metadata | `AgentAuthConnection`, `AgentSecret`, `GiteaTokenAudit` | credential node with redacted status only |
| handoff process | `SubagentRun`, `SubagentRunEvent` | process node plus ordered event edges |
| generic work process | `WorkJob`, `WorkEvent` | process node plus event timeline |
| DAG process | `DagRun`, `DagRunNode` | process/subprocess nodes with dependency edges |
| meta-agent process | `MetaAgentRun` | process node with plan/progress projection |
| deployment | `AgentDeployment`, `AgentDeploymentEvent` | deployment node plus timeline edges |
| review signal | `AgentReviewRun.findings` | review node plus finding signals |
| proof signal | `AgentProofRun` | proof node plus tested-version edge |
| receipt evidence | `AgentReceipt` | signed invocation node with redacted payload refs |
| replay evidence | `AgentSession` | replay node with object-store event ref |
| cost signal | `LLMUsageEvent` | spend/tokens signal tied by user/thread/grant/agent |
| policy state | `UserControlPolicy`, `PlatformSetting` | policy nodes or decision context refs |
| schedule trigger | `AgentSchedule` | trigger node and last-run edge |
| mutation evidence | code-editor result, source-push worker, deployment SHA | inferred mutation node until first-class records exist |

## Append-Only History Vs Mutable Projection

### Append-Only Or Audit-Like Rows

These rows should be treated as immutable evidence once written, even if the
current schema does not enforce immutability everywhere:

- `GrantAudit`
- `SubagentRunEvent`
- `AgentDeploymentEvent`
- `WorkEvent`
- `AgentReceipt.signed_token`
- `AgentSession.signed_token`
- `AgentSession.events_object_key`
- final review/proof/trial result payloads
- Gitea token audit issuance and revocation metadata

The projection may redact them, summarize them, or reference them, but should
not rewrite their meaning.

### Mutable Current-State Rows

These rows are projections or operational state:

- `Agent` current metadata and cache fields
- `AgentDeployment.status`
- `SubagentRun.status`
- `WorkJob.status`
- `DagRun.status`
- `DagRunNode.status`
- `MetaAgentRun.status`, `current_plan`, `progress`, and `state`
- `AgentAuthConnection.status`
- `AgentSchedule.last_run_status`
- `UserControlPolicy`
- `PlatformSetting`

Graph v0 should read these rows to answer "what is current?" questions, but
causal explanations should walk back to event rows, signed receipts, signed
sessions, review/proof rows, grant audits, and source versions.

### Projection Rows

If materialized projection tables are added later, they must be considered
cache/state, not authority.

Required projection fields:

- `projection_id`
- `projection_kind`
- `subject_type`
- `subject_id`
- `owner_user_id` or `organization_id`
- `source_refs`
- `computed_at`
- `watermark`
- `schema_version`
- `redaction_class`

Rebuild rule:

- dropping and rebuilding projection rows must not destroy authority,
  execution evidence, review evidence, grant history, or signed receipts

## Version Model

The graph needs stable version identifiers before active rewrites exist.

Suggested version node ids:

- `source:{owner}/{repo}@{head_sha}`
- `image:{registry_ref_or_digest}`
- `card:{agent_name}@{card_hash}`
- `manifest:{agent_name}@{manifest_hash}`
- `prompt:{agent_name}:{prompt_namespace}@{prompt_hash_or_version}`
- `memory:{agent_name}:{namespace}@{generation_or_snapshot}`
- `policy:{policy_source}:{key}@{updated_at_or_hash}`
- `edge:{edge_id}@{edge_version}`
- `process:{run_or_job_id}@{event_seq_or_status_version}`

Rules:

- source SHA is the strongest source-version anchor when present
- image refs are useful but should prefer immutable digests when available
- Agent Card hash is a behavioral declaration version, not source identity
- prompt, memory, edge, and policy versions are future-state until the platform
  stores stable hashes or version rows
- mutable rows should not be used as version identity without a timestamp,
  hash, or event sequence

## Projection List

### Agent Evidence DAG

Question:

- what happened for this agent?

Sources:

- `Agent`
- `GrantAudit`
- `AgentReceipt`
- `AgentSession`
- `SubagentRun`
- `SubagentRunEvent`
- `AgentDeployment`
- `AgentDeploymentEvent`
- `AgentReviewRun`
- `AgentProofRun`
- `TrialRun`
- `WorkJob`
- `WorkEvent`
- `LLMUsageEvent`

Output:

- nodes and directed evidence edges with provenance refs
- redacted payload summaries
- missing-edge warnings where joins are inferred

### Agent Dossier Projection

Question:

- what is the current trust, version, authority, and risk summary for this
  agent?

Sources:

- Agent Evidence DAG
- latest deployment/review/proof rows
- current Agent Card/cache
- current grant and credential metadata

Output:

- current version summary
- latest review/proof status
- known authority paths
- recurring failures
- unresolved risks

### Capability Projection

Questions:

- what can this node currently do?
- why does it have this access?

Sources:

- `GrantAudit`
- receipt grant ids
- proof grant ids
- handoff grant ids
- `UserControlPolicy`
- platform settings
- credential connection metadata

Output:

- active, expired, denied, and revoked capability edges
- parent grant chain
- scope, TTL, deny patterns, and decision reason
- no signed grant token exposure

### Active Process Projection

Questions:

- what active processes can mutate or call this node?
- what process-local authority exists?

Sources:

- `WorkJob`
- `WorkEvent`
- `SubagentRun`
- `DagRun`
- `DagRunNode`
- `MetaAgentRun`
- proof/review/deployment running rows

Output:

- process nodes grouped by root job, thread, grant, or deploy id
- child process edges
- current status, heartbeat, lease, budget, and last event
- kill-switch targets for later P17 work

### Version Trust Projection

Questions:

- what evidence supports or contradicts this version?
- what changed between two versions?

Sources:

- `AgentDeployment.head_sha`
- `AgentDeployment.image`
- `AgentReviewRun.ref`
- `AgentReviewRun.findings`
- `AgentProofRun.head_sha`
- `AgentProofRun.card_hash`
- code-editor `push.head_sha`
- source-push worker events

Output:

- version nodes
- deployed-by edges
- reviewed-by edges
- tested-by edges
- mutation-produced edges
- unresolved finding summaries

### Dependency And Revocation Projection

Questions:

- what depends on this agent, credential, edge, grant, memory namespace, or
  source version?
- what must be revoked if it is deleted?

Sources:

- grant parent chains
- handoff runs
- receipts and sessions
- deployment and auth connection metadata
- work subject/source refs
- future graph edge projection

Output:

- dependent nodes and processes
- active credentials and grants
- revocation order
- tombstone and retention notes

### Routing Signal Projection

Questions:

- what signals explain this routing decision?
- did routing use authority or just preference?

Sources:

- search/discovery result metadata
- review/proof status
- receipt success/failure
- LLM spend and latency
- user feedback when available
- policy decisions

Output:

- routing explanation candidates
- signal refs and weights
- explicit note that routing weight is not permission

### Policy Decision Projection

Questions:

- what policies blocked this rewrite, grant, route, deletion, or mutation?

Sources:

- `UserControlPolicy`
- `PlatformSetting`
- grant audit decisions and reasons
- review/proof critical findings
- future org and resource policy rows

Output:

- allow, deny, require approval, require review, require canary, or require
  narrower-scope explanation
- policy source list
- highest authority ceiling used

## Query API Sketch

P16 does not implement endpoints. This section defines the shape that later
P20 through P23 can narrow.

### V0 Read-Only Evidence Endpoints

`GET /v1/agents/{name}/evidence-dag`

Returns:

- DAG nodes
- DAG edges
- provenance refs
- redaction metadata
- missing-correlation warnings

Filters:

- `since`
- `until`
- `limit`
- `head_sha`
- `grant_id`
- `deploy_id`
- `review_id`
- `skill_name`
- `status`
- `include_payloads=false`

`GET /v1/agents/{name}/dossier`

Returns:

- current version
- latest deploy/review/proof
- authority summary
- quality summary
- mutation summary
- unresolved risks
- recent evidence timeline

`GET /v1/agents/{name}/evidence-timeline`

Returns:

- flat chronological evidence rows for UI timelines
- lane labels: version, authority, mutation, quality, cost, process

### Post-V0 Kernel Query Endpoints

`GET /v1/graph/nodes/{node_id}/capabilities`

Answers:

- what can this node currently do?
- which grant/policy/edge explains each capability?

`GET /v1/graph/nodes/{node_id}/dependents`

Answers:

- which edges, credentials, grants, processes, deployments, or agents depend
  on this node?

`GET /v1/graph/processes/{process_id}`

Answers:

- what is this process doing?
- what can it mutate?
- what child processes or grants exist?

`POST /v1/graph/revocation-preview`

Answers:

- what would be revoked, paused, tombstoned, hidden, retained, or purged?

`GET /v1/graph/versions/diff`

Answers:

- what changed between two source, card, manifest, prompt, memory, edge, or
  policy versions?

`GET /v1/graph/routing/explanation/{decision_id}`

Answers:

- what candidates were considered?
- which signals and policies affected the route?
- which permission checks were separate from routing preference?

## Authorization And Redaction

### Authorization Boundary

Default rule:

- graph inspection is scoped to the authenticated owner or organization

V0 agent queries should require:

- agent ownership by `user_id`, or
- public visibility plus public-only redaction rules, or
- an explicit operator/admin role

Source rows must be filtered by:

- `user_id`
- `agent_id`
- `agent_name` with owner validation
- `thread_id` with owner validation
- `grant_id` only after joining to an owned grant/run/session/proof
- `deploy_id` only after joining to an owned deployment

Never authorize graph data by `agent_name` alone.

### Redaction Classes

Public:

- agent name
- public Agent Card fields
- public proof summary
- public review summary if explicitly exposed

Owner:

- owned run metadata
- grant scopes and decisions
- deployment history
- proof/review details
- receipt and session summaries
- cost summaries

Operator:

- internal policy decisions
- platform-level diagnostics
- redacted security timelines

Secret:

- signed grant tokens
- CP JWTs
- Gitea tokens
- LLM API keys
- `secret_ciphertext`
- raw object-store replay payloads unless explicitly fetched through an
  owner-authorized forensic endpoint
- private file contents
- private prompt payloads
- unredacted tool call inputs

### Payload Rules

Return by default:

- ids
- timestamps
- statuses
- event types
- short summaries
- counts
- hashes
- object refs only when the caller can access the object
- table and row provenance

Do not return by default:

- `signed_token`
- `secret_ciphertext`
- token hashes beyond short fingerprints
- raw receipt payloads
- raw session event logs
- raw `args_json` if it may include user prompt or file content
- raw `result` if it may include generated secrets or private data
- raw WorkEvent payloads without redaction

## Crash And Rebuild Model

The read-only evidence DAG must tolerate control-plane restarts and partial
event coverage.

Rebuild order:

1. load owned agent row and current metadata
2. load deployments, deployment events, reviews, and proofs by agent id/name
3. load grants by user/audience/grant ids discovered from runs/proofs/receipts
4. load handoff runs and handoff events by user/agent/grant/thread
5. load receipts and sessions by agent id/name and receipt ids
6. load work jobs and work events by source, subject, worker, grant, thread, and
   correlation ids
7. load LLM usage by user/thread/grant/agent
8. create projection nodes
9. create strict edges from exact ids
10. create inferred edges from head SHA, thread id, timestamps, and payload refs
11. mark inferred edges with confidence and join rule

Watermark:

- each projection response should include the newest source row timestamp and
  a list of source families scanned

Partial data:

- missing receipts must not hide known handoff/proof events
- missing sessions must not hide known receipts
- missing work events must not hide specialized run rows
- missing deployment events must not hide deployment rows
- inferred mutation nodes must be marked as inferred until first-class mutation
  records exist

## Query Answers Required By P16

### What Can This Node Currently Do?

Use:

- active grant audits
- grant TTL and decision
- user/org/platform policy
- credential metadata status
- process-local authority from active process rows

Answer shape:

- capability edge id
- action/scope summary
- resource summary
- source grant id
- parent grant id
- policy ceiling
- expires at
- status
- redaction class

### Why Does This Node Have Access?

Use:

- `GrantAudit.parent_grant_id`
- decision and reason
- issuer and audience
- associated handoff/proof/receipt rows
- policy settings effective at decision time where available

Answer shape:

- authority path
- decision timeline
- scope narrowing explanation
- owner/platform policy refs
- evidence refs

### What Edges Depend On This Node?

Use:

- run subject/source/worker refs
- grant audience and parent chains
- receipt agent/caller/task ids
- deployment/proof/review version refs
- credential connections
- future materialized graph edge projection

Answer shape:

- dependents grouped by edge type
- active vs historical
- revocation impact estimate
- missing-correlation warnings

### What Active Processes Can Mutate This Node?

Use:

- active `WorkJob`
- active `SubagentRun`
- active `MetaAgentRun`
- active review/proof/deploy rows
- code-editor target inference
- future rewrite records

Answer shape:

- process id
- process type
- owner
- authority source
- mutation target
- current status
- kill-switch target

### What Policies Block This Rewrite?

Use:

- platform settings
- user control policy
- review findings
- grant audit hard denials
- future org/resource policy rows

Answer shape:

- blocked action
- blocking policy source
- required narrower scope or approval
- review/proof/canary requirement
- evidence refs

### What Signals Explain This Routing Decision?

Use:

- review/proof status
- receipt success/failure
- cost/latency usage
- user feedback when available
- marketplace/search metadata
- policy filters

Answer shape:

- candidate id
- signal list
- weights
- policy filters
- final route
- permission check result

### What Changed Between Two Versions?

Use:

- source SHAs
- image refs
- card hashes
- deployment events
- review/proof deltas
- code-editor output summary

Answer shape:

- added/removed/changed claims
- changed files if known
- fixed/regressed findings
- changed proofs
- changed policy/capability state if known

### What Must Be Revoked If This Is Deleted?

Use:

- grant parent chains
- active process projection
- credential metadata
- dependent deployment/proof/review/session rows
- future graph edge projection

Answer shape:

- grants to revoke
- credentials to revoke or rotate
- active processes to stop
- dependent nodes to freeze or notify
- tombstone and retention requirements

## First-Class Graph Table Gate

Add graph tables only after v0 evidence DAG proves at least one of these is
true:

- endpoint latency cannot be controlled with indexed source queries
- UI needs repeated multi-hop traversals that are too expensive to recompute
- active rewrites require atomic graph state transitions
- revocation previews require stable current-state dependency edges
- routing explanations require durable decision ids
- protocol packs need registry-owned subgraph instances

When that gate is reached, prefer Postgres tables first:

- `graph_events` for kernel-level mutation/event records
- `graph_nodes` for current-state projection
- `graph_edges` for current-state projection
- `graph_edge_versions` for edge history if needed
- `graph_processes` for activated subgraph state if existing work/run rows are
  not enough
- `graph_policy_decisions` for durable allow/deny/approval explanations

Every first-class graph row must have:

- owner or org scope
- provenance refs
- schema version
- redaction class
- created/updated timestamp
- source event id or projection watermark

## Required Gaps Before Implementation

The current substrate is strong enough for v0, but these gaps should be
closed deliberately:

- stable `correlation_id` on deployments, reviews, proofs, code-editor turns,
  and source-push jobs
- deterministic review finding ids or hashes
- first-class mutation id for code-editor and synthetic-manifest edits
- immutable image digest capture, not only image tag/ref
- policy decision records for grant, rewrite, routing, and deletion decisions
- revocation status and tombstone records
- clear object-store access policy for replay/session payloads
- graph redaction helpers shared by dossier, timeline, and future graph APIs
- owner/org authorization helpers that never trust `agent_name` alone

## Recommendation

P16 should commit to this sequence:

1. keep the existing control-plane Postgres as the storage foundation
2. treat existing durable rows and append-only events as the evidence ledger
3. build v0 Agent Evidence DAG as a read-only projection
4. expose dossier/timeline APIs from that projection
5. add targeted correlation ids and finding ids where projection quality needs
   them
6. materialize projection tables only if recompute becomes too slow
7. add first-class graph event/state tables only after active rewrite,
   revocation, routing, or protocol-pack workflows require them
8. delay graph DB adoption until concrete traversal workloads prove Postgres is
   the wrong tool

This gives the platform a real kernel path without pretending that the kernel
already needs its own database.
