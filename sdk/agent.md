# sdk

## Purpose

`sdk/` currently contains `a2a-pack`, the developer SDK and CLI for building,
testing, packaging, and deploying A2A Cloud agents. The primary package turns a
Python `A2AAgent` class plus `@a2a.tool` methods (canonical style:
`import a2a_pack as a2a`) into Agent Cards, A2A invoke endpoints, MCP tools,
sidecar DSL, packed frontends, local dev harnesses, and deployable source
bundles. Tools are published in the agent card's `skills` array (A2A spec
vocabulary); `@skill` remains a permanent alias.

## Important Packages and Languages

- `sdk/a2a-pack/a2a_pack/`: main Python package, requires Python 3.11+.
  Public API is largely re-exported from `a2a_pack/__init__.py`.
- `a2a_pack/agent.py`: `A2AAgent`, the `tool` decorator (used as `@a2a.tool`,
  with `@skill` as a permanent alias), tool schema generation, input
  validation, invocation, receipts, and replay events.
- `a2a_pack/context.py`: `RunContext` and `LocalRunContext`; exposes progress,
  artifacts, auth, scoped workspace access, sandbox helpers, LLM credentials,
  MCP elicitation-style input requests, and agent-to-agent calls.
- `a2a_pack/runtime.py`: runtime/deploy metadata such as lifecycle, resources,
  LLM provisioning, composition, memory, managed databases, endpoints,
  template lineage, and manifest application from `a2a.yaml`.
- `a2a_pack/serve/asgi.py`: FastAPI adapter for `/healthz`,
  `/.well-known/agent-card`, A2A task/message routes, `/invoke/{skill}`,
  `/mcp`, auth/session, input requests, and scope grant/deny callbacks.
- `a2a_pack/dsl.py` and `a2a_pack/sidecar.py`: language-neutral Agent DSL
  (`schema_version` currently `2026-06-04`) and common sidecar runtime.
- `a2a_pack/cli/`: Typer CLI exposed as the `a2a` console script.
- `typescript/`: the `a2a-pack-ts` npm package for TS/JS DSL compilation and
  worker serving. This is the only hand-edited copy. `a2a_pack/typescript/` is
  a generated mirror (source + built `dist/`) that ships in the wheel and is
  vendored into `a2a init --language typescript|javascript` projects; refresh
  it with `scripts/sync-ts-sidecar.sh` (a pytest guard fails on drift).
- `go/a2apack/` and `rust/a2a-pack-rs/`: small Go/Rust sidecar SDKs used by
  generated templates. Embedded copies also live under `a2a_pack/go` and
  `a2a_pack/rust`. These two are demos of the worker protocol, not peers of the
  Python/TS SDKs: they route only the built-in `sum` skill and compile a fixed
  one-skill DSL. The disclosure text lives in `a2a_pack/cli/main.py`
  (`SINGLE_SKILL_LANGUAGES`) and is rendered into `a2a init` output, the
  scaffold README, and `web/apps/docs/content/languages/{go,rust}.md`.
- `docker/sidecar/`: base image Dockerfiles for Node, Go, Rust, JVM, and .NET
  sidecar workers plus `a2a-sidecar-build`.

## Common Commands and Runtimes

From `sdk/a2a-pack`:

- Python setup: `python -m venv .venv`, `pip install -e '.[dev]'`.
- Python tests: `pytest` or targeted tests such as
  `pytest tests/test_cli_init.py tests/test_sidecar.py -q`.
- CLI workflow: `a2a init`, `a2a validate`, `a2a card`, `a2a compile`,
  `a2a dev`, `a2a test --invoke`, `a2a frontend build`, `a2a build`,
  `a2a deploy`, `a2a logs`, `a2a local-deploy`, `a2a chat`, `a2a sidecar`.
- Auth/control-plane CLI commands include `a2a signup`, `a2a login`,
  `a2a logout`, `a2a whoami`, `a2a agents`, `a2a import`, `a2a auth ...`,
  and `a2a mcp-url`.
