"""agent-builder — generate + deploy new agents from natural language.

Inner loop is a deepagents graph (see agent_builder/builder.py). The
outer A2AAgent surface is a single tool ``build`` that takes a
description, writes a scaffolded project to the user's workspace at
``agents/<name>/``, tests it in a sandbox, deploys it through the
control plane, and reports back the live URL.

This agent only works when invoked through the platform orchestrator,
because it needs three things forwarded from the caller:

  1. ``ctx.workspace.bucket`` — the user's MinIO bucket
  2. ``ctx.llm`` — the caller's saved LLM credential for the builder's own
     DeepAgents loop
  3. ``ctx.cp_jwt`` — the user's CP JWT, so cp_deploy_tarball can
     POST to /v1/agents/from-tarball as the user.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from typing import Any

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    LLMProvisioning,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
)

from agent_builder import BuilderContext, build_agent_builder


class BuilderConfig(BaseModel):
    pass


_URL_RE = re.compile(r"https?://[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}[\w/?#%&=.-]*")
DEEPAGENTS_RECURSION_LIMIT = 500
BUILDER_WALL_TIMEOUT_SECONDS = 60 * 60
BUILDER_GRANT_RUN_TIMEOUT_SECONDS = BUILDER_WALL_TIMEOUT_SECONDS - 60


class AgentBuilder(A2AAgent[BuilderConfig, NoAuth]):
    name = "agent-builder"
    description = (
        "Generate, test, and deploy new a2a-pack agents from natural language. "
        "Writes the project into the user's workspace, validates it in a "
        "sandbox, then ships it via the control plane."
    )
    version = "0.1.17"

    config_model = BuilderConfig
    auth_model = NoAuth
    tools_used = ("deepagents", "langgraph", "a2a-pack", "litellm", "microsandbox")
    llm_provisioning = LLMProvisioning.PLATFORM
    wants_cp_jwt = True
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )

    @a2a.tool(
        description=(
            "Generate, test, and deploy a new a2a agent. Pass a kebab-case "
            "name and a natural-language description of what the agent "
            "should do. Returns the live URL when ready."
        ),
        tags=["builder", "scaffold", "deepagents", "meta"],
        stream=True,
        timeout_seconds=BUILDER_WALL_TIMEOUT_SECONDS,
        cost_class="expensive",
        grant_mode="read_write_overlay",
        grant_allow_patterns=("agents/{name}/**",),
        grant_outputs_prefix="agents/{name}/",
        grant_write_prefixes=("agents/{name}/",),
        grant_ttl_seconds=BUILDER_WALL_TIMEOUT_SECONDS,
        grant_run_timeout_seconds=BUILDER_GRANT_RUN_TIMEOUT_SECONDS,
        grant_approval_timeout_seconds=180,
        grant_scope_approval_timeout_seconds=120,
    )
    async def build(
        self,
        ctx: RunContext[NoAuth],
        name: str,
        prompt: str,
        public: bool = True,
        version: str = "0.1.0",
        organization_slug: str = "",
    ) -> dict:
        # ctx.workspace is a property that raises PermissionError when no
        # workspace was provisioned by the runtime (e.g. when this agent
        # is invoked over the MCP gateway, which has no grant flow). Catch
        # it and surface a clean "I need to run through the orchestrator"
        # error rather than letting it bubble up as a 500.
        try:
            workspace = ctx.workspace
            bucket = getattr(workspace, "bucket", None)
        except PermissionError:
            workspace = None
            bucket = None
        if not bucket:
            return {
                "error": (
                    "no workspace grant; this agent only runs through the "
                    "a2acloud platform orchestrator (not the local MCP "
                    "gateway), because it needs a scoped MinIO bucket and "
                    "a CP JWT to write + deploy your project."
                ),
            }
        cp_jwt = getattr(ctx, "cp_jwt", None)
        if not cp_jwt:
            return {
                "error": "no CP JWT forwarded — caller must run me through "
                "the platform orchestrator so deploy can act on your behalf",
            }
        if not _is_valid_slug(name):
            return {"error": f"invalid name {name!r} — use kebab-case, 2-63 chars"}

        await ctx.emit_progress(f"building agent {name!r} in bucket {bucket}")
        creds = ctx.llm
        await ctx.emit_progress(
            f"llm: {creds.model} via {creds.source} ({creds.base_url})"
        )
        try:
            sandbox = ctx.sandbox
        except Exception:  # noqa: BLE001 - sandbox is optional in local/MCP runs.
            sandbox = None

        graph = build_agent_builder(
            BuilderContext(
                bucket=bucket,
                cp_jwt=cp_jwt,
                organization_slug=organization_slug or None,
                project_prefix=f"agents/{name}",
                workspace=workspace,
                sandbox=sandbox,
                grant_token=getattr(workspace, "_grant_token", None),
                llm_base_url=creds.base_url,
                llm_api_key=creds.api_key,
                llm_model=creds.model,
                llm_temperature_mode=creds.temperature_mode,
                llm_temperature=creds.temperature,
                llm_extra_body=creds.extra_body,
                llm_metadata=getattr(creds, "metadata", None),
            )
        )

        user_msg = (
            f"Build an a2a-pack agent named ``{name}``.\n\n"
            f"The agent should: {prompt}\n\n"
            f"Honor the structured Application contract in that request. For "
            f"a full_stack profile, use the full-stack scaffold and finish its "
            f"product frontend, backend tools, auth, managed database/migrations, "
            f"browser upload bridge when requested, and deterministic tests. "
            f"After writing every required source, frontend, migration, and test "
            f"file, run test_agent_in_sandbox and require every Python and "
            f"frontend check to pass, then "
            f"cp_deploy_tarball(name={name!r}, version={version!r}, "
            f"public={public}). Report the URL the platform gives back. "
            f"If the workspace already contains managed source, preserve its "
            f"a2a.yaml version exactly; the version argument is only a default "
            f"for a new scaffold, and an existing version must never be lowered."
        )

        final_state: dict[str, Any] = {}
        deployed_url: str | None = None
        last_reply: str = ""

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(20)
                await ctx.emit_progress(f"agent-builder still working on {name!r}")

        async def _run_build_graph() -> None:
            nonlocal deployed_url, final_state
            async for event in graph.astream_events(
                {"messages": [{"role": "user", "content": user_msg}]},
                version="v2",
                # Agent builds can legitimately run many tool/subagent turns:
                # scaffold, write skills/files, test, fix, retest, deploy, and
                # verify the live card. Keep this aligned with generated child
                # agents so complex DeepAgents workflows have the same budget.
                config={"recursion_limit": DEEPAGENTS_RECURSION_LIMIT},
            ):
                kind = event.get("event")
                tname = event.get("name") or ""
                data = event.get("data") or {}
                if kind == "on_tool_start":
                    raw = data.get("input") or {}
                    if isinstance(raw, dict) and set(raw.keys()) == {"input"}:
                        raw = raw["input"]
                    await ctx.emit_progress(f"→ {tname}({_one_line(raw)})")
                elif kind == "on_tool_end":
                    output = data.get("output")
                    summary = _summarize(tname, output)
                    await ctx.emit_progress(f"  {tname} ← {summary}")
                    if tname == "cp_deploy_tarball":
                        deployed_url = deployed_url or _find_deploy_url(output, name)
                elif kind == "on_chain_end" and not event.get("parent_ids"):
                    out = data.get("output")
                    if isinstance(out, dict):
                        final_state = out

        heartbeat_task = asyncio.create_task(_heartbeat())
        try:
            await asyncio.wait_for(
                _run_build_graph(),
                timeout=BUILDER_WALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return {
                "ok": False,
                "status": "failed",
                "stop_reason": "builder_timeout",
                "name": name,
                "workspace_dir": f"agents/{name}/",
                "error": (
                    "agent-builder exceeded its wall-clock budget before "
                    "reaching a terminal result"
                ),
            }
        except asyncio.CancelledError:
            return {
                "ok": False,
                "status": "canceled",
                "stop_reason": "builder_canceled",
                "name": name,
                "workspace_dir": f"agents/{name}/",
                "error": "agent-builder was canceled before it reached a terminal result",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "status": "failed",
                "stop_reason": "builder_failed",
                "name": name,
                "workspace_dir": f"agents/{name}/",
                "error": _build_failure_error(exc),
            }
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        for msg in final_state.get("messages") or []:
            content = (
                msg.get("content")
                if isinstance(msg, dict)
                else getattr(msg, "content", None)
            )
            if isinstance(content, str) and content.strip():
                last_reply = content
        deployed_url = deployed_url or _find_agent_url(last_reply, name)

        if not deployed_url:
            return {
                "ok": False,
                "name": name,
                "reply": last_reply[:2000],
                "warning": "build graph finished but no live URL was found; "
                "check progress events for errors.",
            }

        await ctx.emit_progress(f"deployed: {deployed_url}")
        return {
            "ok": True,
            "name": name,
            "version": version,
            "url": deployed_url,
            "workspace_dir": f"agents/{name}/",
            "reply": last_reply[:2000],
        }


def _is_valid_slug(name: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9-]{1,62}$", name))


def _one_line(args: Any, limit: int = 120) -> str:
    if isinstance(args, dict):
        bits = []
        for k, v in args.items():
            s = v if isinstance(v, str) else json.dumps(v)
            if len(s) > 60:
                s = s[:60] + "…"
            bits.append(f"{k}={s}")
        return ", ".join(bits)[:limit]
    return (json.dumps(args) if not isinstance(args, str) else args)[:limit]


def _summarize(name: str, output: Any) -> str:
    if output is None:
        return "ok"
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return output[:120] + ("…" if len(output) > 120 else "")
        return _summarize(name, parsed)
    if isinstance(output, dict):
        if "error" in output:
            return f"error: {str(output['error'])[:120]}"
        if name == "write_agent_file":
            return f"wrote {output.get('path')}"
        if name == "list_agent_files":
            return f"{len(output.get('files', []))} files"
        if name == "test_agent_in_sandbox":
            ec = output.get("exit_code")
            return f"exit={ec}"
        if name == "cp_deploy_tarball":
            url = output.get("url") or ""
            return f"shipped → {url}" if url else "shipped"
        return "ok"
    return str(output)[:120]


def _find_deploy_url(value: Any, agent_name: str) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return _find_agent_url(value, agent_name)
    if not isinstance(value, dict):
        return None
    if value.get("error"):
        return None
    if value.get("ok") is False or value.get("live") is False:
        return None
    return _find_agent_url(value.get("url") or value.get("live_url"), agent_name)


def _find_agent_url(value: Any, agent_name: str) -> str | None:
    expected = f"https://{agent_name}.a2acloud.io"
    if value is None:
        return None
    if isinstance(value, dict):
        for k in ("url", "live_url"):
            v = value.get(k)
            if isinstance(v, str) and v.rstrip("/") == expected:
                return v
        # JSON-stringified outputs from tools.
        return _find_agent_url(json.dumps(value), agent_name)
    if isinstance(value, str):
        for m in _URL_RE.finditer(value):
            candidate = m.group(0).rstrip("/")
            if candidate == expected:
                return candidate
    return None


def _build_failure_error(exc: BaseException) -> str:
    message = str(exc)
    lowered = message.lower()
    if (
        "budget has been exceeded" in lowered
        or "budget exceeded" in lowered
        or "ratelimiterror" in type(exc).__name__.lower()
        or "rate limit" in lowered
    ):
        return (
            "LLM budget exceeded before deployment; retry with a larger budget "
            "or a smaller build scope"
        )
    return f"build graph failed: {type(exc).__name__}: {exc}"


# rebuild against a2a-pack 0.1.82 for LiteLLM metadata and long builder grants
