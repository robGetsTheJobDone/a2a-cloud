# Dynamic Capability Graph Ledger, Lineage, And Economics

Status: exploratory schema, not implementation.

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

## Decision

The ledger is the causal substrate for the graph. It records what happened, who
or what initiated it, which authority path allowed it, which policy gates passed
or failed, what evidence was produced, what versions changed, what money or
budget moved, what trust/status/reputation changed, what data was retained or
deleted, and what follow-up signals resulted.

Ledger entries do not grant authority by themselves. Reputation, marketplace
rank, taste, cost, revenue, and status can influence discovery, routing, review
priority, and mutation proposals, but they must never bypass capability and
policy checks.

For v0, do not add a new universal ledger table first. Project ledger events
from existing rows:

- `WorkJob` and `WorkEvent` for generic causal work timelines
- `GrantAudit` for authority decisions and grant parent chains
- `AgentReceipt` and `AgentSession` for signed execution and replay evidence
- `SubagentRun` and `SubagentRunEvent` for handoff evidence
- `AgentDeployment` and `AgentDeploymentEvent` for version deployment evidence
- `AgentReviewRun`, `AgentProofRun`, and `TrialRun` for quality and proof
- `LLMUsageEvent` for token/cost attribution
- `UserControlPolicy` for spend and runtime policy ceilings
- `Bounty` for demand discovery and fixed reward state
- `Agent.source_agent_id`, `Agent.card`, `Agent.status`, and `Agent.public`
  for current lineage and discovery hints

Add first-class graph ledger state only after the evidence DAG projection proves
which cross-cutting queries cannot be answered from those sources.

## Ledger Principles

- Append evidence before deriving scores.
- Store facts, not vibes.
- Use evidence refs, not copied private payloads.
- Keep authority, economics, reputation, taste, and routing as separate
  dimensions.
- Treat scores as projections that can be recomputed.
- Version trust by source SHA, manifest version, Agent Card hash, prompt
  version, policy version, and memory namespace version where available.
- Redact raw inputs, secrets, signed tokens, and private traces.
- Preserve enough lineage to explain forks, promotions, rollback, retirement,
  tombstones, and severance.
- Let policy decide whether any signal can cause a rewrite or routing change.

Rule: a high reputation score may make a node more discoverable. It cannot mint
capabilities, widen scope, ignore budget, skip review, delete data, or inherit
certification without policy.

## Ledger Event Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `ledger_event_id` | Stable id or deterministic hash over source row and event type. |
| `event_type` | Authority, execution, version, economics, reputation, status, deletion, approval, or policy event. |
| `source_ref` | Current source row: `WorkEvent`, `GrantAudit`, `LLMUsageEvent`, etc. |
| `subject_ref` | Agent, edge, process, version, memory namespace, credential, bounty, budget account, listing, lineage edge. |
| `actor_ref` | User, agent, process, scheduler, reviewer, platform, policy, or marketplace actor. |
| `initiator_ref` | Root command, parent process, webhook, schedule, user action, policy trigger, or signal. |
| `authority_path` | Grant id, parent grant id, owner check, policy decision, approval, process-local capability. |
| `policy_refs` | Policies and decisions that allowed, narrowed, denied, or blocked the event. |
| `evidence_refs` | Reviews, proofs, receipts, sessions, deployments, work events, commits, artifacts, traces. |
| `version_refs` | Source SHA, image tag, Agent Card hash, manifest version, prompt version, memory version. |
| `economic_refs` | Budget account, spend event, bounty, price, revenue share, settlement batch, if present. |
| `lineage_refs` | Parent/source agent, fork, merge, successor, tombstone, severance, certification lineage. |
| `metrics` | Cost, latency, tokens, score, calls, conversion, revenue, failure count, trust delta. |
| `redaction_class` | Public, owner, internal, security, legal, or secret ref. |
| `retention_class` | Ephemeral, operational, audit, billing, security, legal hold. |
| `created_at` | Event time. |

Ledger events may be projected at read time. If a future table is added, it
should store references and redacted summaries, not duplicate every raw payload.

## Current Rails Mapping

### WorkJob And WorkEvent

`WorkJob` is the closest current durable process ledger. It has job identity,
kind, status, user, thread, root/parent job ids, correlation id, idempotency key,
source, subject, worker, payloads, artifacts, proofs, metadata, attempts,
leasing, and timestamps.

