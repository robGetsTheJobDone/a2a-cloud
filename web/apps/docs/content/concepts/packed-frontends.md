# Packed frontends

Packed frontends let an agent ship with its own web app. The Python agent,
its tool schemas, and a static React/Vite UI deploy together as one A2A
cloud service.

Use this when the agent is more than an API:

- a chart agent with an editor and preview pane
- a finance agent with upload, reconciliation, approval, and export
- a research agent with report history and artifacts
- an internal ops agent with review queues and run receipts

The important boundary stays the same: the frontend calls the agent through
the generated A2A contract. It does not receive platform secrets.

## Scaffold a React app

```bash
a2a init chart-agent --frontend react
cd chart-agent
```

The project includes the normal agent files plus a Vite app:

```text
chart-agent/
  agent.py
  a2a.yaml
  requirements.txt
  frontend/
    package.json
    vite.config.js
    src/
      App.jsx
      a2a.js
      main.jsx
      style.css
```

The generated React app loads the agent contract, lists inferred tools,
shows JSON input schemas, and can invoke tools from the browser.

## Run locally

Start the agent runtime in one terminal:

```bash
a2a dev
```

Open the bundled local console when you want to test tools, uploads, output
files, and missing local credentials without running Vite:

```text
http://127.0.0.1:8000/_dev
```

Start the React app in another terminal when you are working on the custom UI:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL, usually:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies these paths to `a2a dev`:

```text
/app/config.json
/invoke
/auth
/mcp
/.well-known
```

That means local React code uses the same generated contract the deployed
app uses.

Before deploy, build the static app and check that the agent runtime can serve
the packed bundle:

```bash
a2a frontend build
a2a dev
```

When `frontend/dist/index.html` exists, `a2a dev` serves the app at the
configured mount path:

```text
http://127.0.0.1:8000/app
```

## Manifest

Packed frontend configuration lives in `a2a.yaml`:

```yaml
frontend:
  path: frontend
  build: npm run build
  dist: dist
  mount: /app
  auth: inherit
```

Fields:

- `path`: frontend source directory.
- `build`: optional build command. If present, deploy runs it in a Node build stage.
- `dist`: directory containing the built static app.
- `mount`: URL path where the app is served.
- `auth`: `inherit` by default, so private agents imply private apps. Use
  `platform` for apps that require a logged-in A2A Cloud user.

For a prebuilt static bundle, omit `build` and commit `frontend/dist`.

## Server-rendered frontends

Static frontends are still the default. For a server-rendered Next.js app, use
`server-rendered` mode:

```bash
a2a init app-agent --frontend nextjs --frontend-mode server-rendered
```

The generated manifest uses Next.js standalone output:

```yaml
frontend:
  type: server-rendered
  framework: nextjs
  path: frontend
  build: npm run build
  start: node server.js
  port: 3000
  mount: /
  auth: inherit
```

Hosted deployments run the agent runtime on the public port and start the
frontend server on loopback inside the same container. Browser requests hit the
agent first; the agent enforces `frontend.auth`, keeps `/_a2a/*` for A2A/API
traffic, then proxies frontend page and asset requests to the Next.js server.

Use server-rendered mode when the frontend needs framework server features.
Use the default static mode when a built `dist` or exported `out` directory is
enough.

## Platform Auth

For a packed app with built-in A2A Cloud auth:

```bash
a2a init private-app --frontend react --auth platform
```

The scaffold uses `PlatformUserAuth`:

```python
import a2a_pack as a2a
from a2a_pack import A2AAgent, PlatformUserAuth, RunContext


class PrivateApp(A2AAgent[None, PlatformUserAuth]):
    auth_model = PlatformUserAuth

    @a2a.tool
    async def whoami(self, ctx: RunContext[PlatformUserAuth]) -> dict:
        return {
            "user_id": ctx.auth.user_id,
            "email": ctx.auth.email,
            "org": ctx.auth.org_slug,
        }
```

Hosted deployments verify the HttpOnly platform session cookie against the
control plane before serving `auth: platform` frontend files or invoking
`PlatformUserAuth` tools. Gateway-forwarded identity headers are opt-in via
runtime configuration; direct deployments do not trust them by default.

## Runtime contract

Every packed frontend gets generated endpoints:

| Path | Purpose |
|---|---|
| `/app/` | The bundled frontend app |
| `/app/config.json` | Agent identity, endpoints, docs URL, auth mode, and skill schemas |
| `/app/a2a-client.js` | Small browser helper for `agent.call(...)` |
| `/.well-known/a2a-skills.json` | Skill names, scopes, input schemas, output schemas |
| `/auth/session` | Local dev session or platform-provided user/session metadata |

Example config shape:

```json
{
  "agent": {
    "name": "chart-agent",
    "version": "0.1.0"
  },
  "endpoints": {
    "invoke": "https://chart-agent.a2acloud.io/invoke",
    "agentCard": "https://chart-agent.a2acloud.io/.well-known/agent-card.json",
    "mcp": "https://chart-agent.a2acloud.io/mcp",
    "session": "https://chart-agent.a2acloud.io/auth/session"
  },
  "skills": []
}
```

## How tools are known

Tools are inferred from your Python agent:

```python
from typing import Literal
import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext


class ChartAgent(A2AAgent):
    name = "chart-agent"
    auth_model = NoAuth

    @a2a.tool(description="Render a chart from a CSV file.")
    async def render_chart(
        self,
        ctx: RunContext[NoAuth],
        dataset: str,
        chart_type: Literal["bar", "line"] = "bar",
    ) -> dict:
        return {"chart_type": chart_type, "dataset": dataset}
```

`a2a-pack` derives, per tool:

- name (published in the agent card's `skills` array — A2A spec vocabulary)
- description
- input JSON Schema
- output JSON Schema
- scopes
- streaming flag
- policy hints

The frontend reads those schemas from `/app/config.json` or
`/.well-known/a2a-skills.json`.

For a typed frontend client, generate TypeScript directly from the same tool
contract:

```bash
a2a openapi client \
  --project . \
  --base-url https://chart-agent.a2acloud.io \
  --out frontend/src/a2a-client
```

To export only selected tools and keep the OpenAPI document with the generated
client:

```bash
a2a openapi client \
  --project . \
  --skill render_chart \
  --out frontend/src/a2a-client \
  --spec-out frontend/src/a2a-client/openapi.json
```

See [Skill OpenAPI clients](/concepts/skill-openapi-clients) for the full
workflow.

## Deploy

Deploy stays the same:

```bash
a2a deploy
```

If `frontend.build` is set, the control plane builds the frontend in a Node
stage, copies `frontend/dist` into the agent image, and serves it from the
Python A2A runtime. If `frontend.build` is omitted, the deploy path copies the
prebuilt static bundle.

The deployed agent exposes both the app and protocol surfaces:

```text
/app
/.well-known/agent-card.json
/.well-known/a2a-skills.json
/.well-known/openapi.json
/invoke/{skill}
/mcp
/auth/session
```

## Security model

- Browser code calls generated endpoints with the user session.
- Platform secrets stay server-side.
- Private agents should use `auth: inherit`, so the app follows agent access.
- File access should go through scoped grants, signed URLs, or platform-mediated APIs.
- The app should treat tool schemas as public contract metadata, not as secret configuration.

## Current scope

Packed frontend support is static-SPA first. React/Vite works out of the box,
and other static frameworks work if they build into a `dist` directory.

Server-rendered frameworks such as non-static Next.js or Remix need a later
sidecar/container mode. For now, keep packed frontends static and let the A2A
runtime handle the agent API.
