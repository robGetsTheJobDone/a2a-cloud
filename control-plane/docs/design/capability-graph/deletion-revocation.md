# Dynamic Capability Graph Deletion And Revocation

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

## Decision

Deletion is not a boolean. It is a family of graph rewrites over nodes, edges,
capabilities, credentials, memory namespaces, source artifacts, process state,
routing state, marketplace visibility, lineage, and evidence.

The graph kernel must distinguish "stop using this" from "remove data", "kill
active authority", "hide from discovery", "preserve an audit marker", and
"prevent descendants from inheriting trust". A product layer may call these
death, exile, archival, retirement, legal delete, marketplace removal, or
credential rotation, but the kernel should use precise rewrite types.

For v0, do not add a deletion graph store first. Project deletion and
revocation evidence from existing rails:

- agent delete flow in `routes/agents.py`
- runtime and source cleanup through Argo, Kubernetes, Gitea, and search index
- secret deletion through `routes/agent_secrets.py` and Kubernetes secrets
- memory namespace/key deletion through `routes/memory.py`
- imported auth connection deletion through `routes/agent_auth.py`
- TTL-bounded grant audit and grant parent chains through `GrantAudit`
- process/run evidence through `WorkJob`, `WorkEvent`, `SubagentRun`,
  `AgentDeployment`, `AgentReviewRun`, `AgentProofRun`, `AgentReceipt`, and
  `AgentSession`

Add first-class `GraphTombstone`, `CapabilityRevocation`, or
`DeletionProcess` state only when the platform needs durable global revocation
lookups, dependency previews, retention holds, operator kill-switches, or
rebuildable deletion projections across multiple process families.

## Deletion Family

| Rewrite | Meaning | Preserves evidence? | Blocks future activation? | Removes data? |
|---|---|---:|---:|---:|
| `soft_delete` | Hide from discovery, marketplace, and default routes. | yes | yes | no |
| `freeze_node` | Stop calls, mutation, delegation, and routing while investigating. | yes | yes | no |
| `freeze_edge` | Stop one relationship or capability path. | yes | yes | no |
| `retire_node` | Stop new work while preserving identity, lineage, and history. | yes | yes | no |
| `revoke_edge` | Invalidate capability edge and descendants under policy. | yes | yes | no |
| `revoke_credential` | Remove runtime material such as secret, token, or auth connection. | yes | yes | selected |
| `purge_namespace` | Delete allowed memory/data namespace content. | summary only | usually | yes |
| `hard_delete_node` | Remove source/runtime/control-plane rows where policy permits. | tombstone/audit only | yes | yes |
| `tombstone_identity` | Preserve minimal identity marker, reason, and audit refs. | yes | yes | no |
| `sever_lineage` | Stop trust, certification, reputation, or status inheritance. | yes | selected | no |

Rule: deletion rewrites can compose. A high-risk agent retirement may be:

```text
freeze_node
  -> revoke_edge
  -> revoke_credential
  -> retire_node
  -> tombstone_identity
  -> sever_lineage
```

Hard deletion should usually be the final step, not the first step, because the
system needs enough evidence to explain why authority and data disappeared.

## Rewrite Envelope

Deletion rewrite fields:

| Field | Meaning |
|---|---|
| `deletion_id` | Stable id or idempotency key for the deletion process. |
| `rewrite_type` | `soft_delete`, `freeze_node`, `revoke_edge`, `purge_namespace`, etc. |
| `target_refs` | Agent, edge, grant, credential, memory namespace, repo, deployment, process, marketplace listing, lineage edge. |
| `initiator` | User, owner, org admin, platform operator, policy, process, or signal. |
| `authority_path` | Owner check, admin policy, parent process, revocation edge, legal request, or platform policy. |
| `reason` | User request, policy violation, owner cleanup, unsafe signal, dependency failure, retention expiry. |
| `evidence_refs` | Reviews, proofs, receipts, run events, deployment events, auth rows, memory rows, grant audits, operator notes. |
| `dependency_refs` | Dependents that call, route to, inherit from, certify, or store data for the target. |
| `active_process_refs` | Running work that may hold process-local authority. |
| `retention_class` | Audit-retained, user-data purgeable, legal-hold, security-retained, ephemeral. |
| `redaction_class` | Public tombstone, owner-visible, internal, security, legal. |
| `rollback_plan` | Unfreeze, restore route, reissue credential, redeploy previous version, or no rollback. |
| `downstream_notifications` | Dependents, owners, marketplace, scheduler, running processes, external connectors. |
| `state` | Deletion process state. |
| `ledger_refs` | Rows proving checks, revocations, tombstone, purge, cleanup, and failures. |

Deletion proposals are not permission. Applying them still requires authority,
policy evaluation, dependency checks, retention checks, and idempotent cleanup.

## Required Checks