`WorkEvent` is the closest current append-only event ledger. It has event id,
job id, sequence, parent event id, correlation id, event type, stage, status,
severity, message, actor, source, payload, artifacts, proofs, metrics, metadata,
and timestamp.

Projection:

```text
ledger_event:
  source_ref: WorkEvent(event_id)
  process_ref: WorkJob(job_id)
  parent/root: WorkJob.parent_job_id / root_job_id
  subject: WorkJob.subject_type + subject_id
  actor: WorkEvent.actor_type + actor_id
  evidence: artifact_refs + proof_refs
  metrics: WorkEvent.metrics
```

Use this as the v0 causal spine for generic work. Specialized rows can attach
to it by `job_id`, `correlation_id`, `source_id`, `subject_id`, `grant_id`,
`deploy_id`, `head_sha`, or `thread_id`.

### GrantAudit

`GrantAudit` records grant id, parent grant id, issuer, audience, bucket, mode,
allow/deny patterns, outputs prefix, TTL, user, decision, decider, and reason.

Projection:

```text
ledger_event:
  event_type: capability_granted | capability_denied | capability_narrowed
  subject: edge/capability
  authority_path: parent_grant_id -> grant_id
  policy: decision + decided_by + reason
```

This is the source of truth for v0 authority provenance. It is not enough for
global revocation by itself because runtime signed grants may remain valid until
TTL unless a server-side revocation check exists.

### LLMUsageEvent

`LLMUsageEvent` attributes LLM calls to user, thread, DAG run, grant id, agent,
skill, source, provider, model, prompt/completion/total tokens, cost, metadata,
and timestamp.

Projection:

```text
ledger_event:
  event_type: llm_spend
  subject: agent/skill/process
  economic_refs: grant_id, dag_run_id, thread_id
  metrics: tokens, cost_usd, model, provider
```

This is current cost evidence. It can support budget enforcement, cheap-success
signals, route cost comparison, and billing audit. It is not revenue settlement.

### UserControlPolicy

`UserControlPolicy` stores monthly budget, per-run budget, max calls per run,
file-write approval setting, network deny, approved-agent restrictions,
PII-safe mode, and approved agents.

Projection:

```text
ledger_event:
  event_type: budget_policy_applied | budget_blocked | policy_ceiling
  subject: user budget account / process
  policy_refs: user_control_policy
```

This is a policy ceiling and budget limit, not a wallet.

### Bounty

`Bounty` stores posted demand: slug, title, tags, reward amount, currency,
poster, claimed agent, claimant, status, claimed time, and fulfilled time.

Projection:

```text
ledger_event:
  event_type: bounty_posted | bounty_claimed | bounty_fulfilled | bounty_cancelled
  subject: bounty:<slug>
  economic_refs: none (bounties carry no monetary reward)
  lineage_refs: claimed_agent_id
```

Bounties are current demand and fixed reward evidence. They are not yet a
general per-call revenue ledger.

### Agent

`Agent` stores owner, organization, Gitea owner, `source_agent_id`, name,
description, version, image, public flag, status, URL, Agent Card, and
timestamps.

Projection:

```text
ledger_event:
  event_type: agent_created | lineage_linked | discovery_state_changed
  subject: agent:<name>
  lineage_refs: source_agent_id
  status_refs: public, status, card hash
```

`source_agent_id` is the current lineage hook. It should not imply trust,
certification, rank, or economic entitlement without explicit inheritance
policy and evidence.

### Deployment, Review, Proof, Receipt, Session

These rows provide version and trust evidence:

- deployments connect agent/source SHA/image/URL/status/events
- reviews record findings, severity counts, summary, and status
- proofs record grant id, version refs, events, file ops, result, and status
- receipts record signed runtime execution evidence
- sessions record replayable execution event logs

Projection:

```text
ledger_event:
  event_type: version_deployed | review_completed | proof_completed |
              receipt_recorded | session_recorded
  subject: agent version / process / skill
  version_refs: head_sha, image, card_hash
  evidence_refs: deploy_id, review_id, proof id, receipt_id, session_id
```

Trust and reputation projections should be built from these rows first.

## Ledger Event Taxonomy

### Authority Events

