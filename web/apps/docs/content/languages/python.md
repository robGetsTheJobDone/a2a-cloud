# Python

This page is generated from the A2A Pack language SDK metadata.

## Scaffold

```bash
a2a init math-agent --language python
```

## Implementation Contract

Subclass `a2a_pack.A2AAgent` and implement `@a2a.tool` methods. Tools are published in the agent card's `skills` array (A2A spec vocabulary); `@skill` remains a supported alias of `@a2a.tool`.

```python
import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext

class MathAgent(A2AAgent[None, NoAuth]):
    name = "math-agent"
    description = "Math helper"
    auth_model = NoAuth

    @a2a.tool(description="Add two numbers")
    async def sum(self, ctx: RunContext[NoAuth], left: float, right: float) -> dict[str, float]:
        return {"value": left + right}
```

## Local Commands

```bash
a2a compile
a2a dev --local
```

## SDK Package

`a2a_pack` Python package.

The compiled `.a2a/agent.dsl.json` is the sidecar contract. The sidecar
owns public A2A, MCP, frontend, and invoke endpoints; the language SDK
owns native handler execution through the worker protocol.
