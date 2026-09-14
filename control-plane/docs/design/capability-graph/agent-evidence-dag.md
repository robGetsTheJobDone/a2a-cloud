# Agent Evidence DAG Projection

Status: schema design, not implementation.

Date: 2026-06-02

Parent framing:

- `docs/roadmap.md`
- `docs/storage-query.md`
- `docs/evidence-inventory.md`

## Decision

The first concrete graph product is a read-only Agent Evidence DAG projection
over existing control-plane rows and SDK evidence.

The DAG should explain, for one owned or visible agent:

- what happened
- which authority was used
- which version was involved
- what changed
- what reviews, proofs, receipts, sessions, and work events support or
  contradict current trust
- where correlation is strict and where it is inferred

The projection is not a new source of truth. Every node and edge points back to
source rows or signed SDK artifacts.

## Response Shape

Suggested v0 envelope:

```json
{
  "schema_version": 1,
  "agent": {
    "agent_id": 123,
    "name": "example-agent",
    "owner_user_id": 456,
    "public": false
  },
  "query": {
    "since": null,
    "until": null,
    "limit": 250,
    "include_payloads": false
  },
  "watermark": {
    "computed_at": "2026-06-02T00:00:00Z",
    "max_source_timestamp": "2026-06-02T00:00:00Z",
    "source_families": [
      "agent",
      "grant_audit",
      "deployments",
      "reviews",
      "proofs",
      "handoffs",
      "receipts",
      "sessions",
      "work_ledger",
      "llm_usage"
    ]
  },
  "nodes": [],
  "edges": [],
  "warnings": []
}
```

## Node Schema

Every node:

```json
{
  "id": "deployment:dpl_abc123",
  "type": "deployment",
  "label": "Deploy example-agent",
  "status": "live",
  "summary": "Agent is live",
  "time": "2026-06-02T00:00:00Z",
  "sort_key": "2026-06-02T00:00:00Z:deployment:dpl_abc123",
  "provenance": [],
  "refs": {},
  "redaction": {
    "class": "owner",
    "payload_included": false,
    "payload_ref": null
  },
  "confidence": "strict",
  "metadata": {}
}
```

Required fields:

- `id`: deterministic graph id
- `type`: one normalized node type
- `label`: short human-readable label
- `status`: normalized status when available
- `summary`: redacted summary
- `time`: primary timestamp
- `sort_key`: stable acyclic ordering key
- `provenance`: source row/artifact refs
- `refs`: join keys and external refs
- `redaction`: exposure metadata
- `confidence`: `strict`, `inferred`, or `partial`
- `metadata`: redacted structured details

## Edge Schema

Every edge:

```json
{
  "id": "edge:deployment:dpl_abc123:reviewed_by:review:r_123",
  "type": "reviewed_by",
  "from": "deployment:dpl_abc123",
  "to": "review:r_123",
  "time": "2026-06-02T00:00:00Z",
  "provenance": [],
  "join_rule": "agent_review_runs.deploy_id == agent_deployments.deploy_id",
  "confidence": "strict",
  "redaction": {
    "class": "owner",
    "payload_included": false
  },
  "metadata": {}
}
```

Required fields:

- `id`: deterministic edge id
- `type`: normalized edge type
- `from`: source node id
- `to`: target node id
- `time`: timestamp for ordering and timeline view
- `provenance`: source row/artifact refs
- `join_rule`: exact rule that created the edge
- `confidence`: `strict`, `inferred`, or `partial`
- `redaction`: exposure metadata
- `metadata`: redacted structured details

## Provenance Ref Schema

Every node and edge must include at least one provenance ref unless it is a
synthetic grouping node.

```json
{
  "source": "agent_deployments",
  "row_id": 42,
  "stable_id": "dpl_abc123",
  "timestamp": "2026-06-02T00:00:00Z",
  "field_refs": ["deploy_id", "head_sha", "image"],
  "object_ref": null
}
```

Allowed `source` values for v0:

- `agents`
- `grant_audit`
- `agent_receipts`
- `agent_sessions`
- `subagent_runs`
- `subagent_run_events`
- `agent_deployments`
- `agent_deployment_events`
- `agent_review_runs`
- `agent_proof_runs`
- `trial_rooms`
- `trial_runs`
- `work_jobs`
- `work_events`
- `llm_usage_events`
- `sdk_grant`
- `sdk_receipt`
- `sdk_replay_session`
- `code_editor_result`
- `object_store`