| Event | Meaning |
|---|---|
| `capability_requested` | Actor requested a capability or grant. |
| `capability_granted` | Policy allowed a scoped capability. |
| `capability_denied` | Policy denied or returned empty scope. |
| `capability_narrowed` | Requested scope was reduced before issuance. |
| `capability_delegated` | Child capability was minted from parent. |
| `capability_revoked` | Capability, edge, grant, or process-local right was revoked. |
| `policy_applied` | Policy decision constrained an action. |
| `approval_requested` | Human/platform approval was required. |
| `approval_granted` | Required approval was granted. |
| `approval_denied` | Approval was denied or expired. |

### Execution And Process Events

| Event | Meaning |
|---|---|
| `process_created` | Work/process started or was queued. |
| `process_state_changed` | Process moved through scheduler state. |
| `task_succeeded` | Agent/process completed requested work. |
| `task_failed` | Agent/process failed or errored. |
| `artifact_emitted` | Artifact/object/file was produced. |
| `handoff_started` | Parent invoked child/dependency. |
| `handoff_completed` | Child/dependency returned. |
| `receipt_recorded` | Signed execution receipt was persisted. |
| `session_recorded` | Replay session was persisted. |

### Version And Rewrite Events

| Event | Meaning |
|---|---|
| `version_created` | Source SHA, manifest, prompt, Agent Card, memory version, or image was created. |
| `source_mutated` | Source repo changed. |
| `manifest_mutated` | Synthetic manifest changed. |
| `prompt_mutated` | Prompt/system instructions changed. |
| `memory_mutated` | Memory was written, summarized, redacted, or purged. |
| `dependency_changed` | Dependency edge was added, removed, weakened, or strengthened. |
| `deployment_started` | Build/deploy process started. |
| `deployment_completed` | Deployment reached live/failed/rolled-back state. |
| `rollback_completed` | Previous version or state was restored. |

### Quality, Trust, And Reputation Events

| Event | Meaning |
|---|---|
| `review_completed` | Reviewer evaluated a version or behavior. |
| `proof_completed` | Proof run evaluated behavior under a grant. |
| `trial_scored` | Trial room scored an agent output. |
| `finding_opened` | Review/evaluator finding was created. |
| `finding_resolved` | Later evidence shows a finding was fixed. |
| `certification_granted` | Certification/trust marker was granted. |
| `certification_revoked` | Certification/trust marker was revoked. |
| `reputation_delta` | Reputation projection changed with evidence refs. |
| `trust_decayed` | Trust decreased due to age, failures, or stale evidence. |

### Economic Events

| Event | Meaning |
|---|---|
| `budget_account_created` | Budget node/account exists for user/org/agent/process. |
| `budget_allocated` | Budget was assigned to process/node/edge. |
| `budget_reserved` | Spend was reserved before work. |
| `budget_spent` | LLM/tool/runtime spend occurred. |
| `budget_released` | Unused reserved budget was released. |
| `budget_exceeded` | Policy blocked or stopped work due to budget. |
| `bounty_posted` | Demand and reward were created. |
| `bounty_claimed` | Agent/user claimed a bounty. |
| `bounty_fulfilled` | Poster marked bounty fulfilled. |
| `revenue_earned` | Agent/service earned value. |
| `revenue_shared` | Value was split across owners/dependencies. |
| `settlement_completed` | Payment/credit ledger settled. |

Only `llm_spend` and bounty lifecycle have concrete current rails. Revenue and
settlement events are future product ledgers.

### Discovery, Status, And Taste Events

| Event | Meaning |
|---|---|
| `listing_published` | Agent/listing became public. |
| `listing_hidden` | Agent/listing was hidden or soft-deleted. |
| `status_changed` | Agent/listing/process status changed. |
| `rank_changed` | Marketplace/discovery rank projection changed. |
| `user_feedback_recorded` | Explicit rating, acceptance, rejection, or comment was recorded. |
| `conversion_recorded` | User chose, installed, deployed, bought, or reused after exposure. |
| `taste_signal_recorded` | Explicit product-quality or aesthetic preference was recorded. |

Taste, sex-appeal, polish, or product-quality signals must be explicit,
auditable signal types. They are never hidden magic and never authority.

### Deletion And Retention Events

