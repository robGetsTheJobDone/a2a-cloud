# ADR: Agent Studio Build-Review-Improve Loop

Date: 2026-06-01

Status: accepted for P1 implementation

## Context

Agent Studio is the practical near-term meta-agent for shipping deployed agents
with quality gates. It is separate from the broader synthetic/composable
meta-agent work. This ADR covers a concrete product loop over deployed agent
rails that already exist:

- `agent-builder.build` creates a new managed A2A agent from a natural-language
  prompt, writes source under `agents/<name>/`, runs `test_agent_in_sandbox`,
  deploys through `/v1/agents/from-tarball`, waits for a live Agent Card, and
  returns `{ok, name, version, url, workspace_dir, reply}` when successful.
- `agent-reviewer.review` performs read-only source review for an owned managed
  repo. It requires `ctx.cp_jwt`, mints a short-lived read-scoped Gitea token,
  and returns structured findings with critical/warning/info severity counts.
- `code-editor-agent.turn` runs in shared mode against an opted-in managed repo.
  It requires `ctx.cp_jwt`, checks `/v1/agents/{name}/code-editor`, mints a
  short-lived write-scoped Gitea token, refreshes CodeGraph, runs OpenHarness,
  commits/pushes source changes, and releases the token in `finally`.
- The control plane already exposes code-editor opt-in, cached/live Agent Card
  refresh, deployment status, and scoped Gitea token routes.
- Source pushes already trigger the managed deployment pipeline through Gitea
  webhooks and runtime repo restamping.

## Decision

Build Agent Studio as a new first-party specialist agent named `agent-studio`.
Its MVP is a deterministic coordinator with LLM-assisted brief/rubric
generation. It calls existing specialists through the platform handoff path and
records every handoff, decision, test result, and mutation in an iteration
ledger.

Do not import private implementation modules from `agent-builder`,
`agent-reviewer`, or `code-editor-agent`. Agent Studio interacts with them as
A2A agents through the same control surface a user-visible orchestrator uses.

Do not make this MVP a synthetic-agent runtime, a general self-improvement
loop, or an unbounded recursive agent creator. It builds and improves one
target managed deployed agent per run.

## Primary Skill

Expose one streamed skill:

```python
create_agent(
    name: str,
    goal: str,
    public: bool = False,
    max_iterations: int = 3,
    quality_bar: Literal["standard", "high"] = "standard",
    max_runtime_seconds: int = 3600,
    max_child_calls: int = 12,
    max_spend_cents: int | None = None,
) -> AgentStudioReport
```

Input rules:

- `name` is kebab-case and must pass the same slug restrictions as
  `agent-builder.build`.
- `goal` is the user request plus product expectations.
- `public` defaults to `False`; public publishing requires explicit user input.
- `max_iterations` is clamped to `0..5` for MVP, with default `3`.
- `quality_bar` controls smoke-test breadth and reviewer strictness, not
  permission scope.
- Runtime, child-call, and spend budgets are hard stops.

Output shape:

```json
{
  "ok": true,
  "status": "succeeded|partial|failed|blocked",
  "agent_name": "invoice-helper",
  "agent_url": "https://invoice-helper.a2acloud.io",
  "public": false,
  "version": "0.1.3",
  "head_sha": "optional-source-sha",
  "deployment_id": "optional-deploy-id",
  "live_card": {
    "name": "invoice-helper",
    "version": "0.1.3",
    "skills": []
  },
  "iterations": [],
  "tests": [],
  "review": {
    "status": "passed|warnings|critical|skipped",
    "critical_count": 0,
    "warning_count": 0,
    "findings": []
  },
  "residual_risks": [],
  "ledger_path": "agents/invoice-helper/.agent-studio/report.json",
  "publish_next_step": "already-private"
}
```

The returned report and persisted ledger must never include CP JWTs, Gitea
tokens, grant tokens, LLM API keys, raw secret values, full stdout/stderr beyond
bounded tails, or unredacted user-private payloads.

## Coordinator State Machine

P1 should implement the coordinator as an explicit state machine. P3 and P5
wire real handoffs later.

States:

1. `plan`: validate inputs, derive a build brief, set budgets, initialize the
   ledger, and emit a run id.
2. `build`: call `agent-builder.build`.
3. `load_live_state`: fetch stored agent details, latest deployment, code-editor
   status, and Agent Card.
4. `evaluate`: run deterministic schema/card/smoke checks.
5. `review`: call `agent-reviewer.review` when source is available.
6. `patch`: enable code-editor if needed and call `code-editor-agent.turn` with
   concrete failures and reviewer findings.
7. `refresh`: wait for deployment/webhook propagation, refresh/check card
   freshness, and compare version/head/card expectations.
8. `decide`: stop or continue based on gates.
9. `report`: persist and return the final report for success or partial failure.

Stop conditions:

- all critical acceptance checks pass
- `max_iterations` reached
- reviewer reports unresolved critical findings
- code-editor fails or times out
- live Agent Card does not match expected skill schema after refresh
- budget, runtime, or child-call ceiling is reached
- human approval is required for publish, force overwrite, or accepted risk

## Specialist Handoff Contracts

### Build

Agent: `agent-builder`

Skill: `build`

Arguments:

```json
{
  "name": "<target-name>",
  "prompt": "<build brief>",
  "public": false,
  "version": "0.1.0"
}
```

Required context forwarded by the platform:

- workspace grant
- `ctx.llm`
- `ctx.cp_jwt`

Agent Studio treats any response with `error`, `ok: false`, or no URL as
partial failure and returns a report with build progress and next actions.

### Review

Agent: `agent-reviewer`

Skill: `review`

Arguments:

```json
{
  "agent_name": "<target-name>",
  "ref": "<head-sha-or-main>",
  "owner": "<optional-gitea-owner>"
}
```

