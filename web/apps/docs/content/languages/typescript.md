# TypeScript/JS

This page is generated from the A2A Pack language SDK metadata.

## Scaffold

```bash
a2a init math-agent --language typescript
```

## Implementation Contract

Extend `A2AAgent`, declare static skill schemas, and implement matching handler methods taking `(ctx, input)`. This TypeScript/JS SDK page covers typed projects and plain JS.

```typescript
import { A2AAgent, publicAuth, skill, type RunContext } from "a2a-pack-ts";

type SumInput = { left: number; right: number };

export class MathAgent extends A2AAgent {
  static agent = { name: "math-agent", description: "Math helper", version: "0.1.0" };
  static auth = publicAuth();
  static skills = [skill({
    name: "sum",
    handler: "sum",
    description: "Add two numbers",
    input_schema: {
      type: "object",
      properties: { left: { type: "number" }, right: { type: "number" } },
      required: ["left", "right"]
    },
    output_schema: {
      type: "object",
      properties: { value: { type: "number" } },
      required: ["value"]
    }
  })];

  async sum(ctx: RunContext, input: SumInput): Promise<{ value: number }> {
    return { value: input.left + input.right };
  }
}
```

## Local Commands

```bash
npm run compile
npm run worker
```

## SDK Package

`a2a-pack-ts`, vendored into the scaffold at `vendor/a2a-pack-ts` and wired up as `"a2a-pack-ts": "file:vendor/a2a-pack-ts"`.

The compiled `.a2a/agent.dsl.json` is the sidecar contract. The sidecar
owns public A2A, MCP, frontend, and invoke endpoints; the language SDK
owns native handler execution through the worker protocol.
