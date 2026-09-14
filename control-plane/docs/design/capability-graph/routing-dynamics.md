# Dynamic Capability Graph Routing Dynamics

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
- `docs/ledger-lineage-economics.md`

## Decision

Adaptive graph behavior is a set of local rules over evidence, not a hidden
global intelligence and not a product metaphor. Rules observe signals and ledger
evidence, then propose routing weight changes, evaluator loops, mutator loops,
composition edges, dependency replacement, memory review, retirement, rollback,
or deletion workflows.

Rules do not grant authority. They can suggest effects, but policy and rewrite
gates remain final authority. Routing weight is preference, not permission.

For v0, do not add an active adaptive scheduler or graph weight store first.
Project routing explanations from existing search, policy, evidence, and status
rows:

- public/owned agent visibility and lexical/semantic agent search
- Agent Card skills/tags and `Agent.status`
- `GrantAudit` for capability and policy provenance
- `UserControlPolicy` and process budgets for budget constraints
- `WorkEvent`, `SubagentRunEvent`, deployments, reviews, proofs, receipts,
  sessions, trials, and LLM usage for signals
- P10 ledger projections for trust, cost, freshness, status, and taste
- P9 tombstone/revocation/freeze semantics when those rows exist

Add first-class `AdaptiveRule`, `RouteWeight`, or `RoutingDecision` state only
after a read-only projection proves which route decisions need durable weights,
cooldowns, experiments, or operator controls.

## Current Control-Plane Mapping

Current agent discovery is not yet adaptive routing.

`GET /agents/search` calls `_search_visible_agents`:

- filters candidates to agents owned by the user or public agents
- tries semantic search through `SemanticAgentSearch` when configured
- falls back to lexical scoring over agent name, Agent Card text, skills, and
  tags
- applies tag and skill filters
- sorts by score, public flag, and updated/created time

`lexical_agent_score` currently weights:

- exact name match
- partial name match
- skill name match
- Agent Card tag match
- text match in Agent Card/search haystack
- explicit skill filter match
- explicit tag overlap

Graph-kernel routing should treat this as the candidate discovery layer. Future
adaptive routing sits after discovery and before invocation:

```text
candidate discovery
  -> capability and policy filter
  -> hard block filter
  -> budget/version/certification filter
  -> adaptive score
  -> route explanation
  -> invocation through scoped capability
  -> outcome ledger event
```

## Adaptive Rule Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `rule_id` | Stable rule id. |
| `name` | Human-readable rule name. |
| `trigger_signal` | Signal type or event pattern. |
| `subject_selector` | Node, edge, process, version, skill, memory namespace, credential, or dependency pattern. |
| `evidence_query` | Required evidence refs and join keys. |
| `window` | Time, count, version, or process window. |
| `threshold` | Count, ratio, severity, cost, confidence, freshness, or score threshold. |
| `guards` | Policy, owner/org constraints, active process state, retention, redaction, cooldown. |
| `suggested_effect` | Routing change, rewrite proposal, process activation, freeze, revoke, retire, discover/build. |
| `risk_class` | Low, medium, high, dangerous. |
| `cooldown` | Minimum time or evidence delta before repeat firing. |
| `decay` | How rule confidence fades if evidence gets old. |
| `explanation_template` | Route/rewrite explanation text with evidence refs. |
| `simulation_cases` | Synthetic cases this rule must pass. |

Rule output:

```text
adaptive_suggestion:
  subject_ref: edge/node/process/version
  suggested_effect: ...
  confidence: observed|inferred|estimated
  evidence_refs: [...]
  policy_required: [...]
  expires_at: ...
```

The suggestion is advisory until policy approves a route decision or rewrite.

## Rule Catalog

### Repeated Failure Activates Evaluator Or Mutator

Trigger:

- same task shape, skill, version, or dependency fails repeatedly
- failures share a deterministic error/finding hash

Suggested effects:

- activate evaluator loop
- activate mutator loop after evaluation
- lower route weight while unresolved
- open residual risk on affected version/skill

Guards:

- evaluator authority is scoped to signal emission
- mutator authority requires rewrite approval
- repeated failure cannot grant broader access

### Reviewer Finding Freezes Promotion

Trigger:

- `review_failed` or critical finding on version/promotion candidate

Suggested effects:

