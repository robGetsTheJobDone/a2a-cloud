# a2a cloud Documentation

The official documentation for a2a cloud. One Python or TypeScript/JS agent
becomes a full A2A compliant, deployable, discoverable, sandboxed agent
service. Agents call each other through scoped grants. Users never see Docker,
Kubernetes, Gitea, or ArgoCD. New here? Start with the
[Quickstart](/quickstart).

a2a-pack makes [Google's Agent2Agent protocol](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/)
easy: write typed Python or TypeScript/JS tools and the SDK handles Agent
Cards, task state, message parts, file exchange, structured data, artifacts,
streaming, auth, and protocol errors.

```bash
pip install a2a-pack
a2a init research-agent
cd research-agent
a2a deploy
# → https://research-agent.a2acloud.io
```

Ship a full agent app when the workflow needs a UI:

```bash
a2a init chart-agent --frontend react
cd chart-agent
a2a deploy
# → https://chart-agent.a2acloud.io/app
```

## What's here

- **[Quickstart](/quickstart)** — start with a Python tool agent; React app and OpenAPI auto-agent are alternatives. Writing TypeScript instead? See [TypeScript](/languages/typescript).
- **[Receipts](/concepts/receipts)** — every governed call returns a signed record anyone can verify against a published public key.
- **[Platform](/platform)** — every dashboard surface and the build, run, and govern workflows.
- **[Control-plane API](/platform/api)** — authentication, API families, errors, and the complete generated route inventory.
- **[Concepts](/concepts)** — agents, tools, grants, sandbox, registry.
- **[LLM credentials](/concepts/llm-credentials)** — model setup, LiteLLM compatibility, and pricing attribution.
- **[Packed frontends](/concepts/packed-frontends)** — deploy a React/Vite app with the agent.
- **[Skill OpenAPI clients](/concepts/skill-openapi-clients)** — export skill specs and generate typed frontend clients.
- **[Primetime readiness](/concepts/primetime-readiness)** — proof, receipts, governance, and onboarding signals for production review.
- **[Reference](/reference)** — every public `a2a_pack` symbol, every `a2a` CLI command, and every FastAPI control-plane route, generated from this monorepo revision.
- **[For LLMs](/llms.txt)** — index file you can feed to a coding agent.
- **[Full corpus](/llms-full.txt)** — every doc page concatenated into one plaintext blob.

## Full A2A compliant

a2a cloud and a2a-pack are full A2A compliant. The SDK turns ordinary Python or
TypeScript/JS handlers into protocol-native tools, and the runtime exposes the
A2A surfaces agents expect: discovery, tasks, messages, artifacts, file/data
exchange, streaming updates, bearer auth, JSON-RPC, REST, and MCP.

## Making A2A easy

- `A2AAgent` gives every agent a standard identity, Agent Card, runtime, and
  tool registry.
- `@a2a.tool` turns an async method into typed A2A input/output schema.
- `RunContext` handles progress, artifacts, auth/input requests, scoped file
  grants, sandbox access, and agent-to-agent calls.
- `frontend` in `a2a.yaml` lets an agent ship a static app at `/app` with
  generated config, skill schemas, and session-aware calls.
- `a2a openapi spec` and `a2a openapi client` turn skill schemas into OpenAPI
  contracts and generated TypeScript clients.
- `a2a deploy` ships the agent and wires hosted A2A, MCP, TLS, docs, and
  marketplace discovery.

## The shape of an agent

```python
from pydantic import BaseModel
import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext


class GreeterConfig(BaseModel):
    suffix: str = "!"


class Greeter(A2AAgent[GreeterConfig, NoAuth]):
    name = "greeter"
    description = "Say hi."
    version = "0.1.0"
    config_model = GreeterConfig
    auth_model = NoAuth

    @a2a.tool(description="Greet someone.")
    async def greet(self, ctx: RunContext[NoAuth], who: str) -> str:
        await ctx.emit_progress(f"greeting {who}")
        return f"hello {who}{self.config.suffix}"
```

That's it. `a2a deploy` ships it. Other agents can discover and call your
`greet` tool with a scoped grant; the CP mints + verifies; matplotlib /
pandas / whatever can run in a sandboxed microVM if your tool asks for it.
