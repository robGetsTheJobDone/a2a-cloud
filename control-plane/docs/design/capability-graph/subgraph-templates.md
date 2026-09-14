# Dynamic Capability Graph Subgraph Templates

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

## Decision

Composability is reusable typed subgraph templates. The kernel should not know
product metaphors. It should validate templates that declare nodes, ports,
edges, capabilities, signals, rewrites, policies, budgets, child subgraphs,
exit conditions, and invariants.

For v0, do not add a separate template registry table first. Build from current
composition rails:

- `MetaAgentManifest` declares goals, composition, sub-agents, memory, pricing,
  and runtime intent.
- `CompositionSubAgent` declares named or tagged callable dependencies and raw
  skills.
- `AgentComposition` declares planning mode, max DAG nodes, max parallelism,
  and max replans.
- `run_meta_agent_goal` plans, validates, executes, and replans bounded DAGs.
- `DagNode` and DAG execution keep calls raw-skill based through `ctx.call`.
- `subagent_tools` expose `list_subagents` and `call_subagent` for planners.

The graph-kernel template spec should generalize this without replacing it.

## Template Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `template_id` | Stable id and version. |
| `name` | Human-readable label. |
| `kind` | `evaluator_loop`, `improvement_loop`, `routing_experiment`, `deletion_process`, `dag`, `relationship_bundle`, custom protocol pack id. |
| `required_node_types` | Node kinds the template needs. |
| `required_port_types` | Port kinds and directions it needs. |
| `edge_types` | Edge types it may create, activate, remove, freeze, or route over. |
| `required_capabilities` | Capabilities needed before activation. |
| `minted_capabilities` | Process-local capabilities it may mint after policy approval. |
| `consumed_signals` | Signal types that can start or steer it. |
| `emitted_signals` | Signal types it may emit. |
| `proposed_rewrites` | Rewrite types it may propose. |
| `required_policies` | Policy checks and gates. |
| `budgets` | Calls, depth, children, runtime, tokens, spend, traffic, retries. |
| `child_templates` | Child subgraphs it may activate. |
| `exit_conditions` | Success/failure/cancel/timeout conditions. |
| `ledger_requirements` | Evidence rows required for each transition. |
| `invariants` | Safety properties preserved by the template. |

Rule: a template describes allowed shape. Runtime activation still requires
policy and capability checks.

## Registry Concept

The registry is a versioned catalog of allowed template shapes.

Near-term registry:

- code/docs plus SDK manifest schemas
- generated `a2a.yaml` composition blocks
- examples and tests
- evidence DAG projection knows known template ids

Later registry:

- signed template definitions
- policy attachment per template
- compatibility constraints
- version migration rules
- deprecation/retirement metadata
- UI explorer for template instances

Registry responsibilities:

- validate template schema
- validate referenced node/port/edge/signal/rewrite/policy types
- declare risk class
- declare required gates
- expose public shape without secrets
- bind template version to process/rewrite evidence

## Composition Modes

### Sequential

One template completion unlocks another.

```text
review_template -> repair_template -> proof_template -> promotion_template
```

Rules:

- downstream template consumes upstream terminal signal
- evidence chain preserves each template version
- downstream authority cannot exceed upstream/owner ceiling

### Nested

Parent template activates child subgraphs.

```text
improvement_loop
  -> evaluator_loop
  -> source_mutation
  -> proof_loop
```

Rules:

- child process inherits narrowed authority and budget
- parent process remains responsible for child ledger refs
- child failure propagates a signal to parent

### Parallel

Multiple templates operate over the same subject.

```text
review_loop(subject=v1)
security_scan(subject=v1)
performance_probe(subject=v1)
```

Rules:

- conflict policy decides whether they can run concurrently
- conflicting rewrites are staged, not applied in parallel
- signals can merge, authority cannot merge without policy

### Relationship Bundle

Reusable edge/template group.

```text
agent -> evaluator
agent -> proof runner
agent -> owner budget
agent -> deployment verifier
```

Rules:

- bundle is a template convenience, not ambient authority
- each edge still has its own policy/capability/provenance

### Scope Composition

Multiple scopes combine by intersection by default.

```text
effective_scope = owner_policy intersect template_scope intersect process_scope
```

Union is a separate high-risk rewrite/policy decision.

### Signal Composition

Signals from one template feed another.

```text
task_failed -> evaluator_loop
review_warning -> improvement_loop
proof_passed -> routing_experiment
```

Signals carry evidence. They do not authorize the next template.

## Canonical Template: Evaluator Loop

```text
subject.output
  -> evaluator.input
  -> evaluator.signal
  -> rewrite_planner.input
  -> policy.input
  -> mutator.input
  -> subject.version
```

Template declaration:

```text
template_id: evaluator_loop@v1
required_node_types:
  - subject
  - evaluator
required_port_types:
  - subject.output
  - evaluator.input
  - evaluator.signal
consumed_signals:
  - task_failed
  - user_requested_review
emitted_signals:
  - review_passed
  - review_warning
  - review_failed
proposed_rewrites:
  - mutate_source
  - mutate_manifest
  - decrease_routing_weight
required_capabilities:
  - read subject artifact/version
minted_capabilities:
  - evaluator readonly process-local grant
required_policies:
  - evaluator may emit only declared signal types
  - evaluator cannot mutate subject
exit_conditions:
  - review emitted terminal signal
  - ttl expired
ledger_requirements:
  - process started
  - evaluator invocation
  - review signal
invariants:
  - evaluation authority is not mutation authority
```

