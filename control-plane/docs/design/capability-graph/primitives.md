# Dynamic Capability Graph Primitives

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing: `docs/kernel.md`.

## Goal

Define the conceptual primitives for the Dynamic Capability Graph Kernel tightly
enough that later protocol packs can be expressed without changing the kernel.
The model must support deployed agents, synthetic/manifest-backed agents,
long-running processes, review/evaluation loops, and self-modification as
policy-governed graph rewrites.

This is not a database schema. Field names below are intentionally close to a
future typed model, but v0 should still start as a projection over existing
control-plane evidence rows.

## Primitive Schema

### Node

A durable or executable entity with identity and typed ports.

Conceptual fields:

| Field | Meaning |
|---|---|
| `node_id` | Stable id in the graph namespace, e.g. `agent:chart-agent`, `repo:gitea_admin/chart-agent`, `version:sha:<sha>`. |
| `kind` | `agent`, `human`, `org`, `repo`, `source_version`, `manifest`, `prompt`, `memory_namespace`, `model`, `dataset`, `reviewer`, `budget_account`, `credential`, `policy`, `router`, `evaluator`, `mutator`, `scheduler`, `ledger`, `service`. |
| `owner_ref` | Owning user/org/platform principal, when applicable. |
| `version_ref` | Optional source SHA, manifest version, card hash, image tag, or prompt version. |
| `ports` | Typed interfaces the node exposes. |
| `metadata` | Redacted display metadata only; never store secrets here. |

Rule: a node is identity, not authority. Authority arrives through edges or
process-local capabilities.

### Port

A typed interface on a node.

Conceptual fields:

| Field | Meaning |
|---|---|
| `port_id` | Stable within node, e.g. `invoke:pursue`, `repo:write`, `memory:read`, `review:emit_signal`. |
| `direction` | `in`, `out`, or `both`. |
| `kind` | `receive_work`, `emit_work`, `evaluate`, `mutate`, `store`, `route`, `fund`, `revoke`, `observe`, `certify`, `inherit`, `depend`, `delete`. |
| `schema_ref` | Input/output schema or protocol contract. |
| `risk_class` | `low`, `medium`, `high`, `dangerous`. |

Examples:

- An A2A skill is an `invoke:<skill>` port.
- A code-editor repo write surface is a `mutate:source` port.
- A reviewer finding stream is an `evaluate:source` output port.
- A grant revocation surface is a `revoke:grant` port.

### Edge

An active connection from one port to another. Edges can carry authority, data,
signals, money, dependency, trust, routing preference, control, or revocation.

Conceptual fields:

| Field | Meaning |
|---|---|
| `edge_id` | Stable or derived id for this relation. |
| `from` / `to` | `{node_id, port_id}` endpoint refs. |
| `type` | `call`, `observe`, `evaluate`, `mutate`, `store`, `route`, `fund`, `revoke`, `delete`, `certify`, `inherit`, `depend`, `signal`. |
| `capability` | Scoped right attached to this edge, if any. |
| `weight` | Routing/trust/preference score. Not permission. |
| `state` | `proposed`, `active`, `frozen`, `revoked`, `expired`, `retired`. |
| `provenance_ref` | Ledger/evidence row proving why this edge exists. |
| `policy_refs` | Policies governing use, delegation, renewal, and revocation. |

Rule: all edge activation must be policy-checked. A relationship without a
capability can express dependency or evidence, but it cannot authorize action.

### Capability

The scoped right attached to an edge or minted inside a bounded process.

Conceptual fields:

| Field | Meaning |
|---|---|
| `actor_ref` | Node/process allowed to exercise the right. |
| `resource_ref` | Resource node or artifact set. |
| `actions` | Allowed actions, e.g. `invoke`, `read`, `write`, `review`, `mutate`, `delete`, `delegate`. |
| `scope` | Resource/action limits: file patterns, skill names, repo path, memory namespace, model id, spend class. |
| `bounds` | Max calls, max runtime, max tokens, max spend, max depth, max children. |
| `ttl` | Expiration time or duration. |
| `delegation` | `none`, `attenuated`, or `transitive_under_policy`. |
| `parent_ref` | Parent edge/grant/process capability. |
| `revocation_ref` | Port/policy able to revoke it. |
| `audit_required` | Whether use must emit ledger evidence. |
| `reason` | Human or process-readable reason for issuance. |

