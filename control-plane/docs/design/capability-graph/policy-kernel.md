# Dynamic Capability Graph Policy Kernel

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`

## Decision

Policy is the physics of the graph. It decides whether an edge may activate, a
capability may be minted, a process may run, a rewrite may apply, a node may be
deleted, a route may be preferred, or trust may be inherited.

For v0, do not add a central policy engine table first. Normalize current policy
rails into a common evaluation model:

- `GrantAudit.decision`, `decided_by`, `reason`, and parent grant chains
- `main_agent.scope_policy.decide_extension` for runtime scope negotiation
- `UserControlPolicy` for per-user budget, network, file-write, approved-agent,
  and PII-safe controls
- pending approval and question/input hooks in orchestrator handoffs
- consumer setup requirements before invocation
- ownership checks on agent, deployment, repo, and credential operations
- source-push deployment overlap checks and idempotency
- review/proof/trial status as promotion gates
- sandbox workspace and network write policies

The graph-kernel policy API should be designed before implementation, but the
first practical use should project and explain decisions from existing rows and
functions.

## Policy Sources

Policy sources compose by restriction, not escalation.

Order:

1. platform policy
2. org policy
3. owner policy
4. marketplace policy
5. process/template policy
6. node policy
7. resource policy
8. request-local constraints

Rule: a lower policy can narrow, require additional gates, or deny. It cannot
grant authority above a parent ceiling.

## Evaluation Input

Conceptual fields:

| Field | Meaning |
|---|---|
| `decision_id` | Stable id for this policy evaluation. |
| `request_type` | `activate_edge`, `mint_capability`, `delegate`, `activate_process`, `apply_rewrite`, `route`, `delete`, `inherit_trust`, `allocate_budget`, `revoke`. |
| `actor_ref` | Node/process/user requesting action. |
| `owner_ref` | Owner/org/platform principal that controls target. |
| `target_refs` | Resources, nodes, edges, artifacts, credentials, budgets, memory namespaces, versions. |
| `requested_action` | Operation being requested. |
| `requested_scope` | Scope from the request. |
| `parent_authority` | Parent grant, edge, approval, session, owner right, or platform authority. |
| `effective_constraints` | Already-known constraints from grant/process/template/policy. |
| `signals` | Evidence signals relevant to decision. |
| `evidence_refs` | Audit rows, reviews, proofs, receipts, deployments, commits, work events. |
| `risk_class` | Request risk. |
| `budget_impact` | Spend/time/token/call/process impact. |
| `ttl` | Requested duration. |
| `idempotency_key` | Dedupe key for effects. |
| `audience` | Who will see result/evidence. |

## Evaluation Output

Conceptual fields:

| Field | Meaning |
|---|---|
| `decision` | `allow`, `deny`, `require_narrower_scope`, `require_approval`, `require_review`, `require_proof`, `require_canary`, `freeze`, `revoke`. |
| `reason` | Human-readable explanation. |
| `effective_scope` | Scope after intersection and narrowing. |
| `required_gates` | Approvals/reviews/proofs/checks required before applying. |
| `policy_refs` | Source policies that influenced the decision. |
| `evidence_required` | Evidence that must be recorded. |
| `expires_at` | Decision TTL if applicable. |
| `redaction_class` | Visibility class for decision details. |
| `ledger_ref` | Audit/evidence row once recorded. |

Rule: an allow decision may still return a narrower effective scope. A denied
decision must explain which ceiling or deny rule blocked the request.

## Composition

### Restrictive Merge

Effective policy is the intersection of all applicable ceilings:

```text
effective = platform intersect org intersect owner intersect marketplace
            intersect process intersect node intersect resource
            intersect request-local constraints
```

If any source returns `deny`, the effective result is `deny`.

If any source returns `freeze` or `revoke`, activation is blocked even if another
source allows.

If multiple sources require gates, gates accumulate.

### Scope Narrowing

Policy may narrow:

- read patterns
- write prefixes
- model set
- LLM spend/rate/token caps
- TTL
- max calls/depth/children/runtime
- target files or memory keys
- allowed signal types for an evaluator
- routes and traffic percentage
- approval duration

Policy may not widen without a higher-authority decision.

### Conflict Handling

Conflict rules:

- deny beats allow
- revoke beats active
- freeze beats route/mutate/call
- owner policy cannot override platform hard deny
- marketplace rank cannot override capability policy
- evaluator signal cannot override owner approval requirement
- review pass cannot override retention/legal deletion block
- user taste signal cannot grant mutation authority

## Questions Policy Must Answer

- Can this edge be created or activated?
- Can this capability be minted, delegated, renewed, narrowed, or revoked?
- Does child scope exceed parent scope?
- Can this process activate under budget, TTL, risk, and child limits?
- Can this rewrite touch this resource?
- Does this require owner/human/platform approval?
- Does reviewer/proof output block promotion?
- Does this request violate an owner grant ceiling?
- Would this create a cycle, runaway loop, hidden scope union, or unsafe
  recursion?
- Does deletion conflict with dependency, retention, ownership, legal, or audit
  constraints?
- Can trust/certification/reputation inherit across fork/merge?
- Which evidence row will prove the decision?

## Current Rails Mapping

### Scope Extension

Current rail: `decide_extension`.

Mapping:

```text
request_type: delegate
requested_scope: read/write patterns, mode, ttl
output: auto_approve | ask_user | deny
constraints:
  max ttl: 1800s
  max extensions per grant: 3 before asking user
  write/mode upgrade requires ask_user
