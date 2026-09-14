# Agent Evidence Correlation Chains

Status: causal-chain design, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/evidence-inventory.md`
- `docs/agent-evidence-dag.md`
- `docs/agent-dossier-api.md`

## Decision

The Agent Evidence DAG becomes useful only when rows are correlated into causal
chains.

P22 defines the first correlation layer:

- deploy -> review -> findings -> deployment review event
- grant -> handoff/proof/trial/receipt -> event/result/cost
- receipt -> replay session -> replay object refs
- code-editor handoff -> pushed commit -> source-push deploy -> review/proof
- failure/finding -> mutation -> new version -> subsequent review/proof
- version -> deployments -> reviews -> proofs -> receipts -> trust summary

The v0 chain builder should prefer strict joins and degrade to inferred joins
with explicit confidence and warnings.

## Chain Schema

```json
{
  "id": "chain:failure_remediation:example-agent:abc123",
  "type": "failure_remediation",
  "status": "resolved",
  "confidence": "inferred",
  "score": 0.74,
  "anchor": {
    "agent_id": 42,
    "agent_name": "example-agent",
    "head_sha": "abc123"
  },
  "steps": [],
  "edges": [],
  "open_gaps": [],
  "warnings": []
}
```

Required fields:

- `id`
- `type`
- `status`
- `confidence`
- `score`
- `anchor`
- `steps`
- `edges`
- `open_gaps`
- `warnings`

Confidence values:

- `strict`: exact id or source row relationship
- `strong`: multiple independent matching keys
- `inferred`: time/order/content heuristic
- `weak`: plausible but missing important ids
- `partial`: source evidence exists but chain is incomplete

## Chain Step Schema

```json
{
  "index": 0,
  "role": "finding",
  "node_id": "finding:rv_old:h1",
  "time": "2026-06-02T00:00:00Z",
  "status": "critical",
  "summary": "Reviewer found unsafe file write",
  "source_refs": [],
  "join_evidence": []
}
```

Step roles:

- `deployment`
- `deployment_event`
- `review`
- `finding`
- `grant`
- `handoff`
- `receipt`
- `session`
- `work_job`
- `work_event`
- `proof`
- `trial`
- `mutation`
- `source_push`
- `version`
- `cost`

## Join Precedence

Use keys in this order:

1. exact ids: `deploy_id`, `review_id`, `grant_id`, `receipt_id`, `session_id`,
   `job_id`, `event_id`
2. exact version ids: `head_sha`, `card_hash`, immutable image digest/ref
3. owner scope: `user_id`, `owner_id`, `agent_id`
4. agent identity: owned `agent_name`
5. process scope: `thread_id`, `correlation_id`, `root_job_id`, `parent_job_id`
6. source refs: `source_type`, `source_id`, `subject_type`, `subject_id`
7. time windows and ordering
8. redacted content hints, such as finding hash or prompt issue id

Never correlate private evidence across users without owner/operator authority.

## Chain 1: Deploy Review Chain

Question:

- what review evidence applies to this deployment/version?

Strict path:

```text
AgentDeployment.deploy_id
  -> AgentReviewRun.deploy_id
  -> AgentReviewRun.findings
  -> AgentDeploymentEvent(stage="review", data.review_id)
```

Primary joins:

- `AgentReviewRun.deploy_id == AgentDeployment.deploy_id`
- `AgentDeploymentEvent.deployment_id == AgentDeployment.id`
- review event `data.review_id == AgentReviewRun.review_id` when present

Fallback joins:

- same `agent_id` or owned `agent_name`
- `AgentReviewRun.ref == AgentDeployment.head_sha`
- review created after deployment created
- review created near deployment review event

Output:

- deployment step
- review step
- finding steps
- deployment review event steps
- support or contradiction edges to version

Status:

- `passed` when latest review for deployment has zero critical/warning findings
  according to policy
- `warning` when warnings exist without critical findings
- `blocked` when critical findings exist
- `errored` when reviewer failed
- `skipped` when reviewer disabled

## Chain 2: Handoff/Proof Authority Chain

Question:

- under what authority did this handoff, proof, trial, or receipt run?

Strict paths:

```text
GrantAudit.grant_id
  -> SubagentRun.grant_id
  -> SubagentRunEvent.grant_id/run_id
```

```text
GrantAudit.grant_id
  -> AgentProofRun.grant_id
```

```text
GrantAudit.grant_id
  -> TrialRun.grant_id
```

```text
GrantAudit.grant_id
  -> AgentReceipt.payload.grant_ids[]
