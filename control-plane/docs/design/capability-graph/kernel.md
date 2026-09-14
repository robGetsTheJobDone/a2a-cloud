# ADR: Dynamic Capability Graph Kernel

Status: exploratory framing, not an implementation plan.

Date: 2026-06-02

## Decision

Use **Dynamic Capability Graph Kernel** as the internal name for the future
kernel. Product labels such as Life Engine, Episode Engine, school, market,
employment, reproduction, retirement, or society are protocol packs above the
kernel, not primitives inside it.

The kernel starts from graph dynamics:

- Nodes represent agents, humans, orgs, tools, memories, repos, prompts,
  models, reviewers, policies, budgets, wallets, credentials, datasets,
  manifests, source versions, routers, schedulers, and ledgers.
- Ports are typed interfaces on nodes.
- Edges connect ports and carry authority, data, money, memory, dependency,
  trust, routing preference, evaluation rights, control, or revocation rights.
- Capabilities live on edges, not ambiently on nodes.
- Signals are observations flowing through the graph.
- Processes are activated subgraphs running over time.
- Rewrites are proposed graph changes.
- Policies decide which edge activations and rewrites are legal.
- The ledger records causality.

This keeps the kernel small enough to reason about and broad enough to support
future product-specific protocols without baking those metaphors into the core.

## Context

A2A Cloud already has many concrete pieces that look like graph evidence:

- signed SDK grants and control-plane `GrantAudit` rows
- signed `ExecutionReceipt` and `ReplaySession` records
- `AgentReceipt` and `AgentSession` persistence
- `SubagentRun` and `SubagentRunEvent`
- `DagRun`, `DagRunNode`, and `MetaAgentRun`
- `AgentDeployment` and `AgentDeploymentEvent`
- `AgentReviewRun` and `AgentProofRun`
- `WorkJob` and `WorkEvent`
- `LLMUsageEvent`
- Gitea token audit rows and code-editor commit SHAs

The first practical move should be a read-only evidence DAG projection over
existing tables, not a new generalized graph database. That projection should
answer: what ran, under what authority, against which version, with which
review/proof/deployment evidence, and what changed after it.

## Glossary

| Primitive | Meaning |
|---|---|
| Node | A thing with identity and typed ports: agent, user, org, repo, source SHA, model, memory namespace, budget account, policy, reviewer, dataset, manifest, or service. |
| Port | A typed interface on a node, such as invoke skill, read memory, write repo, evaluate source, spend budget, route traffic, revoke grant, or emit signal. |
| Edge | A typed relation from one port to another. Edges carry scope, ttl, budget, weight, provenance, policy refs, and ledger refs. |
| Capability | The authority encoded on an edge. No node has ambient authority outside its incoming or process-local edges. |
| Signal | An observation emitted by a process or evaluator: success, failure, review finding, cost, latency, user feedback, unsafe event, stale memory, deploy status. |
| Process | An activated subgraph over time, such as a DAG run, agent-builder run, review loop, proof run, deploy, or code-editor turn. |
| Rewrite | A proposed graph mutation: add/remove edge, update prompt, change manifest, patch source, route traffic, revoke credential, retire memory. |
| Policy | A rule source deciding whether an edge activation or rewrite is allowed, denied, needs approval, needs review, needs canary, or must narrow scope. |
| Ledger | Append-only causality evidence tying authority, reason, source version, approval, result, and follow-up signals together. |
| Template | A reusable typed subgraph pattern, such as evaluator loop or build-review-improve loop. |
| Protocol pack | Product-facing semantics layered above templates. Human/social labels live here, not in the kernel. |

## Core Invariants

- Capabilities exist on edges, never as hidden ambient node state.
- Child capability must be less than or equal to parent capability.
- Deny, freeze, revoke, ttl, and budget constraints override allow edges.
- Policy sources compose by restriction, not escalation.
- Nodes do not mutate graph state directly; they may propose rewrites.
- Dangerous rewrites are staged, reviewed, versioned, and ledgered before
  promotion.
- Every edge activation and rewrite has provenance.
- Active processes are bounded by budget, ttl, max depth, and kill-switches.
- Routing weight is not permission.
- Routing decisions must be explainable from signals, weights, constraints,
  and policies.

## Existing Substrate Mapping

