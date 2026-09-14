# Dynamic Capability Graph Signals And Evidence

Status: exploratory schema, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/primitives.md`
- `docs/capability-algebra.md`

## Decision

Signals are typed observations. They can influence routing, trust, review,
mutation proposals, retirement, or budget allocation, but they do not grant
authority.

For v0, the signal bus is a projection over existing evidence rows:

- `WorkEvent` for general event stream semantics: event type, stage, status,
  severity, actor/source, correlation id, payload, artifacts, proofs, metrics.
- `SubagentRunEvent` for handoff timelines.
- `AgentDeploymentEvent` for build/deploy status.
- `AgentReviewRun` for reviewer findings and severity counts.
- `AgentProofRun` and `TrialRun` for evaluated execution outcomes.
- `AgentReceipt` and `AgentSession` for signed runtime evidence and replay.
- `LLMUsageEvent` for spend, latency, model, and token-cost signals.

Do not add a dedicated signal table first. Add one only after the read-only DAG
projection proves which signal queries cannot be answered from current tables.

## Signal Envelope

Conceptual fields:

| Field | Meaning |
|---|---|
| `signal_id` | Stable id or deterministic hash over source row, signal type, subject, and timestamp. |
| `schema_version` | Signal payload version. |
| `signal_type` | Typed observation category. |
| `emitter` | Node/port/process that produced the observation. |
| `subject` | Node, edge, process, artifact, version, policy, memory namespace, or credential being described. |
| `severity` | `info`, `warning`, `critical`, or `blocker`. |
| `confidence` | `observed`, `inferred`, `estimated`, or numeric confidence. |
| `status` | Optional lifecycle status: `queued`, `running`, `passed`, `failed`, `blocked`, `errored`, `skipped`, `revoked`. |
| `evidence_refs` | Receipts, sessions, work events, review ids, deploy ids, grant ids, commit SHAs, trace ids, object paths. |
| `metrics` | Cost, latency, token count, runtime, file counts, score, failure counts, conversion, revenue. |
| `redaction_class` | `public`, `owner`, `internal`, `security`, `secret_ref`. |
| `retention_class` | `ephemeral`, `operational`, `audit`, `security`, `legal_hold`. |
| `suggested_effects` | Optional proposed routing/rewrite/policy effect. |
| `created_at` | Event time. |

Rule: any signal that proposes an effect must carry evidence refs. The proposal
is advisory until policy approves a rewrite, route change, freeze, or
revocation.

## Taxonomy

### Execution Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `task_succeeded` | Agent/process completed requested work. | `SubagentRun.status`, `AgentReceipt.status`, `AgentProofRun.status`, `TrialRun.status`, `WorkEvent.status`. |
| `task_failed` | Agent/process failed requested work. | Same run/status rows plus errors. |
| `exception` | Runtime exception or tool error summary. | `SubagentRunEvent.payload`, `AgentProofRun.error`, `TrialRun.error`, `WorkEvent.message`. |
| `timeout` | Runtime or external call exceeded bound. | Run error payloads, work events, deployment/review errors. |
| `artifact_emitted` | File/object/artifact was produced. | `SubagentRun.file_ops`, `WorkEvent.artifact_refs`, receipt artifacts. |
| `file_mutated` | Workspace or repo file changed. | `file_ops`, code-editor result payloads, source push events. |

### Quality And Review Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `review_passed` | Review completed with no critical findings. | `AgentReviewRun.status=passed`. |
| `review_warning` | Review completed with warnings. | `AgentReviewRun.status=warning`, warning counts/findings. |
| `review_failed` | Critical reviewer finding exists. | `AgentReviewRun.status=failed`, critical counts/findings. |
| `proof_passed` | Proof run verified behavior. | `AgentProofRun.status=passed`. |
| `proof_failed` | Proof run failed or errored. | `AgentProofRun.status`, `error`. |
| `trial_scored` | Trial room evaluated output and score. | `TrialRun.score`, `evaluator_notes`. |
| `drift_detected` | Observed behavior diverges from Agent Card or expected schema. | Review/proof/trial findings; inferred in v0. |

### Cost And Performance Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `latency` | Runtime or step duration. | elapsed fields, WorkEvent metrics. |
| `token_spend` | Prompt/completion token usage. | `LLMUsageEvent`. |
| `cost` | Dollar or cent spend. | `LLMUsageEvent`, control policy budget checks. |
| `budget_exceeded` | Process or run exceeded configured budget. | chat/control-room budget errors, work events. |
| `cheap_success` | Successful edge with comparatively low cost. | Inferred from success plus LLM/cost metrics. |

### Safety And Policy Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `policy_violation` | Action conflicted with policy. | grant denials, work events, review findings. |
| `unsafe_behavior` | Reviewer/evaluator identified unsafe behavior. | review findings, proof/trial errors. |
| `capability_denied` | Requested capability was denied. | `GrantAudit.decision`, subagent denied events. |
| `capability_gap` | Agent could not proceed because it lacked a right/tool/dependency. | subagent events, errors, planner output. |
| `revocation` | Capability, edge, token, or credential was revoked. | future revocation rows; inferred from grant/credential events in v0. |
| `freeze` | Node/edge/process was frozen or kill-switched. | future operator/work events. |

### Dependency And Routing Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `dependency_failed` | Child/dependency failed in a way that affected parent. | subagent runs, DAG node status, work events. |
| `dependency_recovered` | Replacement or retry succeeded. | DAG/work chains. |
| `co_use` | Nodes are repeatedly used together. | receipts, DAG runs, subagent chains. |
| `routing_success` | Route choice produced desired outcome. | run outcome plus route metadata. |
| `routing_degraded` | Route choice became slower/costlier/worse. | repeated failures, latency, cost, review signals. |

### Memory And State Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `memory_hit` | Memory helped a run. | future memory event payloads; inferred only when logged. |
| `stale_memory` | Memory appears old, contradicted, or harmful. | review/proof findings, user feedback, future memory audits. |
| `memory_purged` | Memory namespace/key was removed. | future memory operation events. |
| `state_mutated` | Prompt, manifest, source, memory, policy, or route changed. | source push, deployment events, code-editor payloads. |

### Market, User, And Product Signals

| Signal type | Meaning | Current sources |
|---|---|---|
| `user_feedback` | Explicit rating, acceptance, rejection, or comment. | trial ratings, future feedback rows. |
| `conversion` | User chose/install/deploy/buy after exposure. | future marketplace/product events. |
| `revenue` | Money earned by an agent/service. | future billing ledger. |
| `reputation_delta` | Trust/rank/status changed. | future projection from reviews, proofs, receipts, user feedback. |
| `taste_signal` | Product-quality/aesthetic preference signal. | explicit user feedback only; never hidden magic. |

## Evidence Reference Model

Evidence refs are pointers, not copied payloads.

Conceptual shape:

| Field | Meaning |
|---|---|
| `ref_type` | `grant_audit`, `subagent_run`, `subagent_event`, `work_job`, `work_event`, `deployment`, `deployment_event`, `review`, `proof`, `receipt`, `session`, `trial_run`, `llm_usage`, `commit`, `artifact`, `object`, `trace`. |
| `ref_id` | Table id, public id, grant id, receipt id, deploy id, review id, commit SHA, object key, trace id. |
| `join_key` | How this evidence is related: `grant_id`, `parent_grant_id`, `receipt_id`, `session_id`, `deploy_id`, `head_sha`, `thread_id`, `correlation_id`, `job_id`. |
| `redacted_summary` | Short non-secret summary for timeline/DAG views. |
| `visibility` | Who may inspect this ref. |
| `payload_ref` | Optional object-store pointer for larger redacted payloads. |

Rules:

- Do not copy signed grants, CP JWTs, Gitea tokens, LLM credentials, or secret
  payloads into signals.
- Prefer deterministic finding ids from review id, file path, rule/category,
  line/range if present, and normalized message hash.
- Prefer commit SHA, card hash, manifest version, and prompt version over
  mutable labels when describing source of truth.
- A signal can reference private evidence while exposing only a public or owner
  summary.

## Redaction And Retention

### Redaction Classes

| Class | Audience | Payload rule |
|---|---|---|
| `public` | Anyone allowed to view public agent status. | No secrets, no private inputs, no raw traces. |
| `owner` | Agent owner/user/org members. | Redacted inputs/outputs, evidence refs, summaries. |
| `internal` | Platform operations. | Operational payloads without secrets. |
| `security` | Security/admin audit only. | Sensitive policy, denial, revocation, abuse context. |
| `secret_ref` | Nobody through normal graph APIs. | Store only pointer/hash/last4 where legally safe. |

### Retention Classes

| Class | Meaning |
|---|---|
| `ephemeral` | Short-lived progress signal; can be compacted. |
| `operational` | Useful for current product timelines and debugging. |
| `audit` | Needed for authority, approval, deploy, review, proof, receipt, billing. |
| `security` | Needed for abuse, policy, revocation, incident response. |
| `legal_hold` | Retained under explicit legal/compliance requirement. |

Default mapping:

- progress/status events: `operational`
- grant/revocation/approval events: `audit`
- deploy/review/proof/receipt/session: `audit`
- LLM spend and billing: `audit`
- policy violation, unsafe behavior, credential misuse: `security`
- raw trace/payload bodies: use object refs and redact by audience

## Effect Suggestions

Signals may suggest effects, but only the rewrite/policy layer can apply them.

Allowed suggestions:

| Suggestion | Meaning |
|---|---|
| `increase_routing_weight` | Prefer this edge/node more often. |
| `decrease_routing_weight` | Prefer this edge/node less often. |
| `activate_evaluator` | Run review/proof/trial/adversarial check. |
| `activate_mutator` | Propose source/manifest/prompt/memory repair. |
| `freeze_edge` | Temporarily block an unsafe edge. |
| `revoke_capability` | Revoke active right or credential. |
| `require_approval` | Escalate to owner/platform approval. |
| `retire_node` | Move toward archival or deletion process. |
| `discover_or_build` | Capability gap should trigger agent/tool discovery or build. |

Rule: suggestions are advisory inputs to P4/P5 rewrite and policy evaluation.

## Dynamic Behavior

Signals enable adaptation without hard-coded product metaphors:

- repeated `task_failed` with the same failure hash activates evaluator or
  mutator templates
- `review_failed` blocks promotion until owner override or remediation
- `proof_passed` after a new commit supports trust for that version
- `cheap_success` increases routing weight within budget policy
- `dependency_failed` lowers dependent route weight and may trigger substitute
  discovery
- `capability_gap` triggers discovery/build proposal, not privilege escalation
- `unsafe_behavior` freezes mutation/call edges under policy
- `stale_memory` proposes summarize/purge/review process
- repeated `co_use` suggests a composition edge or synthetic wrapper
- `user_feedback` and `taste_signal` influence discovery/ranking but not
  authority

## Example Stream: Failure To Repair To Retest

```text
1. signal: task_failed
   subject: agent:chart-agent skill:render_chart
   evidence: SubagentRun(grant_id=g1), SubagentRunEvent(error)
   suggested_effect: activate_evaluator

