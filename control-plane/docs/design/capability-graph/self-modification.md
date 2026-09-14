# Dynamic Capability Graph Self-Modification

Status: the bounded source-repair slice is implemented in production; the
broader graph-rewrite schema remains exploratory.

Date: 2026-06-02 (implementation status updated 2026-07-18)

## Implemented Production Slice

Managed source agents can explicitly opt into bounded runtime self-healing in
`a2a.yaml`. Actionable runtime failures create a deduplicated work-ledger job,
delegate the smallest source repair to the scoped code editor, require test
evidence when configured, push a commit, queue that exact SHA for deployment,
and only report `healed` after the exact deployment is live. Sanitized failure,
changed-file, test, commit, deployment, and event evidence is available to the
owner in the dashboard Runtime view.

This implementation does not grant general graph mutation authority. It is
limited by manifest policy, failure window, cooldown, repairs per day, editor
turns, test requirement, deployment timeout, existing source/auth boundaries,
and platform kill-switch configuration. Manifest, prompt, memory, dependency,
routing, price, budget, policy, and arbitrary graph rewrites described below
remain design work unless another document marks them shipped.

Production smoke evidence from 2026-07-18:

- public agent: `https://self-healing-demo-v1.a2acloud.io`
- controlled failure receipt: `21c88c437454e59d`
- self-healing job: `e14c31f2185045b8a3ed8c1bce4bf1a8`
- source: `cc0a37e9faeb` -> `53764a88a51f`
- exact live deployment: `dpl_db1f506bbe614547`
- post-repair success receipt: `792bc300197ceb14`

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/subgraph-templates.md`

## Decision

Self-modification is not a special privilege. It is a normal graph rewrite
against the node itself or artifacts the node owns or is authorized to affect.

An agent may propose changes to its own source, manifest, prompt, skill schema,
memory, dependencies, routing, price, budget, or policy attachments, but it
cannot apply those changes directly. The graph kernel must stage the rewrite,
check authority and policy, run required gates, apply through a mutator, and
record ledger evidence.

## Targets

Self-modification targets:

| Target | Rewrite type |
|---|---|
| source repo / source SHA | `mutate_source` |
| synthetic manifest | `mutate_manifest` |
| prompt / goal / system instructions | `mutate_prompt` |
| skill schema / Agent Card declaration | `mutate_skill_schema` |
| memory namespace | `mutate_memory` |
| dependency edge | `add_edge`, `remove_edge`, `strengthen_edge`, `weaken_edge`, `reroute` |
| routing weight | `strengthen_edge`, `weaken_edge`, `reroute` |
| price / budget | `mutate_price_or_budget`, `allocate_budget` |
| policy attachment | `mutate_policy_attachment` |
| credential / grant | `revoke_credential`, `revoke_edge` |
| identity / lineage | `fork_node`, `retire_node`, `tombstone_identity`, `sever_lineage` |

Rule: changing any of these is a rewrite. There is no separate hidden
self-improvement channel.

## Protocol

1. Node, planner, reviewer, user, or process emits a failure/opportunity signal.
2. A rewrite proposal is created with target refs, evidence refs, expected
   effect, risk class, requested capability, and rollback plan.
3. Authority path is resolved against owner grant ceiling, parent process, and
   policy sources.
4. Policy returns deny, allow, narrower scope, approval gate, review gate, proof
   gate, canary gate, freeze, or revoke.
5. If allowed, a process-local mutator capability is minted.
6. Mutator applies the patch:
   - code-editor mutator for source
   - manifest mutator for synthetic agents
   - prompt mutator for prompt/system text
   - memory mutator for memory namespace changes
   - route/policy/budget mutator for graph state
7. Reviewer/evaluator/proof/canary runs as required.
8. Result is promoted, kept observing, rolled back, retired, or rejected.
9. Ledger records proposal, authority path, approvals, versions, review/proof,
   signals, outcome, and rollback/compensation.

## Authority Rules

- Self-granted scope expansion is forbidden.
- Mutation authority is process-local and expires.
- A call capability cannot mutate source, manifest, prompt, memory, or policy.
- A signal cannot authorize mutation.
- A receipt cannot authorize mutation.
- A route weight cannot authorize mutation.
- An evaluator cannot mutate the subject it evaluates unless separately
  authorized as a mutator.
- A child process cannot exceed parent authority.
- Owner/platform policy may always narrow, freeze, revoke, or require approval.
- Dangerous mutations require rollback or compensation.

## Deployed Agent Mapping

For deployed/source-backed agents:

```text
signal: review_failed or task_failed
  -> rewrite: mutate_source(target=repo + current sha)
  -> policy: owner approval + source mutation gate
  -> process: improvement_loop
  -> mutator: code-editor-agent
  -> result: commit sha:new
  -> process: source_push_deploy
  -> review/proof/canary
  -> promotion or rollback
```

Current concrete rails:

- code-editor turns can edit a target repo with scoped temporary Gitea write
  token
- successful push returns `head_sha`
- source-push webhook enqueues idempotent deployment work
- deployment rows track source SHA, image, URL, verification, and events
- review/proof/trial rows evaluate the new version
- WorkJob/WorkEvent and SubagentRun/SubagentRunEvent provide process evidence

Required future hardening:

- explicit rewrite proposal id attached to code-editor run
- stable finding ids linking review -> patch -> proof
- active kill-switch for the improvement process
- source mutation capability revocation lookup
- rollback/deploy previous SHA path

## Synthetic Agent Mapping

For manifest-backed/synthetic agents:

```text
signal: capability_gap or repeated blocked goal
  -> rewrite: mutate_manifest(target=manifest version)
  -> policy: owner approval if dependencies/scope expand
  -> mutator: manifest mutator
  -> result: manifest version:new
  -> dry-run DAG planner
  -> proof/evaluator
  -> promotion or rollback
