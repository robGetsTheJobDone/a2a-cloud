# Dynamic Capability Graph A2A Pack Mapping

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
- `docs/routing-dynamics.md`

## Decision

A2A Pack and the control plane are the first concrete implementation substrate
for the Dynamic Capability Graph Kernel. The kernel should not replace the SDK.
It should name the graph semantics already present in A2A Pack, then add narrow
projection, explanation, revocation, and rewrite state only where the existing
rails cannot answer graph questions.

The current platform already has real graph pieces:

- executable agent nodes through `A2AAgent`
- callable ports through `@a2a.tool` (bare `@tool` / legacy `@skill` are the
  same object), `SkillSpec`, and Agent Card `SkillCard`
- edge traversal through `ctx.call` / `A2AClient.call`
- discovery through `ctx.discover`, `ControlPlaneDiscovery`, and agent search
- scoped edge authority through signed `Grant` and `WorkspaceGrant`
- bounded recursive composition through `CompositionBudget`
- manifest-backed meta-agents through `MetaAgentManifest` and `MetaAgent`
- evidence through signed `ExecutionReceipt`, signed `ReplaySession`,
  `AgentEvent`, and control-plane ledger rows
- pricing declarations through Agent Card `Pricing`
- source mutation rails through code-editor and source-push deployment
- evaluator rails through agent-reviewer, proof, review, and trial runs

The graph kernel should start by projecting these rails. Do not block Agent
Studio or meta-agent work on a new graph database.

## Primitive Mapping

| Kernel primitive | Existing A2A/platform rail | Current fit | Main gap |
|---|---|---|---|
| Node | `A2AAgent`, deployed agent row, external agent, `MetaAgent`, manifest-backed agent | Strong for executable agents | No generic node table for memory, policy, budget, credential, version, router, evaluator. |
| Port | `@a2a.tool`, `SkillSpec`, `SkillCard`, MCP tools/endpoints | Strong for callable ports | No first-class non-call ports for mutate, evaluate, store, route, fund, revoke, delete. |
| Edge | `ctx.call`, `A2AClient.call`, DAG node dependency, subagent handoff | Strong at runtime | Edge state is not durable or queryable as graph state. |
| Capability | signed `Grant`, `WorkspaceGrant`, LLM caps, consumer setup, CP JWT | Strong for runtime scoped rights | Global active revocation and durable capability edge projection are missing. |
| Discovery | `ctx.discover`, `ControlPlaneDiscovery`, `/agents/search`, semantic/lexical search | Useful candidate discovery | No policy-aware route explanation or adaptive route weights yet. |
| Signal | `AgentEvent`, `WorkEvent`, review/proof/trial statuses, LLM usage | Good substrate | No dedicated signal projection API yet. |
| Process | `MetaAgentRun`, DAG runs, `CompositionBudget`, `WorkJob`, deployments, reviews, proofs | Strong enough for v0 projections | No shared graph process API for pause/resume/kill/approval/canary. |
| Rewrite | code-editor turn, agent-builder, source-push deploy, manifest generation | Concrete mutation rails exist | No first-class rewrite proposal envelope or common approval/canary lifecycle. |
| Policy | grant minting/audit, `UserControlPolicy`, owner checks, consumer setup, review gates | Strong scattered rails | No unified policy explanation API. |
| Ledger | `GrantAudit`, receipts, sessions, `WorkJob/Event`, deployments, reviews, proofs, LLM usage | Strong evidence substrate | No normalized Evidence DAG/dossier projection yet. |
| Routing weight | search score, public/status, future signal projections | Thin current rail | No durable route weight or route decision row. |
| Lineage | `Agent.source_agent_id`, source SHA, Agent Card hash, image tag | Thin current rail | No explicit lineage/inheritance/severance graph edge. |
| Economics | Agent Card `Pricing`, `LLMUsageEvent`, `Bounty` | Partial | No revenue share, settlement, or budget account node. |
| Deletion | agent delete, secret delete, memory delete, auth connection delete | Useful hard rails | No tombstone, dependency preview, active revocation projection. |

