# Dynamic Capability Graph Capability Algebra

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`

## Decision

The first kernel implementation should not start by adding a new graph store.
Today, authority already has hard rails:

- SDK signed `Grant` tokens authorize runtime workspace and LLM use.
- `GrantAudit` records server-side grant decisions and parent chains.
- `SubagentRun`, `AgentProofRun`, `AgentReceipt`, `AgentSession`, and
  `WorkJob` rows prove that a grant-backed edge was actually exercised.
- `CompositionBudget` bounds recursive meta-agent calls by depth, call count,
  cycle checks, and optional LLM budget.

For v0, the Dynamic Capability Graph projects edge authority from those rows.
First-class kernel tables become justified only when the platform needs durable
active graph state for revocation propagation, staged rewrites, scheduler
recovery, dependency queries, or routing policy.

## Edge Authority Model

An edge is an active relation between ports. An edge may carry a capability, but
an edge without a capability is descriptive only.

Conceptual fields:

| Field | Meaning |
|---|---|
| `edge_id` | Stable id or deterministic hash over endpoints, type, scope, and provenance. |
| `from_port` | `{node_id, port_id}` for the actor or emitting interface. |
| `to_port` | `{node_id, port_id}` for the resource or receiving interface. |
| `type` | `call`, `observe`, `evaluate`, `mutate`, `store`, `route`, `fund`, `revoke`, `delete`, `certify`, `inherit`, `depend`, `signal`. |
| `scope` | Resource/action limits: skill names, file patterns, memory namespace, repo path, model ids, spend class. |
| `budget` | Cost, runtime, call, token, recursion-depth, child-count, and process limits. |
| `ttl` | Absolute expiration or duration. |
| `weight` | Routing, trust, preference, or fitness score. Never permission. |
| `delegation` | `none`, `attenuated`, or `transitive_under_policy`. |
| `provenance_ref` | Why the edge exists: grant id, audit row, approval, policy decision, receipt, run id, commit SHA. |
| `policy_refs` | Policies governing activation, renewal, delegation, revocation, and promotion. |
| `ledger_refs` | Evidence rows proving issuance, use, result, and later revocation. |
| `state` | `proposed`, `active`, `expired`, `frozen`, `revoked`, `retired`. |

Rule: an edge capability is valid only if token verification, server policy,
TTL, revocation state, and parent authority checks all pass.

## Capability Shape

A capability is the right attached to an edge or minted inside a bounded
process.

Conceptual fields:

| Field | Meaning |
|---|---|
| `actor_ref` | Node or process allowed to exercise the right. |
| `resource_ref` | Target node, artifact, bucket, repo, model, memory namespace, or process. |
| `actions` | `invoke`, `read`, `write`, `review`, `mutate`, `delete`, `delegate`, `route`, `fund`, `revoke`. |
| `scope` | Concrete limits for the action. |
| `bounds` | Max calls, spend, tokens, runtime, recursion depth, children, file writes. |
| `ttl` | Expiration. |
| `delegation` | Whether child capabilities may be minted. |
| `parent_ref` | Parent grant, edge, process, approval, or owner authority. |
| `attenuation_proof` | Mechanical proof that this capability is no broader than the parent. |
| `revocation_ref` | Edge/policy/process able to revoke or freeze it. |
| `audit_required` | Whether each issuance/use must emit evidence. |
| `reason` | Human/process-readable justification. |

## Algebra

### Narrowing

Narrowing is the default safe operation. A child capability may be minted when
all child dimensions are less than or equal to the parent:

```text
child <= parent
```

Dimensions:

- resource set is subset of parent resource set
- action rank is no stronger than parent action rank
- write prefixes are inside parent write prefixes
- allow patterns are covered by parent allow patterns
- deny patterns are inherited plus optional additional denies
- TTL is no longer than parent remaining TTL
- LLM model set is subset of parent model set
- LLM budget/rate/token caps are no greater than parent caps
- delegation depth is below parent max depth
- composition call/depth budget is decreased for each child call

Current rails:

- SDK `delegate_grant` enforces bucket equality, mode rank, allow-pattern
  coverage, write-prefix containment, LLM cap containment, TTL bounding,
  parent grant id, and delegation depth.
- `WorkspaceClient.delegate` uses the current verified grant as parent.
- `CompositionBudget.for_child` rejects cycles, depth overflow, exhausted call
  budgets, and child LLM budgets above the remaining parent budget.

### Widening

Widening is not delegation. It is a new authority decision.

Examples:

- read-only to write
- more file patterns
- larger write prefix
- additional model id
- higher spend limit
- longer TTL
- extra child depth
- access to a new repo or memory namespace

Rule: widening requires a higher-authority policy decision, normally owner or
platform approval, and a new ledger row explaining why the ceiling changed.

### Intersection

Intersection is the safe composition operator when multiple constraints apply.

```text
effective = requested intersect parent intersect process intersect policy
```

Use it for:

- owner grant plus org policy
- grant scope plus process-local bounds
- memory read scope plus redaction policy
- route selection plus budget policy
- child call scope plus meta-agent composition budget

An empty intersection means the action is denied, not auto-widened.

### Union

Union creates broader authority and is dangerous by default.

```text
effective = scope_a union scope_b
```

Allowed only when policy explicitly permits it. A union must preserve the
authority path for each side and explain why the combined scope is needed.

Examples that require approval:

- combining two repo-write grants into a single mutator grant
- merging memory namespaces for one child agent
- aggregating several children into one transitive delegation chain
- granting a process both delete and mutate rights over the same resource

### Deny, Freeze, Revoke

Deny, freeze, and revoke override allow.

Order:

1. platform hard deny
2. explicit revocation
3. freeze/kill-switch
4. owner/org/marketplace deny
5. process/template deny
6. allow if all remaining checks pass

Effects:

- `deny`: the edge cannot activate.
- `freeze`: existing capability may remain historical evidence but cannot be
  exercised while frozen.
- `revoke`: active tokens, DB-side capabilities, process-local edges, and child
  edges are invalidated under the revocation policy.

Current gap: signed grant tokens are naturally TTL-bounded, but global
revocation requires a server-side revocation check or short-lived runtime
lookup. This is one of the first reasons to add a small active-kernel table
later.

### Delegation

Delegation is child capability minting from an existing parent.

Modes:

| Mode | Meaning |
|---|---|
| `none` | Capability may be exercised but not delegated. |
| `attenuated` | Child may be minted only as a narrower capability. |
| `transitive_under_policy` | Child may itself delegate, bounded by depth, TTL, budget, and policy. |

Rules:

- every child carries parent provenance
- every child must pass attenuation checks
- every child must inherit denies and revocation path
- child TTL and budget cannot exceed parent remaining limits
- child delegation depth cannot exceed parent ceiling
- recursive agent-to-agent calls also consume composition budget

## Current Mapping

### SDK Grant

`Grant` is the portable cryptographic capability for deployed A2A runtime use.

Mapped fields:

| Graph concept | SDK field |
|---|---|
| actor | `audience` |
| issuer | `issuer` |
| resource | `bucket` |
| action rank | `mode` |
| read scope | `allow_patterns`, `deny_patterns` |
| write scope | `outputs_prefix`, `write_prefixes` |
| LLM scope | `llm_models`, `llm_max_budget_usd`, `llm_rpm_limit`, `llm_tpm_limit` |
| parent | `parent_grant_id` |
| delegation bound | `delegation_depth`, `max_delegation_depth` |
| ttl | `expires_at`, `issued_at` |

Use cryptographic grants for capabilities that leave the control plane or must
be independently verified by a deployed agent or service.

### GrantAudit

`GrantAudit` is the current authority ledger for grants minted by the control
plane. It records issuer, audience, bucket, mode, allow/deny patterns,
outputs prefix, TTL, user, decision, decider, reason, and parent grant id.

Use `GrantAudit` as v0 provenance for graph capability edges. The graph
projection can reconstruct a grant chain with `parent_grant_id`.

Current gap: some control-plane mint helpers emit a runtime token format that
does not include every SDK delegation field. Where the token lacks a field, the
v0 projection should treat the DB audit row as ledger context, not as proof that
the runtime token enforces that dimension.

### CompositionBudget

`CompositionBudget` is the current process-local bound for recursive
meta-agent and DAG-style calls.

Mapped fields:

| Graph concept | SDK field |
|---|---|
| process id | `run_id` |
| root node | `root_agent` |
| current node | `current_agent` |
| authority path | `stack` |
| recursion ttl | `max_depth` |
| call budget | `max_calls`, `remaining_calls` |
| LLM process budget | `llm_budget_usd`, `remaining_llm_budget_usd` |

Use it as a process budget, not a permission token. The child call still needs a
valid call capability or platform/API authorization.

### Run Evidence

Grant-backed edge activation is evidenced by:

- `SubagentRun.grant_id` and `SubagentRunEvent`
- `AgentProofRun.grant_id`
- `AgentReceipt.grant_ids`
- `AgentSession.receipt_id`
- `WorkJob` / `WorkEvent` mirrors
- `LLMUsageEvent.grant_id` where present

Those rows prove use and outcome. They do not grant future authority.

## Examples

### Call Capability

```text
edge: user:request -> agent:chart-agent:invoke
capability:
  actions: invoke
  scope:
    skill: render_chart
    bucket: user-42-files
    allow_patterns: ["reports/input.csv"]
    write_prefixes: ["outputs/charts/"]
  bounds:
    ttl: 300s
    max_calls: 1
  provenance:
    GrantAudit(grant_id=g1, decision=auto_approve)