```

Rules:

- manifest edits are versioned artifacts
- adding a sub-agent is an edge rewrite plus manifest rewrite
- adding a skill dependency requires declared scope and policy
- changing goal/success criteria is prompt/manifest mutation
- changing memory scope or retention is memory policy mutation
- synthetic agents still cannot grant themselves broader authority

Current concrete rails:

- `MetaAgentManifest` declares composition, goal, memory, and subagents
- `run_meta_agent_goal` plans bounded raw-skill DAGs from manifest context
- DAG execution uses `ctx.call`, grants, and composition budget
- meta-runs persist plan/progress/state/result

Required future hardening:

- manifest version ids in meta-run evidence
- manifest mutator capability and rollback
- schema validation gate
- dry-run DAG execution gate
- template id/version on meta-run rows

## Memory Mapping

```text
signal: stale_memory or bad_memory
  -> rewrite: mutate_memory
  -> policy: owner/memory retention/redaction check
  -> mutator: memory summarizer/purger
  -> evidence: memory operation event
```

Rules:

- memory mutation cannot exceed memory namespace authority
- retention/legal policy can block purge
- sensitive memory uses redaction class and secret refs
- memory summarization is mutation, not just read
- memory rewrite evidence must say what changed without leaking private content

Current gap: memory operation events need stronger explicit logging before
memory self-modification can be reliably projected.

## Routing And Dependency Mapping

```text
signal: dependency_failed
  -> rewrite: weaken_edge or remove_edge
  -> policy: dependency/conflict check
  -> process: reroute or substitute discovery

signal: cheap_success
  -> rewrite: strengthen_edge
  -> policy: routing canary/budget check
```

Rules:

- routing changes never bypass capability checks
- dependency additions require authority and scope declaration
- dependency removals must account for active processes
- route promotion may require canary
- co-use signals may propose composition wrappers, not ambient authority

## Price, Budget, And Policy Mapping

```text
signal: cost or revenue or budget_exceeded
  -> rewrite: mutate_price_or_budget
  -> policy: owner/billing/platform check

signal: repeated policy denial
  -> rewrite: mutate_policy_attachment
  -> policy: higher-authority approval
```

Rules:

- budget allocation is not source/memory authority
- price changes need billing ledger and marketplace policy
- a node cannot loosen platform/org/owner policy on itself
- policy attachment changes are high risk and ledgered

## Required Gates By Risk

| Risk | Examples | Required gates |
|---|---|---|
| `low` | routing weight decrease, read-only evaluator attachment | policy check, ledger |
| `medium` | manifest default arg, memory summary, dependency removal | policy, rollback/compensation, evaluator if needed |
| `high` | source edit, skill schema change, dependency addition, budget increase | owner approval, review/proof, rollback |
| `dangerous` | delete, credential revoke, policy loosening, lineage severance | owner/platform approval, dependency/retention/security checks, tombstone/rollback plan |

## Kill-Switch

Every self-modification process must have a kill-switch path:

- stop new child processes
- stop new calls/mutations
- revoke process-local capabilities
- freeze target edge/node if needed
- record kill reason and actor
- preserve evidence for review

The kill-switch must be available to owner/platform policy even if the proposing
agent is still running.

## Recursion Control

No unbounded recursive self-improvement:

- composition depth is capped
- child call count is capped
- max replans is capped
- process TTL is capped
- LLM budget is capped
- self-rewrite count per process is capped
- child process count is capped
- cycles in current composition stack are denied

Self-modification loops require an explicit process template and terminal
conditions.

## Promotion Rules

A mutation becomes preferred/current only after promotion policy passes.

Promotion inputs:

- applied version ref
- review/proof/canary results
- failure/residual risk summary
- rollback plan
- owner approval where required
- evidence refs and ledger rows

Promotion outputs:

- current version pointer
- route/ranking/trust update if allowed
- signals explaining result
- retained rollback reference or tombstone

## Invariants

- Self-modification is graph rewrite, not special-case privilege.
- A node cannot widen its own authority.
- A node cannot loosen parent/platform/org/owner policy.
- Mutation authority is process-local and expires.
- Every mutation has evidence and expected effect.
- Every high-risk mutation has rollback or compensation.
- Reviewer critical findings block promotion unless policy explicitly accepts
  risk.
- Kill-switch can stop the loop.
- Source, manifest, prompt, skill schema, memory, dependency, route, budget,
  price, policy, credential, deletion, and lineage changes are ledgered.
- The old version remains explainable after promotion or rollback.

## Open Questions

- Which first-class record should carry rewrite proposal id once implementation
  starts: WorkJob metadata, dedicated GraphRewrite, or both?
- What is the minimal manifest versioning scheme for synthetic agents?
- Which memory operations need object snapshots for rollback?
- How should owner risk acceptance be represented and expired?
- Which reviewer/proof gates are required for each mutation risk class?
- What is the first active revocation/freeze check that should be implemented
  before autonomous repair loops?