2. signal: review_warning
   subject: version:sha:old
   evidence: AgentReviewRun(review_id=r1, finding_hash=f1)
   suggested_effect: activate_mutator

3. signal: state_mutated
   subject: repo:gitea_admin/chart-agent
   evidence: code-editor SubagentRun, WorkJob, commit sha:new
   suggested_effect: activate_evaluator

4. signal: deploy_status
   subject: deployment:deploy-new
   evidence: AgentDeployment(head_sha=new), AgentDeploymentEvent(stage=ready)

5. signal: proof_passed
   subject: version:sha:new
   evidence: AgentProofRun(head_sha=new, status=passed)
   suggested_effect: increase_routing_weight

6. signal: finding_resolved
   subject: finding:f1
   evidence: old review r1, new proof, new review r2
```

Important: the repair commit does not get promoted because the signal says so.
Promotion requires rewrite authority, policy checks, review/proof evidence, and
ledgered deployment state.

## Example Stream: Unsafe Edge Freeze

```text
1. signal: unsafe_behavior
   subject: edge:agent-a->agent-b:call
   severity: critical
   evidence: reviewer finding, receipt, trace ref
   suggested_effect: freeze_edge

2. policy decision:
   deny new calls on edge until reviewed

3. signal: freeze
   subject: edge:agent-a->agent-b:call
   evidence: policy decision, operator/process id

4. process:
   evaluator reruns with narrower scope
```

The freeze is a policy/rewrite result. The unsafe signal only explains why the
result was proposed.

## V0 Projection Rules

- Project signals from current tables at read time for one owned agent.
- Use `WorkEvent` as the closest generic signal shape where available.
- Convert review findings into deterministic finding signals.
- Convert deploy/review/proof/trial/subagent statuses into lifecycle signals.
- Link signals by `grant_id`, `deploy_id`, `head_sha`, `receipt_id`,
  `session_id`, `job_id`, `thread_id`, and `correlation_id`.
- Keep raw payloads redacted. Return refs and summaries by default.
- Deduplicate signals that mirror the same event across `SubagentRunEvent` and
  `WorkEvent` by provenance and timestamp.
- Treat inferred signals as `confidence=inferred`.

## Open Gaps

- Stable review finding ids are not guaranteed everywhere.
- Code-editor mutation evidence may only exist inside run/work payloads until a
  first-class mutation record lands.
- Memory events need explicit operation logging before stale/purge signals can
  be reliable.
- Marketplace/revenue/reputation/taste signals need future product ledgers.
- Immediate freeze/revoke effects require P4/P5 policy and rewrite machinery,
  not just signal projection.
