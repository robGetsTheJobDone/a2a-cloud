"""Graph agent: deepagents-driven chart maker.

One tool — ``chart`` — hands the prompt + workspace bucket to a fresh
deepagents graph that owns the planning, tool calls, and matplotlib
generation. The agent runs the LangGraph once and returns the final
assistant reply plus a best-effort chart_path extracted from the tail
of the graph's tool outputs.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent, LLMProvisioning, NoAuth, RunContext, WorkspaceAccess,
    WorkspaceMode,
)

from graph_agent import ChartContext, build_chart_agent


class GraphConfig(BaseModel):
    pass


_PNG_PATH_RE = re.compile(r"/workspace/(outputs/[^\s'\"]+\.png)")


class GraphAgent(A2AAgent[GraphConfig, NoAuth]):
    name = "graph-agent"
    description = (
        "Deepagents-driven chart maker: a LangGraph picks the right tool "
        "calls (workspace listing, CSV sampling, matplotlib in a sandbox) "
        "from a natural-language prompt and writes a PNG to outputs/."
    )
    version = "0.4.0"

    config_model = GraphConfig
    auth_model = NoAuth
    tools_used = ("deepagents", "langgraph", "litellm", "microsandbox", "matplotlib")
    # Use the caller's own LLM key — the dev (this author) doesn't want
    # to eat the inference bill for every chart. The marketplace will
    # show "$0.05 / chart" — that's pure compute markup, no LLM cost.
    llm_provisioning = LLMProvisioning.CALLER_PROVIDED
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )

    @a2a.tool(
        description=(
            "Render a chart described by ``prompt``. Optionally hint with "
            "``data_path`` (workspace-relative CSV); the agent will list "
            "files itself if no path is given. Output goes to "
            "``outputs/<output_name>``."
        ),
        tags=["visualization", "chart"],
        stream=True,
        timeout_seconds=900,
        cost_class="expensive",
        grant_mode="read_write_overlay",
        grant_allow_patterns=("{data_path}",),
        grant_outputs_prefix="outputs/",
        grant_ttl_seconds=960,
        grant_run_timeout_seconds=900,
    )
    async def chart(
        self,
        ctx: RunContext[NoAuth],
        prompt: str,
        data_path: str | None = None,
        output_name: str = "chart.png",
    ) -> dict:
        bucket = getattr(ctx.workspace, "bucket", None)
        if not bucket:
            return {"error": "no workspace grant; refusing to run"}

        await ctx.emit_progress(f"prompt: {prompt}")
        # Pull whichever LLM creds the platform handed us — caller's if
        # they registered keys, else the in-cluster LiteLLM default.
        creds = ctx.llm
        await ctx.emit_progress(
            f"llm: {creds.model} via {creds.source} ({creds.base_url})"
        )
        graph = build_chart_agent(ChartContext(
            bucket=bucket,
            workspace_backend=ctx.workspace_backend(),
            grant_token=getattr(ctx.workspace, "_grant_token", None),
            llm_base_url=creds.base_url,
            llm_api_key=creds.api_key,
            llm_model=creds.model,
            llm_temperature_mode=creds.temperature_mode,
            llm_temperature=creds.temperature,
            llm_extra_body=creds.extra_body,
        ))

        user_msg = _format_user_message(
            prompt=prompt, data_path=data_path, output_name=output_name,
        )
        await ctx.emit_progress(
            "starting chart graph (pip install pandas+matplotlib on first "
            "run can take 30-60s)"
        )

        # Stream the inner graph so the caller (and ultimately the dashboard)
        # sees each tool call as it happens instead of one minute of silence.
        final_state: dict[str, Any] = {}
        try:
            async for event in graph.astream_events(
                {"messages": [{"role": "user", "content": user_msg}]},
                version="v2",
            ):
                kind = event.get("event")
                name = event.get("name") or ""
                data = event.get("data") or {}
                if kind == "on_tool_start":
                    raw = data.get("input") or {}
                    if isinstance(raw, dict) and set(raw.keys()) == {"input"}:
                        raw = raw["input"]
                    await ctx.emit_progress(f"→ {name}({_one_line(raw)})")
                elif kind == "on_tool_end":
                    summary = _summarize_tool_output(name, data.get("output"))
                    await ctx.emit_progress(f"  {name} ← {summary}")
                elif kind == "on_chain_end" and not event.get("parent_ids"):
                    out = data.get("output")
                    if isinstance(out, dict):
                        final_state = out
        except Exception as exc:  # noqa: BLE001
            return {"error": f"chart graph failed: {type(exc).__name__}: {exc}"}

        messages = final_state.get("messages") or []
        reply = _last_ai_text(messages) or ""
        chart_path = _find_chart_path(messages) or f"outputs/{output_name.lstrip('/')}"

        await ctx.emit_progress(f"done — chart at {chart_path}")
        return {
            "ok": True,
            "bucket": bucket,
            "prompt": prompt,
            "data_path": data_path,
            "chart_path": chart_path,
            "reply": reply[:1000],
            "n_messages": len(messages),
        }


def _format_user_message(*, prompt: str, data_path: str | None, output_name: str) -> str:
    parts = [
        f"Make a chart of: {prompt}.",
        f"Save the PNG to /workspace/outputs/{output_name.lstrip('/')}.",
    ]
    if data_path:
        parts.append(
            f"Hint: the data lives at /workspace/{data_path.lstrip('/')} — "
            "sample it before plotting."
        )
    else:
        parts.append(
            "No data path was provided. Either list_workspace_files for a "
            "reasonable candidate, or invent minimal illustrative data and "
            "tell me you did so."
        )
    return "\n".join(parts)


def _last_ai_text(messages: list[Any]) -> str | None:
    for m in reversed(messages):
        if isinstance(m, dict):
            if m.get("role") == "assistant":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c
        else:
            if getattr(m, "type", None) == "ai":
                c = getattr(m, "content", None)
                if isinstance(c, str) and c.strip():
                    return c
    return None


def _find_chart_path(messages: list[Any]) -> str | None:
    """Look at tool outputs for the first /workspace/outputs/*.png we see."""
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if not isinstance(content, str):
            continue
        match = _PNG_PATH_RE.search(content)
        if match:
            return match.group(1)
        # Some tools return JSON; scan that too.
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            continue
        flat = json.dumps(parsed)
        match = _PNG_PATH_RE.search(flat)
        if match:
            return match.group(1)
    return None


def _one_line(args: Any, limit: int = 120) -> str:
    """Render a tool input as a single-line preview for progress events."""
    if isinstance(args, dict):
        bits = []
        for k, v in args.items():
            s = v if isinstance(v, str) else json.dumps(v)
            if len(s) > 60:
                s = s[:60] + "…"
            bits.append(f"{k}={s}")
        out = ", ".join(bits)
    else:
        out = json.dumps(args) if not isinstance(args, str) else args
    return out[:limit] + ("…" if len(out) > limit else "")


def _summarize_tool_output(name: str, output: Any) -> str:
    """One-line summary of a tool result."""
    if output is None:
        return "ok"
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return output[:120] + ("…" if len(output) > 120 else "")
        return _summarize_tool_output(name, parsed)
    if isinstance(output, dict):
        if "error" in output:
            return f"error: {str(output['error'])[:120]}"
        if name == "list_workspace_files":
            return f"{len(output.get('files', []))} files"
        if name == "read_csv_sample":
            sample = output.get("sample", "")
            return f"{len(sample)} bytes"
        if name == "run_python_in_sandbox":
            ec = output.get("exit_code")
            return f"exit={ec}" if ec is not None else "ran"
        return "ok"
    return str(output)[:120]