## Node Types

### Agent Node

Id:

- `agent:{agent_id}`

Source:

- `Agent`

Fields:

- agent id
- name
- owner
- public/private
- status
- current card hash if available
- current image/source refs if available

### Version Nodes

Ids:

- `source:{owner}/{repo}@{head_sha}`
- `image:{image_ref}`
- `card:{agent_name}@{card_hash}`
- `ref:{agent_name}@{ref}`

Sources:

- deployments
- proofs
- code-editor results
- Agent Card cache

Rules:

- source SHA is strongest
- image ref is useful but should later prefer immutable digest
- card hash represents declared behavior, not source identity
- `ref` is a weak version node when SHA is absent

### Grant Node

Id:

- `grant:{grant_id}`

Source:

- `GrantAudit`

Fields:

- grant id
- parent grant id
- issuer
- audience
- bucket
- mode
- allow/deny summary
- TTL
- decision
- decided by
- reason
- created time

Redaction:

- never include signed grant token

### Handoff Run Node

Id:

- `handoff:{grant_id}`

Source:

- `SubagentRun`

Fields:

- grant id
- rerun grant id
- thread id
- agent name
- skill name
- status
- summary
- file op count
- scope summary
- timestamps

Redaction:

- summarize `args_json`
- include file metadata, not file bytes

### Handoff Event Node

Id:

- `handoff_event:{subagent_run_event_id}`

Source:

- `SubagentRunEvent`

Fields:

- event type
- status when present
- message/summary when present
- created time

Redaction:

- payload is summarized by event type

### Receipt Node

Id:

- `receipt:{receipt_id}`

Source:

- `AgentReceipt`
- SDK `ExecutionReceipt`

Fields:

- receipt id
- agent
- agent version
- caller
- task id
- skill
- status
- eval score
- timing
- grant id count
- file/tool/artifact/handoff counts

Redaction:

- no signed token by default
- no raw payload by default

### Replay Session Node

Id:

- `session:{session_id}`

Source:

- `AgentSession`
- SDK `ReplaySession`
- object-store event log ref

Fields:

- session id
- receipt id
- agent
- caller
- task id
- skill
- event count
- timing

Redaction:

- object-store events are referenced, not embedded, unless explicitly requested
  through an authorized forensic endpoint

### Deployment Node

Id:

- `deployment:{deploy_id}`

Source:

- `AgentDeployment`

Fields:

- deploy id
- trigger
- status
- source repo URL host/path summary
- head SHA
- image
- agent URL
- verification summary
- error summary
- timestamps

### Deployment Event Node

Id:

- `deployment_event:{deployment_event_id}`

Source:

- `AgentDeploymentEvent`

Fields:

- deploy id
- stage
- status
- message
- created time

Redaction:

- data payload summarized

### Review Node

Id:

- `review:{review_id}`

Source:

- `AgentReviewRun`

Fields:

- review id
- deploy id
- agent
- ref
- status
- summary
- critical/warning/info counts
- elapsed
- timestamps

### Finding Node

Id:

- `finding:{review_id}:{finding_hash}`

Source:

- `AgentReviewRun.findings`

Fields:

- deterministic finding hash
- severity
- rule/title/path summary when present
- normalized message summary
- status inferred from later evidence when possible

Hash input:

- severity
- rule/id if present
- file/path if present
- title if present
- normalized message

### Proof Node

Id:

- `proof:{proof_run_id}`

Source:

- `AgentProofRun`

Fields:

- proof run id
- grant id
- agent
- skill
- status
- summary/error
- card hash
- head SHA
- image
- agent URL
- elapsed
- event/file op counts
- timestamps

### Trial Node

Id:

- `trial:{trial_run_id}`

Source:

- `TrialRun`
- `TrialRoom`

Fields:

- trial room slug/title
- agent
- skill
- status
- score
- summary/error
- grant id
- event/file op counts
- timestamps

### Work Job Node

Id:

- `work_job:{job_id}`

Source:

- `WorkJob`

Fields:

- job id
- kind
- status
- title
- summary/error
- thread id
- root/parent/correlation ids
- source/subject/worker refs
- heartbeat/lease
- timestamps

### Work Event Node

Id:

- `work_event:{event_id}`

Source:

- `WorkEvent`

Fields:

- event id
- job id
- event seq
- parent event id
- correlation id
- event type
- stage/status/severity/message
- actor/source refs
- created time

### LLM Usage Node

Id:

- `llm_usage:{llm_usage_event_id}`

Source:

- `LLMUsageEvent`

Fields:

- source
- provider summary
- model
- prompt/completion/total tokens
- cost
- grant/thread/DAG/agent/skill refs
- status from metadata
- created time

### Mutation Node

Id:

- `mutation:code_editor:{head_sha_or_hash}`

Source:

- code-editor returned payload captured in handoff/work evidence

Fields:

- agent
- owner/repo
- ref
- base head SHA
- produced head SHA
- exit code
- timed out
- dry run
- change status summary
- push status
- codegraph status
- OpenHarness metadata summary

Confidence:

- `strict` when a captured payload has `push.head_sha`
- `inferred` when mutation is inferred from timestamps or source-push deploy

Redaction:

- stdout/stderr tails are excluded by default

## Edge Types

### Identity And Version

| Edge | From | To | Strict join |
|---|---|---|---|
| `has_deployment` | agent | deployment | `AgentDeployment.agent_id` or owned `agent_name` |
| `deploys_version` | deployment | source/image/card version | `head_sha`, image, card hash |
| `produces_version` | mutation | source version | `code_editor.push.head_sha` |
| `tests_version` | proof | source/card/image version | `head_sha`, `card_hash`, image |
| `reviews_deployment` | review | deployment | `AgentReviewRun.deploy_id` |
| `reviewed_by` | deployment | review | `AgentReviewRun.deploy_id` |

### Authority

| Edge | From | To | Strict join |
|---|---|---|---|
| `parent_of` | parent grant | child grant | `GrantAudit.parent_grant_id` |
| `authorizes_handoff` | grant | handoff | `grant_id` |
| `authorizes_proof` | grant | proof | `grant_id` |
| `authorizes_trial` | grant | trial | `grant_id` |
| `used_by_receipt` | grant | receipt | `receipt.payload.grant_ids` |
| `attributes_cost` | grant | llm usage | `grant_id` |

### Execution Timeline

| Edge | From | To | Strict join |
|---|---|---|---|
| `emits_event` | handoff | handoff event | `SubagentRunEvent.run_id` |
| `has_event` | deployment | deployment event | `deployment_id` |
| `has_event` | work job | work event | `job_id` |
| `has_replay_session` | receipt | session | `receipt_id` |
| `mirrored_to` | handoff | work job | `source_id`, `grant_id`, or correlation |

### Quality And Trust

| Edge | From | To | Strict join |
|---|---|---|---|
| `has_finding` | review | finding | embedded finding JSON |
| `supports` | proof | version or agent claim | passing proof |
| `contradicts` | proof | version or agent claim | failed proof |
| `supports` | receipt | skill claim | ok receipt |
| `contradicts` | receipt | skill claim | error/cancelled receipt |
| `supports` | review | version | passed review |
| `contradicts` | review/finding | version | warning/critical finding |

### Process And Correlation

| Edge | From | To | Strict join |
|---|---|---|---|
| `parent_job_of` | work job | work job | `parent_job_id` |
| `root_job_of` | work job | work job | `root_job_id` |
| `correlates_with` | any evidence node | work job/event | `correlation_id`, source/subject refs |
| `triggered_deployment` | mutation/source-push job | deployment | produced `head_sha` equals deployment `head_sha` |
| `followed_by` | evidence node | evidence node | timestamp order within same version/thread |

## DAG Constraints

### Acyclic Projection

The v0 output is a DAG even if the real graph has cyclic relationships.

Rules:

- edges point from earlier evidence to later evidence where possible
- version support edges point from evidence to version or claim
- parent grant edges point from parent to child
- if a relationship would create a cycle, represent it as a timeline edge or
  omit it with a warning

### Strict Vs Inferred Edges

Strict:

- exact id equality or foreign-key-like join

Inferred:

- timestamp window
- matching `head_sha`
- matching thread id plus agent/skill
- matching code-editor payload and source-push deployment

Partial:

- source row exists, but target row is missing or redacted

Every inferred edge must include:

- join rule
- confidence
- source refs
- warning when confidence is low

### Deterministic IDs

Node ids and edge ids must be deterministic from source ids.

Do not use random ids in projection output.

### Payload Minimization

The graph duplicates summaries, not raw payloads.