## SDK Rails

### Agent Node

`A2AAgent` is an executable graph node.

Current node fields come from:

- agent class `name`
- `description`
- `version`
- `capabilities`
- `required_secrets`
- `required_env`
- input/output modes
- runtime deployment row in the control plane
- source repo, image, URL, status, owner, organization

Graph interpretation:

```text
node: agent:<name>
kind: executable_agent
version_refs:
  - agent.version
  - source_sha
  - image
ports:
  - skills from Agent Card
```

Gap: non-agent graph nodes such as memory namespace, credential, policy,
budget account, dataset, router, evaluator, mutator, and version are currently
implicit or spread across specialized rows.

### Callable Port

`@a2a.tool` (bare `@tool` / legacy `@skill` are the same object) and
`SkillSpec` define callable ports. `AgentCard.from_agent` projects
them to `SkillCard` with id, name, description, tags, scopes, streaming flag,
policy, input schema, and output schema.

Graph interpretation:

```text
port: agent:<name>.skill:<skill_name>
port_type: call
declares:
  input_schema
  output_schema
  tags
  scopes
  stream
  policy
```

Gap: the kernel also needs typed non-call ports:

- `evaluate`
- `mutate`
- `store`
- `route`
- `fund`
- `revoke`
- `observe`
- `certify`
- `inherit`
- `depend`
- `delete`

Do not force these into skill names unless they truly execute as user-callable
skills. The graph projection can model them above the current SDK.

### Edge Traversal

`ctx.call` and `A2AClient.call` are runtime edge traversal.

Current call signature carries:

- target
- skill
- args
- signed grant
- control-plane JWT where applicable
- LLM credentials
- timeout
- composition payload

Graph interpretation:

```text
edge_activation:
  from: caller process/agent
  to: target agent skill port
  capability: signed grant
  bounds:
    timeout
    composition budget
    LLM budget/rate/token caps
  evidence:
    receipt, replay session, subagent run, work event
```

Gap: edge definitions and route weights are not durable graph rows. Today the
edge often exists only as an invocation plus evidence after the fact.

### Discovery

`ctx.discover` returns a `DiscoveryClient`. Discovery can resolve named agents
or find agents by tags, capability, skill, and limit. The control plane also
has semantic and lexical search for visible agents.

Graph interpretation:

```text
candidate_discovery:
  visible agents
  Agent Card skill/tag/capability match
  semantic or lexical match
```

Gap: discovery is not yet routing. It does not explain policy filters, trust,
cost, review/proof freshness, route weights, tombstones, or user/org
preferences.

### Capability Token

Signed `Grant` is the portable runtime capability token.

Important fields:

- grant id
- issuer
- audience
- bucket
- workspace mode
- allow and deny patterns
- outputs prefix
- write prefixes
- LLM model and budget/rate/token caps
- parent grant id
- delegation depth and max delegation depth
- issued/expires timestamps

Graph interpretation:

```text
capability_edge:
  actor: Grant.audience
  issuer: Grant.issuer
  resource: Grant.bucket
  actions: mode + LLM/tool/resource rights
  scope: allow/deny/write/output patterns
  parent: parent_grant_id
  ttl: expires_at
```

Gap: the signed token is TTL-bounded and independently verifiable, but global
active revocation requires a server-side revocation check or short-lived lookup.

### Workspace Resource Edge

`WorkspaceGrant` is a bounded resource edge for files. It records grant id,
purpose, files, mode, reason, expiration, and whether human approval is
required.

Graph interpretation:

```text
edge: actor -> workspace bucket/files
type: store/read/write
scope: file matches
mode: read/write
approval: required or already granted
```

Gap: the graph needs the same shape for memory namespaces, credentials,
datasets, prompts, source repos, and policy resources.

### Composition Budget

