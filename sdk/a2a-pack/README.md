# a2a-pack

**Developer SDK + CLI for building, packaging, and deploying full A2A compliant agents.**

One Python class becomes a sandboxed, discoverable, MCP-compatible,
full A2A compliant AI agent on the [a2a cloud](https://a2acloud.io)
platform. Other agents reach yours via Ed25519-signed grants. The platform
owns deployment, execution, and permissions.

a2a-pack makes [Google's Agent2Agent protocol](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
easy: Agent Cards, tasks, messages, artifacts, file/data exchange, streaming,
auth, JSON-RPC, REST, and protocol errors are handled by the SDK runtime.

```bash
pip install a2a-pack
a2a login
a2a init research-agent
cd research-agent
a2a dev
a2a test --invoke
a2a deploy
# -> https://research-agent.a2acloud.io   (A2A, MCP, TLS, all wired)
```

## What an agent looks like

```python
from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import A2AAgent, LLMProvisioning, NoAuth, RunContext


class GreeterConfig(BaseModel):
    suffix: str = "!"


class Greeter(A2AAgent[GreeterConfig, NoAuth]):
    name = "greeter"
    description = "Say hi."
    version = "0.1.0"
    config_model = GreeterConfig
    auth_model = NoAuth

    # Hosted generated agents use the caller's saved LLM credential. The
    # platform routes it through LiteLLM and exposes it via ctx.llm at invoke time.
    llm_provisioning = LLMProvisioning.PLATFORM

    @a2a.tool(description="Greet someone.")
    async def greet(self, ctx: RunContext[NoAuth], who: str) -> str:
        await ctx.emit_progress(f"greeting {who}")
        return f"hello {who}{self.config.suffix}"
```

`@a2a.tool` marks the agent's typed callables. They are published in the agent
card's `skills` array (the A2A spec's vocabulary), and `@skill` remains a
permanent alias. The namespaced form avoids colliding with other `@tool`
decorators (e.g. LangChain's); both also work as bare imports
(`from a2a_pack import tool, skill`).

That's it. `a2a deploy` packages the source, the control plane builds
the image, ArgoCD reconciles, you get a full A2A compliant public URL.

## Pointing the CLI at your own platform

The SDK ships with one hosted default (`a2acloud.io`, in `a2a_pack/cli/platform.py`). Everything else is
derived from the control plane you log into, so a self-hosted platform needs no code changes:

| Variable | Effect |
|---|---|
| `A2A_API_URL` | Control-plane base URL (also saved by `a2a login --api ...`). Docs, dashboard, registry, and agent URLs derive from its host (`api.example.com` → `example.com`). |
| `A2A_PLATFORM_DOMAIN` | Force the apex domain instead of deriving it. |
| `A2A_DOCS_URL`, `A2A_DASHBOARD_URL`, `A2A_REGISTRY_HOST` | Override individual hosts. |
| `A2A_OAUTH_ISSUER`, `A2A_OAUTH_REALM`, `A2A_OAUTH_CLIENT_ID` | OIDC issuer/realm/client for `a2a login`. |

URLs reported by the control plane (deployed agent URLs, setup links) always win over derived values.

## Local development

Use the same agent card, invoke path, secret names, and workspace contract
before uploading anything:

```bash
a2a dev --local
```

That starts the agent at `http://127.0.0.1:8000`, loads `.env.local`, creates
`.a2a/workspace/{inputs,outputs}`, and enables hot reload. Tools are callable at
`POST /invoke/{skill}` and the card is visible at
`/.well-known/agent-card`.

It also serves a bundled local console at `http://127.0.0.1:8000/_dev` with
drag-and-drop file upload into `inputs/`, workspace file previews/downloads,
schema-derived tool inputs, streaming progress, and final results.

## Packed frontends

Agents can ship with a static frontend app mounted by the same runtime:

```bash
a2a init chart-agent --frontend static
a2a init chart-agent --frontend react
a2a init private-app --frontend react --auth platform
a2a frontend build
a2a dev --local
```

The manifest lives in `a2a.yaml`:

```yaml
frontend:
  path: frontend
  build: npm run build   # optional for prebuilt static bundles
  dist: dist
  mount: /
  auth: inherit
```

Use `--auth platform` to scaffold a packed app that requires an A2A Cloud
session. The generated agent uses `PlatformUserAuth`, so tool code can read
`ctx.auth.user_id`, `ctx.auth.email`, `ctx.auth.org_slug`, and `ctx.auth.scopes`
without wiring OAuth itself. Hosted deployments verify the HttpOnly platform
session cookie against the control plane; trusted identity headers are opt-in
for gateway deployments.

At runtime the agent serves the app and generated contract data:

| Path | Purpose |
|---|---|
| `/` | Packed frontend app |
| `/config.json` | Agent name, endpoints, auth mode, docs URL, tool schemas |
| `/a2a-client.js` | Browser client for `agent.call(...)` and `agent.skills.<name>(...)`; calls return the declared skill result rather than the HTTP envelope |
| `/_a2a/.well-known/a2a-skills.json` | Skill names, scopes, input schemas, output schemas |
| `/_a2a/auth/session` | Local dev session, or platform-provided user/session metadata |
| `/_a2a/mcp` | MCP endpoint for code and SDK clients |

Deploys either copy a prebuilt `frontend/dist` bundle or run `frontend.build`
inside a Node build stage before the Python A2A runtime starts.

The React scaffold creates a Vite app with a generated tool runner. Start
`a2a dev --local` in one terminal and `npm run dev` inside `frontend/` in another;
Vite proxies `/config.json`, `/a2a-client.js`, `/_a2a`, and `/.well-known` to
the local agent runtime.

`agent.call(...)`, `agent.skills.<name>(...)`, and the generated React
`callSkill(...)` helper unwrap the runtime's `{ result, events, artifacts }`
HTTP envelope. Use `agent.callEnvelope(...)` or pass `{ rawResponse: true }`
only when the UI explicitly needs invocation metadata in addition to the
declared tool result.

Run preflight checks before deploy:

```bash
a2a test
# `a2a init` scaffolds one tool named `ask`; use your own tool name here.
a2a test --invoke --skill ask --args-json '{"prompt":"hello"}'
```

Secrets stay local in `.env.local`. Workspace-backed framework tools, including
DeepAgents via `ctx.workspace_backend()`, write durable local outputs under
`.a2a/workspace/outputs` so you can inspect what will become downloadable files
in A2A Cloud.

If your tool runs code that creates files, use `ctx.workspace_shell(...)` or
`ctx.workspace_python(...)`. Those commands run in the platform sandbox with
the caller's workspace mounted, so `/workspace/...` writes persist directly and
changed files elsewhere in the sandbox root filesystem are mirrored under
`outputs/rootfs-captures/...`. Plain in-process `subprocess` calls inside the
agent container are not workspace-mounted or rootfs-captured.

## Composable meta-agents

A meta-agent is still an `A2AAgent`. It adds a declarative manifest that names
the child agents it may call, the durable goal it should pursue, and the memory
tiers it may use. The generated runtime inherits `MetaAgent.pursue`: plan a
bounded raw-skill DAG, validate it against the manifest, execute through
`ctx.call`, replan on failure within limits, persist progress, and remember the
run.

```yaml
name: launch-report-meta
version: 0.1.0
entrypoint: agent:LaunchReportMeta

composition:
  planning: llm_dag
  max_nodes: 6
  max_parallel: 2
  max_replans: 1
  sub_agents:
    - name: search-agent
      version: 1.2.3
      skills: [search]
      default_args:
        region: us
    - name: summarizer-agent
      skills: [summarize]
    - tag: charting
      skills: [render_chart]
      required: false

goal:
  objective: Create a sourced launch report with a chart.
  success_criteria:
    - report cites source material
    - chart is produced
  constraints:
    - do not call undeclared agents

memory:
  tiers: [files, kv, vector]
  namespace: launch-report
  scope: agent
  retention: durable
```

Manifest fields are intentionally small:

| Block | Purpose |
|---|---|
| `composition.sub_agents[]` | Allow-list of callable child agents by exact `name` or discovery `tag`; optional `version`, allowed `skills`, `default_args`, and `required`. |
| `composition.max_nodes` / `max_parallel` / `max_replans` | Hard planning and execution limits for the DAG engine. |
| `goal` | Default objective, success criteria, and constraints for `pursue`. |
| `memory` | Long-term memory policy. `files` uses workspace memory; `kv` and `vector` use control-plane memory and require `wants_cp_jwt=True`. |

The same manifest is surfaced on the Agent Card under
`capabilities.meta_agent`. Secret values in `default_args` are not exposed;
the card only advertises default argument keys.

## Template Lineage

Generated or copied agents can opt into template update tracking with
`TemplateLineage`. This publishes where the instance came from and what update
behavior the owner allows. It is advisory metadata: platforms can notify,
propose, or run a controlled migration, but they still need the owner's normal
authorization, review, proof, and deployment gates.

```python
from a2a_pack import A2AAgent, TemplateLineage


class SmtpEmailSender(A2AAgent):
    name = "smtp-email-sender"
    description = "Send HTML email through caller-provided SMTP."
    template_lineage = TemplateLineage(
        template_ref="a2acloud/templates/smtp-email-agent",
        template_version="0.3.0",
        source_agent="smtp-email-template",
        source_revision="abc123",
        instance_version="0.1.0",
        update_policy="propose",
        update_channel="stable",
        migration_skill="apply_template_update",
    )
```

The same primitive can be declared in `a2a.yaml`:

```yaml
template_lineage:
  template_ref: a2acloud/templates/smtp-email-agent
  template_version: 0.3.0
  source_agent: smtp-email-template
  source_revision: abc123
  instance_version: 0.1.0
  update_policy: propose
  update_channel: stable
  migration_skill: apply_template_update
```

`update_policy` is opt-in and can be `none`, `notify`, `propose`, or
`auto_patch`. The Agent Card omits `template_lineage` unless the agent declares
lineage or an update policy.

## Bounded production self-healing

Managed source agents can explicitly allow the platform to repair internal
runtime failures and redeploy the exact repair commit:

```yaml
self_healing:
  enabled: true
  consecutive_failures: 1
  window_seconds: 300
  cooldown_seconds: 900
  max_repairs_per_day: 3
  max_turns: 30
  deployment_timeout_seconds: 1800
  require_tests: true
```

`self_healing: true` uses those defaults. The policy is validated with bounded
limits and published as `capabilities.self_healing` on the Agent Card. It is
off unless declared. Expected 4xx/input errors do not trigger repair; internal
runtime failures do. Repairs use the scoped code-editor flow with
`push_on_failure: false`, record redacted error and code-change evidence, and
only become `complete` after the pushed source SHA is the SHA of a verified
live deployment. Owners can inspect every attempt in the dashboard Runtime
screen or `GET /v1/agents/{name}/self-healing`.

### Runtime APIs

| API | What it does |
|---|---|
| `ctx.subagents.list_subagents(...)` / `get_subagent(...)` | Discover callable child agents by tag, capability, skill, or name. |
| `ctx.subagents.call_subagent(...)` | Call a child skill and optionally mint a narrowed workspace grant for that child. |
| `subagent_tools(ctx)` | Return LangChain-compatible `list_subagents` and `call_subagent` tools for DeepAgents loops. |
| `ctx.memory.for_manifest(manifest.memory, agent_name=...)` | Unified facade over file, KV, and vector memory with `remember`, `recall`, `search`, and log helpers. |
| `ctx.meta_runs` | Control-plane backed goal/plan/progress persistence for dashboard run views and resumability. |
| `run_meta_agent_goal(...)` | Lower-level deterministic DAG plan/execute/replan loop used by `MetaAgent.pursue`. |
| `ctx.composition_budget` | Recursive composition guardrail: max depth, cycle detection, call budget, and optional child LLM budget split. |

Recursive safety limits are enforced on every `ctx.call` and transported to
children. The defaults are depth `4` and calls `32`; operators can cap them with
`A2A_COMPOSITION_MAX_DEPTH` and `A2A_COMPOSITION_MAX_CALLS`.

### Compose and deploy

The control plane accepts the same manifest at `POST /v1/agents/compose`:

```json
{
  "name": "launch-report-meta",
  "description": "Builds sourced launch reports from specialist agents.",
  "version": "0.1.0",
  "public": true,
  "manifest": {
    "composition": {
      "max_nodes": 6,
      "max_parallel": 2,
      "sub_agents": [
        {"name": "search-agent", "skills": ["search"]},
        {"name": "summarizer-agent", "skills": ["summarize"]}
      ]
    },
    "goal": "Create a sourced launch report.",
    "memory": ["files", "kv"]
  }
}
```

You can also send top-level `composition`, `goal`, and `memory` fields instead
of `manifest`. The compose path generates an editable A2APack project
(`agent.py`, `a2a.yaml`, `requirements.txt`, `meta_agent_manifest.json`),
validates child agent refs and per-user child auth setup, imports the generated
card, dry-runs a stubbed DAG, sandbox-validates the generated source, then
deploys through the normal source/runtime pipeline.

The conversational agent-builder path writes and edits this same manifest from
natural language, then routes through the compose endpoint. That keeps
dashboard compose, builder compose, and generated source on one contract.

Run the local worked example:

```bash
cd sdk/a2a-pack
PYTHONPATH=. python -m examples.meta_agent_research
```

It creates three in-memory specialist agents, runs a deterministic
manifest-backed `MetaAgent`, executes a DAG, and prints the memory files written
under `memory/...`.

## Bring your own auth

Agents can make caller identity explicit by declaring an `auth_model` and an
`auth_resolver`. The resolver receives the inbound bearer token, validates it
against your auth system, and returns the typed principal that tools read from
`ctx.auth`.

```python
import a2a_pack as a2a
from a2a_pack import A2AAgent, JWTAuth, OIDCUserInfoAuthResolver, RunContext


class CustomerAgent(A2AAgent):
    name = "customer-agent"
    description = "Uses the caller's app login"
    auth_model = JWTAuth
    auth_resolver = OIDCUserInfoAuthResolver(
        "https://auth.example.com/oauth2/userinfo",
        auth_model=JWTAuth,
    )

    @a2a.tool(scopes=["profile:read"])
    async def profile(self, ctx: RunContext[JWTAuth]) -> dict[str, str | None]:
        return {"sub": ctx.auth.sub, "email": ctx.auth.email}
```

For custom APIs, subclass `AuthResolver` and call your own `/me`,
`/introspect`, or session-exchange endpoint. SAML-backed apps use the same
contract by exposing a bearer-token bridge endpoint that returns JSON.

## Public surface

| Concept | Where |
|---|---|
| `A2AAgent` base class + `@a2a.tool` decorator (`@skill` is a permanent alias) | `a2a_pack.agent` |
| `RunContext`, `ctx.llm`, `ctx.ask`, `ctx.request_scope`, `ctx.ensure_read`, `ctx.ensure_write` | `a2a_pack.context` |
| Grant mint/verify (Ed25519, audience-bound, glob-filtered, time-limited) | `a2a_pack.grants` |
| Workspace negotiation surface | `a2a_pack.workspace` |
| Sandbox client (microVM via libkrun) | `a2a_pack.sandbox` |
| Agent-to-agent client (HTTP, in-memory, custom) | `a2a_pack.a2a_client` |
| Sub-agent toolkit, manifest-backed meta-agents, DAG engine, unified memory | `a2a_pack.subagents`, `a2a_pack.meta_agent`, `a2a_pack.dag`, `a2a_pack.memory` |
| MCP server (agent tools exposed as MCP tools, mountable into your FastAPI app) | `a2a_pack.mcp` |
| Lifecycle / Resources / LLMProvisioning declarations | `a2a_pack.runtime` |
| Card schema (auto-derived from your class) | `a2a_pack.card` |

## A2A made easy

- Subclass `A2AAgent`.
- Decorate async methods with `@a2a.tool`.
- Use `RunContext` for progress, auth/input requests, artifacts, scoped files,
  sandbox execution, and agent-to-agent calls.
- Run `a2a deploy`; the runtime exposes full A2A plus MCP and HTTPS.

Full reference + auto-generated docs at **https://docs.a2acloud.io**.

## Self-hosting

The platform pieces (control plane, sandbox runtime, dashboard, docs) live in
the same monorepo as this SDK:
[github.com/robGetsTheJobDone/a2a-cloud](https://github.com/robGetsTheJobDone/a2a-cloud).
The SDK is the only piece you need from PyPI; see the repository's
`SELF_HOSTING.md` to run the whole stack, then point the CLI at it with
`A2A_API_URL` (see "Pointing the CLI at your own platform" above).

## License

MIT — see [LICENSE](LICENSE).
