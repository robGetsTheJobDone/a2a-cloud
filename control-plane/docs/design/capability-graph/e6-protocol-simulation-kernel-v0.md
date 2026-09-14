# Dynamic Capability Graph E6 Protocol Simulation Kernel V0

Status: implementation plan and v0 contract.

Date: 2026-06-03

References:

- `docs/protocol-packs.md`
- `docs/simulation-proofs.md`
- `docs/agent-evidence-dag.md`
- `docs/roadmap.md`

## Decision

E6 adds protocol simulation primitives without adding a protocol-pack runtime.

Protocol simulations are `WorkJob` / `WorkEvent` process evidence. They carry
`protocol_ref` and `template_ref` metadata, emit replayable events, and remain
simulation-only and proposal-only. They cannot apply source, manifest, memory,
policy, budget, route, credential, marketplace, or deletion mutations.

## Metadata Contract

`WorkJob.kind = "protocol_simulation"`.

`WorkJob.metadata_json` must include:

- `protocol_ref.id`
- `protocol_ref.version`
- `protocol_ref.display_name`
- `protocol_ref.template_refs`
- `template_ref`
- `simulation_only = true`
- `proposal_only = true`
- `active_apply_enabled = false`
- `kill_switch_available = true`
- `run_budget_cents`
- `budget_ceiling_cents`
- `ttl_seconds`
- `max_episodes`

`WorkEvent.payload` must repeat `active_apply_enabled = false` and should carry
the relevant protocol ref, episode, signal, invariant, score, or proposal ref.

## Implemented Surface

- `POST /v1/agents/{name}/protocol-simulations`
- `GET /v1/agents/{name}/protocol-simulations`
- `GET /v1/agents/{name}/protocol-simulations/{job_id}`
- `POST /v1/agents/{name}/protocol-simulations/{job_id}/events`
- `POST /v1/agents/{name}/protocol-simulations/{job_id}/stop`

Trial rooms are projected into the Agent Evidence DAG as trial quality evidence
so existing competition/evaluation runs become part of the kernel audit surface.

## Non-Goals

- no protocol-pack registry
- no active rewrites
- no autonomous routing
- no memory propagation
- no marketplace settlement
- no certification authority