```

Parent chain:

```text
GrantAudit.parent_grant_id -> GrantAudit.grant_id
```

Work mirror joins:

- `WorkJob.source_type/source_id`
- `WorkJob.subject_type/subject_id`
- `WorkEvent.source_type/source_id`
- `WorkJob.correlation_id`
- source-specific payload grant id

Output:

- grant chain
- run/proof/trial/receipt step
- events and file/resource effect summaries
- cost nodes by `LLMUsageEvent.grant_id`

Status:

- `authorized` when grant decision allowed and run/proof/trial exists
- `denied` when `GrantAudit.decision` is deny/hard deny
- `expired` when created time plus TTL is before use time
- `partial` when run exists but grant audit is missing

## Chain 3: Receipt/Session Chain

Question:

- can a signed execution receipt be connected to replay evidence and grants?

Strict path:

```text
AgentReceipt.receipt_id
  -> AgentSession.receipt_id
  -> AgentSession.events_object_key
```

Grant path:

```text
AgentReceipt.payload.grant_ids[]
  -> GrantAudit.grant_id
```

Fallback joins:

- same `agent_id` or owned `agent_name`
- same `caller`
- same `task_id`
- same `skill_name`
- near-identical timing
- same `input_hash` if available from replay session

Output:

- receipt node
- replay session node
- object-store event ref
- grant-use edges

Degraded behavior:

- receipt without session is still valid evidence
- session without receipt is still useful replay evidence but marked partial
- object-store missing file creates warning, not API failure

## Chain 4: Code-Editor Mutation Chain

Question:

- which code-editor turn produced a commit and what deployment/review/proof
  followed?

Strict path when captured:

```text
SubagentRun(agent_name=code-editor-agent or generated editor)
  -> returned payload push.head_sha
  -> WorkJob(kind=agent.source_push_deploy, input_payload.source_sha)
  -> WorkJob.output_payload.deploy_id
  -> AgentDeployment.deploy_id
  -> AgentReviewRun.deploy_id
  -> AgentProofRun.head_sha
```

Source-push anchors:

- `WorkJob.kind == "agent.source_push_deploy"`
- `WorkJob.input_payload.source_sha`
- `WorkJob.output_payload.deploy_id`
- `WorkJob.output_payload.review_id`
- `AgentDeployment.head_sha == source_sha`
- `AgentReviewRun.deploy_id == deploy_id`

Code-editor payload anchors:

- `agent_name`
- owner/repo
- ref
- `sync.head_sha`
- `push.head_sha`
- `changes.dirty`
- `exit_code`
- `timed_out`
- `codegraph.ok`

Fallback joins:

- same owned target agent
- `push.head_sha == AgentDeployment.head_sha`
- code-editor completion before source-push job created
- source-push job changed paths align with code-editor change summary
- same `thread_id`
- same grant chain

Output:

- code-editor handoff/process step
- mutation step
- produced source version step
- source-push job step
- deployment/review/proof steps

Confidence:

- `strict` when `push.head_sha`, source-push job `source_sha`, and deployment
  `head_sha` all match
- `strong` when commit SHA matches deployment and source-push job is missing
- `inferred` when only timestamp/thread/target agent match
- `weak` when prompt/content hints are the only link

## Chain 5: Failure Remediation Chain

Question:

- did a failure or finding lead to a patch, and did later evidence improve?

Failure anchors:

- `SubagentRun.status in {"error", "denied", "failed", "canceled"}`
- `AgentProofRun.status == "failed"`
- `AgentReceipt.status in {"error", "cancelled", "partial"}`
- `AgentReviewRun.critical_count > 0`
- `AgentReviewRun.warning_count > 0`
- `TrialRun.status == "failed"`
- `WorkJob.status in {"error", "failed"}`
- `WorkEvent.severity in {"warning", "error", "critical"}`

Remediation path:

```text
failure/finding
  -> code-editor handoff or Agent Studio edit
  -> mutation push.head_sha
  -> deployment with same head_sha
  -> review/proof/receipt/trial after deployment
  -> resolved or still failing status
```

Strict joins:

- explicit target finding id once Agent Studio records it
- `push.head_sha == AgentDeployment.head_sha`
- `AgentReviewRun.deploy_id == AgentDeployment.deploy_id`
- `AgentProofRun.head_sha == AgentDeployment.head_sha`

Fallback joins:

- deterministic finding hash disappears in later review
- later review passes for new `head_sha`
- later proof passes for same `head_sha` and skill
- failure skill matches later proof/receipt skill
- mutation prompt references finding hash, file path, rule id, or issue id
- later deployment time is after failure and after mutation

Statuses:

- `resolved`: later review/proof/receipt supports same skill/version and no
  matching finding remains
- `improved`: severity/count decreased but warnings remain
- `regressed`: later evidence is worse
- `unresolved`: no later supporting evidence
- `unknown`: correlation too weak

## Chain 6: Version Trust Chain

Question:

- what evidence supports or contradicts this source/card/image version?

Version anchors:

- `head_sha`
- `card_hash`
- image ref or digest
- deployment id

Strict path:

```text
source/card/image version
  <- AgentDeployment(head_sha/image)
  <- AgentReviewRun(deploy_id/ref)
  <- AgentProofRun(head_sha/card_hash/image)
  <- AgentReceipt(agent_version)
  <- TrialRun/Receipt evidence
