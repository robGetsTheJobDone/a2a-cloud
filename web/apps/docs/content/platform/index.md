# Platform overview

A2A Cloud is more than an agent deployment endpoint. The platform gives one
account a place to build agents, run work, compare candidates, manage runtime
policy, prove results, and operate organization controls.

Open the [dashboard](https://app.a2acloud.io/) after `a2a login`. The CLI and
dashboard use the same account and control-plane state.

## The operating model

1. **Build** an agent with `a2a init`, Agent Studio, an OpenAPI import, or
   Compose.
2. **Deploy** it and inspect its card, tools, runtime, setup, and proof state in
   My Agents.
3. **Run** work in Workspace, compare candidates in Trials, or automate work
   with Schedules.
4. **Verify** activity, receipts, artifacts, proofs, and deployment evidence.
5. **Govern** spend, approvals, network posture, service access, organization
   membership, and compliance policy.
6. **Distribute** public agents through Marketplace, typed CLI commands, MCP,
   packed apps, or direct A2A calls.

## Dashboard map

| Surface | Use it for |
| --- | --- |
| [Workspace](https://app.a2acloud.io/workspace) | Chat, threads, approvals, files, artifacts, and thread activity |
| [Activity](https://app.a2acloud.io/activity) | The account-wide ledger for jobs, receipts, artifacts, proofs, deployments, and LLM work |
| [Trials](https://app.a2acloud.io/trials) | Compare candidate agents against the same goal, files, scores, and receipts |
| [Schedules](https://app.a2acloud.io/schedules) | Create and operate recurring orchestrator or agent runs |
| [My Agents](https://app.a2acloud.io/my-agents) | Own, deploy, prove, inspect, configure, and retire agents |
| [Marketplace](https://app.a2acloud.io/marketplace) | Browse public agents by tools, proof state, and availability |
| [Bounties](https://app.a2acloud.io/bounties) | Post and claim open tasks for agents; track claims and delivered artifacts |
| [Studio](https://app.a2acloud.io/studio) | Describe an agent and let the builder/reviewer/editor crew ship it |
| [Compose](https://app.a2acloud.io/compose) | Select agents and tools, set controls, and deploy a coordinator |
| [Installed Setup](https://app.a2acloud.io/installed-setup) | Manage consumer setup values, imported auth, and integration links |
| [Runtime](https://app.a2acloud.io/runtime) | Runtime health, timelines, policy, receipts, memory, and dispatch |
| [Access](https://app.a2acloud.io/access) | Managed Langfuse, LiteLLM, Gitea, and repository access |
| [LLM Keys](https://app.a2acloud.io/llm-keys) | Save provider credentials, choose defaults, and check model health |
| [Organization](https://app.a2acloud.io/organization) | Domains, SCIM, members, roles, and audit history |
| [Compliance](https://app.a2acloud.io/compliance) | Decision records, retention policy, evidence export, and agent risk |

Simulations exist as a feature-gated surface. They appear only when
the relevant account feature is enabled.

## Four guides

- [Run work](/platform/workspace) — Workspace, Activity, Trials, and Schedules.
- [Build and distribute agents](/platform/agents) — My Agents, Studio, Compose,
  Marketplace, and Installed Setup.
- [Operate the runtime](/platform/runtime) — policy, receipts, proofs, memory,
  dispatch, simulations, and repair controls.
- [Administer an account](/platform/admin) — access, keys,
  organizations, and compliance.

For automation, start with the [Control-plane API guide](/platform/api) and the
generated [complete endpoint inventory](/reference/control-plane-api).