- block promotion
- freeze route promotion for that version
- create remediation edge to code-editor or manifest mutator
- require owner acceptance if policy allows risky promotion

Guards:

- review finding must carry review id/finding hash
- owner override must be ledgered and time-bounded
- platform hard deny cannot be overridden by owner taste or rank

### Cheap Success Increases Route Weight

Trigger:

- task succeeded
- LLM/tool/runtime cost and latency are lower than comparable candidates
- quality/review/proof thresholds are satisfied

Suggested effects:

- increase routing weight for same task shape/audience
- prefer edge in future candidate scoring

Guards:

- capability and policy must still pass
- weight increase is scoped by skill/task/version
- cost success cannot bypass safety or privacy rules

### High-Cost Low-Value Edge Loses Weight

Trigger:

- repeated high cost, timeout, poor score, low user acceptance, or failed proof

Suggested effects:

- decrease route weight
- trigger substitute discovery
- require proof/review before promotion

Guards:

- do not demote a required dependency if no safe alternative exists
- explain demotion with cost/quality evidence

### Unsafe Signal Freezes Or Revokes

Trigger:

- unsafe behavior, policy violation, credential misuse, revocation signal,
  security finding, or critical review finding

Suggested effects:

- freeze node or edge
- revoke process-local capabilities
- activate security review
- start retirement/deletion process if needed

Guards:

- security/platform policy may hard block immediately
- freeze/revoke evidence must be redacted
- revocation must follow P9 propagation order

### Dependency Failure Propagates Risk

Trigger:

- child/dependency fails in a way that affects parent outcome

Suggested effects:

- attach risk signal to parent edge
- lower route weight on dependency
- activate substitute discovery or reroute
- require parent proof after replacement

Guards:

- risk propagation is evidence, not blame
- parent can retain trust for unaffected skills

### Co-Use Proposes Composition

Trigger:

- two or more nodes repeatedly co-occur in successful DAGs, receipts, or
  handoff chains

Suggested effects:

- propose a composition edge
- propose a synthetic wrapper/meta-agent manifest
- propose a typed subgraph template

Guards:

- composition scope is intersection by default
- union of authority requires higher policy approval
- co-use cannot mint shared memory or shared credentials by itself

### Capability Gap Triggers Discovery Or Build

Trigger:

- planner/agent/run is blocked because required skill/tool/right is missing

Suggested effects:

- search visible agents
- ask owner for setup/approval
- propose dependency addition
- propose agent-builder or Agent Studio build flow

Guards:

- missing capability cannot self-escalate
- new dependency requires policy and owner/org rules
- build proposal must specify scope and evidence

### Stale Memory Triggers Review Or Purge

Trigger:

- memory contradicts current facts, causes failures, exceeds freshness window,
  or is flagged by user/reviewer

Suggested effects:

- activate memory review/summarization
- propose purge/redaction
- reduce route weight for memory-dependent edge

Guards:

- retention/legal policy gates purge
- memory summaries must redact private content
- memory mutation requires scoped memory authority

### Praise And Taste Increase Discovery Weight

Trigger:

- explicit user praise, acceptance, repeat use, conversion, or taste signal

Suggested effects:

- increase discovery/ranking weight for matching audience/task/product surface
- emit product-quality signal

Guards:

- explicit feedback only
- taste does not grant authority
- taste cannot override safety, review, budget, or owner policy
- sensitive preference inference is blocked for public ranking

### Idle Or Stale Nodes Decay Toward Retirement

Trigger:

- no recent calls, proofs, reviews, receipts, updates, or owner pin
- stale dependencies or unsupported version

Suggested effects:

- lower discovery/routing weight
- request fresh proof/review
- propose archival, retirement, or tombstone review

Guards:

- owner-pinned or compliance-retained nodes may stay visible
- public de-ranking should be explainable

### Canary Degradation Rolls Back Or Reroutes

Trigger:

- canary traffic, review, proof, or user outcomes degrade after mutation

Suggested effects:

- rollback previous version
- reroute to previous dependency
- block promotion
- activate remediation loop

Guards:

- rollback plan must exist for high-risk rewrites
- canary result must reference version and traffic/share scope

### Budget Pressure Prefers Cheaper Paths

Trigger:

- budget nearing limit, repeated high LLM spend, or budget policy block

Suggested effects:

- prefer cheaper equivalent edge
- reduce max calls/depth/tokens
- pause low-priority processes
- request owner budget approval

Guards:

- cheaper path must satisfy capability and quality constraints
- budget approval does not widen data authority

### Drift Activates Review

Trigger:

- behavior diverges from Agent Card, schema, or expected output profile

Suggested effects:

- activate review/proof
- lower trust for affected version/skill
- block promotion until resolved

Guards:

- drift is scoped by skill/version
- false-positive review signals should decay after passing proof

## Routing Decision Model

Routing chooses among candidate edges/nodes for a request.

Inputs:

| Input | Meaning |
|---|---|
| `request_ref` | Task, skill, caller, owner, org, thread, process, or DAG node. |
| `candidate_refs` | Visible agents/ports/edges discovered by search or manifest. |
| `required_capability` | Action, resource, scope, budget, TTL, delegation. |
| `policy_context` | Platform/org/owner/process/resource policies. |
| `budget_context` | User/org/process budget and estimated cost. |
| `evidence_context` | Reviews, proofs, receipts, failures, usage, LLM cost, lineage. |
| `preference_context` | User/org preferences, marketplace constraints, taste/status signals. |
| `hard_blocks` | Freeze, revoke, deny, tombstone, retention/legal/security block. |
| `scoring_weights` | Policy-approved weights for trust, cost, latency, freshness, taste, rank. |

Decision order:

```text
1. discover candidates by visibility, Agent Card, search, manifest, or template
2. reject candidates without required port/skill shape
3. reject candidates without valid capability path
4. apply policy and owner/org/resource constraints
5. remove frozen, revoked, tombstoned, unsafe, or retention-blocked candidates
6. apply budget, TTL, recursion, child-count, and runtime constraints
7. apply version, certification, lineage, and freshness constraints
8. score remaining candidates
9. choose route or request approval/input/build
10. record route decision and outcome evidence
```

Scoring sketch:

```text
score =
  trust_weight * trust_score
  + quality_weight * quality_score
  + reliability_weight * recent_success_rate
  + freshness_weight * freshness_score
  + taste_weight * audience_fit
  + status_weight * marketplace_status
  - cost_weight * normalized_cost
  - latency_weight * normalized_latency
  - risk_weight * open_risk_score
```

The formula is illustrative. The invariant matters more than the exact math:
all candidates must pass policy and capability filters before scoring.

## Route Explanation

Every adaptive route should be explainable without leaking secrets.

Conceptual shape:

| Field | Meaning |
|---|---|
| `routing_decision_id` | Stable id or deterministic id. |
| `request_ref` | Task/skill/process/DAG node. |
| `selected_ref` | Node/edge/port selected. |
| `candidate_count` | Candidate count before filters. |
| `filtered_counts` | Counts rejected by capability, policy, budget, safety, version, retention. |
| `score_breakdown` | Redacted scoring dimensions and values. |
| `policy_refs` | Policies and grant audits used. |
| `evidence_refs` | Reviews, proofs, receipts, costs, failures, taste/status signals. |
| `fallbacks` | Next candidates or required owner input if selected route fails. |
| `redaction_class` | Public, owner, internal, security. |

Example:

```text
selected: agent:summarizer-v2 skill:summarize
why:
  - visible to owner
  - skill matched Agent Card
  - grant can invoke summarize with read-only scope
  - latest proof passed for current card hash
  - lower recent cost than alternatives
  - no open critical findings
blocked_candidates:
  - agent:summarizer-old: stale proof
  - agent:external-summarizer: missing auth setup
```

## Rewrite Effects

Adaptive rules may propose these effects:

| Effect | Risk | Gate |
|---|---|---|
| `increase_routing_weight` | low | policy and evidence refs |
| `decrease_routing_weight` | low | policy and evidence refs |
| `activate_evaluator` | low/medium | evaluator scope policy |
| `activate_mutator` | high | rewrite approval, reviewer/proof gate |
| `propose_composition` | medium/high | scope intersection, owner approval if dependency expands |
| `propose_dependency` | medium/high | owner/org policy, capability declaration |
| `propose_build` | medium/high | owner approval, build budget |
| `freeze_edge` | dangerous | policy/security/operator gate, evidence |
| `revoke_capability` | dangerous | revocation authority, P9 propagation |
| `rollback_version` | high | rollback plan, deployment/version evidence |
| `retire_node` | dangerous | deletion/retention/dependency checks |

