# Agent Dossier And Evidence Timeline API

Status: API and UX contract, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/evidence-inventory.md`
- `docs/agent-evidence-dag.md`

## Decision

The first product-shaped graph surface is an Agent Dossier backed by the Agent
Evidence DAG.

The dossier is not a marketing profile. It is a compact trust and provenance
view for an owned or visible agent:

- what the agent claims it can do
- what version is live
- what authority it has used
- what calls, reviews, proofs, trials, deployments, and mutations exist
- what failed repeatedly
- what changed after a failure
- which risks remain unresolved

The dossier API should consume the P20 DAG projection and return a summarized
profile plus timeline lanes. It should not duplicate raw evidence payloads.

## Endpoints

### `GET /v1/agents/{name}/evidence-dag`

Purpose:

- return the graph-shaped evidence nodes and edges for one agent

Query:

- `since`: ISO timestamp
- `until`: ISO timestamp
- `limit`: default 250, max 1000
- `head_sha`: filter to one source version
- `skill_name`: filter by skill
- `event_type`: filter by normalized node/edge/event type
- `status`: filter by status
- `include_payloads`: default false
- `include_inferred`: default true
- `include_warnings`: default true

Response:

- P20 `Agent Evidence DAG Projection` envelope

Authorization:

- owner sees owner redacted view
- public callers see only public evidence
- operators need explicit operator role and operator redaction

### `GET /v1/agents/{name}/dossier`

Purpose:

- return summarized trust, version, authority, quality, mutation, and risk
  profile for one agent

Query:

- `since`: ISO timestamp
- `head_sha`: focus one version
- `skill_name`: focus one skill
- `include_timeline`: default true
- `timeline_limit`: default 100, max 250
- `include_graph_refs`: default true

Response shape:

```json
{
  "schema_version": 1,
  "agent": {},
  "claims": {},
  "current_version": {},
  "ownership": {},
  "trust_profile": {},
  "authority_summary": {},
  "quality_summary": {},
  "mutation_summary": {},
  "risk_summary": {},
  "timeline": [],
  "graph_refs": {},
  "warnings": [],
  "redaction": {}
}
```

### `GET /v1/agents/{name}/evidence-timeline`

Purpose:

- return a flattened timeline derived from the DAG for UI lanes

Query:

- `since`
- `until`
- `limit`: default 100, max 500
- `lane`: `version`, `authority`, `mutation`, `quality`, `process`, `cost`,
  `control`
- `head_sha`
- `skill_name`
- `status`
- `severity`

Response:

```json
{
  "schema_version": 1,
  "agent": {},
  "items": [],
  "next_cursor": null,
  "warnings": []
}
```

## Dossier Summary Schema

### Agent

Fields:

- `agent_id`
- `name`
- `owner_user_id`
- `public`
- `status`
- `agent_url`
- `created_at`
- `updated_at`

Source:

- `Agent`
- latest deployment where useful

### Claims

Fields:

- `description`
- `version`
- `skills`
- `auth_requirements`
- `llm_provisioning`
- `resources`
- `pricing`
- `card_hash`

Source:

- current Agent Card/cache
- latest proof card hash where present

Rules:

- claims are declarations, not proof
- every claim should be separable from evidence that supports it

### Current Version

Fields:

- `head_sha`
- `image`
- `agent_url`
- `card_hash`
- `deployment_id`
- `deployment_status`
- `deployment_completed_at`
- `verification`
- `latest_review_status`
- `latest_proof_status`

Source:

- latest `AgentDeployment`
- latest `AgentReviewRun`
- latest `AgentProofRun`
- Agent Card/cache

### Ownership

Fields:

- `owner_user_id`
- `source_repo_url`
- `created_by_trigger`
- `code_editor_enabled`
- `auth_connection_count`
- `secret_count`

Source:

- `Agent`
- `AgentDeployment`
- `AgentCodeEditorOptIn`
- `AgentAuthConnection`
- `AgentSecret`

Redaction:

- auth/secret metadata only

### Trust Profile

Fields:

- `status`: `unknown`, `unproven`, `reviewed`, `proofed`, `warning`,
  `blocked`, `regressed`, `stale`
- `review_status`
- `proof_status`
- `receipt_status`
- `critical_findings`
- `warning_findings`
- `last_passing_review_at`
- `last_passing_proof_at`
- `last_failure_at`
- `confidence`

Rules:

- passing proof does not erase later critical review
- latest critical finding without later passing review/proof keeps status
  `blocked` or `warning`
- stale status can be computed when live version has no review/proof after a
  newer deployment

### Authority Summary

Fields:

- `grant_count`
- `active_grant_count`
- `expired_grant_count`
- `denied_grant_count`
- `grant_chains`
- `most_recent_grants`
- `credential_refs`
- `policy_refs`

Source:

- `GrantAudit`
- grant ids in proofs, handoffs, trials, receipts, LLM usage
- `UserControlPolicy`
- credential metadata

Rules:

- summarize scope and decisions
- no signed grant tokens
- no credential secrets

### Quality Summary

Fields:

- `calls_total`
- `calls_ok`
- `calls_failed`
- `proofs_total`
- `proofs_passed`
- `proofs_failed`
- `reviews_total`
- `reviews_passed`
- `reviews_warning`
- `reviews_failed`
- `trial_score_latest`
- `recurring_failures`
- `findings_by_severity`
- `findings_by_skill`

Source:

- `AgentReceipt`
- `SubagentRun`
- `AgentProofRun`
- `AgentReviewRun`
- `TrialRun`

### Mutation Summary

Fields:

- `mutation_count`
- `latest_mutation_at`
- `latest_mutation_head_sha`
- `latest_mutation_status`
- `code_editor_turns`
- `source_push_deployments`
- `fix_chains`
- `inferred_mutation_count`

Source:

- code-editor returned payloads in handoff/work evidence
- `WorkJob`
- deployments by `head_sha`
- reviews/proofs after mutation

Rules:

- inferred mutation nodes must be marked inferred
- stdout/stderr are not included by default

### Risk Summary

Fields:

- `open_critical_findings`
- `open_warning_findings`
- `unverified_live_version`
- `failed_recent_proofs`
- `failed_recent_receipts`
- `stale_review`
- `stale_proof`
- `missing_correlation`
- `credential_expiry_risks`
- `revocation_gaps`
- `redaction_warnings`

Source:

- DAG warnings
- reviews/proofs/receipts
- credential metadata
- P19 gap rules

## Timeline Item Schema

Every timeline item:

```json
{
  "id": "timeline:deployment:dpl_123",
  "lane": "version",
  "type": "deployment",
  "title": "Deploy example-agent",
  "status": "live",
  "severity": "info",
  "summary": "Agent is live",
  "time": "2026-06-02T00:00:00Z",
  "agent_name": "example-agent",
  "skill_name": null,
  "head_sha": "abc123",
  "grant_id": null,
  "graph_node_ids": ["deployment:dpl_123"],
  "graph_edge_ids": [],
  "source_refs": [],
  "redaction": {},
  "actions": []
}
```

Required fields:

- `id`
- `lane`
- `type`
- `title`
- `status`
- `severity`
- `summary`
- `time`
- `graph_node_ids`
- `source_refs`
- `redaction`

Optional fields:

- `agent_name`
- `skill_name`
- `head_sha`
- `grant_id`
- `review_id`
- `deploy_id`
- `receipt_id`
- `session_id`
- `job_id`
- `actions`

## Timeline Lanes

### Version Lane

Items:

- deployments
- deployment events
- source versions
- image versions
- card versions
- runtime verification

Questions:

- what version is live?
- when did it change?
- what image and card hash are tied to it?

### Authority Lane

Items:

- grant audit rows
- parent grant chains
- proof/handoff/trial grant use
- auth connection metadata
- secret metadata
- policy changes when available

Questions:

- what authority was used?
- who issued it?
- how was it scoped?
- where did it narrow or deny?

### Mutation Lane

Items:

- code-editor handoffs
- mutation nodes
- source-push jobs
- produced commits
- post-mutation deployments

Questions:

- what code change happened?
- what evidence triggered it?
- what commit did it produce?
- what review/proof followed?

### Quality Lane

Items:

- reviews
- findings
- proofs
- receipts
- trials
- failure signals

Questions:

- what passed?
- what failed?
- what failed repeatedly?
- what evidence supports current trust?

### Process Lane

Items:

- WorkJobs
- WorkEvents
- SubagentRunEvents
- DAG runs/nodes
- MetaAgentRuns

Questions:

- what long-running or child work happened?
- what was still active?
- what stopped or failed?

### Cost Lane

Items:

- LLM usage
- cost summaries
- budget exceedance signals

Questions:

- what did this cost?
- which grant/thread/skill incurred the spend?

### Control Lane

Items:

- future graph control events
- freezes
- revocations
- routing disable
- mutation disable
- deletion previews

Questions:

- what human/operator intervention happened?
- what authority was reduced?

## UI Requirements

Surface:

- add an Evidence tab to My Agents / Agent detail
- keep the first view a dossier summary with latest version, trust, authority,
  quality, mutation, and risk status
- include a lane timeline below the summary
- allow drill-in to graph node detail and source refs
- show strict vs inferred correlations clearly
- show redaction and missing-correlation warnings

Controls:

- filter by version/head SHA
- filter by skill
- filter by lane
- filter by status/severity
- toggle inferred edges
- toggle raw refs for owner/operator only
- copy source ref ids for debugging

Visual behavior:

- dense operational layout
- no hero or marketing treatment
- status and severity should be scannable
- long ids should be truncated with copy affordance
- timeline items should not resize dramatically when expanded
- raw payload access should be a deliberate drill-in, not default

## Privacy And Redaction

Default dossier response must not include:

- signed grant tokens
- signed receipt tokens
- signed replay session tokens
- CP JWTs
- Gitea tokens
- LLM credentials
- secret ciphertext
- raw session event logs
- raw private file contents
- raw tool call payloads
- raw stdout/stderr

Default dossier response may include:

- ids
- hashes
- timestamps
- statuses
- counts
- short summaries
- source refs
- redaction class
- object refs only when caller can access them

Public view:

- restrict to public agent metadata and public proof/review summaries
- omit authority and private execution timelines

Owner view:

- include owned evidence summaries and refs
- redact secrets and raw payloads

Operator view:

- include diagnostics and redacted source refs
- require explicit operator role and audit event for cross-tenant access

## Residual Risk Rules

Open risk if:

- latest deployment has no later review
- latest deployment has no later proof
- latest review has critical findings
- latest proof failed
- latest receipt/session shows repeated failure
- code-editor mutation produced a commit but no later review/proof exists
- finding hash appears in latest review and no later passing evidence addresses
  it
- grant is denied or hard-denied for an attempted workflow
- credential expires soon or was revoked while active process still appears live
- correlation is inferred and low confidence

Closed risk if:

- later version has passing review/proof after the finding/mutation
- finding hash no longer appears and later review passes
- failed proof is followed by passing proof for same version/skill
- failed receipt pattern stops after mutation and later evidence supports claim

## API Validation Rules

The dossier API should validate:

- caller authorization before any source expansion
- all graph refs point to DAG nodes/edges in the same response or are marked
  external
- every timeline item has at least one source ref
- every risk item links to source evidence or graph warning
- no forbidden secret fields appear in serialized JSON
- inferred fix chains are marked inferred
- public view omits private lanes

## Non-Goals

- no active graph mutation
- no new graph database
- no raw forensic replay UI
- no guarantee that inferred fix chains are definitive
- no hidden operator-only payload expansion

## Recommendation

P21 should make the evidence DAG usable:

- `evidence-dag` is the raw graph projection
- `dossier` is the trust/profile summary
- `evidence-timeline` is the UI-friendly lane feed

This gives users and operators a clear answer to "what is this agent, why
should I trust it, what changed, and what risk remains?" without adding new
kernel state before v0 is proven.