| Existing artifact | Graph interpretation |
|---|---|
| SDK `Grant` / `delegate_grant` | Capability edge token with audience, file scope, ttl, LLM budget, and delegation depth. |
| `GrantAudit` | Ledger row for grant creation/decision and parent grant chain. |
| `ExecutionReceipt` / `AgentReceipt` | Signed evidence that a skill invocation occurred with status, timing, caller, artifacts, file ops, and handoffs. |
| `ReplaySession` / `AgentSession` | Ordered event log for reconstructing an invocation process. |
| `SubagentRun` / `SubagentRunEvent` | Cross-agent process and event timeline keyed by `grant_id`. |
| `DagRun` / `DagRunNode` | Activated DAG process, node statuses, and dependency execution evidence. |
| `MetaAgentRun` | Goal, current plan, progress, state, and completion evidence for a composable meta-agent. |
| `AgentDeployment` / `AgentDeploymentEvent` | Source/runtime version change, build, image, Argo, and live URL process evidence. |
| `AgentReviewRun` | Evaluator signal over source, manifest, grants, or runtime behavior. |
| `AgentProofRun` | Proof signal that an agent version performed a live invocation. |
| `WorkJob` / `WorkEvent` | Durable background process and progress ledger. |
| `LLMUsageEvent` | Budget/cost signal for model use. |
| Gitea token audit | Temporary repo capability edge with ttl, permission, issuer, and revocation. |
| Source SHA / image tag / Agent Card hash | Version nodes that evidence, reviews, proofs, and deployments can reference. |

The v0 graph should be a projection over these records. New graph tables should
wait until the projection proves which joins, edge types, and missing
correlation ids matter.

## Relationship To Current Epics

### User-Composable Meta-Agents

The completed meta-agent work supplies near-term composition substrate:
manifest, sub-agent allow-list, goal, memory, DAG plan/execute/replan, child
auth validation, sandbox validation, and recursive safety budget. In graph
terms, a composition manifest is a process template plus allowed child
capability edges. `ctx.call` activates those edges.

### Agent Studio

Agent Studio is the concrete build-review-edit-deploy process over existing
first-party agents. It should not wait for the kernel. Instead it should emit
strong evidence ids: build brief id, review finding ids, code-editor turn id,
commit SHA, deployment id, proof id, and residual risk summary. Those become
excellent v0 graph edges later.

### Self-Improvement Spine

The self-improvement spine is the safety layer required before any autonomous
rewrite becomes real: owner grant ceiling, continuous review, autonomy tiers,
budget and kill-switch enforcement, audited/versioned mutations, canary, and
rollback. The graph kernel should model self-modification as a staged rewrite;
the spine decides whether the rewrite can execute.

### Emergent Ecosystem

Fleet-level phenomena such as crystallize/evaporate, marketplace dynamics,
breeding/culling, adversarial reviewers, and autopoietic networks are protocol
packs and graph dynamics research. They depend on the safety spine and should
not be treated as kernel primitives.

## Non-Goals

- Do not implement a graph database in this task.
- Do not hard-code human or social metaphors into kernel primitives.
- Do not imply autonomous self-expansion.
- Do not allow nodes to mutate themselves or the graph directly.
- Do not block Agent Studio, user-composable meta-agents, or deployment work on
  this research.
- Do not expose secrets, grant tokens, CP JWTs, repo write tokens, private
  traces, or sensitive memory in graph projections.

## Initial Build Sequence

1. Inventory existing evidence sources and stable join keys.
2. Define a read-only Agent Evidence DAG projection over current tables.
3. Build an agent dossier/timeline API over the projection.
4. Correlate failure -> review -> patch -> commit -> deploy -> proof chains.
5. Use Agent Studio to emit stronger correlation ids where gaps exist.
6. Add first-class graph/rewrite/process tables only after v0 proves useful.
7. Before any mutation authority, complete policy, threat model, simulation,
   kill-switch, and proof-obligation work.

## Open Questions

- Which existing ids are stable enough as first-class graph node ids?
- Should `WorkJob`/`WorkEvent` become the primary process ledger projection, or
  should a later `EvidenceEvent` table normalize cross-surface events?
- Which capabilities need signed edge tokens and which can remain server-side
  policy checks?
- How much read-only evidence graph context should be exposed to agents?
- What redaction level is acceptable for user-visible dossiers versus internal
  operator views?
- Which Agent Studio correlation ids should be added first to make review to
  patch to deploy chains unambiguous?
