# code-editor-agent

A2A/MCP wrapper for OpenHarness-backed code editing.

The root `code-editor-agent` runs in shared runtime mode by default. A turn takes
an opted-in managed `agent_name`, checks the caller's control-plane JWT, mints a
short-lived write-scoped Gitea token, clones or updates that repo in a locked
workspace, refreshes CodeGraph, runs OpenHarness, and pushes source changes back
to Gitea. The token is released in `finally`.

Generated local wrappers are still scoped to one source directory. Before every
OpenHarness turn they refresh CodeGraph for that source tree and pass a
temporary MCP config that exposes `codegraph serve --mcp --no-watch`.

Useful local commands:

```bash
scripts/generate-code-editor-agents.py --list
scripts/generate-code-editor-agents.py
cd apps/code-editor-agent
a2a run --entrypoint agent:CodeEditorAgent
A2A_CODE_EDITOR_MODE=local a2a run --entrypoint agent:CodeEditorAgent
```

Generated wrappers live under `apps/code-editor-agent/generated/<agent>-code-editor`.
Each wrapper exports `status` and `turn`; a2a-pack also exposes those skills as
MCP tools at `/mcp`.

Shared `turn` accepts `agent_name`, `prompt`, optional `owner` / `ref`, and
OpenHarness session/runtime args including `session_name`, `continue_session`,
`resume_session`, `max_turns`, `permission_mode`, `output_format`, `dry_run`,
`bare`, `extra_args`, `timeout_seconds`, and `push_on_failure`.
