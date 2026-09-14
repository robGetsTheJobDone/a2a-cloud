# `a2a.yaml`

`a2a.yaml` is the project manifest at the root of every agent repo. It is the
file `a2a deploy`, `a2a dev`, `a2a compile`, `a2a build`, and the control-plane
builder all read to find out what your project is and how to run it.

`a2a init` writes one for you. Most agent metadata (tools, auth model, secrets,
workspace access) lives on the agent class instead — this file carries project
identity, the entrypoint, deploy visibility, and the platform resources the
control plane has to provision *before* your code ever runs.

Every section below that maps to an SDK model — `frontend`, the `runtime.*`
sub-objects, `resources.memory`, `resources.databases[]`, `self_healing`,
`composition`, `goal`, and `template_lineage` — is checked key-by-key against
that model by `web/apps/docs/scripts/gen.py --check` in CI. Adding a field to
`a2a_pack.runtime` or `a2a_pack.frontend` without documenting it here fails the
build, and so does documenting a key the model does not have. The remaining
keys (top-level identity, `expose`, `package`, `resources.mailbox`, and the
scalar `runtime` keys) are parsed by hand in the CLI and control-plane builder
and are verified by reading, not by the gate.

## Minimal manifest

What `a2a init research-agent` writes:

```yaml
name: research-agent
version: 0.1.0
entrypoint: agent:ResearchAgent
expose:
  public: false
```

That is the whole required surface for a Python agent. Everything below is
optional.

## Top-level keys