```

Runtime form: signed `Grant` presented to the deployed agent.

### Read Memory Capability

```text
edge: agent:research-meta -> memory:user-42/research:read
capability:
  actions: read
  scope:
    namespace: research
    keys: ["market/*"]
    redaction_class: owner
  bounds:
    ttl: process lifetime
  provenance:
    process:pursue(run_id=...)
```

Runtime form: DB-side authorization is acceptable while memory access remains
inside the control plane or SDK host. External memory services need a signed or
service-verifiable token.

### Mutate Source Capability

```text
edge: process:repair-loop -> repo:gitea_admin/chart-agent:write
capability:
  actions: mutate
  scope:
    repo: chart-agent
    branch: main
    file_patterns: ["agent/**", "tests/**"]
  bounds:
    ttl: 15m
    max_commits: 1
  provenance:
    review_finding:finding_hash
    owner_approval:approval_id
```

Runtime form: short-lived Gitea write token plus audit row. Later graph-kernel
state should record the active mutation capability and revoke it on failure,
timeout, or owner kill-switch.

### Delete Memory Capability

```text
edge: process:retention-cleanup -> memory:user-42/old-runs:delete
capability:
  actions: delete
  scope:
    namespace: old-runs
    cutoff: 2026-05-01T00:00:00Z
  bounds:
    ttl: 10m
  policy_refs:
    retention:owner-policy-v3
    audit:platform-security-v1