Critical findings block success. Warnings become patch requirements when they
map to the build brief, safety gates, skill schemas, grants, or user-facing
behavior.

### Edit

Agent: `code-editor-agent`

Skill: `turn`

Arguments:

```json
{
  "agent_name": "<target-name>",
  "prompt": "<patch brief>",
  "ref": "main",
  "owner": "<optional-gitea-owner>",
  "session_name": "agent-studio-<run-id>",
  "continue_session": true,
  "max_turns": 12,
  "permission_mode": "full_auto",
  "output_format": "json",
  "dry_run": false,
  "timeout_seconds": 1800,
  "push_on_failure": false
}
```

Patch brief requirements:

- reference exact smoke failures and reviewer findings
- include acceptance criteria and expected skill schema changes
- require a version bump for each real iteration
- require narrow edits only
- forbid unrelated churn
- keep `push_on_failure=False`

Dry-run may be used for diagnostics, but a dry-run never counts as a mutation
iteration.

## Control-Plane Helper Contracts

Agent Studio needs a small CP helper client. P2 can implement it inside the
agent, using the forwarded CP JWT server-side.

Required calls:

- `GET /v1/agents/{name}` - read stored agent detail and cached card without
  waking hosted agents.
- `GET /v1/agents/{name}?refresh=true` - explicit owner refresh when Agent
  Studio needs to verify a just-deployed live card.
- `GET /v1/agents/{name}/deployments` - read latest deployment status, events,
  `head_sha`, image, URL, and verification.
- `GET /v1/agents/{name}/code-editor` - inspect opt-in state.
- `POST /v1/agents/{name}/code-editor` - enable code-editor for the owned
  managed source repo when absent.
- `POST /v1/platform/gitea-token` - only through existing `ctx.mint_gitea_token`
  helpers in specialist agents; Agent Studio should not directly expose token
  secrets.

The helper client must return structured errors and redacted summaries that are
safe to persist in the ledger.

## Evaluation Harness

The MVP smoke harness should be deterministic and bounded. It should not run an
open-ended LLM loop to decide whether the target works.

Minimum checks:

- live Agent Card is present
- card name/version/URL match the expected target
- required skills from the build brief exist
- required input schemas are compatible with generated smoke inputs
- each smoke call uses bounded timeouts and small payloads
- output is classified as `pass`, `warning`, or `fail`

Smoke checks feed the next patch brief. Oversized responses are summarized with
bounded previews.

## Iteration Ledger

Persist a JSON ledger at:

`agents/<name>/.agent-studio/report.json`

The ledger is append-oriented and should contain:

- run id, target name, owner, requested goal, quality bar, budgets
- generated build brief and acceptance checks
- every specialist handoff: agent, skill, args summary, started/completed,
  status, grant ids where safe, thread ids where safe, and result summary
- deployment observations: deployment id, status, head sha, image, URL, events
- evaluation results and smoke inputs/outputs with redaction
- reviewer summary and finding hashes
- code-editor turn result: `sync.head_sha`, `push.head_sha`, changed file list,
  exit code, timeout status, and bounded stdout/stderr tails
- final report and residual risks

Do not persist secrets or token-like values. Add a redaction pass before both
artifact write and returned report.

## Safety Gates

Hard MVP gates:

- private by default
- max iterations required
- max runtime and max child calls required
- no `push_on_failure=True`
- no force deploy over drift
- no public publish without explicit user approval
- reviewer critical findings block success
- code-editor can only target the generated/owned agent unless explicitly
  allowlisted later
- each real edit iteration requires a version bump or an equivalent freshness
  signal before success
- live-card mismatch blocks success
- report and ledger redaction are mandatory

Agent Studio may return `partial` with a useful report. It should not hide
failures behind a green success state.

## Daily Review and Approved Upgrades

Daily review is an explicit per-user opt-in. The schedule worker reviews only
owned, managed-source agents and invokes `agent-reviewer.review` in read-only
`improvements` mode. Findings become durable upgrade proposals and are emailed
as a digest; no source write capability is minted during discovery.

Email actions open an authenticated confirmation screen. Accepting a proposal
queues the durable `agent-studio.upgrade_agent` flow; rejecting it is terminal.
The upgrade flow refuses to mutate when the reviewed source SHA is stale, keeps
`push_on_failure=False`, waits for the exact pushed deployment to become live,
and performs a post-upgrade review. Proposals expire after a bounded TTL and
every decision, report, changed-file list, and failure is stored on the
proposal row.

## MVP Test Plan

P1/P2 unit tests:

- input validation and budget clamps
- state machine happy path with mocked specialists
- budget stop and max-iteration stop
- report/ledger redaction
- CP helper missing-token and non-owned-agent errors

P3/P5 integration tests with mocked handoffs:

- builder success returns URL/card
- builder failure returns partial report
- reviewer critical finding blocks success
- code-editor opt-in failure blocks patching
- code-editor failure returns partial report
- live-card mismatch blocks success
- successful edit records pushed head sha and waits for deployment/card refresh

P8 e2e-style test:

- build -> evaluate -> review -> edit -> refresh -> report using mocked
  specialist agents and fake CP endpoints
- assert exact specialist call arguments, especially `public=False` and
  `push_on_failure=False`

## Open Questions Before Public Launch

- Should Agent Studio expose a dashboard-only publish approval flow, or a
  separate `publish_agent` skill?
- Should the iteration ledger move from workspace JSON to a first-class control
  plane table after MVP?
- Should reviewer findings get stable finding ids from `agent-reviewer`, or
  should Agent Studio hash finding content in v0?
- What quality bar should require proof runs in addition to smoke calls?
- Should Agent Studio be allowed to iterate first-party platform agents, or only
  user-created agents, in the initial launch?
