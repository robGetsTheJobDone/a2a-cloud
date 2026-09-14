# Run work

Workspace is the execution cockpit for a person, the main orchestrator, and any
agents it delegates to. Trials and Schedules reuse the same account identity,
workspace grants, policy controls, receipts, and activity ledger.

## Workspace

Open [Workspace](https://app.a2acloud.io/workspace). Its views are:

- **Chat** — talk to the main agent, stream work, answer approvals, and inspect
  the active thread.
- **Threads** — search, reopen, and manage saved conversations.
- **Settings** — set thread budgets, policy gates, and approved-agent
  overrides.
- **Files** — upload, browse, preview, move, and delete workspace files.
- **Artifacts** — scan generated outputs and download deliverables.
- **Activity** — inspect thread-scoped handoffs, DAG runs, file operations,
  receipts, and LLM usage.

Files are not ambient shared storage. Agents receive scoped, signed workspace
grants. A tool can read or write only the paths and prefixes in its active
grant. When an agent requests wider access, the run pauses for an approval
instead of silently expanding authority.

## Account-wide Activity

[Activity](https://app.a2acloud.io/activity) is the global work ledger. Use it
when you know a job, proof, deployment, or run exists but do not remember which
thread or agent produced it.

Each job can expose:

- state and timing
- event timeline
- receipts and authority
- files and artifacts
- downstream agent handoffs
- proof or deployment linkage
- LLM usage recorded for the work

A runtime execution receipt is evidence about a run. Usage metrics are
separate records; do not treat a runtime receipt as an accounting record.

## Trials

[Trials](https://app.a2acloud.io/trials) compare multiple candidate agents
against the same job.

1. Create a room and define the goal.
2. Attach the shared input files.
3. Select candidate agents.
4. Run the room.
5. Compare results, scores, artifacts, and receipts.
6. Rate or select the result you want to keep.

A trial room keeps the shared brief and candidate history together. Each run
still has its own grants, output, receipt, and status.

The API family is `/v1/me/trial-rooms`; see
[trial-rooms](/reference/control-plane-api#trial-rooms).

## Schedules

[Schedules](https://app.a2acloud.io/schedules) run the main orchestrator or a
selected agent on a saved cadence.

1. Choose the target and tool.
2. Save the arguments and optional workspace inputs.
3. Set the schedule and enable it.
4. Inspect the run history and next-run time.
5. Use **Run now** to test the saved configuration without waiting.

Schedules execute with the account and policy state that owns the schedule.
Removing an agent, required setup, or access can make later scheduled runs
fail; the Activity ledger preserves that failure instead of dropping it.

The API family is `/v1/me/schedules`; see
[schedules](/reference/control-plane-api#schedules).

## Programmatic workspace access

Agents use `ctx.workspace`, `ctx.write_artifact(...)`, sandbox workspace
mounts, and `ctx.workspace_backend()` for durable outputs. External automation
uses workspace-grant and file APIs rather than direct object-store
credentials.

See [Grants](/concepts/grants), [Workspace API](/reference/workspace), and the
[control-plane API guide](/platform/api).
