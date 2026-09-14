---
name: a2apack-best-practices
description: Shape of a well-formed A2A Pack agent. Use when reviewing agent.py for class structure, @a2a.tool decorators (bare @tool / legacy @skill are the same object), type hints, error handling, idempotency, and timeouts.
---
# A2A Pack Best Practices

Use this skill when reviewing the *shape* of an agent — not its security
posture (that's `security-anti-patterns`) and not its grants (that's
`grant-scope-evaluation`).

## Class contract

* The class must inherit from `A2AAgent[ConfigT, AuthT]` with concrete
  generic parameters. Missing parameters → `warning`.
* Class variables `name`, `description`, `version` must be set. Missing →
  `warning`. `description` shorter than ~30 chars → `info`.
* `config_model` and `auth_model` must reference declared Pydantic
  models (or `NoAuth`). A mismatched generic and class var → `warning`.

## @a2a.tool decorators

The canonical style is namespaced: `import a2a_pack as a2a` plus
`@a2a.tool(...)`, which avoids collision with langchain's bare `@tool`.
Bare `@tool` and the legacy `@skill` are the same object with identical
behavior — do not flag either as an error; treat all three identically
below.

* Every public tool should declare `description`. Missing → `warning`.
* `timeout_seconds` should be set explicitly. Default is short; long-running
  tools that omit it will time out in production. Missing on a tool that
  does file work, LLM calls, or sandbox exec → `warning`.
* `stream=True` is required if the tool calls `ctx.emit_progress`,
  `ctx.ask`, `ctx.collect`, or `ctx.request_scope`. Missing while the body
  does emit → `critical`.
* `idempotent=True` matters for tools that can be safely retried. Tools
  that write files, mutate state, or charge money → `info` if not set
  explicitly (either direction is fine, just be intentional).

## Type hints

* Every tool parameter must have a concrete type hint (no `Any`, no missing
  annotation). The Card's `input_schema` is built from these. Missing → `warning`.
* Return type should be `dict[str, Any]`, a concrete Pydantic model, or `str`.
  Returning untyped values surprises callers → `info`.

## Error handling

* Tools should return structured error dicts (e.g. `{"error": "..."}`), not
  raise unhandled exceptions for *expected* failure modes (bad input, missing
  workspace, timeouts). Bare `raise` for invalid input → `info`.
* `try/except Exception` that swallows the error without logging or returning
  → `warning`.

## ctx.workspace / ctx.sandbox usage

* If the tool calls `ctx.workspace_backend()`, the class must declare
  `workspace_access = WorkspaceAccess.dynamic(...)`. Mismatch → `critical`.
* Subprocesses that produce user-visible files must run through
  `ctx.workspace_shell` or `ctx.workspace_python`, not raw
  `asyncio.create_subprocess_exec`. Already covered in
  `security-anti-patterns` but worth a second look here.

## Recursion limits

* If the tool invokes a DeepAgents graph via `ainvoke` or `astream_events`,
  it must pass `config={"recursion_limit": 500}` to match agent-builder.
  Missing → `warning`.

## Manifest (a2a.yaml)

* `name` must match the agent's `name` class var. Mismatch → `critical`.
* `entrypoint` must point at a real `module:Class`. Stub or broken → `critical`.
* `version` should be semantic (major.minor.patch). Bad shape → `info`.

## Severity rubric (specific to this skill)

* `critical` — breaks the runtime contract (stream/emit mismatch, manifest
  doesn't import, name disagreement).
* `warning` — works today but degrades the developer or caller experience
  (no timeout, no type hint, bare raise).
* `info` — style nudges.