`CompositionBudget` is process-local graph physics for recursive composition.
It carries run id, root agent, current agent, stack, max depth, max calls,
remaining calls, LLM budget, and remaining LLM budget.

Graph interpretation:

```text
process_budget:
  process: composition run
  root: root_agent
  current: current_agent
  stack: active call stack
  max_depth/max_calls
  remaining_calls
  llm_budget
```

This already enforces several kernel invariants:

- bounded depth
- bounded call count
- cycle prevention through stack
- bounded LLM budget
- child calls consume parent budget

Gap: this is not yet a general process budget for deletion, rewrite, routing
experiments, canaries, or marketplace processes.

### Execution Receipt

`ExecutionReceipt` is signed runtime evidence.

It records:

- receipt id and schema version
- agent name and version
- caller and task id
- skill name
- input hash/preview
- grant ids used
- file ops, tool calls, artifacts, handoffs
- status, error type, result preview
- eval score and reviewer
- start/end/duration timing

Graph interpretation:

```text
ledger_event:
  event_type: receipt_recorded
  subject: agent skill invocation
  authority: grant_ids
  outcome: status/error/result preview
  evidence: file ops, tool calls, artifacts, handoffs
```

Gap: receipts are excellent evidence, but not every runtime path may post them
to the control plane yet. The DAG projection must tolerate missing receipts.

### Replay Session

`ReplaySession` is a signed ordered event log for one skill run. It links to the
receipt id and carries identity, deterministic random seed, input hash, timing,
and ordered replay events.

Graph interpretation:

```text
ledger_event:
  event_type: session_recorded
  subject: skill run
  edge: receipt_id -> session_id
  evidence: replay events
```

Gap: replay visibility and payload redaction need audience-aware projection.

### Agent Card Capabilities

Agent Cards already publish:

- skills
- capabilities
- MCP endpoint metadata
- meta-agent manifest public payload when present
- package/version metadata
- required secrets/env
- input/output modes

Graph interpretation:

```text
agent_card:
  declares ports
  declares compatibility/capabilities
  declares MCP surfaces
  declares meta-agent manifest summary
```

Gap: Agent Card is a declaration, not proof. Trust requires receipts, reviews,
proofs, deployments, and ledger evidence.

### Pricing Declaration

`Pricing` declares price per call, whether caller pays LLM, and pricing notes.

Graph interpretation:

```text
economic_signal:
```

Gap: pricing declaration is not settlement. Revenue share, earning, and billing
events require future economic ledgers.

### Manifest-Backed Meta-Agent

`MetaAgentManifest`, `AgentComposition`, `CompositionSubAgent`, and `MetaAgent`
give a concrete substrate for synthetic/manifest-backed graph nodes.

Current rails:

- declared sub-agents by name/tag/version
- declared skills and default args
- planning mode
- max nodes, max parallel, max replans
- goal and success criteria
- memory manifest
- `pursue` skill that plans and executes a bounded raw-skill DAG

Graph interpretation:

```text
node: meta-agent manifest
ports:
  - pursue
subgraph_template:
  - declared sub-agents
  - bounded DAG planner
  - memory scope
process:
  - composition budget
  - meta goal
  - DAG execution
```

Gap: manifest version ids, template ids, route explanations, and rewrite
proposal ids need to be emitted for a full evidence DAG.

## Control-Plane Rails

| Graph concept | Control-plane rail |
|---|---|
| agent node persistence | `Agent` row |
| source/deployment version | `AgentDeployment`, `AgentDeploymentEvent` |
| review/evaluator evidence | `AgentReviewRun` |
| proof evidence | `AgentProofRun` |
| signed receipt persistence | `AgentReceipt` |
| replay session persistence | `AgentSession` |
| grant authority audit | `GrantAudit` |
| handoff run evidence | `SubagentRun`, `SubagentRunEvent` |
| generic process ledger | `WorkJob`, `WorkEvent` |
| LLM cost | `LLMUsageEvent` |
| user budget/policy | `UserControlPolicy` |
| memory resource | `AgentMemoryEntry` and memory routes |
| credential resource | `AgentSecret`, runtime secret, auth connection |
| source mutator | code-editor-agent turn result and Gitea push |
| node factory | agent-builder and create/import/from-source/openapi/compose routes |
| discovery/search | public/owned agent list and `/agents/search` |
| deletion/revocation | delete agent, secret, memory, auth connection cleanup |

