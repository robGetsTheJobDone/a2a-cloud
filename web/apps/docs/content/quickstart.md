# Quickstart

**Start here: a Python tool agent.** It is the shortest path from `pip install`
to a hosted, verifiable A2A + MCP service, and everything else on this page is a
variation on it. If you need a real workflow UI or already have an HTTP API,
[alternative paths](#alternative-paths) are at the end — they use the same
deploy command and produce the same kind of service.

## Python Tool Agent

The smallest possible agent: one Python class, one or more typed `@a2a.tool`
methods, no custom UI. Tools are published in the agent card's `skills` array
(A2A spec vocabulary); `@skill` remains a supported alias of `@a2a.tool`.

### 1. Install, log in, scaffold

```bash
pip install a2a-pack
a2a login

a2a init research-agent
cd research-agent
a2a dev --local
```

Open the local console:

```text
http://127.0.0.1:8000/_dev
```

The console shows detected tools, lets you upload files into
`.a2a/workspace/inputs`, streams progress, and previews output files.

`a2a init` also writes an `a2a.yaml` project manifest next to your agent. It
holds the name, version, entrypoint, and — importantly — `expose.public`, which
decides whether `a2a deploy` publishes the agent to the public registry. The
scaffold starts private. Every key it accepts is in the
[`a2a.yaml` reference](/reference/a2a-yaml).

### 2. Deploy

When the agent is ready:

```bash
a2a deploy --public
```

You get:

```text
https://research-agent.a2acloud.io
```

`a2a init` scaffolds `expose.public: false`, so a practice agent is never
listed in the public registry just by existing. `--public` is the one-time act
that lists it; set `expose.public: true` in `a2a.yaml` to make that the
project's default. Listing is not access control: an unlisted agent still gets
its canonical URL, and who may call it is decided by the auth model declared on
the agent.

`a2a deploy` waits for the build to reach a terminal state. If it fails, the
CLI prints the tail of the build log and exits non-zero. To see the full
output, or to watch a build that is still running:

```bash
a2a logs research-agent           # last deploy
a2a logs research-agent --follow  # stream until it finishes
```

### 3. Call it and keep the receipt

Call the deployed agent:

```bash
a2a call research-agent ask prompt="say hello"
```

Alongside the result, the CLI prints one dim line on stderr:

```text
receipt  4b4d56883bb4d526  ->  a2a receipt verify 4b4d56883bb4d526
```

That points at a signed [execution receipt](/concepts/receipts) — an
Ed25519-signed record of what ran, who called it, under what authority, and what
came out. Check it:

```bash
a2a receipt verify 4b4d56883bb4d526
```

```text
PASS 4b4d56883bb4d526  research-agent.ask · ok · 3000ms
  Ed25519 signature valid · key from https://api.a2acloud.io/v1/public/receipt-keys (kid 56475aa75463474c)
```

Two things are worth knowing before you rely on this:

- `a2a call` streams, and the gateway can only send the receipt **id** before a
  stream opens — not the signed token. So verifying by id here fetches the
  stored receipt from the control plane: it needs `a2a login` and ownership of
  the agent. To verify offline, or to hand the proof to someone else, you need
  the token itself — see
  [getting the token](/concepts/receipts#verify-a-receipt).
- A receipt token is not redacted. It embeds short previews of the call's input
  and result, so forwarding one discloses them.

The verifying key is published at
`https://api.a2acloud.io/v1/public/receipt-keys`, so someone holding the token
can run the same check with no account and no access to your logs. Change one
byte and it prints `FAIL` and exits `1`.

That is the part a log cannot do. See
[Receipts](/concepts/receipts) for the field-by-field breakdown, the wire
format, and an honest list of what a receipt does **not** prove.

## Alternative paths

Use these when the default path does not fit. Both deploy with the same
`a2a deploy` command.

### Agent With A React App

Use this when the agent needs a real workflow UI: upload controls, review
screens, approval flows, reports, or visual output.

```bash
pip install a2a-pack
a2a login

a2a init chart-agent --frontend react
cd chart-agent
a2a dev --local
```

In another terminal, run the Vite frontend:

```bash
cd frontend
npm install
npm run dev
```

The Vite app proxies `/app/config.json`, `/invoke`, `/auth`, `/mcp`, and
`/.well-known` to the local agent runtime. The generated app reads the tool
contract from the runtime, so Python type hints become UI-callable schemas.

Deploy both together:

```bash
a2a deploy --public
```

You get:

```text
https://chart-agent.a2acloud.io/app
```

### OpenAPI Auto-Agent

Use this when you already have an HTTP API and want an agent that can call it.
The generator turns operations into tools and adds an `auto` tool for
natural-language goals.

Unlike the other two paths, this one does not scaffold a local project. Look
before you leap — `preview` reads the spec and creates nothing:

```bash
pip install a2a-pack
a2a login

a2a openapi preview https://api.example.com/openapi.json \
  --name api-agent \
  --base-url https://api.example.com
```

It prints the tools, setup fields, and source files the generator would
produce. When that looks right:

```bash
a2a openapi generate https://api.example.com/openapi.json \
  --name api-agent \
  --base-url https://api.example.com
```

Two things this command does **not** do: it writes no files in the current
directory, and it does not wait for you. It runs on the control plane, which
creates the agent's source repo and immediately starts a deployment. There is
no `api-agent` directory to `cd` into. It prints a summary — agent name and
version, deploy `status`, `repo_url` for the generated source, `deployment_id`,
the expected public URL, and how many API operations became tools.

It also **publishes by default**. Pass `--private` to keep the agent out of the
public registry.

Follow the build, and connect whatever credentials the upstream API needs:

```bash
a2a logs api-agent --follow
a2a auth status api-agent
a2a auth bearer api-agent --token ...      # or: a2a auth api-key / oauth-token / mtls
```

To read or change the generated code, open a dev box — it comes up with the
repo already cloned at `~/api-agent`:

```bash
a2a ssh api-agent
```

Inside the box it is an ordinary A2A project, so `a2a dev --local`, `a2a test`,
and `a2a deploy` all work against it. See [SSH dev boxes](/concepts/dev-boxes).

Generated agents call `ctx.llm`, so they need an LLM credential. Locally that
is `AGENT_LLM_KEY` / `AGENT_LLM_URL` / `AGENT_LLM_MODEL` — see
[Local credentials](#local-credentials).
On A2A Cloud, saved dashboard keys route through LiteLLM's compatibility layer.
Use the model dropdown when adding an LLM key; it is populated from LiteLLM's
chat/completion model catalog and lets the platform keep routing, usage, and
pricing aligned. See [LLM credentials & model compatibility](/concepts/llm-credentials).

To let a new user try an LLM-backed agent before bringing a key, edit the
generated class in the dev box and add an account-gated allowance:

```python
import a2a_pack as a2a

class ApiAgent(a2a.A2AAgent):
    name = "api-agent"
    account_access = a2a.AccountAccess(required=True, platform_skill_calls=5)
```

Each account gets five platform-funded skill calls to this agent. Call six and
beyond use that account's saved model key; if no key exists, the API returns a
BYOK setup action instead of silently charging the platform.

The first deploy already ran as part of `a2a openapi generate`. After an edit,
`a2a deploy` from inside the repo ships the change to the same URL:

```text
https://api-agent.a2acloud.io
```

## Running Local Dev

Bare `a2a dev` syncs the project to a public, hot-reloading cloud dev box.
Use `--local` when you want the runtime and built-in console on this machine.

```bash
cd your-agent
a2a dev --local
```

It starts the agent runtime on `http://127.0.0.1:8000` and prints the local
URLs:

```text
url:    http://127.0.0.1:8000
dev ui: http://127.0.0.1:8000/_dev
card:   http://127.0.0.1:8000/.well-known/agent-card
```

Use `/_dev` for the built-in local console. It can:

- run any detected tool
- stream progress and final results
- upload files into `.a2a/workspace/inputs`
- preview files from `.a2a/workspace/outputs`
- prompt for missing local credentials before tool calls are enabled

If port `8000` is busy:

```bash
a2a dev --local --port 8010
```

### Local credentials

Local dev loads setup in this order:

1. Shell environment variables.
2. `.env.local` in the agent project.
3. Saved values in `~/.a2a/credentials.json`.

For generated OpenAPI agents or DeepAgents-based agents that use `ctx.llm`,
the local console asks for LLM credentials and saves them for future runs:

```env
AGENT_LLM_KEY=your_openai_or_compatible_key
AGENT_LLM_URL=https://api.openai.com/v1
AGENT_LLM_MODEL=gpt-4o
```

Hosted chat and caller-provided agent LLM calls use the same OpenAI-compatible
shape, but the control plane forwards them through LiteLLM so usage
is tracked consistently across compatible providers.

Agent-specific setup fields, such as `BLOG_API_KEY`, are also saved per agent
in `~/.a2a/credentials.json`. Project `.env.local` still wins if both are set.

### Docker chat with local resources

Use `a2a chat` when you want the smallest Docker-backed loop for one agent and
its declared resources. It starts the same local console as `a2a dev --local`, plus
Qdrant when vector memory is declared and Postgres when Neon/Postgres databases
are declared.

```bash
a2a chat --env-file .env.local --detach
```

Open:

```text
http://127.0.0.1:8000/_dev
```

For this declaration:

```yaml
resources:
  memory:
    tiers: [vector]
  databases:
    - name: app
      provider: neon
      engine: postgres
      env:
        url: DATABASE_URL
```

the harness sets:

```text
A2A_MEMORY_VECTOR_URL=http://qdrant:6333
DATABASE_URL=postgresql://a2a:a2a@postgres:5432/app
```

If your env file contains `OPENAI_API_KEY`, local Docker chat maps it to
`AGENT_LLM_KEY` for `ctx.llm` unless `AGENT_LLM_KEY` is already set.

Stop and remove local resource volumes:

```bash
a2a chat --down --volumes
```

### Local React frontend

Packed frontends use two local processes during active UI development:

```bash
# terminal 1: Python agent runtime + A2A/MCP endpoints
a2a dev --local

# terminal 2: Vite frontend with hot reload
cd frontend
npm install
npm run dev
```

Open the Vite URL, usually:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies these runtime paths to `a2a dev --local`:

```text
/app/config.json
/invoke
/auth
/mcp
/.well-known
```

Before deploy, verify the packed static bundle:

```bash
a2a frontend build
a2a dev --local
```

When `frontend/dist/index.html` exists, `a2a dev --local` also serves the packed app at
the configured mount path, usually:

```text
http://127.0.0.1:8000/app
```

## Common Commands

```bash
a2a card                         # print the Agent Card JSON
a2a openapi spec --out openapi.json
a2a openapi client --out frontend/src/a2a-client
a2a test                         # validate local project wiring
a2a test --invoke                # run one local tool call
a2a dev                          # public cloud dev box + hot reload
a2a dev --local --port 8010      # local runtime on another port
a2a chat --detach                # run one agent with local Docker resources
a2a agents                       # list deployed agents
a2a receipt list                 # receipts from recent calls
a2a receipt verify <receipt-id>  # check a receipt's Ed25519 signature
a2a mcp-url research-agent       # print MCP config for a deployed agent
```

## What Deploy Creates

- Public URL with TLS.
- Full A2A surfaces for Agent Card discovery, tasks, messages, artifacts,
  files, structured data, streaming, auth, JSON-RPC, REST, and protocol errors.
- `/healthz`, `/.well-known/agent-card`, `/`, `/message:send`,
  `/message:stream`, `/tasks/{id}`, `/invoke/{skill}`, and `/mcp`.
- Optional `/app`, `/app/config.json`, `/app/a2a-client.js`, and
  `/.well-known/a2a-skills.json` when the project declares a packed frontend.
- `/.well-known/openapi.json` for OpenAPI 3.1 tool contracts.
- Auto-derived JSON Schema validation from Python type hints.
- Discoverability from other agents through scoped, signed grants.

## Next

- [Platform overview](/platform)
- [Run work in Workspace, Trials, and Schedules](/platform/workspace)
- [Build and distribute agents](/platform/agents)
- [Concepts: agents + tools](/concepts/agents)
- [Concepts: receipts](/concepts/receipts)
- [Concepts: packed frontends](/concepts/packed-frontends)
- [Concepts: skill OpenAPI clients](/concepts/skill-openapi-clients)
- [Concepts: grants](/concepts/grants)
- [Reference: `a2a.yaml` project manifest](/reference/a2a-yaml)
- [Reference: `a2a_pack`](/reference)