Every deletion process should answer these questions before applying effects:

- Ownership: does the requester own the target or hold platform/org authority?
- Authority: can the requester delete, revoke, freeze, purge, or sever this
  target class?
- Dependency graph: who calls, depends on, inherits from, routes to, evaluates,
  stores for, or certifies this target?
- Active process graph: which running jobs, DAGs, grants, receipts, sessions,
  deployments, proofs, trials, and handoffs may still exercise authority?
- Retention: which evidence must stay for audit, legal, billing, security, or
  rollback?
- Data class: which payloads are user data, secrets, public listing data,
  source artifacts, memory, derived summaries, or immutable ledger evidence?
- Credential surface: which Kubernetes secrets, OAuth connections, Gitea tokens,
  LLM credentials, runtime grants, and repo secrets must be invalidated?
- Downstream notification: which owners, dependents, routers, schedulers, or
  marketplace surfaces need a state change?
- Rollback window: can the deletion be undone, and which state would be
  restored?
- Tombstone: what minimal identity and reason must remain so stale references
  and audit queries are explainable?

Policy decides whether the result is allow, deny, require approval, require
narrower scope, require retention hold, freeze first, revoke first, or require
manual operator review.

## Revocation Propagation

Revocation is ordered to avoid races with active work.

```text
1. freeze target routes and new activations
2. mark active edges/capabilities as frozen or revoking
3. stop new child processes and new delegation
4. revoke process-local capabilities
5. revoke child grants and descendant edges under policy
6. kill, pause, or reroute active processes
7. remove runtime credential material
8. remove route/search/marketplace visibility
9. delete or purge allowed data
10. emit tombstone and lineage-severance evidence
11. commit durable state and notify dependents
```

Rules:

- Revoke beats active.
- Freeze beats call, route, mutate, and delegate.
- Deletion cannot rely only on TTL if active processes can still use a token.
- Child capabilities inherit parent revocation path.
- A process that observes revocation must stop minting children immediately.
- Failed cleanup leaves the control-plane record intact for retry.
- Evidence of revocation must not include raw secrets, signed grant tokens, or
  private payloads.

Current gap: signed grants are TTL-bounded, but there is no global active grant
revocation lookup for every runtime token. Until that exists, deletion should
prefer short TTLs, server-side checks for control-plane actions, runtime secret
removal, and explicit process cancellation where possible.

## Current Control-Plane Mapping

### Agent Hard Delete

Current rail: `DELETE /agents/{name}` in `routes/agents.py`.

Behavior today:

- loads the agent by name
- requires `agent.owner_id == current_user.id`
- finds dependent agents by `source_agent_id`
- blocks if a dependent is owned by another user
- cleans owned dependents before the source agent
- for source-backed agents, deletes:
  - Argo CD application
  - Kubernetes runtime
  - runtime secret
  - Argo repo secret
  - Gitea runtime repo
  - Gitea source repo
- for external agents, deletes only the runtime secret
- rolls back the DB transaction if cleanup fails
- deletes control-plane rows only after cleanup succeeds
- deletes search index rows after DB commit
- revalidates public listing pages when needed

Graph projection:

```text
rewrite: hard_delete_node
target: agent:<name>
authority: owner
checks:
  dependents: Agent.source_agent_id
  cleanup: argo, k8s, runtime secret, repo secret, gitea repos
result:
  DB row deleted
  search row deleted
  public listing revalidated
evidence:
  deletion process event and cleanup failures if any
```

This is a useful current hard-delete rail, but it is not enough for the full
graph model because it has no durable tombstone, lineage severance record,
global revocation record, or dependency preview API.

### Secret Revocation

Current rail: `DELETE /agents/{name}/secrets/{key}`.

Behavior today:

- verifies the caller owns the agent
- validates the secret key
- finds the `AgentSecret` row for agent/user/key
- removes the key from the Kubernetes runtime secret
- deletes the whole runtime secret if it becomes empty
- reapplies runtime secret projection and rolls pods
- deletes the `AgentSecret` DB row after runtime cleanup succeeds

Graph projection:

```text
rewrite: revoke_credential
target: credential:agent:<name>:secret:<key>
authority: owner
cleanup_order:
  runtime material first
  projection rollout second
  DB row last
```

This ordering is correct for credentials: remove live material before removing
the record that lets operators retry or diagnose failure.

### Runtime Secret Delete

Current rail: `delete_agent_runtime_secret(agent_name)`.

Projection:

```text
rewrite: revoke_credential
target: kubernetes-secret:<agent>-agent-secrets
effect: delete runtime secret if present
idempotent: 404 and 410 are success
```

Runtime secret deletion is part of both agent hard delete and external-agent
cleanup.

### Memory Purge

Current rail: `DELETE /agents/{name}/memory/{namespace}/{key}`.

