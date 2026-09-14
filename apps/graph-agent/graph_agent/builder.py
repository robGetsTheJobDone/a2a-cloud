"""Build a deepagents chart-maker.

Caller (``agent.py``'s ``chart`` skill) constructs a :class:`ChartContext`
per invocation, builds the graph, and invokes it once. Stateless.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from a2a_pack.deepagents import create_a2a_deep_agent

from .config import Settings, load_settings
from .tools import ToolContext, build_tools


@dataclass(frozen=True)
class ChartContext:
    bucket: str
    settings: Settings | None = None
    workspace_backend: Any | None = None
    grant_token: str | None = None
    # Optional LLM creds forwarded by the caller. When None, the
    # builder falls back to the platform defaults from Settings.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature_mode: str | None = None
    llm_temperature: float | None = None
    llm_extra_body: dict[str, Any] | None = None


SYSTEM_PROMPT = """\
You make charts. The user's workspace lives at /workspace inside an
isolated Python microVM you reach via the run_python_in_sandbox tool.

Your tools:
  - list_workspace_files(): list everything currently in /workspace
  - read_csv_sample(path, rows): peek the head of a CSV
  - run_python_in_sandbox(code): execute Python; the sandbox installs
    pandas + matplotlib + numpy automatically. Set matplotlib.use('Agg')
    before importing pyplot. Save figures to /workspace/outputs/<name>.png
    after os.makedirs('/workspace/outputs', exist_ok=True).

Discipline:
  - If the caller named a file, read_csv_sample first to learn the columns.
  - If no file was named, list_workspace_files and pick something obvious,
    OR invent a minimal illustrative dataset that matches the prompt and
    say so.
  - Never fabricate values that look like they came from the user's data.
  - Run the chart code with run_python_in_sandbox. If exit_code != 0,
    read stderr and adjust — don't retry the same broken code.
  - When done, reply with the workspace path of the PNG you wrote.

Keep the rendered chart simple, readable, and titled.
"""


def build_chart_agent(ctx: ChartContext) -> Any:
    settings = ctx.settings or load_settings()
    tools = build_tools(
        ToolContext(
            bucket=ctx.bucket,
            settings=settings,
            grant_token=ctx.grant_token,
        )
    )
    # Prefer caller-forwarded creds; fall back to platform defaults so
    # the agent still runs against the in-cluster LiteLLM if the caller
    # didn't register their own keys.
    llm_creds = SimpleNamespace(
        model=ctx.llm_model or settings.litellm_model,
        base_url=ctx.llm_base_url or (settings.litellm_url + "/v1"),
        api_key=ctx.llm_api_key or settings.litellm_key,
        temperature_mode=ctx.llm_temperature_mode or "default",
        temperature=ctx.llm_temperature,
        extra_body=dict(ctx.llm_extra_body or {}),
    )
    kwargs: dict[str, Any] = {
        "tools": tools,
        "system_prompt": SYSTEM_PROMPT,
    }
    if ctx.workspace_backend is not None:
        kwargs["backend"] = ctx.workspace_backend
    return create_a2a_deep_agent(
        ctx,
        creds=llm_creds,
        **kwargs,
    )
