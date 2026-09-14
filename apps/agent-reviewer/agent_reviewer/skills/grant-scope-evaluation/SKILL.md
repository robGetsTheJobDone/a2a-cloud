---
name: grant-scope-evaluation
description: Evaluate whether a tool's declared workspace grants match what its code actually does. Use when reviewing @a2a.tool grant_* parameters (also on bare @tool or the legacy @skill alias) against ctx.workspace, ctx.sandbox, and file-writing patterns in the tool body.
---
# Grant Scope Evaluation

A2A grants are the platform's principle-of-least-authority lever. Every
public tool declares the scope it intends to use; the platform mints a
matching grant; the runtime enforces it at the FUSE layer.

Your job is to flag mismatches between **declared** and **actual** scope.

## What the decorator declares

Look at the `@a2a.tool(...)` call (bare `@tool(...)` / `@skill(...)` in
legacy code — same fields).
The relevant fields:

* `grant_mode` — `read_only`, `read_write_overlay`, or absent.
* `grant_allow_patterns` — fnmatch globs the tool may *read*.
* `grant_deny_patterns` — fnmatch globs explicitly denied (overrides allow).
* `grant_outputs_prefix` — single prefix the tool writes outputs under.
* `grant_write_prefixes` — multiple write prefixes (more flexible than
  `outputs_prefix`).
* `grant_ttl_seconds` — how long the grant lives.
* `allow_scope_expansion` — whether `ctx.request_scope` may be called.

## What the tool body actually does

Read the tool body and tabulate:

1. **Reads** — every `ctx.workspace.read(...)` / `read_bytes`, and every
   built-in DeepAgents `read_file` / `grep` / `glob`. Each read implies a
   path or glob that must be matched by `grant_allow_patterns`.
2. **Writes** — every `ctx.workspace.write(...)`, `write_artifact`,
   `ctx.workspace_shell` / `workspace_python` invocation that produces
   files. Each write implies a path that must be under
   `grant_outputs_prefix` or one of `grant_write_prefixes`.
3. **Scope expansion** — any call to `ctx.request_scope(...)`. If present
   without `allow_scope_expansion=True` → `critical` (will raise at
   runtime).

## Common findings

* **Over-broad declared scope** (`info`/`warning`): grants the agent
  more than it uses, e.g. `grant_allow_patterns=("**",)` when the body
  only reads `agents/{name}/**`. Tighten the declaration.
* **Under-declared writes** (`critical`): body writes to a path not under
  any declared write prefix → FUSE returns `EACCES` and the tool fails.
* **Under-declared reads** (`warning`): body reads paths not in allow
  patterns → reads return "not found" even when the file exists.
* **Missing `stream=True`** combined with `allow_scope_expansion=True`
  (`critical`): scope expansion requires the SSE stream.
* **Long TTL on a fast tool** (`info`): `grant_ttl_seconds=3600` on a
  10-second tool is sloppy. Match TTL to expected runtime + buffer.

## Path template variables

Decorators support `{name}` and similar template variables that the
runtime substitutes from tool args. When evaluating coverage:

* Treat `agents/{name}/**` as "any path matching that template after the
  caller's `name` arg is substituted".
* Flag templates referencing args that don't exist on the tool (`critical`).

## Cross-tool consistency

If the agent has multiple tools writing to overlapping prefixes, they
should declare consistent patterns. Divergence is `warning`.

## Severity rubric

* `critical` — would fail at runtime (writes hit `EACCES`, scope expansion
  without `stream`, template references missing arg).
* `warning` — works but mismatched (over-broad scope, divergent siblings).
* `info` — tighten-the-declaration suggestions.