Mapping to current platform:

- SDK `Grant` is the concrete signed token form for workspace/LLM/call
  capabilities.
- `GrantAudit.parent_grant_id` is a parent capability chain.
- Gitea token audit rows are temporary source-mutation capabilities.

Rule: child capability must be less than or equal to parent capability.

### Signal

A typed observation from execution, evaluation, user feedback, cost tracking,
or platform policy.

Conceptual fields:

| Field | Meaning |
|---|---|
| `signal_id` | Stable id or deterministic hash. |
| `signal_type` | `success`, `failure`, `timeout`, `cost`, `latency`, `review_finding`, `policy_violation`, `unsafe_behavior`, `dependency_failure`, `capability_gap`, `drift`, `user_feedback`, `stale_memory`, `deploy_status`, `mutation_outcome`. |
| `emitter` | Node/port/process that emitted it. |
| `subject` | Node/edge/process/version/artifact it describes. |
| `severity` | `info`, `warning`, `critical`, `blocker`. |
| `confidence` | Numeric or categorical confidence. |
| `evidence_refs` | Receipts, sessions, reviews, deployments, work events, logs, commit SHA, manifest version. |
| `redaction_class` | `public`, `owner`, `internal`, `security`, `secret_ref`. |
| `suggested_effects` | Optional rewrite proposals or routing updates. |

Rule: signals may influence routing and rewrite proposals, but signals do not
grant authority.

### Process

An activated subgraph that runs over time.

Conceptual fields:

