# Agents & tools

An a2a agent can be authored in Python or TypeScript/JS. In Python, it is one
class that subclasses [`A2AAgent`](/reference/agent) and exposes one or more
`@a2a.tool`-decorated methods. See [TypeScript/JS](/languages/typescript) for the
Node SDK shape.

Tools are published in the agent card's `skills` array (A2A spec vocabulary);
`@skill` remains a supported alias of `@a2a.tool`.

## Required class attributes

```python
class MyAgent(A2AAgent[MyConfig, MyAuth]):
    name = "my-agent"        # globally unique on the platform
    description = "..."
    version = "0.1.0"        # semver, surfaces on Card + dashboard
    config_model = MyConfig  # Pydantic; passed as `self.config`
    auth_model = MyAuth      # Pydantic; bound to `ctx.auth` per call
```

## Tools

Decorate `async` methods. The first parameter (after `self`) must be a
[`RunContext`](/reference/context). The rest must be type-annotated;
their types become the tool's input JSON Schema.

```python
@a2a.tool(
    description="Generate a chart.",
    tags=["visualization"],
    timeout_seconds=120,
    allow_scope_expansion=False,
)
async def chart(
    self,
    ctx: RunContext[NoAuth],
    prompt: str,
    data_path: str | None = None,
) -> dict:
    await ctx.emit_progress(f"working on: {prompt}")
    return {"ok": True, "path": "outputs/chart.png"}
```

The decorator:

- captures the signature with `inspect`
- builds a `pydantic.TypeAdapter` per parameter
- emits a JSON Schema with `additionalProperties: false` and
  `required` populated from non-default args
- publishes the resulting `SkillCard` on your agent's `AgentCard`

## Input validation, two levels

1. **Pydantic at the agent.** Every `/invoke/{skill}` call runs your
   args through `adapter.validate_python(...)` before the handler
   fires. Bad types → HTTP 400 (`SkillInputError`).
2. **Strict tool schema at the LLM.** The orchestrator can register
   each tool as an OpenAI-strict function tool — the model gateway
   refuses to emit calls that don't conform.

## Optional opt-ins

| Attribute / flag                    | What it enables                                            |
|-------------------------------------|------------------------------------------------------------|
| `@a2a.tool(allow_scope_expansion=True)` | Tool may call `ctx.request_scope(...)` / `ctx.ensure_*` mid-flight |
| `@a2a.tool(stream=True)`            | Tool emits multiple events; the platform SSE-bridges them |
| `workspace_access = WorkspaceAccess.dynamic(...)` | The platform attaches a workspace client to `ctx` |
| `egress = EgressPolicy(allow_hosts=...)` | Limit outbound network from the sandbox             |
| `tools_used = ("sandbox", "matplotlib")` | Surfaces on the Card; informational                |

## Lifecycle

`A2AAgent.startup(ctx)` and `A2AAgent.shutdown(ctx)` run once each per
process. Use for warm-up (load a model, hydrate a cache) or graceful
shutdown.

## Reference

- [`A2AAgent`](/reference/agent)
- [`@a2a.tool` / `@skill`](/reference/agent)
- [`SkillSpec`, `SkillPolicy`](/reference/agent)
