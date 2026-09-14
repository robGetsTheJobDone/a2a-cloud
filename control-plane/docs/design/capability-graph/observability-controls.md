# Dynamic Capability Graph Observability And Controls

Status: control-surface design, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/kernel.md`
- `docs/policy-kernel.md`
- `docs/process-scheduler.md`
- `docs/deletion-revocation.md`
- `docs/threat-model.md`
- `docs/storage-query.md`

## Decision

Operator control is part of the graph kernel.

Humans and platform operators must be able to inspect, explain, interrupt, and
audit dynamic graph behavior. A kill-switch is not an out-of-band shortcut. It
is a restrictive graph action with authority, reason, target, provenance,
policy decision, before/after state, and follow-up verification.

P17 should build on existing rails:

- `/v1/me/control-room` for user policy, summary, timeline, and receipts
- `/v1/me/activity` and `/v1/me/jobs/{job_id}/events` for generic work ledger
  activity and event streams
- `UserControlPolicy` for user-level budgets and runtime constraints
- `PlatformSetting` and `/v1/admin/settings` for platform toggles
- `SubagentRun` and `SubagentRunEvent` for handoff timelines
- `WorkJob` and `WorkEvent` for generic process timelines
- `AgentDeployment` and `AgentDeploymentEvent` for deploy state
- `AgentReviewRun` and `AgentProofRun` for safety and quality evidence
- `AgentAuthConnection`, `AgentSecret`, and `GiteaTokenAudit` for credential
  metadata and revocation hooks
- Argo helpers for application refresh/delete and repo-secret cleanup

The first implementation should extend these surfaces. Do not create an
unbounded operator console that can mutate graph state without ledgered,
redacted, policy-checked events.

## Existing Control Substrate

| Need | Current substrate | Gap |
|---|---|---|
| user timeline | `GET /v1/me/control-room` | not graph-shaped yet |
| user receipt details | `GET /v1/me/control-room/receipts/{source}/{id}` | payload redaction needs graph policy |
| generic activity list | `GET /v1/me/activity` | not tied to graph node ids |
| job event stream | `GET /v1/me/jobs/{job_id}/events/stream` | no kill/control actions yet |
| user budget policy | `UserControlPolicy` and `/policy` | no graph-level policy explanation |
| platform toggle | `PlatformSetting.reviewer_enabled` | only reviewer toggle exists |
| handoff run state | `SubagentRun` and `SubagentRunEvent` | no stop/freeze endpoint |
| deployment state | `AgentDeployment/Event` | no graph revocation preview |
| review/proof evidence | `AgentReviewRun`, `AgentProofRun` | no alert policy |
| auth connection revoke | `DELETE /v1/agents/{name}/auth/{connection_id}` | no dependency preview |
| secret revoke | `DELETE /v1/agents/{name}/secrets/{key}` | no graph tombstone/evidence event |
| Gitea token revoke | `gitea_meta.revoke_token` | not surfaced as graph control action |
| Argo refresh/delete | `request_application_refresh`, `delete_application` | needs user/operator action envelope |

## Control Principles

### Restriction Is Safer Than Expansion

Control actions may reduce authority, stop work, freeze edges, disable routing,
or require review. Expanding authority requires the normal grant/policy path and
should never be hidden inside an operator action.

### Every Action Has A Ledger Trail

Required control record fields:

- `control_action_id`
- `actor_type`
- `actor_id`
- `actor_role`
- `target_type`
- `target_id`
- `target_owner_user_id` or `organization_id`
- `action_type`
- `reason`
- `risk_class`
- `requested_at`
- `policy_decision`
- `before_refs`
- `after_refs`
- `evidence_refs`
- `rollback_plan`
- `verification_status`
- `redaction_class`

Until first-class graph control tables exist, these records can be mirrored into
`WorkJob` and `WorkEvent` using `kind=graph_control` and target refs.

### Explain Before Mutating

Dangerous controls should have a preview step:

- affected nodes
- affected grants and credentials
- active processes
- dependent deployments
- memory/data retention impact
- user-visible effect
- rollback or irreversibility

### Kill-Switches Are Narrow

Each switch targets one scope:

- node freeze
- process stop
- edge revoke
- grant revoke
- credential revoke
- routing disable
- mutation disable
- deletion hold
- memory lock or purge
- platform-wide feature toggle

Avoid vague global switches where a narrow target is available.

### Redaction Applies To Operators Too

Operators need explanations and refs, not secrets. No control surface should
display signed grant tokens, CP JWTs, Gitea tokens, LLM credentials,
`secret_ciphertext`, raw private file contents, raw session logs, or raw
tool-call payloads by default.

## Control-Room UX Requirements

### Graph Explorer

Purpose:

- inspect nodes, ports, edges, capabilities, active processes, policies, and
  signals

Required views:

- current agent node and version nodes
- incoming and outgoing call/evaluate/mutate/store/route/fund/revoke/delete
  edges
- capability edges with TTL, budget, scope, parent grant, and decision reason
- active process nodes and child process edges
- deployment, review, proof, receipt, and work-ledger evidence refs
- redaction labels on every expandable payload

Required actions:

- open node dossier
- open capability inspector
- open process timeline
- open revocation preview
- freeze node
- disable routing to node
- disable mutation for node
- export redacted audit bundle

### Process Timeline

Purpose:

- explain what a dynamic graph process did and whether it is still safe to run

Sources:

- `WorkJob`
- `WorkEvent`
- `SubagentRun`
- `SubagentRunEvent`
- `DagRun`
- `DagRunNode`
- `MetaAgentRun`
- deployment, review, proof, and trial rows

Required lanes:

- state transitions
- child processes
- grants and authority changes
- file/resource effects
- cost and budget
- approvals and denials
- emitted signals
- proposed rewrites
- kill-switch actions

Required actions:

- stop process
- pause on approval
- revoke process-local grants
- rerun from safe checkpoint where supported
- open audit export

### Rewrite Review Screen

Purpose:

- evaluate proposed graph/source/manifest/prompt/memory/edge/policy mutation
  before it applies

Required fields:

- proposed change
- target nodes and artifacts
- authority path
- policy decisions
- risk class
- reviewer/proof evidence
- expected effect
- budget impact
- rollback plan
- canary requirement
- owner approval requirement
- affected dependents

Required actions:

- approve
- deny
- require narrower scope
- require review
- require proof
- require canary
- freeze target

### Capability Inspector

Purpose:

- answer why a node has access to a resource

Required fields:

- grant id
- parent grant id
- issuer
- audience
- bucket/resource
- mode/actions
- allow and deny summaries
- TTL and expiry
- budget constraints
- policy source
- decision and reason
- related run/proof/receipt/session refs

Required actions:

- revoke grant
- revoke child grants
- freeze edge
- require owner approval for renewal
- export authority chain

### Signal Viewer

Purpose:

- show the evidence that affects routing, trust, mutation, and retirement

Signal groups:

- reviewer findings
- proof results
- receipt success/failure
- handoff failures
- repeated failure shapes
- LLM cost and latency
- user feedback
- budget pressure
- policy denials
- credential expiry
- stale process heartbeat
- deletion/revocation events

Required actions:

- open evidence row
- mark false positive where policy allows
- create rewrite review item
- quarantine routing
- freeze mutation
- export redacted signal set

### Deletion And Revocation Preview

Purpose:

- show blast radius before deleting, retiring, freezing, revoking, purging, or
  tombstoning graph state

Required fields:

- target node/edge/credential/grant/memory namespace
- active processes
- dependent nodes and edges
- grant parent/child chains
- credentials and secrets
- deployments and running pods
- sessions and receipts
- retained audit rows
- purgeable payloads
- irreversible effects

Required actions:

- soft delete
- freeze
- retire
- revoke
- purge eligible data
- tombstone
- hard delete where retention policy permits

### Kill-Switch Panel

Purpose:

- give users and operators a concise emergency surface without hiding details

Required switches:

- stop active process
- freeze agent node
- revoke active grants
- disconnect credential
- delete runtime secret
- disable routing to agent
- disable mutation for agent
- disable reviewer bypass by forcing review-required mode
- disable self-modification platform-wide
- disable source-push auto-deploy platform-wide

Each switch must show:

- target
- reason input
- authority level
- expected effect
- rollback option
- evidence refs
- policy result

## Operator Action Catalog

### Inspect

Effect:

- read graph evidence, timelines, policy decisions, and redacted payload refs

Authority:

- owner for own graph
- org admin for org graph
- platform operator for redacted cross-tenant diagnostics

Ledger:

- optional for normal owner views
- required for operator/admin views of private graph evidence

### Update User Runtime Policy

Existing fields:

- monthly budget
- per-run budget
- max agent calls per run
- require approval for file writes
- deny external network
- only approved agents
- PII safe mode
- approved agents list

Graph meaning:

- restricts future process activation, handoff, file write, network, and routing
  decisions

Ledger:

- emit `control.policy_updated`

### Update Platform Toggle

Current field:

- `reviewer_enabled`

Future fields:

- `source_push_deploy_enabled`
- `agent_self_mutation_enabled`
- `adaptive_routing_enabled`
- `protocol_pack_activation_enabled`
- `graph_rewrite_enabled`
- `public_agent_invocation_enabled`

Graph meaning:

- platform policy source that can restrict entire classes of processes or
  rewrites

Ledger:

- emit `control.platform_setting_updated`

### Freeze Node

Effect:

- block new calls, routing, mutation, and delegation for a node while retaining
  evidence

Targets:

- agent
- synthetic agent
- memory namespace
- credential
- repo/source version
- policy

Ledger:

- emit `control.node_freeze_requested`
- emit `control.node_freeze_applied`
- emit verification event

Rollback:

- unfreeze with reason and policy approval

### Stop Process

Effect:

- stop or mark failed an active process and revoke process-local grants

Targets:

- `WorkJob`
- `SubagentRun`
- `DagRun`
- `MetaAgentRun`
- deployment/review/proof/trial run

Current gap:

- many rows have status, but no shared stop endpoint

Ledger:

- emit `control.process_stop_requested`
- emit `control.process_stop_applied`
- emit child grant revocation refs

### Revoke Edge Or Grant

Effect:

- revoke capability for a graph edge or signed grant chain

Targets:

- grant id
- parent grant chain
- child grants
- edge projection id

Current substrate:

- `GrantAudit` has grant and parent grant ids, but no revocation row yet

Future need:

- revocation record with target grant, actor, reason, timestamp, and affected
  runs

### Revoke Credential

Effect:

- remove auth connection, runtime secret, Gitea token, or Argo repo secret

Current hooks:

- delete agent auth connection
- delete agent secret
- revoke Gitea token
- delete Argo repo secret

Graph requirements:

- preview dependent agents/processes first
- verify runtime no longer has credential
- emit tombstone or retained audit ref

### Disable Routing

Effect:

- keep node alive but prevent discovery/routing/new calls

Targets:

- agent node
- edge
- route policy
- protocol-pack instance

Graph requirements:

- routing weight must be separate from permission
- routing disable must not delete evidence
- explain which route decision changed

### Disable Mutation

Effect:

- prevent source, manifest, prompt, memory, edge, and policy mutation for a
  target node or platform-wide class

Targets:

- node
- owner/org
- protocol-pack instance
- platform toggle

Graph requirements:

- running mutation processes must be paused or stopped
- future rewrite proposals should be denied with explanation

### Refresh Or Reconcile Deployment

Effect:

- ask Argo/deployment verification to re-read state and update evidence

Current hook:

- `request_application_refresh`
- deployment verification events

Graph meaning:

- non-destructive observation/reconciliation action

Ledger:

- emit `control.deployment_refresh_requested`
- attach resulting deployment events

### Delete Or Retire Runtime

Effect:

- remove Argo application/runtime resources or retire an agent from service

Current hook:

- `delete_application`

Graph requirements:

- must use deletion/revocation preview
- must preserve audit and tombstone where policy requires
- must not silently delete source, receipts, reviews, proofs, or grant audits

## Required Observability Events

Control events:

- `control.policy_updated`
- `control.platform_setting_updated`
- `control.node_freeze_requested`
- `control.node_freeze_applied`
- `control.node_unfreeze_requested`
- `control.process_stop_requested`
- `control.process_stop_applied`
- `control.edge_revoke_requested`
- `control.edge_revoke_applied`
- `control.credential_revoke_requested`
- `control.credential_revoke_applied`
- `control.routing_disabled`
- `control.mutation_disabled`
- `control.deletion_preview_created`
- `control.deletion_applied`
- `control.audit_export_created`

Process events:

- `process.started`
- `process.heartbeat`
- `process.paused_for_approval`
- `process.approval_denied`
- `process.policy_denied`
- `process.budget_exceeded`
- `process.child_started`
- `process.child_completed`
- `process.child_failed`
- `process.stopped_by_control`
- `process.completed`
- `process.failed`

Rewrite events:

- `rewrite.proposed`
- `rewrite.policy_checked`
- `rewrite.review_required`
- `rewrite.approval_required`
- `rewrite.canary_started`
- `rewrite.applied`
- `rewrite.reverted`
- `rewrite.blocked`

Security events:

- `security.secret_created`
- `security.secret_deleted`
- `security.auth_connected`
- `security.auth_revoked`
- `security.gitea_token_minted`
- `security.gitea_token_revoked`
- `security.grant_revoked`
- `security.scope_escalation_denied`
- `security.policy_violation_detected`

Routing events:

- `routing.candidates_ranked`
- `routing.policy_filtered`
- `routing.decision_made`
- `routing.disabled_by_control`
- `routing.quarantined`

## Alert Catalog

### Critical Alerts

- runaway recursive activation exceeds max depth
- active process continues after grant revocation
- credential revocation fails
- source mutation applies without review when review is required
- critical reviewer finding followed by promotion
- signed receipt verification fails for accepted result
- cross-user graph evidence is exposed
- deletion/purge violates retention policy

### Warning Alerts

- repeated handoff failure shape
- repeated proof failure
- deployment stuck in active status beyond timeout
- stale work heartbeat
- budget burn rate exceeds threshold
- LLM cost spike
- repeated policy denials
- reviewer disabled while source-push deploys continue
- Gitea token sweeper failure
- agent auth credential near expiry
- routing repeatedly selects a failing agent

### Informational Alerts

- node frozen
- node unfrozen
- process stopped
- route disabled
- mutation disabled
- review skipped by platform setting
- revocation preview generated
- audit export generated

## Alert Routing

Owner-visible:

- budget burn
- process stuck
- repeated failure
- proof/review regression
- credential expiry
- routing quarantine
- node freeze/unfreeze

Operator-visible:

- cross-tenant safety issues
- platform setting changes
- sweeper failures
- revocation failures
- secret projection failures
- Argo reconciliation failures
- audit export events

Security-visible:

- credential leakage suspicion
- grant revocation failure
- scope escalation denied
- private payload redaction failure
- unauthorized graph read attempt

## Audit Export Requirements

An audit export should be a redacted evidence bundle, not a database dump.

Required sections:

- target summary
- actor and requester summary
- policy decisions
- grant/capability chain
- process timeline
- deployment/review/proof refs
- control actions
- alerts
- redaction manifest
- source table refs
- object-store refs only when caller is authorized

Never include:

- signed grant tokens
- CP JWTs
- Gitea tokens
- LLM credentials
- secret ciphertext
- raw private files
- raw replay logs unless explicitly requested through a forensic endpoint with
  owner/operator authority

## Implementation Sequence

1. Add redacted graph/evidence timeline and dossier from P20 through P23.
2. Add graph-control event helpers that mirror into `WorkJob` and `WorkEvent`.
3. Add read-only graph explorer and process timeline using existing events.
4. Add kill-switch previews before any destructive action.
5. Add user-level restrictive controls: freeze node, disable routing, disable
   mutation, stop process where safe.
6. Add credential revocation actions with preview and verification.
7. Add platform toggles for mutation, routing, source-push deploy, and protocol
   pack activation.
8. Add alert detection from existing rows before adding new graph alert tables.
9. Add first-class graph control/action tables only after the read-only
   projection and controls need stable ids beyond `WorkEvent`.

## Non-Goals

- no hidden operator mutation without audit
- no graph explorer that reveals secrets
- no global kill switch when a narrow target is available
- no active autonomous rewrite controls before policy/review/canary gates exist
- no destructive deletion without revocation preview and retention checks
- no graph database dependency for observability v0

## Recommendation

P17 should treat control surfaces as productized safety rails:

- users get clear ownership controls for budget, approvals, credentials,
  routing, mutation, and active process interruption
- operators get redacted diagnostics, platform toggles, and audited emergency
  controls
- every control action emits evidence
- every dangerous action has a preview
- every kill-switch reduces authority and records why

This is the operator layer required before active graph rewrites, adaptive
routing, protocol packs, or self-modifying agents can safely run in production.
