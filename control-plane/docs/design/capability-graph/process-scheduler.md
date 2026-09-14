# Dynamic Capability Graph Process Scheduler

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`
- `docs/signals-evidence.md`
- `docs/rewrite-engine.md`
- `docs/policy-kernel.md`

## Decision

Processes are temporary activated subgraphs that run over time. They are not
privileges. A process may mint narrowed process-local capabilities, but those
capabilities expire when the process ends unless policy explicitly promotes a
result.

For v0, do not add a new scheduler table first. Project process state from
existing substrates:

- `WorkJob` and `WorkEvent` for generic queued work, retries, idempotency,
  parent/root/correlation ids, worker identity, events, artifacts, proofs, and
  status.
- `MetaAgentRun` for durable goal, plan, progress, state, summary, and error.
- `DagRun` and `DagRunNode` for structured participant nodes, dependencies,
  grants, results, file ops, and timings.
- `SubagentRun` and `SubagentRunEvent` for individual agent handoffs.
- deployment, review, proof, and trial rows for specialized process families.
- `CompositionBudget` for recursive call/depth/LLM budget inside meta-agent
  processes.

Add first-class `GraphProcess` state only when multiple process families need a
shared pause/resume/kill/retry/approval/canary scheduler.

## Process Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `process_id` | Stable run/job/deployment/review/proof/deletion id. |
| `template_ref` | Process template: DAG execution, evaluator loop, deploy, repair, deletion, canary, marketplace experiment. |
| `initiator` | User/node/process that started it. |
| `authority_path` | Capability chain, owner approval, policy decision, or parent process. |
| `participants` | Nodes and ports involved. |
| `active_edges` | Process-local call/mutate/review/delete/fund edges. |
| `state` | Current state. |
| `transition_history` | Ordered event refs. |
| `start_conditions` | Signals, user command, schedule, webhook, policy trigger. |
| `exit_conditions` | Success criteria, failure threshold, TTL, budget, approval timeout, kill-switch. |
| `budget` | Calls, depth, children, runtime, tokens, spend, retries, traffic share. |
| `ttl` | Maximum lifetime. |
| `kill_switch_refs` | Policies/operators/edges able to stop it. |
| `emitted_signals` | Signal refs produced by process. |
| `proposed_rewrites` | Rewrite refs proposed during process. |
| `required_gates` | Approval, review, proof, canary, dependency check, retention check. |
| `ledger_refs` | Work events, grants, receipts, deployments, reviews, proofs, commits. |

Rule: a process can carry state and temporary authority, but it cannot exceed
its parent authority or policy ceiling.

## State Machine

```text
created
  -> queued
  -> policy_blocked
  -> awaiting_approval
  -> awaiting_input
  -> scheduled
  -> running
  -> pausing
  -> paused
  -> retrying
  -> compensating
  -> succeeded
  -> failed
  -> cancelled
  -> killed
  -> expired
  -> rolled_back
```

State meanings:

- `created`: process row/projection exists but has not entered a queue.
- `queued`: eligible to run.
- `policy_blocked`: policy denied or requires a gate before running.
- `awaiting_approval`: human/owner/platform approval needed.
- `awaiting_input`: process needs external answer or setup.
- `scheduled`: not eligible until time/signal/dependency condition.
- `running`: worker or scheduler is executing.
- `pausing`: stop requested; waiting for safe point.
- `paused`: resumable but not active.
- `retrying`: retry scheduled after transient failure.
- `compensating`: rollback or compensation action running.
- `succeeded`: exit criteria satisfied.
- `failed`: terminal failure without compensation success.
- `cancelled`: caller/operator cancelled before completion.
- `killed`: kill-switch stopped it.
- `expired`: TTL elapsed.
- `rolled_back`: process result was undone or compensated.

Terminal states:

- `succeeded`
- `failed`
- `cancelled`
- `killed`
- `expired`
- `rolled_back`

## Scheduler Responsibilities

### Activation

- detect eligible start conditions
- validate template shape
- evaluate policy before activation
- bind initiator, owner, participants, authority path, and correlation id
- create idempotent work/process row
- emit process-created signal/event

### Capability Minting

- mint process-local capabilities only after policy allows
- narrow child capabilities from parent authority
- carry parent provenance and revocation path
- apply TTL, budget, depth, child-count, and call-count limits
- revoke/expire process-local capabilities at terminal state

### Execution

- claim runnable work
- enforce dependency ordering
- call participants through typed ports
- record progress/status signals
- append ordered ledger events
- handle approval/input interrupts
- resume from durable state after worker crash

### Budget And TTL

- enforce max runtime
- enforce max calls and child processes
- enforce max depth and cycle prevention
- enforce LLM spend/rate/token caps
- stop or pause when budget is exhausted
- explain budget decisions through policy/evidence rows

### Retry And Compensation

- retry transient failures within policy
- dedupe by idempotency key
- avoid duplicate external side effects
- schedule rollback/compensation when post-apply signals degrade
- record failed compensation as residual risk

### Kill, Freeze, And Revocation

- observe kill-switch/freeze/revoke signals
- stop new child activation
- cancel pending work where possible
- revoke active process-local capabilities
- emit cancellation/killed evidence
- leave enough ledger state to explain what stopped and why

## Current Mapping

### WorkJob

`WorkJob` is the closest current generic process row.

Mapped fields:

| Graph process field | WorkJob field |
|---|---|
| `process_id` | `job_id` |
| `template_ref` | `kind` |
| `state` | `status` |
| `initiator` | `user_id`, `thread_id` |
| `parent/root` | `parent_job_id`, `root_job_id` |
| `correlation` | `correlation_id` |
| `subject` | `subject_type`, `subject_id` |
| `worker` | `worker_type`, `worker_name` |
| `input` | `input_payload` |
| `output` | `output_payload`, `summary`, `error_payload` |
| `attempts` | `attempt`, `max_attempts` |
| `lease` | `leased_until`, `heartbeat_at` |
| `artifacts/proofs` | `artifact_refs`, `proof_refs` |
| `timing` | `queued_at`, `started_at`, `completed_at` |

### WorkEvent

`WorkEvent` is process transition history.

Mapped fields:

| Graph process field | WorkEvent field |
|---|---|
| transition id | `event_id`, `event_seq` |
| process id | `job_id` |
| parent transition | `parent_event_id` |
| correlation | `correlation_id` |
| status/stage | `status`, `stage` |
| severity | `severity` |
| actor/source | `actor_type`, `actor_id`, `source_type`, `source_id` |
| data | `payload`, `metrics`, `metadata_json` |
| evidence | `artifact_refs`, `proof_refs` |

### MetaAgentRun And DAG

`MetaAgentRun` tracks goal-level process state: goal, success criteria, current
plan, progress, state, summary, error, and status.

`DagRunNode` tracks participants inside a structured subgraph: node id, agent,
skill, deps, args, grant id, status, result, file ops, and timing.

These map directly to process participants and dependency scheduling.

### CompositionBudget

`CompositionBudget` is process-local budget for recursive agent calls:

- root/current agent
- stack
- max depth
- max calls and remaining calls
- optional LLM budget and remaining budget

It is not permission. The scheduler must pair it with capability/policy checks.

## Template Examples

### Evaluator Loop

```text
template: evaluator_loop
participants:
  subject.version -> evaluator.review -> signal.review_finding