```

Runtime form: DB-side policy check until memory deletion leaves the trusted
control-plane boundary.

### Delegate Child Grant

```text
parent:
  actor: agent:research-meta
  actions: invoke, read, write
  allow_patterns: ["data/**"]
  write_prefixes: ["reports/"]
  llm_budget: 5.00
  delegation_depth: 1/3

child:
  actor: agent:chart-agent
  actions: invoke, read, write
  allow_patterns: ["data/*.csv"]
  write_prefixes: ["reports/charts/"]
  llm_budget: 1.00
  delegation_depth: 2/3
  parent_ref: parent.grant_id
```

Valid because every dimension is narrower and the parent has remaining
delegation depth.

## Token Versus DB Authorization

Use signed tokens when:

- a deployed agent/service must verify access without a live DB lookup
- the capability crosses process or network boundaries
- LiteLLM, workspace, or another service needs bearer-style credentials
- the capability must be portable in receipts or replay

Use DB-side checks when:

- the action stays inside the control plane
- the capability is tied to current user/session policy
- immediate revocation is more important than portability
- the operation already requires a live transaction

Use both when:

- a short-lived token is needed for execution and a ledger row is needed for
  audit, revocation lookup, dossier projection, and future graph queries

## Revocation Discovery

Running processes discover invalid authority through:

- token expiration
- runtime verification failure
- server-side revocation checks before each sensitive operation
- process kill-switch events
- scheduler cancellation
- deny/freeze policy checks on every rewrite or delegation request

Near-term recommendation:

- keep grant TTLs short
- add revocation checks around dangerous control-plane operations
- model revocation as evidence in the v0 DAG before adding a universal active
  capability store

Later store trigger:

- if a process can run long enough that short TTL is inadequate
- if child processes need cascading revocation
- if source/memory/delete credentials need immediate kill semantics
- if operator UI must answer "what active rights exist right now?"

## Invariants For Later Tests

- A child capability never exceeds parent scope.
- Denies are inherited and cannot be removed by a child.
- TTL and budget are monotonically decreasing through delegation.
- Delegation depth is bounded.
- A routing weight cannot authorize an action.
- A signal cannot authorize an action.
- A receipt proves past use, not future access.
- A DB audit row cannot be treated as a runtime token.
- A signed token cannot explain approval by itself; it needs ledger context.
- Union of scopes requires explicit policy approval.
- Deny, freeze, and revoke override allow.
- A process-local capability expires when the process ends unless promoted by
  policy.
- Source, manifest, memory, and deletion changes require rewrite authority, not
  ordinary call authority.
- External services use signed tokens or scoped service credentials; internal
  control-plane operations use live policy checks.