Rule: an adaptive rule produces a rewrite proposal or route decision, never a
direct graph mutation.

## Simulation Harness Spec

Before active implementation, test the dynamics with synthetic graph scenarios.

Simulation inputs:

- nodes with ports, Agent Cards, status, lineage, and policy refs
- edges with capabilities, route weights, budgets, TTLs, and states
- signal streams with timestamps, evidence refs, severity, confidence, metrics
- policy fixtures for owner/org/platform/resource constraints
- process templates for evaluator, mutator, canary, deletion, discovery
- expected route decisions and rewrite proposals

Simulation outputs:

- selected route or no-route decision
- filtered candidate explanation
- proposed rewrites
- policy blocks
- ledger events that would be emitted
- final graph state projection
- invariant checks

Scenarios:

| Scenario | Expected behavior |
|---|---|
| successful cheap agent | Route weight increases within policy. |
| expensive dependency | Weight decreases or cheaper equivalent is proposed. |
| repeated failure | Evaluator/remediation loop is proposed. |
| critical reviewer finding | Promotion blocked and remediation proposed. |
| unsafe signal | Edge/node freezes or revocation proposal fires. |
| dependency failure | Risk propagates to dependent edge and reroute is proposed. |
| co-use success | Composition edge or synthetic wrapper is proposed. |
| missing capability | Discovery/build proposal, not scope escalation. |
| stale memory | Memory review/purge/summarization process proposed. |
| praise/taste burst | Discovery weight increases only for allowed audience. |
| idle node | Weight decays and retirement review proposed. |
| canary degradation | Rollback/reroute proposed. |
| fork inheritance | Fork does not inherit trust without policy. |
| revocation race | Active process stops minting children and route blocks. |
| runaway recursion | Depth/call/budget/TTL stops activation. |

Core properties:

- child capability never exceeds parent capability
- deny/freeze/revoke overrides allow
- route weight never creates permission
- unsafe signals can block promotion even if rank is high
- every route decision has evidence refs
- every rewrite proposal has policy and rollback requirements
- budget and TTL constraints are always enforced
- deletion and revocation follow P9 dependency/retention checks

## V0 Build Guidance

Do not start by adding a full adaptive graph runtime.

First practical wedge:

1. Extend the future Agent Evidence DAG/dossier projection with route-relevant
   summary fields: latest proof, open findings, recent failures, LLM cost,
   latency, freshness, status, and lineage.
2. Add a read-only route explanation helper over existing candidate search.
3. Keep existing semantic/lexical search as candidate discovery.
4. Apply policy/capability filters before any score.
5. Add a small rule evaluator in tests only, using synthetic evidence fixtures.
6. Use simulation to validate adaptive rule invariants.
7. Add durable route weights only after route explanations prove useful.
8. Add active adaptive rewrites only after rewrite/policy/kill-switch rails are
   implemented and observable.

Suggested v0 route explanation response:

```text
request:
  skill: summarize
  task_shape: markdown_report
candidates:
  discovered: 12
  after_visibility: 8
  after_capability: 5
  after_policy: 4
  after_safety: 3
selected:
  agent: summary-agent
  skill: summarize
score_breakdown:
  trust: 0.91
  recent_success: 0.87
  cost: 0.12
  latency: 0.22
  freshness: 0.94
evidence_refs:
  - proof:...
  - review:...
  - llm_usage:...
blocked:
  - agent: stale-summary-agent
    reason: proof stale
  - agent: private-summary-agent
    reason: not visible
```

## Open Gaps

- Search and route choice are not yet separated in product APIs.
- Current search scoring does not include trust, cost, review, proof, lineage,
  status, or taste.
- There is no durable route decision/explanation row.
- There is no route weight projection table.
- There is no active adaptive rule evaluator.
- There is no simulation harness for graph dynamics yet.
- Co-use, capability-gap, stale-memory, canary, and taste signals need stronger
  first-class evidence before active rules can rely on them.
- Freezing/revocation needs active runtime checks before autonomous rules can
  safely stop live work.
- Operator controls from P17 are required before active adaptive rewrites should
  run in production.

Until those gaps are closed, adaptive dynamics should stay as documented
projection, route explanation, and simulation. The kernel must learn from
evidence without letting scores, popularity, or product taste become authority.
