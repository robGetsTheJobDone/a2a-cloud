# a2a-pack-ts

TypeScript declaration compiler and worker shim for the common A2A sidecar.

```ts
import { A2AAgent, compileAgent, publicAuth, skill } from "a2a-pack-ts";

class ResearchAgent extends A2AAgent {
  static agent = {
    name: "research-agent",
    description: "Research helper",
    version: "0.1.0"
  };
  static auth = publicAuth();
  static skills = [
    skill({
      name: "ask",
      handler: "ask",
      description: "Answer a question",
      input_schema: {
        type: "object",
        properties: { prompt: { type: "string" } },
        required: ["prompt"],
        additionalProperties: false
      },
      output_schema: { type: "string" }
    })
  ];
}

const dsl = compileAgent(ResearchAgent, {
  entrypoint: { command: ["node", "dist/worker.js"] }
});
```

The sidecar calls workers at:

```text
POST /_a2a/invoke/{handler}
```

Use `serveWorker(AgentClass, handlers)` to expose that endpoint from Node.
