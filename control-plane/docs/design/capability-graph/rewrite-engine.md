# Dynamic Capability Graph Rewrite Engine

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`

## Decision

Graph rewrites are the universal mutation mechanism. Agents, protocol packs,
reviewers, users, and operators may propose effects, but the platform applies
only approved rewrites.

For v0, do not add a rewrite table first. Treat current mutation rails as the
implementation substrate:

- code-editor turns mutate source and return changed files and `push.head_sha`
- source-push webhooks enqueue idempotent deployment `WorkJob`s
- `AgentDeployment` and `AgentDeploymentEvent` record deploy state
- `AgentReviewRun`, `AgentProofRun`, and `TrialRun` evaluate changed versions
- `WorkJob`, `WorkEvent`, and `IdempotencyRecord` already model queued effects,
  event timelines, dedupe, attempts, and results

The first rewrite engine should be a projection and protocol over those rails.
Add first-class `GraphRewrite` state only when multiple rewrite families need a
shared approval/canary/rollback lifecycle.

## Rewrite Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `rewrite_id` | Stable proposal id or deterministic idempotency key. |
| `type` | Mutation class. |
| `target_refs` | Nodes, edges, ports, versions, repos, manifests, prompts, memory namespaces, policies, budgets, credentials. |
| `requested_capability` | Capability required to apply the rewrite. |
| `authority_path` | Parent grant, owner approval, policy decision, or process authority chain. |
| `evidence_refs` | Signals, reviews, receipts, proofs, deploys, commits, traces, user feedback. |
| `expected_effect` | What should improve, change, freeze, retire, or delete. |
| `risk_class` | `low`, `medium`, `high`, `dangerous`. |
| `budget_impact` | Cost, runtime, token, traffic, revenue, or quota change. |
| `rollback_plan` | Revert, redeploy previous version, restore memory, undo route, compensate, tombstone. |
| `required_gates` | Approval, review, proof, canary, security review, retention check. |
| `idempotency_key` | Dedupe key for safe retry. |
| `state` | Rewrite lifecycle state. |
| `ledger_refs` | Rows proving proposal, approval, application, signals, outcome, rollback. |

Rule: a rewrite proposal is not permission. Applying it requires capability
checks, policy evaluation, and any required gates.

## Type Catalog

### Edge Rewrites

| Type | Meaning |
|---|---|
| `add_edge` | Create new relation/capability edge. |
| `remove_edge` | Remove relation or stop future activation. |
| `strengthen_edge` | Increase routing weight or trust score, not authority. |
| `weaken_edge` | Decrease routing weight or trust score. |
| `freeze_edge` | Temporarily block activation. |
| `revoke_edge` | Revoke active capability and descendants under policy. |
| `reroute` | Prefer substitute path under the same authority ceiling. |

### Node Rewrites

| Type | Meaning |
|---|---|
| `create_node` | Add agent, manifest, memory namespace, evaluator, router, scheduler, budget account, policy, or service node. |
| `fork_node` | Create new lineage from an existing node/version. |
| `merge_node` | Merge identity or functionality under strict policy. |
| `freeze_node` | Stop calls, mutation, delegation, or routing. |
| `retire_node` | Stop new work while preserving audit and lineage. |
| `delete_node` | Delete allowed state after dependency/retention checks. |
| `tombstone_identity` | Preserve identity marker and reason after retirement/deletion. |
| `sever_lineage` | Prevent trust/certification inheritance. |

### Artifact Rewrites

| Type | Meaning |
|---|---|
| `mutate_source` | Change source repo and produce a new commit SHA. |
| `mutate_manifest` | Change synthetic agent manifest. |
| `mutate_prompt` | Change prompt/system instructions. |
| `mutate_memory` | Write, summarize, redact, purge, or move memory. |
| `mutate_skill_schema` | Change Agent Card or skill contract. |
| `mutate_policy_attachment` | Attach, detach, or narrow policy. |
| `mutate_price_or_budget` | Change price, spend, quota, or revenue share. |

### Process And Topology Rewrites

| Type | Meaning |
|---|---|
| `activate_process` | Start evaluator, mutator, deployment, deletion, review, proof, canary, or repair loop. |
| `attach_evaluator` | Connect evaluator to a subject. |
| `attach_mutator` | Connect mutator to a subject with bounded capability. |
| `attach_router` | Connect route selector to a node/edge set. |
| `attach_scheduler` | Connect recurring or event-triggered process. |
| `allocate_budget` | Move budget into a process or node account. |
| `transfer_revenue` | Move earned value under billing policy. |
| `revoke_credential` | Revoke service token, Gitea token, LLM key, or grant. |

## State Machine

```text
proposed
  -> shape_invalid
  -> policy_denied
  -> awaiting_approval
  -> awaiting_review
  -> awaiting_canary
  -> approved
  -> applying
  -> applied
  -> observing
  -> promoted
  -> degraded
  -> rollback_pending
  -> rolled_back
  -> failed
  -> rejected
  -> expired