| Event | Meaning |
|---|---|
| `node_frozen` | Node stopped accepting call/mutate/delegate/routing activation. |
| `edge_frozen` | Edge stopped activating. |
| `node_retired` | Node stopped new work but retained evidence. |
| `credential_revoked` | Live credential material was removed. |
| `data_purged` | Allowed data namespace/key was deleted. |
| `identity_tombstoned` | Minimal identity marker was retained. |
| `lineage_severed` | Trust/status/certification inheritance was blocked. |

These events map to the P9 deletion model.

## Lineage Model

Lineage answers: what did this come from, what changed, and which inherited
claims remain valid?

Lineage dimensions:

| Dimension | Meaning |
|---|---|
| Source lineage | Derived from source repo, source agent, manifest, prompt, or dataset. |
| Version lineage | Source SHA, image tag, Agent Card hash, manifest version, prompt version. |
| Ownership lineage | Same owner/org, transferred owner, imported external agent, fork owner. |
| Capability lineage | Capabilities delegated from parent grant or process. |
| Trust lineage | Reviews, proofs, certifications, receipts, and resolved findings. |
| Reputation lineage | Usage success/failure, user feedback, marketplace signals. |
| Economic lineage | Revenue share, bounty claim, budget spend, dependency contribution. |
| Deletion lineage | Retired, tombstoned, severed, replacement, successor. |

Current concrete hook: `Agent.source_agent_id`. Future lineage should add
explicit edge/event refs instead of overloading one column.

### Identity Preservation

A mutation may preserve identity when all are true:

- owner or authorized process controls the target
- Agent Card contract remains compatible or changes are reviewed
- source/prompt/manifest mutation is versioned
- review/proof/canary gates pass when required
- no policy requires fork/new identity
- tombstone/severance state does not block inheritance

Examples:

- small source patch under same Agent Card
- prompt improvement with same declared skill behavior
- dependency removal that does not change external contract
- memory summarization under same namespace policy

### New Node Or Fork

A change should create a new node/fork when any are true:

- owner changes without explicit transfer policy
- Agent Card skill contract changes incompatibly
- source/manifest rewrite changes core purpose
- certification does not apply to the new version
- model/prompt/data dependency changes risk class materially
- marketplace listing identity should not carry old reputation
- ancestor was tombstoned or lineage was severed

Forks may link to ancestors for explainability, but inheritance must be
explicit and policy-bound.

### Inheritance Rules

Trust inheritance:

- version-specific review/proof trust applies only to the evaluated version
- compatible patch versions may inherit limited trust after policy
- major contract changes require fresh review/proof
- unresolved critical findings inherit forward until resolved or severed
- ancestor safety warnings remain visible to reviewers

Reputation inheritance:

- usage reputation may decay or partially inherit across compatible versions
- user feedback should be tied to product surface and version where possible
- marketplace rank should not fully transfer to forks by default
- status/reputation inheritance can be severed by P9 lineage policy

Economic inheritance:

- revenue share never inherits implicitly
- dependency revenue must be declared by economic edges or contracts
- bounty fulfillment attaches to the claiming agent/version evidence
- budget accounts do not transfer without owner/org policy

Certification inheritance:

- certification is narrower than source lineage
- certification may require exact version, version range, card hash, or
  dependency set
- certification revocation blocks promotion and inheritance

## Reputation And Trust Projection

Reputation is a computed projection, not an append-only source of truth.

Inputs:

- review status and finding severities
- proof status and result quality
- receipt success/failure counts
- replay/session evidence quality
- task success/failure and repeated failure shapes
- latency, token spend, and cost for equivalent tasks
- user feedback and acceptance/rejection
- bounty fulfilled/cancelled/claimed outcomes
- unsafe behavior, policy denials, revocations, and tombstones
- freshness of evidence
- version compatibility and lineage state

Projection outputs:

| Output | Meaning |
|---|---|
| `trust_score` | Evidence-backed confidence for a version/skill. |
| `quality_score` | Reviewer/proof/trial/user outcome projection. |
| `reliability_score` | Success/failure/latency/cost stability. |
| `safety_score` | Policy/safety/revocation risk projection. |
| `freshness_score` | Whether evidence is recent enough. |
| `market_rank_score` | Discovery preference, after policy and redaction. |
| `residual_risks` | Open findings, warnings, stale evidence, lineage caveats. |

Rules:

- Scores must explain their evidence refs.
- Scores are scoped by agent, skill, version, and audience.
- Scores cannot grant permissions.
- Critical safety or policy findings can cap route weight regardless of other
  positive signals.
- Reputation can decay with age, drift, failed review, or stale dependencies.
- User taste can influence rank but not safety/trust certification.

## Economic Model

The graph should model economics as nodes, edges, and ledger events:

- budget account nodes for user, org, agent, process, bounty, or experiment
- spend edges from process to model/tool/runtime
- earning edges from caller/marketplace/bounty to agent/service
- revenue-share edges from agent to dependencies/owners
- reserve/spend/release events for budget enforcement
- settlement events for future billing/payment ledger

Current concrete rails:

- `UserControlPolicy` enforces monthly and per-run budget limits.
- `LLMUsageEvent` records token and cost attribution.
- `Bounty` records fixed reward demand, claim, and fulfillment state.
- Grant and process budgets bound runtime authority and recursion.

Future rails:

- `BudgetAccount`
- `BudgetReservation`
- `EconomicEvent`
- `PriceSchedule`
- `RevenueShareEdge`
- `SettlementBatch`

Rules:

- Budget is not authority to touch data.
- Revenue does not authorize broader scope.
- A cheap route still needs valid capability.
- A high-earning agent still needs review/policy gates.
- Revenue share must be explicit and ledgered.
- Costs and earnings should be version/skill/process attributable.
- Settlement should be idempotent and replayable from economic events.
- Product ranking may include price and conversion only after policy filters.

## Status And Taste Signals

Status is a projection used for discovery, dashboards, and routing
explanations. Taste is explicit product preference evidence.

Status signal inputs:

- public/listing visibility
- live/deploying/failed/needs_auth/retired/tombstoned state
- review/proof status
- recent success/failure
- bounty fulfillment
- marketplace installation/use/conversion
- owner/operator flags
- lineage and severance state

Taste signal inputs:

- explicit user rating
- explicit acceptance/rejection
- user-selected style/aesthetic preference
- conversion after exposure
- repeat use for similar task
- owner/marketplace curated labels

Rules:

- Taste signals must record subject, audience, and evidence ref.
- Taste signals should be versioned or time-bounded.
- Taste can raise discovery rank for an audience segment.
- Taste cannot override safety, owner policy, budget, or capability checks.
- Do not infer sensitive preferences from private payloads for public ranking.
- Public taste/status summaries must be redacted.

## Routing With Ledger Signals

Routing decision order:

```text
1. Filter by valid capability, policy, freeze/revocation, budget, owner/org rules.
2. Filter by version compatibility and required certifications.
3. Apply hard safety blocks and retention constraints.
4. Score candidates from trust, cost, latency, quality, freshness, status,
   taste, and user/org preferences.
5. Emit routing explanation with evidence refs.
6. Record route outcome as ledger/signal evidence.
```

Scoring dimensions may include:

- trust score
- open risk count
- cost estimate
- latency estimate
- recent success rate
- proof/review freshness
- user/org preference
- marketplace status
- taste/product-quality fit

No score can resurrect a revoked edge, bypass policy, skip budget, or hide a
critical finding.

## Redaction Rules

| Ledger class | Redaction rule |
|---|---|
| Authority | Show grant id/decision/scope summary, not signed grant token. |
| Execution | Show status, skill, timing, artifact refs; redact private inputs/outputs. |
| Version | Show commit/card/image refs allowed by audience; redact private repo data. |
| Review/proof | Show finding summary/severity; redact private traces and secrets. |
| Economics | Show cost/reward summaries by audience; redact payment instruments. |
| Reputation | Show score explanation and evidence refs; do not expose private payloads. |
| Taste | Show explicit feedback summary only where consent/audience permits. |
| Deletion | Show tombstone/reason class; redact legal/security/private details. |
| Security | Restrict to authorized security/admin contexts. |
| Secret refs | Store pointer/hash/last4 only, never raw secret. |

Retention defaults:

- authority, approvals, reviews, proofs, receipts, deployments: audit
- LLM spend, bounty, billing/settlement: billing/audit
- policy violation, abuse, credential misuse: security
- progress/status events: operational
- private payloads/traces: object refs with stricter retention
- taste/user feedback: product retention policy, with audience controls
- tombstones/severance: audit or security depending on reason

