# agent-builder

The platform's own meta-agent. Generates, tests, and deploys new
a2a-pack agents from a natural-language description.

## What it does

The single `build(name, prompt)` tool:

1. Builds an inner `deepagents` LangGraph configured with project tools and
   packaged builder skills:
   - `deepagent-agent-design` — when to use DeepAgents skills, subagents,
     workspace backends, and deterministic tools
   - `a2apack-agent-authoring` — how to shape `agent.py`, public `@a2a.tool`
     schemas, runtime metadata, resources, and auth
   - `deepagents-implementation-patterns` — concrete `create_deep_agent`
     wiring for `ctx.llm` credentials, skills, tools, and subagents
   - `workspace-artifact-safety` — workspace grants, artifacts, bounded
     subprocesses, and scope expansion
   - `agent-quality-review` — pre-sandbox and pre-deploy checks for generated
     agents
   - `list_agent_files(name)` / `write_agent_file(name, path, content)`
     / `read_agent_file(name, path)` — boto3 → MinIO, scoped to
     `agents/<name>/` in the caller's workspace
   - `write_agent_skill(...)` — creates valid DeepAgents
     `skills/<skill-name>/SKILL.md` bundles for generated agents, so rich
     behavior lives in progressive-disclosure skills instead of fake tools
   - `init_agent_template(name, description, frontend="react"|"static"|"none")`
     — starts from the current `a2a-pack` scaffold and can include packed
     frontend templates under `frontend/` for app-like agents
   - `test_agent_in_sandbox(name)` — tarballs the workspace project,
     spins a microVM with the public `a2a-pack` wheel installed, runs
     `a2a card` to verify the scaffold is valid, and prints frontend metadata
     when a packed app is declared
   - `cp_deploy_tarball(name, version, public)` — POSTs the tarball
     to `/v1/agents/from-tarball` on the user's behalf using their
     forwarded CP JWT
   - `cp_deploy_source_repo(name)` — POSTs the current managed source repo
     to `/v1/agents/{name}/source/deploy` when source edits already live in
     Gitea
2. Streams the graph's tool calls back to the dashboard as
   `agent_progress` events (so the user watches the build happen in
   real time, not in silence).
3. Returns the live URL when the new agent finishes deploying.

## What it needs forwarded

Three platform-managed bits land on the inner tool via `RunContext`:

| Field | Source | Required? |
|---|---|---|
| `ctx.workspace.bucket` | grant minted by the orchestrator | yes |
| `ctx.llm` | caller's saved LLM credential, routed by the platform (Card declares `llm_provisioning=platform`) | yes |
| `ctx.cp_jwt` | caller's CP JWT (Card declares `wants_cp_jwt=True`) | yes — used by `cp_deploy_tarball` |

The user opts into all three when they pick `agent-builder` from the
marketplace — the dashboard already surfaces these on the agent card.

## LLM credentials

The builder uses your saved LLM credential. The deployed agent is yours forever;
subsequent invocations of a generated LLM agent also use the caller's saved LLM
credential via `ctx.llm`.

## How to call it

In the dashboard chat (Workspace tab):

```
use agent-builder.build to make a new agent named "csv-sanitizer" that takes a CSV path, strips whitespace from every column, deduplicates rows, and writes the cleaned file back next to the original
```

The orchestrator will discover agent-builder, hand off with the
required forwards, you'll watch the inner deepagents scaffold + test +
deploy in real time, then get a live URL.

## Local dev

```bash
cd apps/agent-builder
python3 -m venv .venv
.venv/bin/pip install -e ../a2a -r requirements.txt
.venv/bin/a2a card                # see what the Card looks like
.venv/bin/a2a validate
```

You can't run the full tool locally without a workspace bucket + CP
JWT — those come from the platform at invoke time.