| Key | Type | Required | Default | What it does |
| --- | --- | --- | --- | --- |
| `name` | string | yes | — | Agent slug. Becomes the registry name and the `<name>.a2acloud.io` hostname. Also copied onto the agent class, overriding the class attribute. |
| `version` | string | yes | — | Agent version. Copied onto the agent class. Used to tag the built image. |
| `description` | string | no | agent's own description | Copied onto the agent class and sent to the control plane on deploy. |
| `entrypoint` | string | yes | — | How to start the agent. Shape depends on `language` — see [Entrypoint](#entrypoint). |
| `language` | string | no | `python` | `python`, `typescript`, `javascript`, `go`, or `rust`. Accepts `ts`/`node` for TypeScript and `js`/`nodejs` for JavaScript. Anything else fails the compile step. |
| `expose` | object | no | published | Deploy visibility. See [`expose`](#expose). |
| `package` | object | no | — | Extra paths to ship in the upload. See [`package`](#package). |
| `frontend` | object \| string \| bool | no | none | Packed frontend served alongside the agent. See [`frontend`](#frontend). |
| `runtime` | object | no | class defaults | Runtime/deployment declaration. See [`runtime`](#runtime). |
| `resources` | object | no | none | Platform-managed memory, databases, and mailbox. See [`resources`](#resources). |
| `self_healing` | bool \| object | no | off | Opt-in bounded source self-repair. See [`self_healing`](#self-healing). |
| `composition` | object \| list | no | none | Sub-agents a meta-agent may call. See [`composition`](#composition). |
| `goal` | string \| object | no | none | Durable objective for a meta-agent. See [`goal`](#goal). |
| `memory` | string \| list \| object | no | none | Meta-agent long-term memory. Same shape as [`resources.memory`](#resources-memory). |
| `template_lineage` | string \| object | no | none | Source-template provenance and update policy. Also accepted as `template`. See [`template_lineage`](#template-lineage). |

Unknown top-level keys are ignored rather than rejected. A typo in a key name
is silently dropped, so check `a2a card` or `a2a frontend info` after editing.

### Entrypoint

`entrypoint` means different things per language, because only Python agents are
imported in-process:

| `language` | `entrypoint` shape | Scaffold value |
| --- | --- | --- |
| `python` | `module:ClassName`, importable from the project root | `agent:ResearchAgent` |
| `typescript` | shell command that starts the worker | `node dist/worker.js` |
| `javascript` | shell command that starts the worker | `node src/worker.js` |
| `go` | shell command that starts the worker | `./worker` |
| `rust` | shell command that starts the worker | `./worker` |

For non-Python languages the tool contract comes from `.a2a/agent.dsl.json`,
which `a2a compile` produces by shelling out to that language's build
(`npm run compile`, `go run . compile`, `cargo run -- compile`).

Only Python projects apply the class-level parts of this manifest
(`runtime`, `self_healing`, `composition`, `goal`, `memory`,
`template_lineage`) at compile time, because those are attached to an imported
agent class. `expose`, `package`, `frontend`, and `resources` are read straight
from the YAML and apply to every language.

## `expose`

```yaml
expose:
  public: false
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `public` | bool | `False` | Whether the agent is listed in the public registry. A listing choice, **not access control**. |

With `public: true` the agent is listed in the public registry at `a2acloud.io`
and returned by the public discovery endpoints. With `public: false` it is kept
out of both.

Three things to know:

- **It is not a security boundary.** An unlisted agent still gets its canonical
  URL; who may call it is decided by the auth model declared on the agent, not
  by this key. `a2a deploy` prints the same warning when it ships an unlisted
  agent.
- **Omitting the key means "unspecified", not "publish".** `a2a deploy` resolves
  the listing in this order: `--public` / `--private` on the command line, then
  `expose.public` in this file, then **the listing the agent already has**, and
  only for a name the registry has never seen does it fall back to unlisted.
  So deleting the block from a published agent's manifest does not unlist it on
  the next deploy, and a brand-new agent is never published by accident. Every
  deploy prints which rule decided, and why.
- `a2a deploy --public` / `a2a deploy --private` overrides the manifest for a
  single deploy.

## `package`

```yaml
package:
  include:
    - vendor/a2a-pack-ts/dist
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `include` | string \| list of strings | `[]` | Relative paths to force into the deploy tarball. |

`a2a deploy` tars the project but skips build and dev artifacts:
`__pycache__`, `.venv`, `.git`, `.pytest_cache`, `.mypy_cache`, `node_modules`,
`.next`, `.turbo`, `.vercel`, `dist`, `build`, `.gitea`, `deploy`, `.a2a`,
`.claude`, plus `Dockerfile`, `.dockerignore`, `.env`, `.env.local`, and any
`*.pyc`.

`package.include` re-adds specific paths from that exclusion list. The
TypeScript and JavaScript scaffolds use it to ship the vendored SDK build,
which otherwise falls under the `dist` exclusion. Entries must be relative and
must not escape the project (`..` and absolute paths are rejected).

## `frontend`

Serves a packed web app from the same deployment. Accepts an object, a string
(treated as `path`), `true` (all defaults), or `false` (disabled).

```yaml
frontend:
  path: frontend
  build: npm run build
  dist: dist
  mount: /app
  auth: inherit
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `type` | string | `static` | `static`, `static-spa`, `spa`, or `server-rendered` (`server`/`ssr` are accepted spellings). Any other value is an error. Also accepted as `kind`. |
| `framework` | string | none | Only meaningful for `server-rendered`, where it defaults to and must be `nextjs`. |
| `path` | string | `frontend` | Frontend source directory, relative to the project. |
| `dist` | string | `dist` | Built static output, relative to `path`. |
| `build` | string | none | Build command. When set, the deploy build stage runs it; when unset, the committed `dist` is shipped as-is. |
| `start` | string | none | Start command for `server-rendered` apps. |
| `port` | integer | `3000` | Loopback port the server-rendered app listens on. Must be 1–65535. |
| `mount` | string | `/` | URL path the app is served at. |
| `auth` | string | `inherit` | `inherit`, `platform`, or `public`. Unrecognised values fall back to `inherit`. |
| `docs_url` | string | `https://docs.a2acloud.io/` | Docs link injected into the generated frontend config. Also accepted as `docsUrl`. |

`auth: inherit` means a private agent implies a private app. `auth: platform`
requires a signed-in A2A Cloud user and is refused by hosted deploys unless the
platform has the browser-session gateway enabled.

See [Packed frontends](/concepts/packed-frontends) for the full workflow.

## `runtime`

Declarative deployment metadata. For Python agents these values are applied to
the agent class at compile time and published on the Agent Card; the control
plane additionally reads a few of them straight from the YAML when it builds
the image.

```yaml
runtime:
  lifecycle: warm
  availability: always_on
  concurrency: 4
  resources:
    cpu: 500m
    memory: 1Gi
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `lifecycle` | string | `ephemeral` | `ephemeral`, `session`, or `warm`. `warm` keeps at least one instance running. |
| `availability` | string | `on_demand` | `on_demand` or `always_on`. |
| `state` | string | `none` | `none`, `session`, or `durable`. |
| `llm_provisioning` | string | `platform` | `platform`, `platform_or_caller_provided`, `caller_provided`, or `agent_byok`. |
| `concurrency` | integer | `1` card / `100` hosted | Requests one instance handles at once. Must be positive. `1` is only the value published on the Agent Card; nothing in the SDK enforces it. On a hosted deploy the declared value becomes Knative `containerConcurrency`, capped at `100`, and **omitting the key yields `100`, not `1`** — so `concurrency: 8` lowers the hosted limit rather than raising it. |
| `tools_used` | string \| list | `[]` | Informational list of external tools the agent uses. |
| `wants_cp_jwt` | bool | `false` | Forward the *caller's* control-plane JWT into `/invoke`. Only for trusted platform agents — a JWT can do anything the caller can. |
| `resources` | object | see below | Resource budget. See [`runtime.resources`](#runtime-resources). |
| `egress` | object | see below | Outbound network policy. See [`runtime.egress`](#runtime-egress). |
| `account_access` | object | see below | Account-gated trial policy. See [`runtime.account_access`](#runtime-account-access). |
| `endpoints` | object \| list | `[]` | Raw HTTP adapters. Also accepted as `webhooks`. See [`runtime.endpoints[]`](#runtime-endpoints). |
| `apt_packages` | list of strings | `[]` | Debian packages installed into the image at build time. Read by the control-plane builder, not by local SDK parsing. Entries must match `[a-z0-9][a-z0-9.+-]{1,63}`; invalid entries are dropped and at most 32 are used. |
| `features` | string \| list | `[]` | Opt-in build features. Only `codegraph` is supported today; unknown names are dropped. Read by the control-plane builder. |

### `runtime.resources`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `cpu` | string | `100m` | Kubernetes-style CPU spec. On hosted deploys this becomes the burst limit; the request is a small fixed reservation. |
| `memory` | string | `256Mi` | Kubernetes-style memory spec. Used for both request and limit on hosted deploys. |
| `gpu` | integer | `0` | GPU count hint. Must be non-negative. |
| `max_runtime_seconds` | integer | `600` | Per-invocation budget. Feeds the hosted request timeout. Must be positive. |

### `runtime.egress`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `allow_hosts` | list of strings | `[]` | External hosts the agent may reach. |
| `allow_internal_services` | list of strings | `[]` | In-cluster service DNS names the agent may reach. |
| `deny_internet_by_default` | bool | `true` | Deny everything not listed above. |

### `runtime.account_access`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `required` | bool | `false` | Callers must have an A2A Cloud account. |
| `platform_skill_calls` | integer | `0` | Platform-funded invocations per account before the caller's own LLM key is used. Non-zero requires `required: true`, and requires `llm_provisioning` to be `platform` or `platform_or_caller_provided`. |
| `after_trial` | string | `byok` | What happens once the allowance is spent. `byok` is the only value. |

### `runtime.endpoints[]`

Raw HTTP adapters. The runtime accepts a provider's native request shape, maps
it into handler arguments, and dispatches through the normal skill path.

```yaml
runtime:
  endpoints:
    - path: /hooks/stripe
      methods: [POST]
      skill: handle_stripe
      headers_arg: headers
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `path` | string | required | Absolute URL path. Must not collide with reserved A2A paths (`/invoke`, `/mcp`, `/tasks`, `/message`, `/auth`, `/healthz`, `/_a2a`, `/.well-known`, and friends). |
| `skill` | string | required | Tool to dispatch to. Also accepted as `target`. |
| `methods` | string \| list | `["POST"]` | HTTP methods. Also accepted as `method`. |
| `name` | string | none | Optional label for the adapter. |
| `body_arg` | string | `body` | Handler argument that receives the request body. |
| `headers_arg` | string | none | Handler argument that receives request headers. |
| `query_arg` | string | none | Handler argument that receives query parameters. |

## `resources`

Platform-managed resources the control plane provisions before the agent runs.

```yaml
resources:
  memory:
    tiers: [kv, vector]
  databases:
    - name: app
      provider: neon
      engine: postgres
      env:
        url: DATABASE_URL
  mailbox: true
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `memory` | string \| list \| object | none | Long-term memory. A bare string or list is read as `tiers`. |
| `databases` | list | `[]` | Platform-managed Postgres. Must be a list, even for one database — the control plane rejects a bare mapping with `resources.databases must be a list` and fails the deploy before the build starts. |
| `mailbox` | bool \| object | none | Per-agent email inbox. |

### `resources.memory`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `tiers` | string \| list | `files` | Any of `files`, `kv`, `vector`. Unknown names are dropped; an empty result falls back to `files`. |
| `namespace` | string | `notes` | Namespace key for stored records. Unset resolves to the literal `notes`, *not* to the agent name. |
| `scope` | string | `agent` | `agent`, `user`, or `thread`. |
| `retention` | string | `durable` | `durable` or `ephemeral`. |

### `resources.databases[]`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `name` | string | required | Slug matching `[a-z][a-z0-9-]{0,62}`. |
| `engine` | string | `postgres` | Only `postgres`. |
| `provider` | string | `neon` | Only `neon`. |
| `scope` | string | `user` | `user` for a database per caller, `org` for one per organization. |
| `branch` | string | `main` | Neon branch, same slug rules as `name`. |
| `access_mode` | string | `read_write` | `read_only`, `read_write`, or `owner`. Also accepted as `role`. |
| `scale_to_zero` | bool | `true` | Let the database suspend when idle. |
| `env` | object | `{url: DATABASE_URL}` | Where credentials are injected. See [`resources.databases[].env`](#resources-databases-env). |
| `migrations` | object | none | Migrations to run. Also accepted as `migrations_path: <path>`. See [`resources.databases[].migrations`](#resources-databases-migrations). |

### `resources.databases[].env`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `url` | string | `DATABASE_URL` | Environment variable that receives the connection URL. Upper-cased; must match `[A-Z_][A-Z0-9_]{0,127}`. |

### `resources.databases[].migrations`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `path` | string | none | Relative path to the migrations directory. Absolute paths and `..` are rejected. |

### `resources.mailbox`

Gives the agent a real address at `<agent-name>@agents.a2acloud.io`. The SDK
parses and forwards this declaration; the control plane validates the options
and provisions the inbox.

```yaml
resources:
  mailbox:
    enabled: true
    allowed_senders:
      - alice@example.com
```

| Option | Type | Default | What it does |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Set `false` (or drop the key) to tear the mailbox down. |
| `allowed_senders` | list of strings | `[]` (owner only) | Sender allowlist — **default-deny**. The owner's address is always allowed; every other sender must be listed, so an empty list accepts mail from the owner alone. Addresses are lower-cased; each must look like an email and at most 50 are accepted. |

`mailbox: true` is shorthand for `{enabled: true}`. See
[Email inboxes](/concepts/email-inboxes).

## `self_healing`

Opt-in bounded source self-repair. `self_healing: true` is shorthand for
`{enabled: true}` with the defaults below.

| Key | Type | Default | Range | What it does |
| --- | --- | --- | --- | --- |
| `enabled` | bool | `true` | — | Turn the policy on. |
| `consecutive_failures` | integer | `1` | 1–10 | Failures in a row before a repair is attempted. |
| `window_seconds` | integer | `300` | 30–3600 | Window those failures must fall inside. |
| `cooldown_seconds` | integer | `900` | 60–86400 | Minimum gap between repair attempts. |
| `max_repairs_per_day` | integer | `3` | 1–20 | Daily repair cap. |
| `max_turns` | integer | `30` | 1–100 | Agent turns allowed per repair attempt. |
| `deployment_timeout_seconds` | integer | `1800` | 60–7200 | Time budget for the repair's deploy. |
| `require_tests` | bool | `true` | — | Refuse to ship a repair whose tests do not pass. |

The SDK validates and advertises this policy on the Agent Card; the control
plane enforces the limits.

## Meta-agent keys

`composition`, `goal`, and `memory` together form the meta-agent contract. They
are only read for Python projects.

### `composition`

Accepts a list (read as `sub_agents`) or an object.

```yaml
composition:
  planning: llm_dag
  max_nodes: 6
  sub_agents:
    - name: summarizer
      skills: [summarize]
```

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `sub_agents` | list | `[]` | Callable dependencies. Also accepted as `agents` or `children`. |
| `planning` | string | `llm_dag` | `llm_dag` or `deterministic_dag`. |
| `max_nodes` | integer | `8` | Maximum plan nodes. Must be positive. |
| `max_parallel` | integer | `3` | Maximum nodes run at once. Must be positive. |
| `max_replans` | integer | `1` | Replans allowed per run. |

### `composition.sub_agents[]`

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `name` | string | none | Agent name to call. Required unless `tag` is set. |
| `tag` | string | none | Discovery tag to resolve instead of a fixed name. |
| `version` | string | none | Pin a version. |
| `skills` | string \| list | `[]` | Tools this dependency is allowed to expose. |
| `default_args` | object | `{}` | Arguments merged into every call. |
| `required` | bool | `true` | Fail the run when the dependency cannot be resolved. |

### `goal`

A bare string is read as `objective`.

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `objective` | string | `""` | The durable objective. |
| `success_criteria` | string \| list | `[]` | What "done" means. |
| `constraints` | string \| list | `[]` | Limits the planner must respect. |

## `template_lineage`

Provenance for agents generated from a template. A bare string is read as
`template_ref`. Also accepted under the key `template`.

| Key | Type | Default | What it does |
| --- | --- | --- | --- |
| `template_ref` | string | none | Template identifier. |
| `template_version` | string | none | Template version. |
| `template_digest` | string | none | Content digest of the template. |
| `source_agent` | string | none | Agent the template came from. |
| `source_agent_version` | string | none | Version of that agent. |
| `source_repo_url` | string | none | Repository the template came from. |
| `source_revision` | string | none | Revision within that repository. |
| `instance_id` | string | none | Identifier for this generated instance. |
| `instance_version` | string | none | Version of this generated instance. |
| `update_policy` | string | `none` | `none`, `notify`, `propose`, or `auto_patch`. Anything other than `none` requires `template_ref` or `source_agent`. |
| `update_channel` | string | none | Channel to watch for updates. |
| `migration_skill` | string | none | Tool that applies a template update. |
| `schema_version` | string | `2026-06-02` | Lineage schema version. |

The policy is advisory: the platform decides whether an update is proposed,
applied, reviewed, or denied.

## What does not live here

- **Tools, auth model, and input/output schemas** come from the agent class
  (`@a2a.tool`, `auth_model`) or, for other languages, from the compiled
  `.a2a/agent.dsl.json`.
- **Secrets** are never stored in `a2a.yaml`. Local values come from the shell,
  `.env.local`, or `~/.a2a/credentials.json`; hosted values come from the
  dashboard.
- **Kubernetes manifests, Dockerfile, and CI workflow** are generated by the
  platform at deploy time from the keys above.

## See also

- [`a2a` CLI](/reference/cli) — the commands that read this file.
- [`a2a_pack.runtime`](/reference/runtime) — the models behind `runtime`,
  `resources`, `self_healing`, `composition`, `goal`, and `template_lineage`.
- [`a2a_pack.frontend`](/reference/frontend) — the model behind `frontend`.
- [Quickstart](/quickstart)
