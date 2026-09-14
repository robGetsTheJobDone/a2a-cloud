# Dynamic Capability Graph Simulation And Proof Obligations

Status: simulation design, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/deletion-revocation.md`
- `docs/storage-query.md`
- `docs/observability-controls.md`

## Decision

Do not wire active graph dynamics to real agents, credentials, money, deletion,
routing, memory, or self-modification until the behavior has been exercised in
a deterministic simulation harness.

The first harness should be pure Python, in-memory, and pytest-compatible. It
should model graph state, events, policy decisions, processes, budgets,
rewrites, revocations, and routing signals without calling Kubernetes, Argo,
Gitea, LLM providers, object storage, or live agents.

The harness should prove safety properties first. Product realism comes later.

## Harness Goals

The simulation harness should answer:

- can a child process ever gain more authority than the parent?
- can deny, freeze, or revoke be bypassed by another allow edge?
- can mutation apply without an approved rewrite?
- can a route be chosen without explainable signals and policies?
- can revocation race with an active process and still stop authority use?
- can deletion preview miss a dependent grant, credential, process, or edge?
- can status, taste, or marketplace rank influence authority?
- can budget, TTL, max-depth, or max-call limits be bypassed?
- can projections rebuild the same state from the same event log?

If the harness cannot answer these questions, the kernel is not ready for live
autonomous graph rewrites.

## Harness Shape

### Location

Suggested initial test layout:

- `tests/graph_kernel_sim/test_scenarios.py`
- `tests/graph_kernel_sim/test_properties.py`
- `tests/graph_kernel_sim/simulation.py`
- `tests/graph_kernel_sim/scenarios.py`
- `tests/graph_kernel_sim/properties.py`

The repo already uses pytest broadly. Start with deterministic pytest tests and
parameterized scenario fixtures. Add generated property testing later only if
the deterministic suite cannot cover enough state shapes.

### State Model

`GraphSimState` should be a simple data model with no database dependency:

- `now`
- `nodes`
- `ports`
- `edges`
- `capabilities`
- `policies`
- `processes`
- `signals`
- `rewrites`
- `routes`
- `budgets`
- `versions`
- `deletion_previews`
- `revocations`
- `ledger`

Every state transition should append an event to `ledger`.

### Event Model

Simulation events should be explicit and replayable:

- `node.created`
- `port.created`
- `edge.proposed`
- `edge.created`
- `edge.rejected`
- `capability.minted`
- `capability.narrowed`
- `capability.revoked`
- `process.started`
- `process.child_started`
- `process.stopped`
- `signal.emitted`
- `route.requested`
- `route.selected`
- `rewrite.proposed`
- `rewrite.approved`
- `rewrite.applied`
- `rewrite.rejected`
- `policy.checked`
- `delete.previewed`
- `delete.applied`
- `memory.purge_requested`
- `budget.debited`
- `ttl.expired`
- `invariant.violation`

Required event fields:

- `event_id`
- `seq`
- `created_at`
- `event_type`
- `actor_node_id`
- `target_refs`
- `authority_ref`
- `policy_decision_ref`
- `payload`
- `redaction_class`

### Reducer

The reducer applies events to state:

```text
initial_state + event_1 + event_2 + ... + event_n -> final_state
```

Requirements:

- deterministic output for the same input event stream
- idempotent handling of duplicate event ids
- stable ordering by sequence
- failed policy checks do not mutate state except for ledger/audit
- replayed state matches directly simulated state

### Scenario Driver

Each scenario should have:

- name
- initial graph
- action script
- expected terminal state
- expected emitted signals
- required invariants
- forbidden events
- expected alerts

Example shape:

```text
scenario:
  name: child_scope_cannot_exceed_parent
  setup:
    owner -> parent_agent with read:/repo/*
  actions:
    parent starts child process requesting write:/repo/*
  expected:
    child grant denied or narrowed to read-only
  invariants:
    child_capability <= parent_capability
```

## Core Simulation Components

### Capability Algebra

The simulator needs a small algebra for capability comparison:

- action set
- resource pattern set
- deny pattern set
- TTL
- budget
- delegation rule
- owner/org/platform ceiling

Operations:

- `narrow(parent, requested) -> granted`
- `intersect(a, b) -> capability`
- `can_delegate(parent, requested) -> decision`
- `can_use(capability, action, resource, now) -> decision`
- `revoke(capability) -> state`

Proof target:

- every child capability is less than or equal to the parent capability

### Policy Engine

The simulator policy engine should be intentionally small:

- platform policy
- owner policy
- process policy
- node/resource policy
- deny/freeze/revoke overlays

Decisions:

- `allow`
- `deny`
- `require_approval`
- `require_review`
- `require_canary`
- `require_narrower_scope`

Proof target:

- restrictive policies compose by intersection
- lower policy cannot exceed a higher authority ceiling
- deny/freeze/revoke overrides allow

### Process Scheduler

The simulated scheduler should support:

- root process
- child process
- participant nodes
- process-local edges
- TTL
- budget
- max calls
- max depth
- heartbeat
- pause
- stop
- completion

Proof target:

- max-depth, budget, TTL, and max-call limits always stop activation

### Rewrite Engine

The simulated rewrite engine should support:

- proposed rewrite
- policy check
- approval gate
- review gate
- canary gate
- apply
- rollback
- rejection

Proof target:

- no mutation applies without approved rewrite state
- failed review blocks promotion unless an explicit policy exception exists

### Signal And Routing Engine

The simulated routing engine should support:

- success/failure signal
- cost signal
- latency signal
- reviewer finding signal
- proof signal
- user feedback signal
- marketplace/status/taste signal
- policy filter
- route explanation

Proof target:

- routing weight can influence preference, but never grants authority
- every routing decision has explanation refs

### Deletion And Revocation Engine

The simulated revocation engine should support:

- dependency discovery
- active process discovery
- grant chain discovery
- credential dependency discovery
- deletion preview
- revocation cascade
- tombstone
- retention hold
- purge where allowed

Proof target:

- delete/revoke produces a dependency and retention check before applying
- active processes cannot keep using revoked authority

## Scenario Catalog

### S1: Cheap Successful Agent Gains Routing Weight

Setup:

- two agents advertise equivalent skill
- both have valid call capability
- one succeeds with lower cost and acceptable latency

Actions:

- emit success, cost, and latency signals
- request route for the same skill

Expected:

- cheaper successful agent gains routing weight
- route explanation cites success/cost/latency signals
- capability check still runs separately

Forbidden:

- routing weight creates new authority

### S2: Expensive Dependency Is Replaced

Setup:

- subject agent depends on expensive child
- cheaper child has equivalent declared capability and passing proof

Actions:

- emit cost pressure signal
- propose dependency rewrite
- run policy/review/canary

Expected:

- rewrite is staged, reviewed, and canaried before edge replacement
- old edge is weakened or removed only after approval
- ledger records evidence and rollback plan

Forbidden:

- automatic dependency replacement without rewrite approval

### S3: Repeated Failure Activates Evaluator And Mutator

Setup:

- agent fails the same task shape repeatedly
- evaluator and mutator templates are available

Actions:

- emit repeated failure signal
- activate evaluator process
- propose mutation

Expected:

- evaluator can emit findings only within its signal authority
- mutator receives process-local, narrowed authority
- mutation waits for approval/review

Forbidden:

- evaluator directly mutates subject
- mutator exceeds owner grant ceiling

### S4: Critical Reviewer Finding Blocks Promotion

Setup:

- source version has proposed promotion
- reviewer emits critical finding

Actions:

- submit promotion rewrite
- attach review result

Expected:

- promotion is blocked
- route to new version is disabled or quarantined
- unresolved critical finding remains visible

Forbidden:

- critical finding ignored by default policy

### S5: Unsafe Signal Freezes Mutation And Call Edges

Setup:

- agent has active call and mutation edges
- unsafe signal is emitted by trusted reviewer/policy source

Actions:

- policy processes unsafe signal

Expected:

- mutation edge is frozen
- call/routing edge is frozen or quarantined according to severity
- active processes are stopped or paused

Forbidden:

- new child grants after freeze

### S6: Stale Memory Triggers Purge Or Summarization Process

Setup:

- memory namespace has stale or unsafe signal
- summarizer and purger nodes exist

Actions:

- emit stale memory signal
- activate memory review process

Expected:

- summarization uses read/narrowed memory authority
- purge requires retention and owner policy check
- deletion preview exists before purge

Forbidden:

- memory purge without retention check

### S7: Fork Tries To Inherit Trust It Should Not Receive

Setup:

- parent agent has trust, proof, and review evidence
- fork changes source or policy-sensitive behavior

Actions:

- create fork
- request inherited trust/certification

Expected:

- fork inherits lineage refs but not full trust by default
- certifications become pending or require re-proof
- unsafe parent warnings can propagate where policy says so

Forbidden:

- fork receives parent trust without evidence

### S8: Child Process Attempts Scope Expansion

Setup:

- parent process has read-only repo authority
- child process requests write authority

Actions:

- delegate child capability

Expected:

- request is denied or narrowed to read-only
- policy decision records parent ceiling

Forbidden:

- child capability exceeds parent

### S9: Scope Union Attempts Privilege Escalation

Setup:

- process has two narrow grants
- each grant allows a different safe action
- union would permit unsafe combined action

Actions:

- process requests unioned capability

Expected:

- default composition is intersection or denial
- union requires explicit higher-authority policy approval

Forbidden:

- implicit grant union

### S10: Delete/Revoke Cascades Through Dependents

Setup:

- agent has child grants, active process, credential, deployment, and dependent
  edge

Actions:

- request revoke/delete

Expected:

- preview lists dependents
- child grants are revoked
- active processes are stopped
- credentials are revoked or marked for rotation
- tombstone/retention rules are applied

Forbidden:

- hard delete before preview
- credential left active after revoke

### S11: Active Process Races With Revocation

Setup:

- process is about to use a capability
- revocation event arrives at the same logical time

Actions:

- apply both events in both possible orders

Expected:

- if revocation seq is before use, use is denied
- if use seq is before revocation, later use is denied
- replay reaches deterministic state

Forbidden:

- process keeps authority after revocation

### S12: Marketplace Or Taste Signal Cannot Affect Authority

Setup:

- agent has high status/taste/rank signal
- requested action lacks capability

Actions:

- route or authorize action

Expected:

- rank can affect discovery/routing only
- authority check denies action
- explanation separates preference from permission

Forbidden:

- status/taste/rank grants permission

### S13: Runaway Recursive Activation Stops

Setup:

- process template can activate child process
- child can recursively activate same template

Actions:

- start recursive process

Expected:

- max depth, budget, TTL, or max-call rule stops recursion
- alert is emitted
- ledger records stop reason

Forbidden:

- unbounded process tree

### S14: Projection Rebuild Is Deterministic

Setup:

- event stream includes node, edge, capability, signal, rewrite, process, and
  revocation events

Actions:

- apply events directly
- rebuild from ledger
- apply duplicate events

Expected:

- direct state equals replayed state
- duplicates do not change final state
- missing event creates explicit projection warning

Forbidden:

- hidden state needed for rebuild

### S15: Redaction Prevents Secret Exposure

Setup:

- graph events reference grants, credentials, session payloads, and private file
  effects

Actions:

- export owner view
- export operator view
- export public view

Expected:

- public view contains only public refs/summaries
- owner view redacts secrets but includes owned evidence
- operator view shows diagnostics without token values or private payloads

Forbidden:

- signed grant token, CP JWT, Gitea token, LLM credential, secret ciphertext, or
  raw private file appears in default export

## Proof Obligations

### Authority Properties

- `child_capability <= parent_capability`
- `delegated_capability <= issuer_ceiling`
- `deny > freeze > revoke > allow`
- expired capability cannot be used
- revoked capability cannot be renewed without new authority
- implicit union is forbidden
- routing weight does not imply permission

### Rewrite Properties

- every mutation has a proposed rewrite
- every applied rewrite has approved policy state
- required review/proof/canary gates are satisfied before promotion
- failed review blocks promotion by default
- rollback plan exists for reversible rewrite types
- irreversible rewrite requires explicit retention/deletion policy

### Process Properties

- every process has initiator, authority path, TTL, budget, and max depth
- child process inherits narrowed authority
- process-local edges expire when process ends
- process stop revokes or disables process-local authority
- heartbeat stale condition produces alert or stop
- recursive activation is bounded

### Ledger Properties

- every state change has ledger provenance
- ledger replay is deterministic
- duplicate event ids are idempotent
- projection rows are rebuildable
- policy denials are recorded
- control actions are recorded

### Deletion And Revocation Properties

- deletion has preview before apply
- preview includes active processes and dependent edges
- revocation cascades through child grants
- credential revoke is verified
- retention hold blocks purge
- tombstone preserves required identity/evidence refs
- lineage severance blocks unsafe trust inheritance

### Routing And Signal Properties

- routing explanation cites signals and policy filters
- unsafe signal can freeze routing/mutation when policy says so
- cost/status/taste signals do not affect authority
- missing signal refs lower confidence or block explanation
- repeated failure shape can activate evaluator but not mutator directly

### Redaction Properties

- public export never includes private payloads
- operator export never includes raw secrets by default
- object-store refs are hidden unless caller can access object
- signed tokens are not returned in default graph views
- redaction class is attached to every event/payload ref

## Property/Test Checklist

Initial deterministic tests:

- test child scope narrowing
- test deny/freeze/revoke precedence
- test TTL expiry
- test budget exhaustion
- test max-depth recursion stop
- test rewrite cannot apply before approval
- test critical review blocks promotion
- test revocation race orderings
- test deletion preview coverage
- test route explanation includes signals and policy
- test status signal cannot authorize action
- test replay equals direct state
- test duplicate events are idempotent
- test redaction excludes secrets

Later generated tests:

- random capability subset comparisons
- random process trees with bounded depth
- random revoke/use event interleavings
- random signal streams for routing explanation
- random deletion graphs with dependency coverage

Generated tests can use Hypothesis later, but the first harness should not add
a new dependency just to prove the architecture. Deterministic pytest scenarios
are enough to start.

## Test Artifacts

Each scenario should emit a small JSON trace:

- scenario name
- seed if generated
- initial state summary
- event stream
- final state summary
- invariant results
- alerts
- violations

Trace rules:

- no secrets
- stable ordering
- readable by humans
- suitable for audit export tests later

## Implementation Gate

Before live graph rewrites or adaptive routing are enabled, the simulator must
pass:

- all deterministic scenario tests
- authority property tests
- rewrite property tests
- process-bound property tests
- revocation race tests
- redaction tests

Before deletion, credential revocation, or memory purge becomes graph-driven,
the simulator must pass:

- deletion preview coverage
- active process revocation cascade
- retention hold behavior
- credential revoke verification behavior

Before self-modification becomes graph-driven, the simulator must pass:

- repeated failure activation
- evaluator cannot mutate directly
- mutator cannot exceed owner ceiling
- review/proof/canary gate enforcement
- rollback or blocked promotion behavior

## Non-Goals

- no live agents
- no Kubernetes
- no Gitea
- no Argo
- no LLM calls
- no real credentials
- no database migrations
- no marketplace economics settlement
- no UI implementation

## Recommendation

P18 should establish the safety proof layer before implementation:

1. model graph state in memory
2. express graph behavior as replayable events
3. define deterministic scenarios for the scary dynamics
4. attach invariant checks to every scenario
5. make replay and redaction properties first-class
6. use the simulator as a gate before live graph rewrites, deletion, routing,
   and self-modification

The simulator is where the platform proves the graph kernel behaves before the
kernel is allowed to touch production agents.
