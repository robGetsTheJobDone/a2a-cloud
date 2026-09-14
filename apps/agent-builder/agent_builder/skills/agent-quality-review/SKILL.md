---
name: agent-quality-review
description: Review a generated a2a-pack agent before sandbox testing or deploy. Use to catch broken cards, missing schemas, fake tools, unwired DeepAgents skills, missing runtime mirrors, unsafe IO, and stale live Agent Card schemas.
---
# Agent Quality Review

Before `test_agent_in_sandbox` and before deploy, review the generated project
as source code, not just as text.

## Required Files

Every generated project needs:

- `agent.py`
- `a2a.yaml`
- `requirements.txt`
- Optional `skills/<skill-name>/SKILL.md` bundles for nontrivial DeepAgents
  behavior
- Optional `frontend/` source when the agent ships a packed app

Run `list_agent_files` and read the files that matter. If the project was
deployed before, use `sync_agent_workspace_from_repo` before editing when the
builder needs to continue from the current managed repo source.

## Agent.py Review

Check these items:

- Public `@a2a.tool` methods (canonical: `import a2a_pack as a2a`; bare
  `@tool` and legacy `@skill` are the same object — not errors) are async and
  have `RunContext[...]` plus typed JSON arguments.
- The public schema is small and stable. No unbounded command strings, hidden
  prompt fragments, or provider credentials as public inputs.
- LLM-backed implementations read `ctx.llm` for both `CALLER_PROVIDED` and
  `PLATFORM` LLM provisioning. `PLATFORM_OR_CALLER_PROVIDED` is acceptable
  only for trusted platform/meta agents and must also read `ctx.llm`.
- Hosted generated/user agents that call an LLM should default to
  `llm_provisioning = LLMProvisioning.PLATFORM`. `LLMProvisioning.CALLER_PROVIDED`
  is acceptable for explicit BYOK wording. In both modes, the code must use
  `ctx.llm` and never read LiteLLM/provider keys directly.
- Deterministic/local-logic agents that do not call an LLM omit the concrete
  `llm_provisioning` declaration, remove unused `LLMProvisioning` imports, and
  do not read `ctx.llm`.
  Reject no-LLM agents that would require a user's LLM credential before
  invoking deterministic tools.
- DeepAgents code uses `create_a2a_deep_agent` or another `ctx.llm`-backed
  resolver, and checks `ctx.llm.api_key` before model construction.
- Any use of DeepAgents file tools passes `backend=ctx.workspace_backend()`.
- Any project skills are seeded into the backend and passed with
  `skills=skill_sources or None`.
- Custom subagents that need project skills include their own `skills` field.
- Deterministic tools do exact work; no fake canned-response tools.
- File-producing tools call `ctx.write_artifact` and `ctx.emit_artifact`.
- Every tool whose output schema advertises artifacts, generated files, or
  durable paths declares `grant_mode="read_write_overlay"`, nonempty
  `grant_allow_patterns`, `grant_outputs_prefix`, and `grant_write_prefixes`;
  its outputs prefix is covered by a write prefix matching the real path.
- Commands that create downloadable outputs run through `ctx.workspace_shell`
  or `ctx.workspace_python`; `/workspace` writes persist directly and other
  changed sandbox rootfs files are mirrored under `outputs/rootfs-captures/...`.
  Plain subprocesses in the agent container are not durable workspace writes.
- External calls, secrets, workspace access, and resources are
  declared on the class.

## A2A YAML Review

Mirror deployment-only details in `a2a.yaml` because the platform reads it
before importing user code:

```yaml
name: research-agent
version: 0.1.0
entrypoint: agent:ResearchAgent
expose:
  public: true
runtime:
  apt_packages: [ffmpeg]
  resources:
    cpu: "2"
    memory: 2Gi
    max_runtime_seconds: 900
```

Use `runtime.apt_packages` only for Debian system binaries. Python packages
belong in `requirements.txt`.

If a packed frontend is declared, verify the manifest and files line up:

```yaml
frontend:
  path: frontend
  build: npm run build
  dist: dist
  mount: /app
  auth: inherit
```

For React/Vite frontends, expect `frontend/package.json`,
`frontend/vite.config.js`, `frontend/src/App.jsx`, and `frontend/src/a2a.js`.
For static frontends, expect `frontend/dist/index.html`. The browser code must
not contain platform secrets, provider keys, LiteLLM keys, hard-coded deployment
URLs, or private stack details. It should load `/app/config.json`, use
`/app/a2a-client.js` or generated config endpoints, and call only the public
`@a2a.tool` schemas.

Reject custom frontend clients that bind the raw `{result, events, artifacts}`
invoke envelope to product state. The scaffolded `unwrapInvokeResponse` helper
must remain in the call path, or equivalent code must select `payload.result`
before rendering. If a tool produces a file, SVG, image, document, archive, or
other browser-safe asset, reject a UI that has no rendered/previewed result or
no visible download control. A “Copy” button alone is insufficient.

Then check the signed-out path, because a UI that renders is not a UI that
works. A public page whose skills are caller-funded is the normal case, not an
edge case: `invokeRequiresSession` is true whenever the agent declares
`llm_provisioning=PLATFORM` or `PlatformUserAuth`, so a visitor can always
reach the page while every button still needs a session.

Reject the frontend if any of these hold:

- It redirects to `config.auth.loginUrl` without first trying
  `config.auth.authorizeUrl`. The dashboard cookie is host-locked to the
  dashboard, so a bare login redirect returns the visitor still signed out and
  loops forever. Only `authorizeUrl` gives this origin its own session.
- It assumes `session.authenticated` is true, or shows a blank screen / a raw
  `sign in required` error when it is false.
- It offers no visible sign-in control while `invokeRequiresSession` is true.
- It treats a `401` from an invoke as fatal instead of re-authorizing.
- It presents a skill's `status: "setup_required"` (such as a missing LLM
  credential) as a crash rather than an actionable message.

## Sandbox And Live Card

Always run `test_agent_in_sandbox(name)` before deploy. Treat nonzero exit as
source failure, then read stderr, edit, and rerun.

When a frontend is present, inspect the sandbox output's `a2a frontend info`.
For React apps, make sure `frontend/package.json` defines a build script and
the deployed build process can produce `frontend/dist/index.html`.

After `cp_deploy_tarball`, compare `live_skills[].input_schema` with the
actual public `@a2a.tool` signatures. If a live schema is missing an argument or
shows an old shape, refresh or redeploy until the live Agent Card matches the
source.

## Minimal Good Result

For most generated agents, a good result is one public A2A `@a2a.tool`, one
workspace-backed DeepAgent, one or two internal DeepAgents skills, and a few
small deterministic tools. Resist broad endpoint sets unless the user asked
for a multi-operation agent.