```

State rules:

- `proposed`: advisory request with evidence and expected effect.
- `shape_invalid`: proposal lacks target, capability, rollback, or evidence.
- `policy_denied`: policy blocks before approval.
- `awaiting_approval`: human/owner/platform action required.
- `awaiting_review`: reviewer/evaluator must check the proposed change.
- `awaiting_canary`: limited traffic/run window required.
- `approved`: gates passed; capability may be minted.
- `applying`: mutator/deployer/policy engine is changing state.
- `applied`: mutation produced an artifact/version/state change.
- `observing`: post-apply review/proof/traffic/cost signals are being watched.
- `promoted`: policy accepts result as current preferred state.
- `degraded`: post-apply signals show regression.
- `rollback_pending`: rollback or compensation scheduled.
- `rolled_back`: previous state restored or compensating action completed.
- `failed`: application failed and no automatic rollback succeeded.
- `rejected`: owner/operator/reviewer rejected proposal.
- `expired`: TTL elapsed before approval/application.

Dangerous rewrites may not skip from `proposed` to `applying`.

## Staging Flow

1. Propose rewrite from signal, user command, operator action, protocol pack, or
   process.
2. Validate shape: targets, capability class, evidence, risk, rollback, budget,
   idempotency key.
3. Resolve authority path from grant/audit/session/owner/platform context.
4. Evaluate policy: deny, allow, require narrower scope, require approval,
   require review, require canary.
5. Request approvals if needed.
6. Mint process-local capability if approved.
7. Apply with a specialized mutator/deployer/policy engine.
8. Emit signals and ledger rows.
9. Run review/proof/canary when required.
10. Promote, keep observing, rollback, or retire based on signals and policy.

## Current Mapping

### Source Edit

Existing rail:

- code-editor turn receives a short-lived source mutation capability
- OpenHarness edits the workspace under restricted permissions
- result includes sync/codegraph/change/push information
- successful push emits a new `head_sha`
- Gitea source-push webhook enqueues an idempotent deploy `WorkJob`
- source-push worker creates `AgentDeployment` and `AgentDeploymentEvent`
- review/proof rows evaluate the new version

Rewrite projection:

```text
rewrite:mutate_source
  target: repo:gitea_admin/<agent>
  evidence: review finding, failure signal, owner prompt
  capability: repo write token ttl=limited
  idempotency: source sha / work job id / code-editor turn id
  result: version:sha:<new_head_sha>
```

### Deploy From Source Push

Existing rail:

- `source_push_idempotency_key(owner, repo, source_sha)` dedupes deploy effects
- `enqueue_source_push_deploy_job` writes a `WorkJob`
- worker blocks overlapping active deployments
- repeated deploy for already-live SHA returns an idempotent success
- deployment events record source, build, deploy, verify, and errors

Rewrite projection:

```text
rewrite:activate_process
  type: source_push_deploy
  target: agent:<agent> version:sha:<source_sha>
  idempotency_key: gitea-source-push:<owner>:<repo>:<sha>
  result: AgentDeployment(deploy_id=...)
```

### Review And Proof Gates

Existing rail:

- `AgentReviewRun` stores findings and severity counts
- `AgentProofRun` stores invocation evidence, grant id, head SHA, card hash,
  result, events, file ops, and status
- `TrialRun` stores scored evaluation and receipt JSON

Rewrite projection:

```text
rewrite gate:
  required_gates: review, proof
  pass: review has no critical finding and proof passed
  fail: critical finding, proof failure, timeout, or policy violation
```

## Examples

### Source Edit

```text
proposal:
  type: mutate_source
  target_refs:
    - repo:gitea_admin/chart-agent
    - version:sha:old
  evidence_refs:
    - signal:task_failed
    - review:finding:f1
  requested_capability:
    action: mutate
    scope:
      repo: chart-agent
      file_patterns: ["agent/**", "tests/**"]
      max_commits: 1
      ttl: 15m
  risk_class: high
  rollback_plan:
    redeploy previous head_sha
  required_gates:
    - owner_approval
    - review
    - proof