```

This is policy, but it currently permits some union-like read expansion as a
runtime negotiation. The graph policy model should reclassify scope union as a
higher-risk request and explain why it is allowed.

### Control Room Policy

Current rail: `UserControlPolicy`.

Mapped fields:

- monthly budget
- per-run budget
- max agent calls per run
- require approval for file writes
- deny external network
- only approved agents
- PII-safe mode
- approved agent list

This maps to owner policy and request-local budget/network/file gates.

### Grant Audit

Current rail: `GrantAudit`.

Mapped fields:

- grant id and parent grant id
- issuer/audience/bucket/mode/scope/TTL
- decision
- decided by auto/user/policy
- reason

This is both policy evidence and capability provenance.

### Ownership And Consumer Setup

Current rails:

- agent and deployment ownership checks
- repo access checks before Gitea token issuance
- consumer setup required before invocation

These map to resource policy and precondition gates.

### Review, Proof, And Trial Gates

Current rails:

- `AgentReviewRun` statuses and findings
- `AgentProofRun` status/evidence
- `TrialRun` score and acceptance evaluation

These map to promotion policy. They should not authorize a source mutation by
themselves; they can block or support promotion after rewrite authority exists.

### Source-Push Deploy Guard

Current rail: source-push worker blocks overlapping active deployments and uses
idempotency by source SHA.

This maps to process policy: no overlapping deploy process unless policy
explicitly allows it.

## Decision Examples

### Allow

```text
request:
  type: activate_edge
  actor: agent:research-meta
  target: agent:summarizer invoke:summarize
  parent_authority: Grant(g1)
  requested_scope:
    read_patterns: ["reports/*.md"]
    ttl: 60s

decision:
  allow
  effective_scope:
    read_patterns: ["reports/*.md"]
    ttl: 60s
  reason: child scope is narrower than parent grant and under process budget
```

### Deny

```text
request:
  type: mutate_source
  actor: agent:chart-agent
  target: repo:gitea_admin/chart-agent
  parent_authority: call grant only

decision:
  deny
  reason: call authority cannot mutate source
```

### Require Approval

```text
request:
  type: delegate
  actor: agent:research-meta
  requested_scope:
    mode: read_write_overlay
    write_prefixes: ["reports/charts/"]

decision:
  require_approval
  reason: write/mode upgrade
  required_gates:
    - owner_approval
```

### Require Review

```text
request:
  type: apply_rewrite
  rewrite: mutate_source
  risk_class: high
  target: version:sha:new

decision:
  require_review
  reason: high-risk source mutation needs reviewer gate before promotion
```

### Require Canary

```text
request:
  type: reroute
  target: edge:router->new-agent
  expected_effect: increase traffic from 0% to 100%

decision:
  require_canary
  effective_scope:
    max_traffic_percent: 5
  reason: new route has no production proof history
```

### Require Narrower Scope

```text
request:
  type: mint_capability
  requested_scope:
    write_prefixes: ["**"]
    ttl: 3600s

decision:
  require_narrower_scope
  effective_scope:
    write_prefixes: ["outputs/run-123/"]
    ttl: 300s
  reason: requested write scope and TTL exceed owner policy
```

## Invariant Catalog

### Authority

- No node mutates graph state directly.
- No capability exists without provenance.
- Child capability is always less than or equal to parent authority.
- Deny, freeze, and revoke override allow.
- Routing weight is not permission.
- Signals are not permission.
- Receipt/session evidence is not permission.
- Marketplace/status/taste signals are not permission.

### Rewrite Safety

- Dangerous rewrites are staged before applying.
- Rewrites are versioned and ledgered.
- Source, manifest, prompt, memory, policy, budget, route, credential, deletion,
  and lineage changes use rewrite authority.
- Every high-risk rewrite has rollback or compensation.
- Reviewer critical findings block promotion unless owner/platform policy
  explicitly accepts the risk.

### Process Safety

- Active processes are killable.
- Process-local capabilities expire at process end unless promoted by policy.
- Budgets, TTL, max calls, max depth, and max child processes are bounded.
- Recursive activation cannot cycle through the same stack.
- Child processes inherit narrowed authority.

### Data And Deletion

- Secrets are never copied into signals, receipts, or public graph views.
- Deletion checks ownership, dependency, retention, legal, active process, and
  credential revocation policy.
- Tombstones preserve enough identity and evidence to explain deletion.
- Lineage severance prevents unsafe descendants from inheriting trust.

### Explainability

- Every policy decision has a reason and policy refs.
- Every edge activation can be traced to authority and ledger evidence.
- Every routing decision can be explained from signals, weights, budget, and
  policy.
- Every denied request can name the ceiling it hit.

## Implementation Shape

Near-term:

- add no central policy table
- expose policy decisions in the evidence DAG/dossier projection
- normalize existing decision rows into the policy output shape
- keep pure policy helpers where possible, like `decide_extension`
- write invariants as tests against projection and future kernel helpers

Later:

- central policy evaluator interface
- policy source registry
- active revocation/freeze checks
- policy decision ledger rows
- conflict detector for rewrite/process activation
- UI decision explorer for operators and owners

## Open Gaps

- Current runtime scope negotiation can union read patterns; the graph policy
  model should make union explicit and higher risk.
- Immediate global revocation needs active capability state or live lookup.
- Reviewer findings need stable ids for repeatable policy gates.
- Deletion/retention policy is not yet a first-class graph operation.
- Marketplace/reputation/economics policy needs future billing/product ledgers.
- Canary routing needs durable route state before policy can enforce traffic
  percentages.