start_conditions:
  signal.task_failed OR user requested review
exit_conditions:
  review passed OR critical finding emitted OR ttl expired
required_gates:
  readonly evaluation authority
```

Behavior:

- activates evaluator with read-only capability
- emits review signals
- may propose a repair rewrite
- cannot mutate subject by itself

### Improvement Loop

```text
template: improvement_loop
participants:
  subject -> evaluator -> rewrite_planner -> policy -> mutator -> proof
start_conditions:
  repeated failure signal OR explicit owner request
exit_conditions:
  proof passed, owner rejected, rollback completed, budget exhausted
required_gates:
  owner approval for mutation
  review/proof before promotion
```

Behavior:

- mints temporary mutator capability only after approval
- applies source/manifest/prompt/memory rewrite
- observes review/proof signals
- promotes or rolls back under policy

### Marketplace Routing Experiment

```text
template: routing_experiment
participants:
  router -> candidate_edge -> evaluator
start_conditions:
  cheap_success signal OR substitute candidate discovered
exit_conditions:
  canary metrics pass/fail OR traffic cap reached
budget:
  max_traffic_percent: 5
  max_runtime: 24h
required_gates:
  canary
```

Behavior:

- changes routing weight, not permission
- emits cost/latency/success/failure signals
- promotes only if policy allows

### Deletion And Retirement Process

```text
template: retirement_process
participants:
  target_node -> dependency_checker -> retention_checker -> revoker -> tombstone
start_conditions:
  owner delete request OR unsafe/failing retirement proposal
exit_conditions:
  retired, tombstoned, blocked by dependency/retention, rollback window expired
required_gates:
  owner approval
  dependency check
  retention check
  credential revocation
```

Behavior:

- blocks new activations first
- revokes active capabilities
- checks dependents and active processes
- preserves tombstone/ledger evidence

## Invariants

- Processes are not privileges.
- Process-local capabilities are narrower than parent authority.
- Process-local capabilities expire at terminal state.
- Child processes inherit narrowed authority and budget.
- Every process has a kill-switch path.
- Every process has ledger evidence for start, transitions, and terminal state.
- Every external side effect is idempotent or guarded by a conflict policy.
- Approval/input waits are explicit states, not hidden blocking.
- Scheduler restart can reconstruct active process state from durable rows.
- A process may propose rewrites but policy applies them.
- Retrying cannot duplicate source pushes, payments, credential issuance, or
  deletion effects.
- Deletion/retirement processes must check dependencies and retention before
  destructive effects.

## Implementation Shape

Near-term:

- project graph processes from existing work/meta/DAG/deploy/review/proof rows
- use `WorkJob` for generic queued process records where possible
- attach process refs to evidence DAG nodes and edges
- add process summaries to the agent dossier
- keep specialized tables for specialized proof/deploy/review facts

Later:

- shared `GraphProcess` model if needed
- process-template registry
- active capability leases and revocation lookup
- scheduler conflict detector
- pause/resume/kill API
- approval/canary/retry policy integration
- process DAG explorer in control room

## Open Gaps

- Not every process family emits shared WorkJob rows yet.
- Approval/input wait state is split across orchestrator hooks and event
  payloads.
- Active process kill/revoke is not yet a universal API.
- Process-local capability expiration is enforced by grant TTL/composition
  budget today, not a central lease table.
- Durable route/canary state does not exist yet.
- Deletion/retirement process needs first-class dependency and retention checks.