Raw payloads remain in their source tables or object store.

## Redaction Rules

### Public View

Allowed:

- public agent metadata
- public proof summary when already exposed
- public review summary if product policy allows
- source/image/card hashes

Not allowed:

- private receipts
- private sessions
- private grants
- private run args/results
- private file ops beyond counts

### Owner View

Allowed:

- owned timelines
- grant summaries
- proof/review details
- redacted receipt/session summaries
- file op metadata
- cost summaries

Not allowed by default:

- signed tokens
- raw session logs
- raw private files
- raw tool call payloads
- raw stdout/stderr
- credentials

### Operator View

Allowed:

- redacted diagnostics
- policy and control refs
- source row refs
- alert and revocation summaries

Not allowed by default:

- cross-tenant private payloads
- secrets
- signed tokens
- credentials

## Build Algorithm

For `GET /v1/agents/{name}/evidence-dag`:

1. authorize caller against owned or visible agent
2. create agent node
3. load deployments and deployment events
4. create deployment and version nodes
5. load reviews by deploy id, agent id, and agent name
6. create review and finding nodes
7. load proofs by agent id/name and owner
8. create proof nodes and version edges
9. load handoff runs/events by agent name, owner, thread, and grant ids
10. create handoff nodes and event edges
11. load receipts by agent id/name
12. create receipt nodes and grant-use edges
13. load sessions by agent id/name and receipt ids
14. create session nodes and receipt-session edges
15. load grant audit rows discovered from proofs, handoffs, trials, receipts,
    LLM usage, and DAG nodes
16. create grant nodes and authority edges
17. load work jobs/events by owner, source, subject, thread, grant, and
    correlation refs
18. create work nodes and process/correlation edges
19. load trial and LLM usage evidence
20. infer mutation nodes from code-editor payloads and `push.head_sha`
21. add strict edges first
22. add inferred edges second with confidence metadata
23. topologically sort by timestamp/version/source id
24. attach warnings for missing sources, redactions, and inferred joins

## Worked Example

Scenario:

- user deploys `example-agent`
- reviewer flags a critical finding
- user runs code editor
- code editor pushes commit `abc123`
- source-push deploys that commit
- reviewer and proof later pass

Nodes:

```text
agent:42
deployment:dpl_old
source:owner/example-agent@oldsha
review:rv_old
finding:rv_old:h1
grant:g_editor
handoff:g_editor
mutation:code_editor:abc123
source:owner/example-agent@abc123
work_job:source_push_1
deployment:dpl_new
review:rv_new
proof:99
card:example-agent@cardhash
```

Edges:

```text
agent:42 has_deployment deployment:dpl_old
deployment:dpl_old deploys_version source:owner/example-agent@oldsha
deployment:dpl_old reviewed_by review:rv_old
review:rv_old has_finding finding:rv_old:h1
grant:g_editor authorizes_handoff handoff:g_editor
handoff:g_editor correlates_with mutation:code_editor:abc123
mutation:code_editor:abc123 produces_version source:owner/example-agent@abc123
mutation:code_editor:abc123 triggered_deployment deployment:dpl_new
agent:42 has_deployment deployment:dpl_new
deployment:dpl_new deploys_version source:owner/example-agent@abc123
deployment:dpl_new reviewed_by review:rv_new
proof:99 tests_version source:owner/example-agent@abc123
proof:99 tests_version card:example-agent@cardhash
review:rv_new supports source:owner/example-agent@abc123
proof:99 supports source:owner/example-agent@abc123
```

Warnings:

```text
finding:rv_old:h1 fixed_by edge is inferred until review finding hashes are
stored and code-editor turns record target finding ids.
```

## Validation Rules

Projection tests should assert:

- every node id is unique
- every edge id is unique
- every edge endpoint exists
- every node has provenance or explicit synthetic marker
- every edge has provenance and join rule
- every redacted payload has redaction metadata
- no signed token or secret appears in JSON output
- strict edges use exact ids
- inferred edges carry confidence and warning when appropriate
- topological ordering is stable
- missing source rows produce warnings, not crashes

## Recommendation

The P20 schema should be the contract for v0 implementation:

- normalize existing rows into graph-shaped evidence
- preserve source-of-truth refs
- use deterministic node and edge ids
- distinguish strict from inferred joins
- redact by default
- keep the output acyclic and explainable

The DAG is an explanation layer over current evidence, not a new authority
system.
