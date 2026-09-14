# Primetime readiness

Production agent systems need more than a live endpoint. A2A Cloud packages the
runtime controls that make an agent usable inside a company: scoped authority,
proof, receipts, governance, and activation tracking.

## What readiness means

An agent is ready for production review when the platform can answer these
questions:

- What Agent Card and version is live?
- What source, deployment, and runtime produced the current endpoint?
- What proof has run against that version, and did it pass?
- What authority was granted to the run?
- What files, artifacts, costs, and downstream calls did the run create?
- Which organization controls own access to the agent and connected services?

## Control surfaces

The dashboard gives operators these surfaces:

- **Agent inventory** for live status, deployments, Agent Cards, source links,
  and proof actions.
- **Marketplace proof summaries** for latest badge plus aggregate pass counts.
- **Control Room** for spend caps, approval gates, network policy, receipts, and
  recent activity.
- **Organization governance** for verified domains, SCIM tokens, member state,
  and audit logs.
- **Service access** for managed Gitea, Langfuse, and LiteLLM connection state.

## Evidence path

The strongest demo path is linear:

1. Deploy or import an agent.
2. Inspect the live Agent Card.
3. Run a proof against a named tool.
4. Call the agent through a scoped grant.
5. Open the Control Room receipt.
6. Check organization audit and service access state.

The result is an evidence chain that connects deployment, card, proof, grant,
run output, and governance state.

## Activation path

Onboarding records setup milestones for product analytics:

- onboarding row created
- LLM key setup completed
- walkthrough started
- latest walkthrough step and step count
- walkthrough completed

These fields make it possible to distinguish users who landed, users who became
able to run agents, and users who completed the guided path.