result:
  state: observing
  ledger_refs:
    - SubagentRun(code-editor)
    - WorkJob(source_push_deploy)
    - AgentDeployment(head_sha=new)
```

### Manifest Patch

```text
proposal:
  type: mutate_manifest
  target_refs:
    - manifest:research-meta@v3
  evidence_refs:
    - signal:capability_gap
  requested_capability:
    action: mutate
    scope:
      fields: ["subagents", "dag_templates"]
      no_owner_change: true
  risk_class: medium
  rollback_plan:
    restore manifest v3
  required_gates:
    - schema_validation
    - dry_run
```

Manifest patches are versioned artifacts. A synthetic agent does not rewrite its
own manifest directly; it proposes a rewrite that a manifest mutator applies.

### Dependency Removal

```text
proposal:
  type: remove_edge
  target_refs:
    - edge:research-meta->old-search-agent:call
  evidence_refs:
    - signal:dependency_failed
    - signal:routing_degraded
  expected_effect:
    stop routing to failing child
  risk_class: medium
  rollback_plan:
    restore edge at lower weight
  required_gates:
    - dependency_check
```

Removing an edge does not revoke historical evidence. It stops future activation
and may require active process rerouting.

### Budget Transfer

```text
proposal:
  type: allocate_budget
  target_refs:
    - budget:user-42/monthly
    - process:evaluator-loop
  evidence_refs:
    - signal:review_failed
  requested_capability:
    action: fund
    scope:
      max_usd: 2.00
      process_id: evaluator-loop
  risk_class: low
  rollback_plan:
    expire unused budget at process end
```

Budget is a capability bound. It never grants source/memory/repo authority.

### Node Retirement

```text
proposal:
  type: retire_node
  target_refs:
    - agent:old-parser
  evidence_refs:
    - signal:repeated_failure
    - signal:dependency_recovered
  expected_effect:
    stop discovery and routing, preserve audit history
  risk_class: high
  rollback_plan:
    unretire within retention window
  required_gates:
    - owner_approval
    - dependency_check
    - retention_check
```

Retirement is not deletion. Deletion, tombstone, revocation, and lineage
severance are separate rewrites.

## Idempotency

Every rewrite that can trigger external side effects needs an idempotency key.

Good keys:

- `source_push:<owner>:<repo>:<head_sha>`
- `manifest_patch:<agent>:<from_version>:<patch_hash>`
- `memory_purge:<namespace>:<cutoff>:<policy_version>`
- `freeze_edge:<edge_id>:<reason_hash>`
- `retire_node:<node_id>:<request_id>`

Rules:

- identical key plus identical request hash returns the prior result
- identical key plus different request hash is a conflict
- active conflicting rewrites block or queue behind policy
- idempotency rows are evidence refs in the DAG

Current rail: `IdempotencyRecord` and `WorkJob` already provide this shape for
some queued effects.

## Rollback And Compensation

Rollback is rewrite-specific:

- source: redeploy previous known-good SHA, or revert commit
- manifest/prompt: restore previous version
- memory: restore snapshot where retention allows, or append corrective memory
- edge routing: restore prior weight/path
- budget: expire or credit unused allocation
- credential: issue replacement credential only after revocation audit
- deletion: restore only when retention and tombstone policy allow it

If exact rollback is impossible, the rewrite needs a compensation plan and a
ledger entry explaining residual risk.

## Policy Hooks

P4 depends on P5 policy, but the rewrite envelope must expose the inputs:

- actor and owner
- target resources
- requested action and scope
- parent authority path
- evidence refs
- risk class
- budget impact
- rollback plan
- approval state
- review/proof/canary results
- conflict set
- retention/dependency impact

Policy outputs:

- deny
- allow
- require narrower scope
- require approval
- require review
- require proof
- require canary
- require dependency check
- require retention check
- require rollback plan
- freeze/revoke before proceeding

## Invariants

- No node mutates graph state directly.
- Signals can propose rewrites but cannot apply them.
- Dangerous rewrites are staged and ledgered.
- Every applied rewrite has an authority path.
- Every applied rewrite has evidence refs.
- Every external side effect has an idempotency key.
- Source, manifest, prompt, memory, policy, budget, routing, credential, and
  deletion changes all use the same rewrite envelope.
- Rollback or compensation is required before high-risk application.
- Reviewer critical findings block promotion unless owner/platform policy
  explicitly accepts the risk.
- Active kill-switch/freeze policy overrides pending rewrites.
- A rewrite may mint process-local capabilities, but those expire unless policy
  promotes the result.
- Applying a rewrite emits signals and ledger evidence for the next projection.