## Examples

### Bounty To Reputation

```text
event: bounty_posted
  subject: bounty:invoice-parser
  reward_cents: 5000

event: bounty_claimed
  subject: bounty:invoice-parser
  claimed_agent: agent:invoice-agent

event: deployment_completed
  subject: agent:invoice-agent version:sha:new

event: review_completed
  status: passed

event: proof_completed
  status: passed

event: bounty_fulfilled
  subject: bounty:invoice-parser
  claimed_agent: agent:invoice-agent

projection:
  demand_signal + quality_signal + reputation_delta
```

The bounty reward is demand/economic evidence. It does not certify every future
version of the agent.

### Cheap Success Route

```text
event: task_succeeded
  subject: edge:router->summary-agent

event: llm_spend
  cost_usd: 0.002
  total_tokens: 800

signal: cheap_success
  suggested_effect: increase_routing_weight
```

The route weight can increase only after policy confirms capability, budget,
and safety constraints.

### Compatible Patch Preserves Identity

```text
event: source_mutated
  old_version: sha:old
  new_version: sha:new

event: review_completed
  status: passed

event: proof_completed
  status: passed

projection:
  identity_preserved: true
  inherited_trust: limited_to_skill:render_chart
```

The old version remains explainable and can be rolled back if later signals
degrade.

### Fork With Limited Inheritance

```text
event: lineage_linked
  parent: agent:research-agent
  child: agent:research-agent-fork

policy:
  inheritance: source_only

projection:
  source lineage visible
  trust/reputation/certification not inherited
```

Source lineage is not trust inheritance.

### Taste Signal

```text
event: user_feedback_recorded
  subject: agent:deck-builder version:sha:abc
  audience: owner
  feedback_type: aesthetic_preference
  value: accepted

signal: taste_signal_recorded
  subject: product_surface:deck_output
  suggested_effect: increase_discovery_for_similar_preferences
```

Taste affects discovery and ranking for the appropriate audience segment. It
does not grant source, memory, credential, or policy authority.

## V0 Build Guidance

Do not start by adding a broad ledger table.

First practical wedge:

1. Project ledger events from `WorkEvent`, `GrantAudit`, deployments, reviews,
   proofs, receipts, sessions, `LLMUsageEvent`, bounties, and agents.
2. Normalize evidence refs and join keys for one owned agent.
3. Add reputation/trust summaries as read-only projections over evidence.
4. Add lineage summary from `source_agent_id`, source SHA, Agent Card hash,
   reviews, proofs, and tombstone/severance state when available.
5. Add economic summary from LLM usage and bounties only; label future revenue
   share as unavailable until product ledgers exist.
6. Add routing explanation schema that separates policy filters from scoring
   weights.
7. Add explicit user feedback/taste events only when there is a product surface
   ready to collect them.
8. Add first-class `LedgerEvent` only after projection queries prove too slow,
   ambiguous, or impossible from current tables.

Suggested v0 dossier section:

```text
ledger_summary:
  latest_version: sha:...
  authority:
    grants: [...]
    denials: [...]
  quality:
    latest_review: ...
    latest_proof: ...
    open_findings: [...]
  lineage:
    source_agent_id: ...
    inherited_trust: limited|none|blocked
  economics:
    llm_cost_usd_30d: ...
    bounties_fulfilled: ...
    revenue_share: unavailable
  reputation:
    trust_score: ...
    residual_risks: [...]
  status:
    public: true
    live_state: running
    tombstone: none
```

## Open Gaps

- No first-class generic `LedgerEvent` projection table.
- No stable reputation/trust projection API yet.
- No explicit versioned lineage edge beyond `Agent.source_agent_id`.
- No certification inheritance policy.
- No revenue-share or settlement ledger.
- No budget account abstraction beyond user control policy and grant/process
  budgets.
- No durable marketplace conversion/rank/product feedback ledger.
- No explicit taste signal collection surface.
- No common routing explanation endpoint tying policy filters to score weights.
- No first-class tombstone/severance row yet.
- No universal redaction policy for ledger projections across public, owner,
  internal, security, legal, and secret-ref audiences.

These gaps should be closed incrementally from existing evidence projections.
The graph kernel should not invent ambient reputation, money, rank, or taste.
Every such signal must point back to causal evidence and remain subordinate to
capability and policy.
