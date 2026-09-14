---
name: a2apack-agent-authoring
description: Author production a2a-pack agents with clear public @a2a.tool schemas, runtime declarations, caller-funded LLM credentials, optional agent BYOK, workspace grants, resources, and secure platform capabilities. Use when writing or reviewing agent.py for an A2A agent.
---
# A2A Pack Agent Authoring

An A2A agent should expose a small, stable public API and put rich reasoning
inside implementation helpers, DeepAgents skills, or subagents. The public
`@a2a.tool` methods are the contract shown on the Agent Card (they publish
under the card's `skills` array for wire compatibility). The canonical style
is namespaced — `import a2a_pack as a2a` plus `@a2a.tool(...)` — so the
decorator never collides with langchain's bare `@tool`. Bare `@tool` and the
legacy `@skill` are the same object and still work; always write `@a2a.tool`
in new code.

## Public Contract

Use `A2AAgent` class attributes for runtime and registry behavior:

- `name`, `description`, and `version` identify the agent.
- `config_model` and `auth_model` define agent configuration and caller auth.
- `llm_provisioning` tells the platform where LLM credentials come from.
- `resources`, `egress`, `tools_used`, and `workspace_access` describe runtime
  requirements.
- `wants_cp_jwt=True` is only for trusted platform agents. It forwards the
  caller's control-plane token.

For hosted generated/user-owned agents that call an LLM, default to
`LLMProvisioning.PLATFORM` so main-agent handoffs forward the caller's saved LLM credential for the callee.
`LLMProvisioning.CALLER_PROVIDED` is still acceptable for explicit BYOK wording.
Reserve `LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED` for legacy compatibility.
In every mode, read `ctx.llm`;
never read `A2A_LITELLM_KEY`, `OPENAI_API_KEY`, provider keys, or platform
secrets directly. If `ctx.llm.api_key` is empty, return a clear setup/config
result before constructing a model or calling `create_a2a_deep_agent`.

For explicitly deterministic/local-logic agents that do not call an LLM, omit
the concrete class `llm_provisioning` declaration, remove unused
`LLMProvisioning` imports, and do not read `ctx.llm`. A no-LLM agent must be callable
without requiring the user's LLM credential.

Keep each `@a2a.tool` method `async`, put `RunContext[...]` immediately after
`self`, and annotate every public argument. Do not use `*args` or `**kwargs`;
they are rejected and would not publish a useful schema.

## Code Example

```python
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    EgressPolicy,
    LLMProvisioning,
    NoAuth,
    Resources,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
)


class ResearchConfig(BaseModel):
    default_depth: int = 3


class ResearchAgent(A2AAgent[ResearchConfig, NoAuth]):
    name = "research-agent"
    description = "Researches a topic and writes a concise cited report."
    version = "0.1.0"

    config_model = ResearchConfig
    auth_model = NoAuth

    llm_provisioning = LLMProvisioning.PLATFORM
    resources = Resources(cpu="1", memory="1Gi", max_runtime_seconds=900)
    egress = EgressPolicy(allow_hosts=("api.openai.com",))
    tools_used = ("deepagents", "langchain")
    workspace_access = WorkspaceAccess.dynamic(
        max_files=128,
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
        require_reason=False,
    )

    @a2a.tool(
        description="Research a topic, save report artifacts, and return a summary",
        timeout_seconds=900,
        cost_class="llm-heavy",
    )
    async def research(
        self,
        ctx: RunContext[NoAuth],
        topic: str,
        depth: int | None = None,
        save_path: str = "outputs/research-report.md",
    ) -> dict[str, Any]:
        creds = ctx.llm
        await ctx.emit_progress(f"researching with {creds.model}")
        if not creds.api_key:
            return {
                "summary": "LLM credentials were not available for this run.",
                "artifact": None,
                "path": save_path,
            }
        graph = self._build_graph(ctx)
        state = await graph.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Research {topic!r} at depth {depth or self.config.default_depth}. "
                            f"Write the final markdown report to /workspace/{save_path}."
                        ),
                    }
                ]
            },
            config={"recursion_limit": 500},
        )
        report_text = _last_message_text(state)
        ref = await ctx.write_artifact(
            "research-summary.md",
            report_text.encode("utf-8"),
            "text/markdown",
        )
        await ctx.emit_artifact(ref)
        return {"summary": report_text, "artifact": ref.uri, "path": save_path}
```

## Schema Rules

Public arguments become the live A2A input schema. Prefer plain JSON-shaped
types: `str`, `int`, `float`, `bool`, `list[str]`, `dict[str, Any]`, or
Pydantic models. Provide defaults for optional knobs. Avoid exposing internal
implementation details such as model names, temporary paths, prompt fragments,
or unbounded command strings.

## Runtime Rules

If a tool calls `ctx.workspace_backend()` or `ctx.workspace`, declare
`workspace_access`. If it writes user-visible files, also emit artifacts with
`ctx.write_artifact` and `ctx.emit_artifact`.

Every public tool that returns artifacts, generated files, or durable workspace
paths must also advertise the exact first-grant write scope on its decorator.
This policy is required for direct Agent API calls as well as handoffs:

```python
@a2a.tool(
    description="Create a report under outputs/reports",
    grant_mode="read_write_overlay",
    grant_allow_patterns=("outputs/reports/**",),
    grant_outputs_prefix="outputs/reports/",
    grant_write_prefixes=("outputs/reports/",),
)
async def create_report(ctx: RunContext[NoAuth], topic: str) -> ReportResult:
    ...
```

Keep all four values aligned with the path the implementation actually writes.
Do not assume the class-level `workspace_access` declaration creates these
per-skill write prefixes.

If a tool runs code, renderers, converters, or data tools that create
downloadable files, run them through `await ctx.workspace_shell(...)` or
`await ctx.workspace_python(...)`. The sandbox persists `/workspace` writes
directly and mirrors changed files elsewhere in the guest rootfs under
`outputs/rootfs-captures/...`. In-process subprocesses write inside the agent
pod, not the caller's mounted or rootfs-captured workspace.

When invoking an inner DeepAgents graph, pass
`config={"recursion_limit": 500}` to `graph.ainvoke(...)` or
`graph.astream_events(...)`. Complex skill/subagent workflows can exceed
LangGraph defaults during normal operation.

If an agent needs system binaries, mirror the need in `a2a.yaml`:

```yaml
runtime:
  apt_packages: [ffmpeg, poppler-utils]
  resources:
    cpu: "2"
    memory: 2Gi
    max_runtime_seconds: 900
```

If it calls external hosts, declare `egress` on the class. If it needs secrets,
use `required_secrets` and `ctx.secret("NAME")`; do not hard-code credentials.

## Packed Frontends

If the user asks for a usable app, dashboard, workflow UI, or customer-facing
demo surface, scaffold a packed frontend instead of a separate web app. Use
`init_agent_template(name, description, frontend="react")` for the React/Vite
starter or `frontend="static"` for a no-build HTML bundle.

The deployment manifest should keep the browser app under the agent source:

```yaml
frontend:
  path: frontend
  build: npm run build
  dist: dist
  mount: /app
  auth: inherit
```

At runtime the platform serves the app from `/app`, exposes generated config at
`/app/config.json`, exposes a helper client at `/app/a2a-client.js`, and uses
the agent's public `@a2a.tool` schemas for callable operations. Frontend code
should load the generated config/client and call the public tools. Do not put
control-plane tokens, provider keys, secrets, internal stack details, or
private execution assumptions in frontend source.

### Viewing the page and invoking a skill are two different permissions

This is the single most common way a generated UI ships broken: the page loads
fine, then every button fails. Read `config.auth` and treat these as separate:

- `requiresSession` — whether the **page** needs a signed-in visitor.
- `invokeRequiresSession` — whether **calling a skill** needs one.

`invokeRequiresSession` is true whenever the agent declares
`llm_provisioning=PLATFORM` (or `PLATFORM_OR_CALLER_PROVIDED`), or an
`auth_model` of `PlatformUserAuth`. Caller-pays LLM is the usual reason: the
platform must know whose credential funds the run. **So the normal case for a
public, caller-pays agent is a page anyone may view whose skills still require
sign-in.** Design for that state; do not assume a viewer is authenticated.

Never send the visitor to `config.auth.loginUrl` yourself. Agents live on
`<name>.a2acloud.io` and the dashboard session cookie is host-locked to the
dashboard, so a bare login redirect signs the user in somewhere else and
returns them here still signed out — an infinite bounce. Use
`config.auth.authorizeUrl`, the platform gateway that hands this origin its own
scoped session. The generated client and the scaffolded `a2a.js` already
prefer it; keep that order if you write your own:

```js
const target = config.auth?.authorizeUrl || config.auth?.loginUrl;
```

Then render the signed-out state as a real affordance:

```jsx
{needsSignIn ? <a href={signInUrl(config)}>Sign in to run</a> : null}
```

Requirements for any UI you generate:

- Render and explain itself while signed out; never a blank screen or a raw
  `sign in required` error.
- Offer a visible sign-in control when `invokeRequiresSession` and the visitor
  is not authenticated.
- Treat a `401` from an invoke as "session expired" and re-authorize, not as a
  fatal error.
- Surface `status: "setup_required"` from a skill (for example a missing LLM
  credential) as an actionable message, not a crash.

## A2A Tool vs DeepAgents Skill

Use A2A `@a2a.tool` for caller-visible operations. Use
DeepAgents skills (prompt-pack `SKILL.md` bundles) for internal procedural
knowledge the LLM should apply. A generated project can have one public
`@a2a.tool` that invokes a DeepAgent loaded with several internal
`skills/<name>/SKILL.md` bundles.