Graph-kernel implementation should project these rows before adding new tables.

## Existing Epic Relationship

### User-Composable Meta-Agents

Provides:

- composition manifests
- raw-skill DAG planning
- bounded `ctx.call` chains
- memory declarations
- synthetic-host direction
- DAG/meta-run evidence

Graph role:

- near-term subgraph/template substrate
- proof that composability already exists in A2A rails

### Self-Improvement Spine

Provides:

- owner grant ceiling
- continuous review/proof gates
- autonomy tiers
- budgets
- kill-switch direction
- audited versions
- canary/rollback requirements

Graph role:

- safety spine required before active self-rewrites or adaptive routing

### Agent Studio

Provides:

- concrete build/review/edit/deploy loop
- code-editor and source-push mutation evidence
- user-facing planner/coordinator surface
- deployed-agent improvement workflow

Graph role:

- practical rewrite-planner pipeline for deployed/source-backed agents
- should continue independently while graph kernel projections mature

### Emergent Ecosystem

Provides:

- future product metaphors and long-running community dynamics
- marketplace/status/economic/taste surfaces

Graph role:

- protocol packs above the kernel, not hard-coded kernel behavior

## Gaps In Current A2A Pack

### Graph State

- no durable generic node/edge/port table
- no route weight table
- no typed non-call port declarations
- no explicit dependency edge projection beyond manifests and call evidence

### Capability And Revocation

- no global active revocation lookup for all signed grants
- no durable capability edge state
- no shared revocation propagation model in SDK
- process-local capabilities are not all visible through one query

### Policy

- policy is scattered across grant minting, owner checks, setup checks, budget
  checks, review/proof gates, and runtime validation
- no unified policy explanation response
- no common allow/deny/narrow/approval/review/canary decision object

### Evidence DAG

- rich evidence exists, but no normalized evidence DAG endpoint yet
- join keys need normalization across grant id, receipt id, session id,
  deploy id, review id, proof id, head SHA, job id, correlation id, and thread
  id
- code-editor mutation evidence may be nested inside result payloads
- missing receipt/session rows must be tolerated

### Rewrite And Process

- code-editor/source-push/agent-builder are real mutators, but no generic
  rewrite proposal envelope
- no shared rewrite state machine
- no common approval/canary/rollback lifecycle
- no shared graph process API for pause/resume/kill/retry

### Routing And Adaptation

- discovery/search exists, but policy-aware route explanation does not
- route weights are not durable
- adaptive rules are not implemented
- simulation harness does not exist yet

### Lineage, Deletion, Economics

- lineage is mostly `Agent.source_agent_id` and version refs
- tombstones and lineage severance are not durable yet
- pricing is declared, but revenue share and settlement are future ledgers
- taste/status/reputation are projection concepts, not product ledgers yet

## Migration Path

### Step 1: Normalize Projections

Build read-only projections over existing rows:

- capability chain from `GrantAudit`
- execution chain from receipts, sessions, handoffs, work events
- version chain from source SHA, deployment, review, proof
- mutation chain from code-editor result and source-push deployment
- cost chain from LLM usage and process budgets
- lineage chain from `Agent.source_agent_id`, source SHA, card hash, tombstone

Output should be agent dossier and evidence DAG, not a new graph runtime.

### Step 2: Add Missing Join Keys Where Cheap

Prefer small fields or metadata additions:

- rewrite proposal id on code-editor runs
- manifest version id on meta-runs
- template id/version on DAG/meta-run evidence
- deterministic review finding id/hash
- route decision id where route explanation is shown
- memory operation id for purge/summarize/write