| Field | Meaning |
|---|---|
| `process_id` | Run/job/deployment/review/proof id. |
| `template_ref` | Template such as DAG execution, reviewer evaluation, deploy, code-editor mutation, deletion cascade. |
| `initiator` | Node/process that started it. |
| `authority_path` | Capability chain that allows it to run. |
| `participants` | Nodes/ports involved. |
| `state` | `queued`, `running`, `blocked`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`, `rolled_back`. |
| `budget` | Calls/runtime/tokens/spend/depth/child process limits. |
| `ttl` | Maximum process lifetime. |
| `kill_switch_refs` | Ports/policies able to stop it. |
| `emitted_signals` | Signal refs. |
| `proposed_rewrites` | Rewrite refs. |
| `ledger_refs` | Evidence rows. |

Rule: a process is not a privilege. Process-local capabilities expire when the
process ends unless policy explicitly promotes a result.

### Rewrite

A staged graph change. Nodes and protocol packs may propose rewrites; the
kernel applies only policy-approved rewrites.

Conceptual fields:

| Field | Meaning |
|---|---|
| `rewrite_id` | Stable proposal id. |
| `type` | `add_edge`, `remove_edge`, `strengthen_edge`, `weaken_edge`, `create_node`, `fork_node`, `merge_node`, `freeze_node`, `retire_node`, `delete_node`, `mutate_source`, `mutate_prompt`, `mutate_manifest`, `mutate_memory`, `reroute`, `allocate_budget`, `revoke_credential`, `tombstone_identity`, `sever_lineage`. |
| `target_refs` | Nodes, edges, artifacts, versions, or policies affected. |
| `requested_capability` | Capability needed to apply it. |
| `authority_path` | Why proposer may request it. |
| `evidence_refs` | Signals/reviews/receipts/proofs justifying it. |
| `expected_effect` | What should improve or change. |
| `risk_class` | Risk level. |
| `rollback_plan` | How to revert or compensate. |
| `state` | `proposed`, `policy_blocked`, `awaiting_approval`, `awaiting_review`, `canarying`, `applied`, `rolled_back`, `rejected`. |

Rule: source edits, manifest edits, memory mutation, credential revocation,
deletion, and routing changes are all rewrites. Self-modification is not a
special exception.

### Policy

Constraints over edge activation, capabilities, processes, rewrites, deletion,
retention, routing, and approval gates.

Conceptual fields:

| Field | Meaning |
|---|---|
| `policy_id` | Stable id and version. |
| `scope` | Platform, org, owner, marketplace, process template, node, or resource. |
| `rule_type` | `allow`, `deny`, `require_approval`, `require_review`, `require_canary`, `require_narrower_scope`, `freeze`, `revoke`. |
| `condition` | Predicate over actor, action, resource, capability, signal, process state, risk, budget, ttl. |
| `effect` | Decision or additional constraint. |
| `precedence` | Composition order. |
| `evidence_required` | Ledger evidence required to pass. |

Composition rule: policies compose by restriction. Lower policies may narrow,
but cannot grant above the parent ceiling.

### Ledger

Immutable causal history tying authority, reason, version, approval, outcome,
and follow-up signals together.

Conceptual fields:

| Field | Meaning |
|---|---|
| `ledger_ref` | Table/row/object reference or signed token id. |
| `event_type` | Grant minted, edge activated, process started, signal emitted, rewrite proposed, review passed, deploy completed, proof failed, capability revoked. |
| `actor_ref` | Who/what caused it. |
| `subject_ref` | What changed or was observed. |
| `authority_ref` | Grant, token, approval, or policy decision. |
| `version_refs` | Source SHA, image, manifest, prompt, card hash. |
| `payload_ref` | Redacted payload or object-store pointer. |
| `timestamp` | Event time. |

Rule: ledger rows are evidence, not arbitrary memory. They are append-only and
redacted according to audience.

## Example Graph Snippets

### A2A Agent Call

```text
node:user:42 port:request
  -> edge:call(capability: invoke chart-agent.render_chart, ttl=300s)
  -> node:agent:chart-agent port:invoke:render_chart

edge provenance:
  GrantAudit(grant_id=...)
  SubagentRun(grant_id=...)
  AgentReceipt(receipt_id=...)
  AgentSession(session_id=...)
```

The grant is the edge capability. The receipt/session are ledger evidence that
the edge was activated. Any child call must mint a narrower child capability.

### Reviewer Evaluation

```text
node:version:sha:<head_sha>
  -> edge:evaluate(scope=source tree, readonly)
  -> node:agent:agent-reviewer port:invoke:review
  -> signal:review_finding(severity=warning, subject=version)
```

The reviewer emits signals, not authority. A critical signal may block a later
promotion rewrite through policy.

### Source Mutation

```text
signal:review_finding
  -> rewrite:mutate_source(target=repo + source version)
  -> policy:require_owner_grant + require_review
  -> process:code_editor_turn
  -> node:version:sha:<new_head_sha>
  -> process:source_push_deploy
```

The code editor receives a temporary repo-write capability. The resulting
commit SHA becomes a version node. Deployment, review, and proof processes
attach evidence to that version.

### Deletion And Revocation

```text
rewrite:retire_node(target=agent)
  -> policy:dependency_check + retention_check
  -> process:revocation_cascade
  -> edge:revoke(active grants)
  -> node:tombstone:agent
```

Deletion is a family of rewrites. Revocation edges stop active capabilities;
tombstones preserve enough identity and ledger evidence to explain the action.

## Design Checks

Any future implementation should be able to answer these questions:

- Which edge authorizes this action?
- Which parent edge or grant allowed that edge to exist?
- Which policy allowed or denied it?
- What ledger row proves it happened?
- What process-local capabilities were minted, and when do they expire?
- What signals changed routing or trust, and why are they not permissions?
- Which rewrite changed this source/manifest/prompt/memory?
- What rollback or tombstone exists if the rewrite fails or is revoked?