Graph projection:

```text
rewrite: purge_namespace or purge_memory_key
target: memory:<agent>:<namespace>:<key>
authority: owner
retention: must be checked before purge
evidence: operation summary, not raw private content
```

Current gap: memory deletion removes the row. A graph-kernel memory purge needs
stronger operation evidence, namespace-level retention policy, and redacted
summaries so deletion can be explained without retaining private memory content.

### Imported Auth Connection Delete

Current rail: `DELETE /agent-auth/connections/{connection_id}`.

Graph projection:

```text
rewrite: revoke_credential
target: auth-connection:<connection_id>
authority: owner or visible connection policy
effect: remove connection row
followup: mark agent needs_auth when owner deletes a required auth connection
```

This is credential revocation over connector authority. The graph model should
also record which agents and skills depended on the connection so active
processes can fail closed or request setup.

### Search And Marketplace Visibility

Current rail: `_delete_agent_from_search` after DB commit.

Graph projection:

```text
rewrite: soft_delete or hard_delete_node
target: discovery/search/listing refs
effect: remove from search and refresh public listing cache
```

Search/listing deletion is visibility state. It should not be confused with
credential revocation, source deletion, or audit purge.

### Evidence And Process Rows

Existing rows that should usually remain as deletion evidence:

- `GrantAudit`
- `WorkJob` and `WorkEvent`
- `SubagentRun` and `SubagentRunEvent`
- `AgentDeployment` and `AgentDeploymentEvent`
- `AgentReviewRun`
- `AgentProofRun`
- `AgentReceipt`
- `AgentSession`
- `LLMUsageEvent`

These rows are the current ledger substrate. Deletion should redact sensitive
payloads and retain enough refs to explain causality, authority, billing, and
security decisions.

## Tombstones

A tombstone is a minimal identity marker that explains why a target no longer
activates or inherits trust.

Conceptual fields:

| Field | Meaning |
|---|---|
| `tombstone_id` | Stable id. |
| `target_ref` | Deleted/retired/frozen identity. |
| `target_kind` | Agent, credential, memory namespace, repo, listing, lineage edge, policy, process. |
| `name_or_slug` | Minimal stale-reference hint. |
| `owner_ref` | Owner/org/platform ref, redacted by audience. |
| `reason_class` | user_requested, unsafe, policy_violation, retention_expired, duplicate, compromised, migrated. |
| `state` | frozen, retired, deleted, purged, revoked, severed. |
| `effective_at` | When activation stopped. |
| `evidence_refs` | Redacted ledger refs. |
| `replacement_ref` | Optional successor or migration target. |
| `lineage_policy` | inherit_none, inherit_limited, successor_only, blocked. |
| `retention_expires_at` | Optional cleanup date for tombstone itself. |

Tombstones must not store raw secrets, signed grants, private prompts, full
memory payloads, or private execution traces. They store refs and redacted
summaries.

## Lineage Severance

Lineage is not authority, but it influences trust, certifications, reputation,
marketplace status, routing preference, and user confidence. Deletion needs a
way to stop inheritance without destroying all history.

Lineage states:

| State | Meaning |
|---|---|
| `inherits` | Descendant may inherit eligible trust/status under policy. |
| `inherits_limited` | Descendant inherits only selected certifications or evidence. |
| `inheritance_frozen` | No new inheritance until review or owner action. |
| `severed` | Descendant/fork/dependent cannot inherit trust, status, certifications, or marketplace rank. |
| `successor_only` | Trust migrates to one replacement identity, not arbitrary descendants. |

Severance triggers:

- unsafe behavior by ancestor
- compromised source or credential
- owner explicitly disowns a fork
- legal/retention policy requires separation
- marketplace listing was tombstoned for deception or abuse
- certification was revoked
- child dependency was removed because it exceeded policy

Rules:

- Lineage severance does not erase historical evidence.
- Severance blocks future trust inheritance and discovery boost.
- A fork cannot use an ancestor tombstone to launder trust.
- A replacement identity needs explicit successor evidence.
- Certification inheritance should be narrower than source lineage inheritance.
- Marketplace status and taste signals can influence discovery only after
  lineage policy allows them.

## Deletion Process State

Conceptual state machine:

```text
proposed
  -> policy_denied
  -> awaiting_approval
  -> dependency_blocked
  -> retention_blocked
  -> freeze_pending
  -> frozen
  -> revoking
  -> cleaning_runtime
  -> purging_data
  -> tombstoning
  -> severing_lineage
  -> completed
  -> failed_retryable
  -> failed_manual
  -> rolled_back
```

State rules:

- Dangerous deletes may not skip dependency and retention checks.
- `freeze_pending` should happen before destructive cleanup for active nodes.
- `failed_retryable` keeps enough state to safely retry idempotent cleanup.
- `failed_manual` requires operator or owner action.
- `rolled_back` means activation or visibility was restored, not that audit
  evidence was erased.

