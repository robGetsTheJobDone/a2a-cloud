# Dynamic Capability Graph Evidence Inventory

Status: existing-substrate inventory, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/roadmap.md`
- `docs/storage-query.md`
- `docs/observability-controls.md`
- `docs/simulation-proofs.md`

## Decision

The Agent Evidence DAG starts from existing evidence rows and SDK artifacts.

Do not create greenfield graph tables until this inventory is normalized and a
read-only projection proves useful. The control plane already has enough
evidence to answer most v0 questions:

- what ran
- who owned it
- what grant or policy allowed it
- what version was involved
- what changed
- which review/proof/deployment evidence followed
- what failed
- what cost money or tokens

The first graph/DAG work should map these sources, not replace them.

## Inventory Table

| Source | Evidence kind | Key fields | Join keys | Current endpoint/surface | Missing for DAG v0 |
|---|---|---|---|---|---|
| SDK `Grant` | signed authority token | `grant_id`, `issuer`, `audience`, `bucket`, `mode`, allow/deny patterns, `outputs_prefix`, `write_prefixes`, LLM limits, `parent_grant_id`, delegation depth, `expires_at`, `issued_at` | `grant_id`, `parent_grant_id`, `issuer`, `audience`, bucket | carried in runtime calls; audited in `GrantAudit` | revocation status is server-side only; signed token must never be exposed in DAG |
| `GrantAudit` | server-side grant decision | `grant_id`, `parent_grant_id`, `issuer`, `audience`, `bucket`, `mode`, allow/deny patterns, `outputs_prefix`, `ttl_seconds`, `user_id`, `decision`, `decided_by`, `reason`, `created_at` | `grant_id`, `parent_grant_id`, `user_id`, `audience`, `issuer` | `GET /v1/me/grants`, `GET /v1/me/grants/{grant_id}` | no revoke/tombstone row; no expiry timestamp, only TTL and created time |
| SDK `ExecutionReceipt` | signed execution receipt | `receipt_id`, `agent_name`, `agent_version`, `caller`, `task_id`, `skill_name`, `input_hash`, `input_preview`, `grant_ids`, file ops, tool calls, artifacts, handoffs, status, error type, result preview, eval score, reviewer, timing | `receipt_id`, `agent_name`, `caller`, `task_id`, `skill_name`, `grant_ids` | posted to `POST /v1/agents/{name}/receipts` | raw signed token and payload need graph redaction; no owner-scoped graph wrapper yet |
| `AgentReceipt` | persisted verified receipt | `receipt_id`, `agent_id`, `agent_name`, `agent_version`, `caller`, `task_id`, `skill_name`, `status`, `eval_score`, timing, `signed_token`, `payload` | `receipt_id`, `agent_id`, `agent_name`, `caller`, `task_id`, `skill_name`, grant ids inside payload | `GET /v1/agents/{name}/receipts`, `GET /v1/agents/{name}/receipts/{receipt_id}` | route exposes full signed token/payload; DAG should return redacted refs by default |
| SDK `ReplaySession` | signed replay event log | `session_id`, `agent_name`, `agent_version`, `caller`, `task_id`, `skill_name`, `random_seed`, `input_hash`, timing, events, `receipt_id` | `session_id`, `receipt_id`, `agent_name`, `caller`, `task_id`, `skill_name`, `input_hash` | posted to `POST /v1/agents/{name}/sessions` | replay payload can be large and private; graph should expose event summaries first |
| `AgentSession` | persisted replay session header | `session_id`, `agent_id`, `agent_name`, `agent_version`, `caller`, `task_id`, `skill_name`, `receipt_id`, timing, `event_count`, `signed_token`, `events_object_key` | `session_id`, `receipt_id`, `agent_id`, `agent_name`, `caller`, `task_id`, `skill_name` | `GET /v1/agents/{name}/sessions`, `GET /v1/sessions/{session_id}` | global session lookup must be wrapped by owner/public policy in graph API |
| `SubagentRun` | cross-agent handoff run | `grant_id`, `rerun_of_grant_id`, `user_id`, `thread_id`, `agent_name`, `skill_name`, `args_json`, `scopes`, `status`, `summary`, `file_ops`, `start_files`, timestamps | `grant_id`, `rerun_of_grant_id`, `user_id`, `thread_id`, `agent_name`, `skill_name` | subagent run APIs, control room, agent insights | `args_json` may contain private payloads; no explicit mutation/correlation id |
| `SubagentRunEvent` | ordered handoff event timeline | `run_id`, `grant_id`, `user_id`, `event_type`, `payload`, `created_at` | `run_id`, `grant_id`, `user_id`, event id/order | subagent run APIs, control room receipts, agent insights | payload redaction is per-view, not centralized |
| `SubagentRunRecorder` | handoff recording helper | records handoff, invoke start/complete/error, progress, auth/input required, artifact, denied, status changes, file diff snapshots | `grant_id`, `run_id`, `thread_id`, A2A task ids | internal helper | good event source, but event taxonomy needs normalization |
| `WorkJob` | generic work ledger job | `job_id`, kind, status, user/thread/root/parent/correlation ids, source/subject/worker refs, payloads, artifact/proof refs, metadata, timestamps, heartbeat/lease | `job_id`, `root_job_id`, `parent_job_id`, `correlation_id`, `user_id`, `thread_id`, source/subject/worker refs | `GET /v1/me/activity`, `GET /v1/me/jobs/{job_id}` | not every specialized run is mirrored yet; payload redaction needs graph policy |
| `WorkEvent` | append-only work event | `event_id`, `job_id`, `event_seq`, `parent_event_id`, `correlation_id`, event type, stage/status/severity/message, actor/source refs, payload, artifact/proof refs, metrics, metadata, created time | `event_id`, `job_id`, `event_seq`, `parent_event_id`, `correlation_id`, source refs | `GET /v1/me/jobs/{job_id}/events`, event stream | must remain append-only; needs normalized graph event names |
| `AgentDeployment` | deploy attempt and current status | `deploy_id`, `agent_id`, `user_id`, `agent_name`, trigger, status, source repo URL, `head_sha`, image, agent URL, error, verification, timestamps | `deploy_id`, `agent_id`, `user_id`, `agent_name`, `head_sha`, image | `GET /v1/agents/{name}/deployments`, control room | no `correlation_id`; image should prefer immutable digest later |
| `AgentDeploymentEvent` | deployment timeline | deployment id, `deploy_id`, `agent_name`, stage, status, message, data, created time | deployment id, `deploy_id`, `agent_name`, stage | deployment receipts, deployment routes, review pipeline | no explicit event id/correlation id; data payload needs redaction |
| `AgentReviewRun` | reviewer evidence | `review_id`, `deploy_id`, `agent_id`, `agent_name`, ref, `user_id`, status, summary, findings, severity counts, error, elapsed, timestamps | `review_id`, `deploy_id`, `agent_id`, `agent_name`, `user_id`, ref/head SHA if encoded | review routes, deployment events | findings need stable ids or deterministic hashes |
| review pipeline | review-to-deployment linkage | writes `AgentReviewRun` and review stage `AgentDeploymentEvent` rows | `review_id`, `deploy_id`, `agent_name`, ref | deployment timeline | review is advisory today; DAG must mark whether policy blocks promotion |
| `AgentProofRun` | proof invocation evidence | id, `agent_id`, `agent_name`, `user_id`, `skill_name`, `grant_id`, status, summary/error, args/result/events/file ops, `card_hash`, repo URL, `head_sha`, image, agent URL, elapsed/timestamps | id, `agent_id`, `agent_name`, `user_id`, `grant_id`, `head_sha`, `card_hash`, image | proof routes, control room, agent insights | proof events/result need redaction; no `proof_id` string |
| proof grant audit | proof authority evidence | proof routes write `GrantAudit` for proof calls | `grant_id`, `agent_name`, `user_id` | proof routes, grants route | no direct proof-to-grant foreign key beyond `grant_id` |
| `TrialRoom` and `TrialRun` | competitive/eval evidence | room slug/title/goal/criteria/status, run agent/skill/status/score/summary/error/result/events/file ops/receipt JSON | trial id, room slug, `agent_name`, `grant_id`, receipt id, user id | trial routes, control room, agent insights | trial receipt JSON can be inconsistent; finding ids absent |
| `LLMUsageEvent` | cost/token attribution | `user_id`, `thread_id`, `dag_run_id`, `grant_id`, `agent_name`, `skill_name`, source, provider, model, tokens, cost, metadata, created time | `user_id`, `thread_id`, `dag_run_id`, `grant_id`, `agent_name`, `skill_name` | control room; mirrored to work ledger | provider metadata may contain errors/private info; not all LLM calls may be captured |
| `UserControlPolicy` | user policy/budget state | budget limits, max agent calls, file-write approval, external network deny, only-approved agents, PII mode, approved agents | `user_id` | `GET/PATCH /v1/me/control-room/policy` | no policy decision event log yet |
| `PlatformSetting` | platform toggle state | key/value/description/updated_by/timestamps | setting key, `updated_by` | `GET/PUT /v1/admin/settings/{key}` | only `reviewer_enabled` exists; setting changes need audit event |
| `AgentAuthConnection` | credential connection metadata | agent/user/scheme/type/scope/status/metadata/expires/verified timestamps, secret ciphertext | `agent_id`, `user_id`, `agent_name`, connection id | agent auth routes | ciphertext is secret; no graph revocation event yet |
| `AgentSecret` | runtime secret metadata | agent/user/name/key/redacted value/timestamps | `agent_id`, `user_id`, `agent_name`, key | agent secrets routes | no dependency preview or graph tombstone on delete |
| `GiteaTokenAudit` | short-lived repo token audit | token name, username, scopes, owner, repo, permission, issuing user, purpose, token hash, issued/expires/revoked times | token name, user id, owner/repo, purpose, token hash | internal `gitea_meta` helpers | not connected to code-editor run by stable mutation id |
| `AgentCodeEditorOptIn` | code-editor enablement | agent/user/status/shared agent/workspace metadata/last error/timestamps | `agent_id`, `user_id`, `agent_name` | agent code-editor routes | enable/disable should become mutation-control evidence |
| code-editor turn result | source mutation evidence | `agent_name`, owner, ref, elapsed, exit code, sync status, codegraph status, changes, push metadata, OpenHarness metadata, parsed output, stdout/stderr tail | `agent_name`, owner/repo, ref, sync `head_sha`, push `head_sha`, grant/run ids via handoff | returned by `code-editor-agent.turn`; captured if persisted by handoff/work ledger | no first-class mutation row; stdout/stderr must be redacted |
| control-room timeline | user evidence aggregator | SubagentRun, DagRun, ProofRun, TrialRun, Deployment, LLM usage summaries | `user_id`, `thread_id`, `grant_id`, `deploy_id`, `head_sha` | `GET /v1/me/control-room` | flat timeline, not DAG-shaped |
| agent-insights call logs | owned-agent call aggregator | handoff, trial, proof logs for one owned agent | `agent_name`, `grant_id`, `head_sha`, trial room, proof id | `GET /v1/agents/{name}/insights/calls` | not enough for version/authority chains by itself |

## Join-Key Map

### `agent_name`

Meaning:

- human-readable and DNS-friendly agent identity

Sources:

- Agent rows
- receipts
- sessions
- handoffs
- deployments
- reviews
- proofs
- trials
- LLM usage
- code-editor results

Rules:

- never authorize by `agent_name` alone
- first resolve owned/visible Agent row, then join other rows
- use `agent_id` when present

### `agent_id`

Meaning:

- stable database id for a registered agent

Sources:

- Agent-owned rows, deployments, receipts, sessions, proofs, auth, secrets

Rules:

- strongest owner-scoped join key when present
- rows with nullable `agent_id` must fall back to owned `agent_name` and user id

### `user_id`

Meaning:

- owner/caller scope

Sources:

- most control-plane evidence rows

Rules:

- graph APIs should filter source rows by owner/user before expanding joins
- cross-user operator views require explicit operator role and redaction

### `grant_id`

Meaning:

- authority/capability chain key

Sources:

- SDK grant
- `GrantAudit`
- `SubagentRun`
- `SubagentRunEvent`
- `AgentProofRun`
- `TrialRun`
- `AgentReceipt.payload.grant_ids`
- `LLMUsageEvent`
- DAG nodes

Rules:

- use `GrantAudit` as authority source of truth
- use runtime rows as evidence that a grant was used
- child grant chains follow `parent_grant_id`

### `receipt_id`

Meaning:

- signed invocation receipt identity

Sources:

- SDK `ExecutionReceipt`
- `AgentReceipt`
- `AgentSession.receipt_id`
- trial receipt JSON

Rules:

- use `AgentReceipt` as receipt source of truth
- use `AgentSession.receipt_id` to attach replay event log

### `session_id`

Meaning:

- signed replay session identity

Sources:

- SDK `ReplaySession`
- `AgentSession`

Rules:

- session header is SQL
- event stream payload is object storage
- graph returns summaries/refs by default

### `deploy_id`

Meaning:

- deploy attempt identity

Sources:

- `AgentDeployment`
- `AgentDeploymentEvent`
- `AgentReviewRun.deploy_id`
- deployment control-room receipts

Rules:

- use deployment row as deploy source of truth
- join review via `deploy_id`
- join version via `head_sha` and image

### `review_id`

Meaning:

- review run identity

Sources:

- `AgentReviewRun`
- deployment review events

Rules:

- use review row as finding source of truth
- use deployment events for timeline
- deterministic finding ids are needed for remediation tracking

### `head_sha`

Meaning:

- source version identity

Sources:

- deployments
- proofs
- code-editor sync/push output
- source-push deployment jobs

Rules:

- strongest version join for managed source
- code-editor `push.head_sha` should connect mutation to deployment/review/proof
- absence of SHA should lower confidence, not hide evidence

### `thread_id`

Meaning:

- conversation/process context

Sources:

- chat threads
- subagent runs
- DAG runs
- WorkJobs
- LLM usage

Rules:

- useful for correlating user prompt, handoff, LLM cost, and DAG activity
- must be owner-scoped

### `job_id` And `correlation_id`

Meaning:

- generic work-ledger process identity and cross-job correlation

Sources:

- `WorkJob`
- `WorkEvent`
- source-push jobs
- LLM usage mirror
- provisioning jobs

Rules:

- use `job_id` for work timelines
- use `correlation_id` when present
- add `correlation_id` to specialized rows later

## Source Of Truth Recommendations

| Evidence type | Source of truth | Supporting sources |
|---|---|---|
| agent identity | `Agent` | Agent Card/cache, deployments, proofs |
| authority grant | `GrantAudit` | SDK Grant, receipt grant ids, proof/handoff/trial grant ids |
| grant use | `SubagentRun`, `AgentProofRun`, `TrialRun`, `AgentReceipt`, `LLMUsageEvent` | WorkEvent mirror |
| signed execution receipt | `AgentReceipt` | SDK `ExecutionReceipt`, sessions |
| replay header | `AgentSession` | SDK `ReplaySession` |
| replay event payload | object-store JSONL at `AgentSession.events_object_key` | signed session token |
| handoff run | `SubagentRun` | SubagentRunEvent, WorkJob/WorkEvent |
| handoff timeline | `SubagentRunEvent` | WorkEvent mirror |
| generic process | `WorkJob` | specialized run rows |
| generic event timeline | `WorkEvent` | specialized event rows |
| deployment attempt | `AgentDeployment` | Argo/Kubernetes verification |
| deployment timeline | `AgentDeploymentEvent` | WorkEvent if mirrored later |
| reviewer findings | `AgentReviewRun` | deployment review event |
| proof evidence | `AgentProofRun` | GrantAudit, LLMUsageEvent |
| trial/evaluation evidence | `TrialRun` | TrialRoom, receipt JSON |
| LLM cost | `LLMUsageEvent` | WorkJob/WorkEvent mirror |
| user policy | `UserControlPolicy` | future policy decision events |
| platform policy | `PlatformSetting` | future setting audit events |
| credential metadata | `AgentAuthConnection`, `AgentSecret`, `GiteaTokenAudit` | revocation events later |
| mutation evidence | code-editor returned result captured in handoff/work rows | first-class mutation row later |
| current timeline | control-room aggregation | evidence DAG projection later |
| owned-agent call summary | agent-insights aggregation | evidence DAG projection later |

## DAG Node Candidates From Inventory

Agent/version nodes:

- `agent:{agent_id}`
- `agent_name:{name}`
- `source:{owner}/{repo}@{head_sha}`
- `image:{image_ref}`
- `card:{agent_name}@{card_hash}`

Authority nodes:

- `grant:{grant_id}`
- `policy:user:{user_id}`
- `policy:platform:{setting_key}`
- `credential:agent_auth:{connection_id}`
- `credential:agent_secret:{agent_id}:{key}`
- `credential:gitea_token:{token_name}`

Execution nodes:

- `handoff:{grant_id}`
- `subagent_event:{event_id_or_run_id_seq}`
- `receipt:{receipt_id}`
- `session:{session_id}`
- `proof:{proof_run_id}`
- `trial:{trial_run_id}`
- `work_job:{job_id}`
- `work_event:{event_id}`

Lifecycle nodes:

- `deployment:{deploy_id}`
- `deployment_event:{deployment_id}:{event_id}`
- `review:{review_id}`
- `finding:{review_id}:{finding_hash}`
- `mutation:{mutation_id_or_inferred_sha}`

Signal nodes:

- `llm_usage:{id}`
- `review_signal:{review_id}`
- `proof_signal:{proof_run_id}`
- `failure_signal:{source}:{id}`
- `cost_signal:{llm_usage_id}`

## DAG Edge Candidates From Inventory

Authority edges:

- `grant PARENT_OF grant`
- `grant AUTHORIZES handoff`
- `grant AUTHORIZES proof`
- `grant AUTHORIZES trial`
- `grant USED_BY receipt`
- `grant ATTRIBUTES_COST llm_usage`

Execution edges:

- `agent HANDLED receipt`
- `agent RAN handoff`
- `handoff EMITS subagent_event`
- `work_job HAS_EVENT work_event`
- `receipt HAS_REPLAY_SESSION session`
- `session HAS_EVENT replay_event_ref`

Version edges:

- `agent HAS_DEPLOYMENT deployment`
- `deployment DEPLOYS_VERSION source_version`
- `deployment USES_IMAGE image_version`
- `review REVIEWS_DEPLOYMENT deployment`
- `review REVIEWS_REF source_version_or_ref`
- `proof TESTS_VERSION source_or_card_version`
- `mutation PRODUCES_VERSION source_version`

Quality edges:

- `review HAS_FINDING finding`
- `proof SUPPORTS version`
- `trial SUPPORTS_OR_CONTRADICTS agent_claim`
- `receipt SUPPORTS_OR_CONTRADICTS agent_claim`

Process/correlation edges:

- `subagent_run MIRRORED_TO work_job`
- `work_event CORRELATES_WITH deployment_event`
- `code_editor_turn PRODUCES_COMMIT source_version`
- `source_push_job TRIGGERS deployment`

## Redaction Rules For Inventory Sources

Always redact from graph defaults:

- signed grant tokens
- signed receipt tokens
- signed replay session tokens
- CP JWTs
- Gitea token values
- LLM API keys
- `secret_ciphertext`
- raw object-store session logs
- raw private file contents
- raw tool call payloads
- raw stdout/stderr beyond short, scrubbed summaries

Return by default:

- ids
- timestamps
- statuses
- event types
- summaries
- counts
- hashes
- source refs
- redaction classes
- object-store refs only when caller can access the object

## Explicit Gaps Before Agent Evidence DAG V0

### Authorization And Redaction

The graph API must not reuse raw receipt/session response shapes as-is.

Gaps:

- receipt routes return full `signed_token` and payload
- session post response returns full `signed_token` and object key
- global session payload route is keyed by `session_id`
- WorkEvent and SubagentRunEvent payload redaction is not centralized

V0 action:

- add graph-specific serializers that owner-scope and redact by default

### Correlation IDs

Current joins work for many rows, but correlation is uneven.

Gaps:

- deployments do not carry `correlation_id`
- reviews do not carry `correlation_id`
- proofs do not carry `correlation_id`
- code-editor turns do not have first-class `mutation_id`
- source-push job, code-editor result, deployment, review, and proof are joined
  mostly by `head_sha` and timestamps

V0 action:

- infer first, then add stable ids only where missing correlation creates user
  value

### Finding Identity

Review findings are stored as JSON.

Gaps:

- no stable finding id
- no deterministic hash defined
- no explicit fixed-by relationship

V0 action:

- compute deterministic finding hash from severity, file/path, rule, title, and
  normalized message

### Mutation Evidence

Code-editor mutation evidence exists but is not first-class.

Gaps:

- mutation lives in returned payload captured by handoff/work rows
- `push.head_sha` can be inferred but not guaranteed
- stdout/stderr tails need redaction
- Gitea token audit purpose references code-editor but not a mutation id

V0 action:

- project inferred mutation nodes from code-editor handoff/work payloads
- mark confidence and join rule explicitly

### Revocation And Deletion

Grants and credentials have audit rows, but graph revocation is not yet a full
state model.

Gaps:

- `GrantAudit` has no revoked status
- deletion/revocation previews are not persisted
- credential deletes do not emit graph control events
- tombstone and retention state are not represented

V0 action:

- evidence DAG can show current rows and missing revocation gaps
- active revocation tables wait until deletion/revocation implementation

### Policy Decisions

Policies exist as current settings.

Gaps:

- policy checks are not consistently recorded as events
- platform settings have no dedicated audit event beyond updated metadata
- user policy changes are mutable current state

V0 action:

- include current policy state and mark missing historical decisions

### Graph Event Taxonomy

Event sources use different names.

Gaps:

- SubagentRunEvent event types differ from WorkEvent event types
- DeploymentEvent uses stage/status/message
- ReplaySession uses closed SDK replay event kinds
- review/proof/trial store embedded events/payloads

V0 action:

- normalize to DAG node/edge types while preserving raw source refs

## First V0 Projection Order

1. Resolve owned agent by `agent_name` and `user_id`.
2. Load deployments, deployment events, reviews, and proofs by `agent_id` and
   `agent_name`.
3. Load grants by owner and by grant ids discovered from proofs, trials,
   receipts, sessions, DAG nodes, and handoffs.
4. Load handoff runs and events by owner, `agent_name`, `grant_id`, and
   `thread_id`.
5. Load receipts by `agent_id` and `agent_name`; extract grant ids from payload.
6. Load sessions by `agent_id`, `agent_name`, and `receipt_id`.
7. Load trial runs and rooms by owner and `agent_name`.
8. Load work jobs/events by owner, thread, grant/correlation/source/subject refs.
9. Load LLM usage by owner, thread, grant, DAG run, and agent.
10. Infer mutation nodes from code-editor payloads and `push.head_sha`.
11. Build strict edges from exact ids.
12. Build inferred edges from `head_sha`, timestamps, and thread/correlation ids.
13. Attach redaction and confidence metadata to every node and edge.

## Recommendation

P19 establishes that the v0 DAG should be a projection over existing rows:

- `GrantAudit` is the authority source
- `AgentReceipt` and `AgentSession` are signed execution/replay sources
- `SubagentRun/Event` and `WorkJob/Event` are process timelines
- `AgentDeployment/Event`, `AgentReviewRun`, and `AgentProofRun` are version and
  quality sources
- `TrialRun`, `LLMUsageEvent`, control-room, and agent-insights complete the
  evidence picture
- code-editor output provides mutation evidence until a first-class mutation
  record exists

The missing work is not a new ledger. The missing work is normalization,
redaction, stable correlation, finding hashes, and a read-only DAG projection.
