"""Build a LangGraph deepagents orchestrator wired to the a2a platform.

The orchestrator is per-request — call :func:`build_orchestrator` with a
fresh :class:`OrchestratorContext` each time and the tools close over the
user's identity + bucket + JWT. The resulting :class:`CompiledStateGraph`
can be invoked normally or streamed via the LangGraph runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator

from a2a_pack.deepagents import create_a2a_deep_agent

from .config import Settings, load_settings
from .hooks import PlatformHooks
from .tools import (
    build_dag_tools,
    build_discovery_tools,
    build_file_tools,
    build_handoff_tools,
    build_sandbox_tools,
)


MAIN_AGENT_RECURSION_LIMIT = 300


def main_agent_graph_config(*, thread_id: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"recursion_limit": MAIN_AGENT_RECURSION_LIMIT}
    if thread_id:
        config["configurable"] = {"thread_id": thread_id}
    return config


@dataclass(frozen=True)
class OrchestratorContext:
    """Per-request inputs the tool builders close over.

    Attributes:
        user_id: Numeric user id (matches the control plane's User.id).
        bucket: MinIO bucket name; defaults to ``user-<id>-files``.
        jwt: Optional CP JWT, forwarded to discovery + future CP calls.
        settings: Loaded :class:`Settings` (env-driven defaults).
        hooks: :class:`PlatformHooks` to surface SSE events, approval gates,
            audit writes back to a host runtime. Defaults to a no-op.
        llm_base_url/llm_api_key/llm_model: Optional per-request override for
            the main orchestrator model. When unset, platform LiteLLM
            settings are used.
        llm_temperature_enabled/llm_temperature: Controls whether a
            temperature parameter is sent to the chat model.
        llm_extra_body: Optional provider-specific request body fields.
    """

    user_id: int
    bucket: str
    jwt: str | None = None
    policy_controls: dict[str, Any] | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature_enabled: bool = True
    llm_temperature: float | None = None
    llm_extra_body: dict[str, Any] | None = None
    llm_metadata: dict[str, Any] | None = None
    thread_id: str | None = None
    settings: Settings = None  # type: ignore[assignment]
    hooks: PlatformHooks = None  # type: ignore[assignment]

    @classmethod
    def for_user(
        cls,
        user_id: int,
        *,
        jwt: str | None = None,
        policy_controls: dict[str, Any] | None = None,
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
        llm_model: str | None = None,
        llm_temperature_enabled: bool = True,
        llm_temperature: float | None = None,
        llm_extra_body: dict[str, Any] | None = None,
        llm_metadata: dict[str, Any] | None = None,
        thread_id: str | None = None,
        settings: Settings | None = None,
        hooks: PlatformHooks | None = None,
    ) -> "OrchestratorContext":
        s = settings or load_settings()
        return cls(
            user_id=user_id,
            bucket=f"user-{user_id}-files",
            jwt=jwt,
            policy_controls=policy_controls or {},
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            llm_temperature_enabled=llm_temperature_enabled,
            llm_temperature=llm_temperature,
            llm_extra_body=dict(llm_extra_body or {}),
            llm_metadata=dict(llm_metadata or {}),
            thread_id=thread_id,
            settings=s,
            hooks=hooks or PlatformHooks.noop(),
        )


SYSTEM_PROMPT = """\
You are the user's orchestrator. Use tools for real actions; never fabricate
tool results. Answer directly only when no tool is needed.

Tools:
- Workspace I/O: ``list_files``, ``read_file``, ``write_file``,
  ``delete_file``, ``deploy_agent_source``. Paths are workspace-relative.
  ``agents/<name>/...`` is that agent's Gitea source repo; ``repos/<name>/...``
  is a first-party repo mount. After coherent edits under
  ``agents/<name>/``, call ``deploy_agent_source(name)``.
- Sandbox: ``run_shell`` and ``run_python`` run in an isolated microVM with
  ``/workspace`` mounted. Use them for computation, installs, transforms, or
  shell/Python work that file tools cannot do.
- Discovery: ``discover_agent`` searches visible specialist agents;
  ``list_my_agents`` lists only this user's deployments.
- Handoff: ``call_agent`` invokes a discovered specialist. Pass ``args_json``
  as a JSON string.
- DAGs: use ``plan_agent_dag`` and ``execute_agent_dag`` for multi-agent work
  that naturally has dependent steps. Keep DAGs under 8 nodes and validate
  nontrivial graphs before execution.

Decision rules:
- Do not hardcode that a specialist exists. Use ``list_my_agents`` first for
  "my agent", "the bot I built", or iteration on past work; otherwise use
  ``discover_agent`` only when a specialist capability is likely useful.
- If no specialist fits, use sandbox or workspace tools yourself.
- Do not invent paths. Reuse shown paths. For discovery, call ``list_files`` on
  the narrowest directory with ``limit<=50``. Use recursive listings only for
  small trees or explicit requests; if truncated, narrow the path.
- When iterating on an existing agent, use agent-builder.build with the same
  ``name`` and a higher ``version`` so it overwrites the prior deployment.
- Read tool errors and adjust. Do not retry blindly.
- Keep final replies short; tool calls are visible to the user.
"""


def build_orchestrator(
    ctx: OrchestratorContext, *, checkpointer: Any | None = None,
) -> Any:
    """Build a compiled LangGraph deep-agent for ``ctx``.

    Returns the compiled graph from :func:`deepagents.create_deep_agent`.
    Callers invoke it with ``await graph.ainvoke({"messages": [...]})`` or
    iterate ``async for ev in graph.astream(...)``.

    If ``checkpointer`` is supplied, message state + virtual files
    persist across invocations keyed by ``thread_id``.
    """
    settings = ctx.settings or load_settings()
    ctx = OrchestratorContext(
        user_id=ctx.user_id,
        bucket=ctx.bucket or f"user-{ctx.user_id}-files",
        jwt=ctx.jwt,
        policy_controls=ctx.policy_controls or {},
        llm_base_url=ctx.llm_base_url,
        llm_api_key=ctx.llm_api_key,
        llm_model=ctx.llm_model,
        llm_temperature_enabled=ctx.llm_temperature_enabled,
        llm_temperature=ctx.llm_temperature,
        llm_extra_body=dict(ctx.llm_extra_body or {}),
        llm_metadata=dict(ctx.llm_metadata or {}),
        thread_id=ctx.thread_id,
        settings=settings,
        hooks=ctx.hooks or PlatformHooks.noop(),
    )
    llm_creds = SimpleNamespace(
        model=ctx.llm_model or settings.litellm_model,
        base_url=ctx.llm_base_url or (settings.litellm_url + "/v1"),
        api_key=ctx.llm_api_key or settings.litellm_key,
        temperature_mode="default" if ctx.llm_temperature_enabled else "omit",
        temperature=ctx.llm_temperature,
        extra_body=dict(ctx.llm_extra_body or {}),
        metadata=dict(ctx.llm_metadata or {}),
    )
    tools: list[Any] = []
    tools.extend(build_file_tools(ctx))
    tools.extend(build_sandbox_tools(ctx))
    tools.extend(build_discovery_tools(ctx))
    tools.extend(build_dag_tools(ctx))
    tools.extend(build_handoff_tools(ctx))
    kwargs: dict[str, Any] = {
        "tools": tools,
        "system_prompt": SYSTEM_PROMPT,
    }
    backend = _build_workspace_backend(ctx)
    if backend is not None:
        kwargs["backend"] = backend
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return create_a2a_deep_agent(
        ctx,
        creds=llm_creds,
        **kwargs,
    )


def _build_workspace_backend(ctx: OrchestratorContext) -> Any | None:
    """Use the SDK's durable DeepAgents backend when a2a-pack is installed.

    Without this, DeepAgents' built-in file tools write into LangGraph state
    only. The explicit file tools still used MinIO, but model-created files
    through ``write_file`` never appeared in the user's FUSE workspace.
    """
    try:
        from a2a_pack import Grant, HttpSandboxClient, WorkspaceAccess, WorkspaceMode
        from a2a_pack.deepagents import WorkspaceBackend
        from a2a_pack.workspace import MinIOWorkspaceClient
    except Exception:  # noqa: BLE001
        return None

    settings = ctx.settings or load_settings()
    workspace = MinIOWorkspaceClient(
        bucket=ctx.bucket,
        endpoint_url=settings.minio_endpoint,
        access_key_id=settings.minio_access_key,
        secret_access_key=settings.minio_secret_key,
        access=WorkspaceAccess.dynamic(
            max_files=1000,
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            require_reason=False,
        ),
        issuer=f"main-agent:user-{ctx.user_id}",
    )
    workspace.install_grant(
        Grant(
            grant_id=f"main-agent-user-{ctx.user_id}",
            issuer=f"main-agent:user-{ctx.user_id}",
            audience="main-agent",
            bucket=ctx.bucket,
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("**",),
            deny_patterns=(),
            outputs_prefix=None,
        )
    )
    sandbox = HttpSandboxClient(
        settings.sandbox_url,
        default_workspace=ctx.bucket,
        timeout_seconds=settings.sandbox_timeout_s,
        auth_token=settings.sandbox_token,
    )
    return WorkspaceBackend(workspace, sandbox=sandbox)


async def run_chat(
    ctx: OrchestratorContext,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """One-shot: build a graph, invoke once, return the final state."""
    graph = build_orchestrator(ctx)
    return await graph.ainvoke(
        {"messages": messages},
        config=main_agent_graph_config(),
    )


async def stream_chat(
    ctx: OrchestratorContext,
    messages: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Stream LangGraph events from a freshly-built orchestrator.

    Yields raw LangGraph ``astream`` events; the caller is responsible for
    flattening them into a UI-friendly SSE shape.
    """
    graph = build_orchestrator(ctx)
    async for event in graph.astream(
        {"messages": messages},
        config=main_agent_graph_config(),
    ):
        yield event
