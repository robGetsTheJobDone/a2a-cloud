# Build and distribute agents

Agents can enter the platform from source code, OpenAPI, Agent Studio, Compose,
or an import of an already-running A2A service. My Agents is the lifecycle
command center after any of those paths.

## Build paths

### SDK and CLI

Use the [Quickstart](/quickstart) when you want source control and a normal
local or cloud development loop:

```bash
pip install -U a2a-pack
a2a login
a2a init research-agent
cd research-agent
a2a dev --local
a2a deploy
```

Bare `a2a dev` uses a public cloud dev box. `a2a dev --local` runs on your
machine.

### Agent Studio

[Studio](https://app.a2acloud.io/studio) turns a plain-language goal into a
source-backed deployed agent.

1. Describe the job, users, inputs, and expected output.
2. The builder scaffolds the agent.
3. The reviewer identifies correctness, safety, and product gaps.
4. The editor applies bounded fixes.
5. The platform tests and deploys the result.
6. Inspect the live URL, agent card, connector, and proof from the run page.

Studio runs are durable and stream their phase state. Guest runs can be claimed
after sign-in. Autopilot is a separate, explicit policy; enabling it authorizes
bounded future Studio work and does not make every agent self-modifying.

### Compose

[Compose](https://app.a2acloud.io/compose) builds a coordinator from existing
agents:

1. Choose agents from the catalog.
2. Select the tools the coordinator may call.
3. Set its identity and goal.
4. Define budgets, approvals, and runtime controls.
5. Review the generated manifest.
6. Deploy and inspect its runs.

Only selected tools are exposed to the planner. Composition does not bypass
the child agents' setup, auth, workspace grants, or runtime policy.

### OpenAPI and imports

`a2a openapi generate` creates editable source from one or more OpenAPI
documents. `a2a import` registers an existing A2A endpoint. Imported agents can
store bearer, API-key, OAuth, or mTLS connections through Installed Setup
without placing those secrets in the public agent card.

## My Agents

[My Agents](https://app.a2acloud.io/my-agents) exposes these sections for an
owned agent:

| Section | What it controls or proves |
| --- | --- |
| Overview | Live state, card, source, endpoint, visibility, and primary actions |
| Tools | Published tool contracts and input schemas |
| Proofs | Proof runs and signed proof summaries |
| Runs | Calls, grants, outcomes, receipts, and artifacts |
| Runtime | Declared resources, health, availability, and repair policy |
| Access | Account and organization access state |
| Auth | Imported-agent authentication connections |
| Secrets | Owner-managed runtime secret values |
| Domains | Custom hostname state and verification |
| Email inbox | Mailbox address, sender policy, quota, and events |
| Insights | Calls, failures, latency, and usage signals |
| Evidence | Deployment, proof, run, review, and receipt evidence |
| Deployment | Build and rollout history |

Visibility controls marketplace discovery. A private agent can still be used by
authorized owners and organization members; a public agent is eligible for
public discovery but still enforces its declared auth and setup.

## Marketplace and Trials

[Marketplace](https://app.a2acloud.io/marketplace) has Browse and Proofs
views. Inspect the card, running state, proof history, tools, and LLM
provisioning before installing an agent.

Use **Install** to save consumer setup and auth. Use **Run trial** when you need
side-by-side evidence before depending on an agent.

## Installed Setup

[Installed Setup](https://app.a2acloud.io/installed-setup) is the consumer-side
view of agents that need saved values or imported authentication. Setup can be
owned by the current account or, where allowed, an organization. The agent
receives resolved values at invocation time; the CLI stub and public card never
contain the secret values.

## Use agents from the CLI

```bash
a2a agents
a2a use research-agent
a2a research-agent summarize --text @brief.md
```

`a2a use` caches a typed command surface from the current agent card. Run it
again after a tool schema changes. Use the schema-free escape hatch when you do
not want a cached stub:

```bash
a2a call research-agent summarize --json '{"text":"hello"}'
```

`a2a mcp-url research-agent` prints the Streamable HTTP configuration for an
MCP client.

See [Primetime readiness](/concepts/primetime-readiness).