## Examples

### Retire Bad Agent

```text
signal: repeated review critical finding
proposal:
  rewrite_type: retire_node
  target: agent:chart-builder
checks:
  owner authority
  active processes
  dependents
  marketplace visibility
effects:
  freeze calls and routing
  stop new delegation
  keep deployments/reviews/proofs/receipts
  mark listing retired
  optionally route to successor
  tombstone identity with reason unsafe_or_failed_review
```

Retirement preserves evidence and lineage while stopping new work.

### Purge Memory Namespace

```text
signal: owner requested purge or stale_memory
proposal:
  rewrite_type: purge_namespace
  target: memory:research-agent:customer-notes
checks:
  owner authority
  retention and legal hold
  active processes reading namespace
effects:
  freeze memory write/read edge if needed
  delete allowed rows
  retain redacted operation evidence
  emit stale references as tombstoned memory namespace
```

Memory purge is data deletion. It should not remove grant audit, billing, or
security evidence unless retention policy explicitly permits it.

### Delete Credential

```text
proposal:
  rewrite_type: revoke_credential
  target: agent:mailer secret:SENDGRID_API_KEY
checks:
  owner authority
  active processes using mailer edge
effects:
  remove key from Kubernetes secret
  roll projection
  delete AgentSecret row
  mark dependent auth/call edges revoked
  notify processes requiring the credential
```

Credential deletion must remove live material before deleting the database row.

### Tombstone Marketplace Identity

```text
signal: marketplace policy violation
proposal:
  rewrite_type: tombstone_identity
  target: listing:agent:invoice-agent
checks:
  platform/marketplace authority
  owner notification
  retained evidence class
effects:
  remove from discovery
  preserve name marker for stale links
  sever marketplace rank inheritance
  block successor claims unless reviewed
```

Marketplace tombstones are visibility and reputation state, not necessarily
source or memory deletion.

### Hard Delete Source-Backed Agent

```text
proposal:
  rewrite_type: hard_delete_node
  target: agent:old-parser
checks:
  owner authority
  dependent agents
  active deployments and jobs
  retention policy
effects:
  delete Argo app
  delete Kubernetes runtime
  delete runtime secret
  delete Argo repo secret
  delete runtime repo
  delete source repo
  delete control-plane row
  delete search row
  keep redacted audit/evidence refs
```

This maps to the current `remove_agent` path, with future tombstone and
revocation evidence layered around it.

## V0 Build Guidance

Do not start P9 by adding a general graph database.

First practical wedge:

1. Define a deletion projection over existing agent, secret, memory, auth,
   deployment, review, proof, grant, receipt, session, and work rows.
2. Add a read-only preview API that explains what would be affected by deleting
   one owned agent or credential.
3. Add redacted tombstone fields only where current product behavior needs stale
   link or marketplace explanation.
4. Add explicit deletion/revocation events to `WorkEvent` or a narrow audit row
   before introducing broad graph tables.
5. Add active grant revocation lookup only when runtime token revocation cannot
   be solved with short TTLs and server-side checks.
6. Add first-class `DeletionProcess` only when pause/resume/retry/approval is
   shared across agent delete, memory purge, credential delete, and marketplace
   retirement.

Suggested v0 preview response:

```text
target:
  kind: agent
  name: old-parser
allowed: true
required_gates:
  - owner_confirmation
affected:
  dependents: [...]
  active_processes: [...]
  credentials: [...]
  memory_namespaces: [...]
  deployments: [...]
  search_listing: true
retention:
  retained_evidence: [...]
  purgeable_data: [...]
cleanup_plan:
  - freeze routes
  - revoke runtime secrets
  - delete runtime/source resources
  - delete DB row
  - delete search row
  - write tombstone
risks:
  - foreign_owned_dependent_blocks_delete
  - active_process_may_need_kill
```

## Open Gaps

- No durable global revocation table for signed grant tokens.
- No first-class tombstone for agent identity after DB row deletion.
- No shared dependency preview over agents, routes, credentials, memory,
  marketplace, and lineage.
- No first-class deletion process state for pause/resume/retry/manual operator
  actions.
- Memory deletion needs operation evidence and namespace retention policy.
- Auth connection deletion needs stronger dependent-agent/process projection.
- Search/listing deletion is separate from audit tombstoning.
- Lineage severance needs explicit inheritance policy before reputation,
  certification, and marketplace rank can safely propagate.
- Active process cancellation needs a unified kill/freeze/revoke signal across
  work jobs, DAG runs, handoffs, proofs, and sessions.

These gaps are reasons to add small, purpose-built kernel state later. They are
not a reason to ignore the current cleanup rails or design a greenfield ledger
before the existing evidence substrate is projected.
