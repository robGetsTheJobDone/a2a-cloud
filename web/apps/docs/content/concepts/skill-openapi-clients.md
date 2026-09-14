# Skill OpenAPI clients

Every A2A Pack agent can expose its tools — published as the agent card's
`skills` (A2A spec vocabulary) — as a small OpenAPI API. This is useful when
you want a typed frontend client, a generated SDK, or an integration contract
for another system.

The generated API is skill-first:

```text
POST /invoke/{skill}
```

Each operation includes the skill input schema, output schema, streaming flag,
scopes, and auth requirements. A frontend can call the same contract locally
and after deploy.

## Generate the spec

From an agent project:

```bash
a2a openapi spec --project . --out openapi.json
```

Include a deployed server URL:

```bash
a2a openapi spec \
  --project . \
  --base-url https://research-agent.a2acloud.io \
  --out openapi.json
```

For private deployments or runtimes protected by `A2A_API_KEY`, force bearer
transport auth in the spec:

```bash
a2a openapi spec --project . --require-bearer --out openapi.json
```

Use `--public` to force a public transport contract when the agent itself is
public.

## Runtime discovery

Running and deployed agents also serve the generated spec:

```text
/.well-known/openapi.json
```

When a static frontend is mounted at `/`, the runtime keeps the protocol
metadata available under the reserved A2A prefix:

```text
/_a2a/.well-known/openapi.json
```

The spec uses OpenAPI `3.1.0` and includes an `x-a2a-auth` extension with the
agent auth strategy, principal schema, transport bearer requirement, resolver
metadata, and per-skill scopes.

## Generate a TypeScript client

A2A Pack wraps [`@hey-api/openapi-ts`](https://heyapi.dev/docs/openapi/typescript/get-started)
so frontend code can generate a client directly from the skill contract:

```bash
a2a openapi client --project . --out frontend/src/a2a-client
```

The command creates a temporary OpenAPI file, runs:

```bash
npx --yes @hey-api/openapi-ts -i <spec> -o <out>
```

and writes the generated TypeScript client to the output directory.

Generate only one skill:

```bash
a2a openapi client \
  --project . \
  --skill ask \
  --out frontend/src/a2a-client
```

Repeat `--skill` to export multiple selected skills:

```bash
a2a openapi client \
  --project . \
  --skill summarize \
  --skill publish \
  --out frontend/src/a2a-client
```

Keep the exact intermediate spec for review, CI, or client generation in other
languages:

```bash
a2a openapi client \
  --project . \
  --base-url https://research-agent.a2acloud.io \
  --skill ask \
  --out frontend/src/a2a-client \
  --spec-out frontend/src/a2a-client/openapi.json
```

`--spec-out` is written after the Hey API generator finishes, so it is safe to
place the spec inside the generated client directory.

## Example output

For an agent named `robertseares` with an `ask` skill, the generated spec
contains:

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "robertseares Skills API"
  },
  "paths": {
    "/invoke/ask": {
      "post": {
        "operationId": "invokeAsk"
      }
    }
  },
  "x-a2a-exported-skills": ["ask"]
}
```

The generated TypeScript client includes an `invokeAsk` helper. The input shape
comes from the skill schema:

```typescript
import { invokeAsk } from "./a2a-client";

const response = await invokeAsk({
  body: {
    arguments: {
      prompt: "What are you building?"
    }
  }
});
```

If the agent is private or the spec requires bearer transport auth, pass headers
through the generated client call or configure a shared Hey API client instance
in your frontend.

## Frontend workflow

For packed frontends, keep the generated client under `frontend/src`:

```bash
a2a openapi client \
  --project . \
  --base-url https://your-agent.a2acloud.io \
  --out frontend/src/a2a-client

cd frontend
npm run build
```

The static frontend can then import typed skill calls from the generated client.
Deploy still uses the normal path:

```bash
a2a deploy
```

## Other client generators

`a2a openapi spec` is generator-agnostic. Use the saved `openapi.json` with any
OpenAPI client generator for Python, Go, Rust, mobile apps, or internal SDKs.

