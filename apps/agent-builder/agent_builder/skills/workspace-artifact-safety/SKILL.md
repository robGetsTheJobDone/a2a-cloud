---
name: workspace-artifact-safety
description: Write A2A agents that use ctx.workspace_backend, workspace grants, artifacts, sandbox commands, and file paths safely. Use for agents that read files, create outputs, run subprocesses, convert media, or return downloadable artifacts.
---
# Workspace And Artifact Safety

Generated agents run with explicit workspace grants. File work must fit those
grants and produce artifacts callers can find.

## Workspace Access

If `agent.py` uses `ctx.workspace`, `ctx.workspace_backend()`, or a DeepAgents
graph with file tools, declare workspace access on the class:

```python
from a2a_pack import WorkspaceAccess, WorkspaceMode

workspace_access = WorkspaceAccess.dynamic(
    max_files=128,
    allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    require_reason=False,
)
```

Pass the backend into DeepAgents:

```python
backend = ctx.workspace_backend()
graph = create_a2a_deep_agent(ctx, backend=backend, skills=skill_sources or None)
```

For stable, human-readable paths, ask model-created files to use
`/workspace/outputs/...`. When writing through `ctx.workspace`, use
workspace-relative paths such as `outputs/report.md`.

Class-level workspace access only declares capability. Each public tool that
writes a stable path must declare the first-grant policy that makes that path
writable for Agent API calls and handoffs:

```python
@a2a.tool(
    description="Write the requested report",
    grant_mode="read_write_overlay",
    grant_allow_patterns=("outputs/reports/**",),
    grant_outputs_prefix="outputs/reports/",
    grant_write_prefixes=("outputs/reports/",),
)
async def write_report(ctx: RunContext[NoAuth], request: ReportRequest) -> ReportResult:
    ...
```

The outputs prefix must be covered by at least one write prefix, and the allow
pattern must include every file the implementation may read or write. Repeat
the policy on every artifact-producing `@a2a.tool`; policies are per skill.

## Artifact Pattern

For user-visible outputs, return a small JSON object and emit an artifact:

```python
async def save_report(ctx: RunContext[NoAuth], report: str) -> dict[str, str]:
    ref = await ctx.write_artifact(
        "report.md",
        report.encode("utf-8"),
        "text/markdown",
    )
    await ctx.emit_artifact(ref)
    return {"artifact_uri": ref.uri, "name": ref.name}
```

For binary files, use the correct media type:

```python
ref = await ctx.write_artifact("frames.zip", zip_bytes, "application/zip")
await ctx.emit_artifact(ref)
```

This is the platform's first-class artifact primitive. Browser-facing products
also return a bounded descriptor such as
`{"name": ref.name, "media_type": ref.mime_type, "uri": ref.uri}`. Never
return only an internal workspace/object-store path. Match declared output
filenames and media types exactly so Studio can prove the emitted artifact and
the visible preview/download.

## Workspace-Mounted Execution

Media, rendering, archive, and data conversion agents must run commands through
the workspace-mounted sandbox helpers when those commands create files:

```python
async def run_checked(script: str, timeout_s: int = 120) -> dict[str, str | int]:
    result = await ctx.workspace_shell(
        script,
        image="python:3.11-slim",
        timeout_seconds=timeout_s,
        memory_mib=1024,
        cpus=1,
    )
    return {
        "ok": 1 if result.ok else 0,
        "returncode": result.exit_code,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
```

Commands may write under the grant's `write_prefixes` for stable paths, or to
normal tool defaults such as `/tmp`, `/root`, or `/app`. The platform sandbox
mirrors changed files outside `/workspace` into `outputs/rootfs-captures/...`.
Do not use `asyncio.create_subprocess_exec(...)` for durable outputs. Plain
subprocesses run inside the agent container, which is not mounted to the caller
workspace and is not rootfs-captured.

If a nonzero command still produced a usable output, preserve the output and
include the command failure as a warning instead of discarding the file.

## Scope Expansion

Only use `ctx.request_scope(...)`, `ctx.ensure_read(...)`, or
`ctx.ensure_write(...)` when the public `@a2a.tool` declares
`allow_scope_expansion=True`. Explain the reason in plain language and keep
the requested read/write patterns narrow. New write prefixes always require
caller approval.

```python
@a2a.tool(
    description="Analyze a protected folder after the caller approves access",
    allow_scope_expansion=True,
)
async def analyze_folder(ctx: RunContext[NoAuth], folder: str) -> dict[str, str]:
    await ctx.ensure_read(
        reason=f"Need to read {folder} to analyze the requested files",
        patterns=(f"{folder.rstrip('/')}/**",),
    )
    return {"status": "approved"}
```

## Safety Checklist

Do not hard-code absolute local paths. Do not expose arbitrary shell command
strings as public inputs. Do not write secrets to artifacts. Do not use
`wants_cp_jwt=True` for normal agents. Do not assume a file exists; check and
return a structured error that the caller can act on.