```

Output:

- version node
- all deployments for version
- reviews for deployments/ref
- proofs for head/card/image
- receipts for agent version where useful
- current trust status

Rules:

- latest evidence wins for current status, but older contradictory evidence is
  retained
- review/proof must be after deployment to support current version
- evidence for old SHA does not support new SHA unless policy declares it
  inherited

## Finding Hash Strategy

Because review findings are JSON without stable ids, v0 should compute a
deterministic hash.

Normalize fields:

- severity
- rule id or check id
- file/path
- line range if present
- title
- normalized message
- category

Normalization:

- lowercase severity and rule/category
- trim whitespace
- collapse repeated whitespace
- normalize path separators to `/`
- remove volatile line numbers from message text when a structured line field
  exists
- sort JSON keys

Hash:

```text
finding_hash = sha256(
  "v1|" + severity + "|" + rule + "|" + path + "|" + line_key + "|" +
  title + "|" + normalized_message + "|" + category
)[0:16]
```

Finding node id:

```text
finding:{review_id}:{finding_hash}
```

Cross-version matching:

- same hash means same finding shape
- same path/rule/title with changed line can be probable same finding
- missing hash in later review does not automatically mean fixed unless later
  review/proof status supports improvement

## Join-Key Rules

### Agent Scope

Use:

- `agent_id` first
- owned `agent_name` second
- `user_id` or owner id always

Rule:

- never expand a graph by `agent_name` alone

### Version Scope

Use:

- `head_sha` for managed source
- `card_hash` for Agent Card declaration
- image digest/ref for runtime artifact
- ref only as weak fallback

Rule:

- a review `ref` equal to SHA is strong; `ref=main` is weak unless time-bounded

### Time Windows

Suggested default windows:

- code-editor completion to source-push job: 0 to 30 minutes
- source-push job to deployment row: 0 to 10 minutes
- deployment to review enqueue/result: 0 to 60 minutes
- deployment to proof: 0 to 24 hours unless user requested proof later
- failure to remediation mutation: 0 to 14 days

Rules:

- exact ids override windows
- time-only joins are always inferred or weak

### Work Ledger

Use:

- `job_id`
- `root_job_id`
- `parent_job_id`
- `correlation_id`
- `source_type/source_id`
- `subject_type/subject_id`
- `worker_name`
- payload-specific ids

Rule:

- payload ids must be redacted/summarized before surfacing

## Gap List

### First-Class Mutation Record

Needed fields:

- `mutation_id`
- `agent_id`
- `agent_name`
- `owner_user_id`
- `source`
- `trigger_evidence_refs`
- `target_finding_ids`
- `base_head_sha`
- `produced_head_sha`
- `changed_paths`
- `prompt_hash`
- `result_status`
- `work_job_id`
- `subagent_grant_id`
- `created_at`

Why:

- makes finding -> patch -> commit chains strict

### Agent Studio Ledger Hooks

Agent Studio should emit:

- finding id/hash selected for fix
- code-editor prompt id/hash
- target files
- intended fix summary
- produced commit SHA
- deployment id
- post-fix review/proof ids

Why:

- avoids relying on prompt text or time windows

### Stable Correlation IDs

Add to:

- deployments
- reviews
- proofs
- code-editor turns
- source-push jobs
- receipts/sessions when initiated from a platform workflow

Why:

- lets DAG chains remain strict across subsystems

### Review Finding IDs

Add:

- deterministic `finding_id`
- `finding_hash`
- `status`
- `fixed_by_mutation_id`
- `introduced_in_head_sha`
- `resolved_in_head_sha`

Why:

- makes residual risk and fix status reliable

## Degraded Behavior

Missing receipt:

- show handoff/proof/work evidence and warn `receipt_missing`

Missing session:

- show receipt and warn `replay_session_missing`

Missing source-push job:

- join mutation to deployment by `head_sha` and warn `source_push_job_missing`

Missing review:

- show deployment/proof and warn `review_missing_for_version`

Missing proof:

- show deployment/review and warn `proof_missing_for_version`

Missing commit SHA:

- use deployment/ref time ordering and warn `head_sha_missing`

## Recommendation

P22 should make the DAG causal, not just connected:

- strict joins first
- inferred joins only with confidence and warnings
- deterministic finding hashes now
- first-class mutation ids later
- Agent Studio hooks for target finding and produced commit
- degradation paths for missing receipts, sessions, reviews, proofs, and jobs

This is the bridge from evidence rows to an actually useful agent trust story.