- OpenAPI helpers: `a2a openapi spec` and `a2a openapi client`; the client
  command shells out to `npx @hey-api/openapi-ts`.
- Local dev runs in-process by default (`a2a dev --local`); pass `--docker` to
  run inside the agent container image instead. It serves the dev UI at `/_dev`
  and workspace data under `.a2a/workspace`. Neither dev runtime starts declared
  Qdrant/Postgres — `a2a chat` is the compose harness that does.
- TypeScript package: `cd typescript && npm install && npm run build && npm test`.
- Go package: `cd go/a2apack && go test ./...`.
- Rust package: `cd rust/a2a-pack-rs && cargo test`.

CI (`.github/workflows/ci.yml`) runs the Python suite, the TypeScript
build/tests, and the sidecar drift check. Release tags must match
`a2a_pack.__version__`, `typescript/package.json`, and package metadata.

## Integration Points

- Agent projects are discovered through `a2a.yaml`; the common template records
  `name`, `version`, `entrypoint`, exposure, optional `frontend`, and optional
  platform `resources`.
- `compile_agent_to_dsl` projects Python agents to the same DSL consumed by
  non-Python sidecar workers. The sidecar calls native workers at
  `POST /_a2a/invoke/{handler}`.
- A2A runtime integration includes Agent Cards, task/message routes, JSON
  invoke, streaming/SSE, MCP tools, OpenAPI generation, OAuth protected resource
  metadata, scope negotiation, receipts, and replay signing.
- Platform integration points include the control-plane API client, Gitea
  backend, Ed25519 grant/receipt/replay keys, LiteLLM credential forwarding,
  workspace grants, sandbox execution, managed Neon/Postgres declarations,
  memory tiers, and packed frontend auth/session handling.
- Packed frontends can be static, React/Vite, or server-rendered Next.js-style
  configurations. Runtime exposes `/config.json`, `/a2a-client.js`, `/_a2a/*`,
  and frontend assets at the configured mount.

## Tests, Docs, and Examples

- `tests/` has broad Python coverage for agents, CLI, local dev, auth, MCP,
  OpenAPI, workspace/grants/sandbox, sidecar, replay/receipts, meta-agents,
  memory, procurement, and protocol compatibility.
- `examples/` includes simple coder/research agents, `multi_agent.py` for
  discovery plus delegated workspace grants, `meta_agent_research.py` for
  manifest-backed DAG/meta-agent flow, and a larger `procurement_agent` with a
  Vite frontend, generated OpenAPI client, SQLite/Postgres-backed store, file
  uploads, migrations, and packed frontend config.
- `docs/` covers sidecar worker protocol, Docker chat harness, local deploy
  harness, server-rendered frontends, and an A2A spec note about typed skill
  input schemas.

## Cautions

- Edit only the relevant source of truth and check embedded copies. The Python
  wheel includes templates, dev UI assets, `a2a_pack/go`, `a2a_pack/rust`, and
  `a2a_pack/typescript`. The TypeScript mirror is regenerated by
  `scripts/sync-ts-sidecar.sh`; the Go/Rust copies are still maintained by hand
  and are not identical to the top-level language package directories.
- Versioning is coupled across publish paths: Python version comes from
  `a2a_pack/__init__.py`; NPM publish checks the git tag against both Python
  and `typescript/package.json`.
- `a2a chat` always uses Docker and may build temporary images. `a2a dev --local`
  does not: it defaults to the in-process Python dev server, and only builds an
  image when `--docker` is passed explicitly.
- Packaging excludes common generated/dev directories such as `.a2a`, `dist`,
  `build`, `node_modules`, `.gitea`, `.env`, and `.env.local` unless explicitly
  included by package/frontend logic.
- Many tests depend on runtime signing env vars; `tests/conftest.py` supplies
  throwaway Ed25519 keys automatically for pytest.
- Be careful with `wants_cp_jwt`, auth resolvers, workspace grants, and scope
  expansion policies. These are platform trust boundaries, not just metadata.