These improve projection quality without blocking Agent Studio.

### Step 3: Add Explanation APIs

Add read APIs before write APIs:

- why can this agent call that skill?
- what grants and policies allowed this call?
- what version is live and why is it trusted?
- why was this route selected?
- what would deletion affect?
- what evidence supports this reputation/status?

### Step 4: Add Narrow Active State

Only after projections prove demand, add small active-state tables:

- grant revocation lookup
- tombstone/severance row
- route decision/weight row
- rewrite proposal row
- graph process row

Each table should map back to existing evidence and policy rows.

### Step 5: Keep Agent Studio Moving

Agent Studio should keep shipping:

- build from prompt/source/openapi
- deploy/review/proof loop
- code-editor repair loop
- source-push deploy loop
- evidence views using current rows

The graph kernel should become the explanation and safety layer around that
workflow, not a blocker before the workflow exists.

## Worked Examples

### A2A Call As Graph Edge Activation

```text
caller process
  -> discovers agent:summary-agent skill:summarize
  -> policy mints Grant(g1)
  -> ctx.call(target=summary-agent, skill=summarize, grant=g1)
  -> callee executes skill port
  -> receipt records grant_ids=[g1], status, file_ops, artifacts, handoffs
  -> replay session records ordered events
  -> control-plane rows project edge activation
```

Kernel view:

```text
edge: caller.call -> summary-agent.skill.summarize
capability: Grant(g1)
ledger: GrantAudit(g1), AgentReceipt, AgentSession, WorkEvent
```

### Meta-Agent DAG As Activated Subgraph

```text
MetaAgentManifest
  -> composition sub-agents
  -> pursue skill
  -> bounded DAG planner
  -> CompositionBudget
  -> ctx.call children
  -> DAG/meta-run/subagent/work evidence
```

Kernel view:

```text
process: meta-agent goal run
template: DAG composition
participants: declared sub-agent ports
budget: CompositionBudget
ledger: meta-run, DAG run, subagent runs, receipts, work events
```

### Source Mutation As Rewrite

```text
review finding
  -> Agent Studio/code-editor request
  -> scoped Gitea write token
  -> code-editor changes source
  -> push.head_sha
  -> source-push deploy
  -> review/proof/canary
  -> promotion or rollback
```

Kernel view:

```text
rewrite: mutate_source
mutator: code-editor-agent
target: source repo + old SHA
result: new SHA
gates: owner approval, review, proof
ledger: work events, deployment, review, proof
```

## V0 Build Guidance

Do not implement P12 as a new framework layer inside A2A Pack first.

Practical order:

1. Keep A2A Pack primitives stable: grants, receipts, replay, composition
   budget, meta-agent manifest, Agent Card declarations.
2. Implement graph projections in the control plane over existing SDK/control
   plane evidence.
3. Add tiny SDK metadata only when projection quality needs it.
4. Make Agent Studio emit rewrite/correlation ids as it coordinates build,
   review, code-edit, deploy, proof, and repair.
5. Use graph docs and evidence projection to shape future SDK APIs.
6. Add first-class graph state only for active revocation, route decisions,
   tombstones, graph processes, or rewrite lifecycle when needed.

## Open Questions

- Should typed non-call ports be represented in Agent Card capabilities, a new
  graph projection, or both?
- Which active revocation check can be added without making every runtime call
  control-plane dependent?
- Should `CompositionBudget` generalize into a broader `ProcessBudget`, or stay
  specific to composition while the control plane owns broader process state?
- Where should route decision ids live: SDK call metadata, control-plane
  work events, or both?
- What is the smallest manifest versioning scheme that helps evidence DAGs?
- Which Agent Studio actions should emit rewrite proposal ids first?
- How much pricing/accounting belongs in Agent Card declarations versus future
  billing ledgers?

The answer should stay pragmatic: project what already exists, add explicit
metadata where it unlocks evidence, and avoid turning the SDK into a speculative
graph database.