The same shape can support review, adversarial testing, quality gates,
certification, regression repair, marketplace ranking, or product feedback
without hard-coding those product labels into the kernel.

## Worked Examples

### Research Meta-Agent DAG

Current rail:

- manifest declares sub-agents and skills
- planner emits bounded DAG
- DAG executor calls raw skills
- meta-run state records goal, plan, progress, summary, result
- composition budget prevents runaway recursive calls

Template view:

```text
template_id: raw_skill_dag@v1
required_node_types:
  - meta_agent
  - subagent
required_port_types:
  - invoke:<skill>
edge_types:
  - call
required_capabilities:
  - invoke declared subagent skill
budgets:
  max_nodes: manifest.composition.max_nodes
  max_parallel: manifest.composition.max_parallel
  max_replans: manifest.composition.max_replans
  max_depth: composition_budget.max_depth
emitted_signals:
  - task_succeeded
  - task_failed
  - capability_gap
ledger_requirements:
  - meta_run
  - dag events
  - subagent run/receipt/grant refs
```

### Source Repair Template

```text
template_id: source_repair@v1
consumed_signals:
  - review_failed
  - task_failed
participants:
  - subject repo
  - code editor mutator
  - reviewer
  - proof runner
required_policies:
  - owner approval for mutation
  - no source mutation without rollback plan
  - review/proof gate before promotion
proposed_rewrites:
  - mutate_source
  - rollback_source
exit_conditions:
  - proof_passed
  - owner_rejected
  - budget_exhausted
  - rolled_back
```

### Deletion Template

```text
template_id: deletion_cascade@v1
participants:
  - target node
  - dependency checker
  - retention checker
  - revoker
  - tombstone writer
required_policies:
  - owner approval
  - dependency check
  - retention/legal check
  - credential revocation
proposed_rewrites:
  - freeze_node
  - revoke_edge
  - retire_node
  - delete_node
  - tombstone_identity
  - sever_lineage
exit_conditions:
  - tombstone complete
  - blocked by dependency
  - blocked by retention
```

### Routing Experiment Template

```text
template_id: routing_experiment@v1
consumed_signals:
  - cheap_success
  - routing_degraded
  - dependency_failed
required_policies:
  - routing weight is not permission
  - canary traffic cap
  - rollback on degradation
proposed_rewrites:
  - strengthen_edge
  - weaken_edge
  - reroute
budgets:
  max_traffic_percent: 5
  max_runtime: 24h
```

## Conflict Resolution

Parallel templates may conflict over target, capability, budget, or rewrite.

Conflict dimensions:

- same target artifact/version
- overlapping write scope
- incompatible edge state changes
- competing route weights
- shared budget account
- deletion versus mutation
- freeze/revoke versus activation
- child process count/depth exhaustion

Resolution rules:

- freeze/revoke blocks call/mutate/route activation
- deletion/retirement blocks new mutation unless policy says remediation first
- two source mutations on the same branch serialize by idempotency/conflict key
- budget allocation conflicts use policy priority and owner ceiling
- route-weight conflicts can run in parallel only if traffic/canary scopes do
  not overlap
- signals can merge, capabilities cannot merge without policy
- failed child template emits a signal to parent and may trigger compensation

## Mapping To Existing Manifest Fields

| Template concept | Current field/API |
|---|---|
| subagent dependency | `CompositionSubAgent.name` / `tag` |
| required raw skills | `CompositionSubAgent.skills` |
| default args | `CompositionSubAgent.default_args` |
| optional dependency | `CompositionSubAgent.required` |
| planning mode | `AgentComposition.planning` |
| max nodes | `AgentComposition.max_nodes` |
| max parallel | `AgentComposition.max_parallel` |
| max replans | `AgentComposition.max_replans` |
| goal | `AgentGoal.objective` |
| success criteria | `AgentGoal.success_criteria` |
| memory | `AgentMemory` |
| runtime call | `ctx.call`, DAG `context_node_caller` |
| process budget | `CompositionBudget` |
| process evidence | `MetaAgentRun`, `DagRun`, `SubagentRun`, `WorkJob` |

## Invariants

- Templates describe shape; policy grants activation.
- Template activation cannot exceed parent capability.
- Child templates inherit narrowed scope and budget.
- Scope composition is intersection by default.
- Union requires explicit policy approval.
- Signals can trigger templates but cannot authorize them.
- Parallel rewrites over same target need conflict policy.
- Evaluator templates cannot mutate subjects unless separately authorized.
- Relationship bundles do not create ambient authority.
- Every template instance records template id/version and ledger refs.
- Product metaphors live above templates, not inside kernel primitives.

## Implementation Shape

Near-term:

- document graph template ids for existing meta-agent/DAG/review/proof/deploy
  flows
- project template instances in the evidence DAG from existing rows
- preserve `MetaAgentManifest` as the current authoring surface for user-facing
  composition
- add template id/version metadata to new WorkJob or MetaAgentRun rows when
  practical

Later:

- signed template registry
- template validation API
- process-template runner
- conflict detector
- template explorer UI
- marketplace/protocol-pack template publishing

## Open Gaps

- Existing manifests do not declare required signals, rewrites, policies, or
  invariants explicitly.
- DAG nodes are raw agent/skill calls, not typed graph ports yet.
- Template version is not consistently recorded on all process rows.
- Conflict resolution is currently family-specific, not generic.
- Product-pack templates need registry/signing before third-party publication.
