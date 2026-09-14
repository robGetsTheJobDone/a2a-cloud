"""Core MCP JSON-RPC handler — transport-agnostic.

The handler maps the MCP wire protocol onto an :class:`A2AAgent`:

- ``initialize`` advertises tool support and echoes the server identity
  from the agent's :class:`AgentCard`.
- ``tools/list`` enumerates the agent's skills.
- ``tools/call`` dispatches into :meth:`A2AAgent.invoke_json`.

A transport (stdio, HTTP) calls :meth:`MCPServer.handle` with each
decoded JSON-RPC message and forwards the returned dict back to the
client. Notifications return ``None``.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from typing import Any, Awaitable, Callable

from ..agent import A2AAgent, SkillInputError, SkillNotFound, SkillSpec
from ..cli import platform as _platform
from ..auth import NoAuth
from ..context import LLMCreds, LocalRunContext, MissingScopes, RunContext
from ..consumer_setup_runtime import (
    fetch_consumer_setup_from_cp,
)
from ..runtime import LLMProvisioning

MCP_PROTOCOL_VERSION = "2025-06-18"
_PLATFORM_LLM_GRANT_GRACE_SECONDS = 30

# JSON-RPC error codes — standard plus MCP additions.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

log = logging.getLogger(__name__)


ContextBuilder = Callable[[str], Awaitable[RunContext[Any]] | RunContext[Any]]
"""Factory called per tools/call with the skill name; returns the RunContext."""

ExtraToolHandler = Callable[
    [str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]
]
"""Optional handler for transport/runtime-provided virtual MCP tools."""

ToolCallHandler = Callable[
    [str, dict[str, Any], dict[str, Any]],
    Awaitable[dict[str, Any] | None] | dict[str, Any] | None,
]
"""Optional handler that may intercept any ``tools/call`` before default dispatch."""

ToolsProvider = Callable[[], list[dict[str, Any]]]
"""Optional replacement renderer for ``tools/list``."""


async def _post_mcp_tracking(cp_url: str, cp_jwt: str, event: dict) -> None:
    """Best-effort POST of a subagent-run tracking event to the control-plane.

    Mirrors the HTTP /invoke handler's ``_post_tracking`` so MCP tool-calls
    record a run in the control-plane ``subagent_runs`` table. Swallows all
    exceptions — tracking must never break the tool call.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{cp_url.rstrip('/')}/v1/me/subagent-runs/track",
                json=event,
                headers={"authorization": f"bearer {cp_jwt}"},
            )
    except Exception:  # noqa: BLE001
        log.debug("failed to post mcp subagent tracking event", exc_info=True)


def _apply_caller_meta(ctx: RunContext[Any], meta: dict[str, Any]) -> None:
    """Mutate ``ctx`` to carry caller-supplied credentials forwarded by an
    MCP gateway (e.g. ``a2amcp``).

    Recognized keys: ``cp_jwt`` + ``cp_url`` (forwarded to skills via the
    ``ctx.cp_jwt`` / ``ctx.cp_url`` properties); ``bucket`` (mints a
    workspace whose ``bucket`` attribute resolves, AND overrides
    ``ctx.write_artifact`` to persist to MinIO so outputs survive past
    the request — the default in-memory artifacts dict drops them on
    pod restart, which the early version of this shim accidentally
    inherited).
    """
    import os

    cp_jwt = meta.get("cp_jwt")
    cp_url = meta.get("cp_url")
    bucket = meta.get("bucket")
    if isinstance(cp_jwt, str) and cp_jwt:
        ctx._cp_jwt = cp_jwt  # noqa: SLF001
    if isinstance(cp_url, str) and cp_url:
        ctx._cp_url = cp_url  # noqa: SLF001
    if not isinstance(bucket, str) or not bucket:
        return

    if getattr(ctx, "_workspace", None) is None:
        from ..workspace import (
            LocalWorkspaceClient,
            MinIOWorkspaceClient,
            WorkspaceAccess,
            WorkspaceMode,
        )

        access = WorkspaceAccess.dynamic(
            max_files=256,
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
            require_reason=False,
            require_human_approval=False,
        )
        endpoint = os.environ.get("A2A_MINIO_ENDPOINT")
        access_key = os.environ.get("A2A_MINIO_ACCESS_KEY", "")
        secret_key = os.environ.get("A2A_MINIO_SECRET_KEY", "")
        if endpoint:
            ctx._workspace = MinIOWorkspaceClient(  # noqa: SLF001
                bucket=bucket,
                endpoint_url=endpoint,
                access_key_id=access_key,
                secret_access_key=secret_key,
                access=access,
                issuer="mcp-gateway",
            )
        else:
            ctx._workspace = LocalWorkspaceClient(  # noqa: SLF001
                files={}, access=access, bucket=bucket, issuer="mcp-gateway",
            )

    # Persist ctx.write_artifact outputs to MinIO under
    # ``outputs/{task_id}/{name}`` so users see them in their bucket
    # instead of memory://. Pod needs ``A2A_MINIO_*`` env — when not
    # set, fall back to the in-memory default (no regression).
    endpoint = os.environ.get("A2A_MINIO_ENDPOINT")
    access_key = os.environ.get("A2A_MINIO_ACCESS_KEY", "")
    secret_key = os.environ.get("A2A_MINIO_SECRET_KEY", "")
    if endpoint:
        _bind_s3_write_artifact(ctx, bucket, endpoint, access_key, secret_key)


async def _apply_consumer_setup(
    *,
    agent: A2AAgent,
    ctx: RunContext[Any],
    meta: dict[str, Any],
) -> None:
    if _apply_inline_consumer_setup(ctx, meta):
        return
    if not _agent_declares_consumer_setup(agent):
        return

    cp_jwt = meta.get("cp_jwt") or getattr(ctx, "_cp_jwt", None)
    cp_url = meta.get("cp_url") or getattr(ctx, "_cp_url", None)
    if not isinstance(cp_jwt, str) or not cp_jwt:
        return
    if not isinstance(cp_url, str) or not cp_url:
        return

    payload = await _fetch_consumer_setup_from_cp(
        cp_url=cp_url,
        cp_jwt=cp_jwt,
        agent_name=type(agent).name,
    )
    _merge_consumer_setup(ctx, payload)


async def _apply_llm_creds(
    *,
    agent: A2AAgent,
    ctx: RunContext[Any],
    meta: dict[str, Any],
) -> None:
    if isinstance(getattr(ctx, "_llm_creds", None), LLMCreds):
        return

    provisioning = getattr(type(agent), "llm_provisioning", LLMProvisioning.PLATFORM)
    inline_creds = meta.get("llm_creds")
    if isinstance(inline_creds, dict) and _llm_accepts_caller_creds(provisioning):
        _set_llm_creds(ctx, inline_creds, source="caller")
        return
    if "llm_provisioning" not in type(agent).__dict__:
        return

    if not _llm_accepts_platform_creds(provisioning):
        return

    cp_jwt = meta.get("cp_jwt") or getattr(ctx, "_cp_jwt", None)
    cp_url = meta.get("cp_url") or getattr(ctx, "_cp_url", None)
    if not isinstance(cp_jwt, str) or not cp_jwt:
        raise RuntimeError(
            "LLM key required. Add an LLM credential in Settings > "
            "LLM credentials before running this MCP tool."
        )
    if not isinstance(cp_url, str) or not cp_url:
        raise RuntimeError(
            "LLM key required. Add an LLM credential in Settings > "
            "LLM credentials before running this MCP tool."
        )

    try:
        payload = await _fetch_platform_llm_creds_from_cp(
            cp_url=cp_url,
            cp_jwt=cp_jwt,
            agent_name=type(agent).name,
            ttl_seconds=_agent_platform_llm_ttl_seconds(agent),
        )
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        message = (
            "Platform-funded calls are exhausted. Add your model key at "
            f"{_platform.dashboard_url()}/llm-keys to continue."
            if "platform_trial_exhausted" in detail
            else (
                "LLM key required. Add an LLM credential in Settings > "
                "LLM credentials before running this MCP tool."
            )
        )
        log.warning("%s for %s: %s", message, type(agent).name, exc)
        raise RuntimeError(message) from exc
    llm_creds = payload.get("llm_creds") if isinstance(payload, dict) else None
    if isinstance(llm_creds, dict):
        _set_llm_creds(ctx, llm_creds, source=str(llm_creds.get("source") or "user"))


def _llm_accepts_caller_creds(provisioning: LLMProvisioning | str) -> bool:
    value = provisioning.value if isinstance(provisioning, LLMProvisioning) else str(provisioning)
    return value in {
        LLMProvisioning.PLATFORM.value,
        LLMProvisioning.CALLER_PROVIDED.value,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED.value,
    }


def _llm_accepts_platform_creds(provisioning: LLMProvisioning | str) -> bool:
    value = provisioning.value if isinstance(provisioning, LLMProvisioning) else str(provisioning)
    return value in {
        LLMProvisioning.PLATFORM.value,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED.value,
    }


def _agent_platform_llm_ttl_seconds(agent: A2AAgent) -> int:
    resources = getattr(type(agent), "resources", None)
    max_runtime = getattr(resources, "max_runtime_seconds", None)
    try:
        seconds = int(max_runtime)
    except (TypeError, ValueError):
        seconds = 300
    if seconds <= 0:
        seconds = 300
    return seconds + _PLATFORM_LLM_GRANT_GRACE_SECONDS


def _set_llm_creds(
    ctx: RunContext[Any],
    llm_creds: dict[str, Any],
    *,
    source: str,
) -> None:
    api_key = str(llm_creds.get("api_key") or "")
    base_url = str(llm_creds.get("base_url") or "")
    model = str(llm_creds.get("model") or "")
    if not (api_key and base_url and model):
        return
    temperature = llm_creds.get("temperature")
    try:
        parsed_temperature = float(temperature) if temperature is not None else None
    except (TypeError, ValueError):
        parsed_temperature = None
    ctx._llm_creds = LLMCreds(  # noqa: SLF001
        base_url=base_url,
        api_key=api_key,
        model=model,
        source=source,
        temperature_mode=str(llm_creds.get("temperature_mode") or "default"),
        temperature=parsed_temperature,
        extra_body=dict(llm_creds.get("extra_body") or {}),
        metadata=dict(llm_creds.get("metadata") or {}),
    )


def _apply_inline_consumer_setup(
    ctx: RunContext[Any],
    meta: dict[str, Any],
) -> bool:
    payload = {
        "consumer_config": meta.get("consumer_config"),
        "consumer_secrets": meta.get("consumer_secrets"),
    }
    if not isinstance(payload["consumer_config"], dict):
        payload["consumer_config"] = {}
    if not isinstance(payload["consumer_secrets"], dict):
        payload["consumer_secrets"] = {}
    if not payload["consumer_config"] and not payload["consumer_secrets"]:
        return False
    _merge_consumer_setup(ctx, payload)
    return True


def _merge_consumer_setup(ctx: RunContext[Any], payload: dict[str, Any]) -> None:
    config = payload.get("consumer_config")
    secrets = payload.get("consumer_secrets")
    if isinstance(config, dict):
        current = getattr(ctx, "_consumer_config", {})
        if not isinstance(current, dict):
            current = {}
        ctx._consumer_config = {**current, **config}  # noqa: SLF001
    if isinstance(secrets, dict):
        current = getattr(ctx, "_consumer_secrets", {})
        if not isinstance(current, dict):
            current = {}
        ctx._consumer_secrets = {  # noqa: SLF001
            **current,
            **{str(key): str(value) for key, value in secrets.items()},
        }


def _agent_declares_consumer_setup(agent: A2AAgent) -> bool:
    setup = getattr(type(agent), "consumer_setup", None)
    fields = getattr(setup, "fields", None)
    return bool(fields)


async def _fetch_consumer_setup_from_cp(
    *,
    cp_url: str,
    cp_jwt: str,
    agent_name: str,
) -> dict[str, Any]:
    return await fetch_consumer_setup_from_cp(
        cp_url=cp_url,
        cp_jwt=cp_jwt,
        agent_name=agent_name,
    )


async def _fetch_platform_llm_creds_from_cp(
    *,
    cp_url: str,
    cp_jwt: str,
    agent_name: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{cp_url.rstrip('/')}/v1/platform/llm-grant",
            headers={"authorization": f"bearer {cp_jwt}"},
            json={"audience": agent_name, "ttl_seconds": ttl_seconds},
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"LLM credential lookup failed: HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _bind_s3_write_artifact(
    ctx: Any,
    bucket: str,
    endpoint: str,
    access_key: str,
    secret_key: str,
) -> None:
    """Replace ``ctx.write_artifact`` with one that PUTs to MinIO."""
    try:
        import boto3
        from botocore.config import Config as _BotoConfig
    except Exception:  # noqa: BLE001
        return

    from ..context import ArtifactRef

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=_BotoConfig(s3={"addressing_style": "path"}),
    )

    task_id = getattr(ctx, "task_id", "task")

    async def write_artifact(
        name: str, data: bytes, mime_type: str
    ) -> ArtifactRef:
        key = f"outputs/{task_id}/{name}"
        s3.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType=mime_type,
        )
        # Also keep a memory ref so tests / introspection that read
        # ctx.artifacts still work.
        try:
            ctx.artifacts[name] = data  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return ArtifactRef(
            name=name,
            uri=f"s3://{bucket}/{key}",
            mime_type=mime_type,
            size_bytes=len(data),
        )

    ctx.write_artifact = write_artifact  # type: ignore[assignment]


def _default_context_builder(agent: A2AAgent) -> ContextBuilder:
    """Build a permissive :class:`LocalRunContext` keyed by skill name."""

    auth_cls = type(agent).auth_model

    def _build(skill_name: str) -> LocalRunContext[Any]:
        try:
            auth = auth_cls()
        except Exception:  # noqa: BLE001 — best-effort default
            auth = NoAuth()
        return LocalRunContext(auth=auth, task_id=f"mcp-{skill_name}")

    return _build


def skills_to_tools(
    agent: A2AAgent,
    *,
    extra_tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Render the agent's skills as MCP tool descriptors."""
    tools: list[dict[str, Any]] = []
    for spec in agent.skills.values():
        tool: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description or f"{agent.name}.{spec.name}",
            "inputSchema": spec.input_schema,
        }
        # outputSchema is optional in MCP 2025-06-18 but lets clients
        # validate structuredContent. Always emit when we have one.
        if spec.output_schema:
            tool["outputSchema"] = _ensure_object_schema(spec.output_schema)
        tools.append(tool)
    tools.extend(extra_tools or [])
    return tools


def _ensure_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """MCP requires outputSchema to be ``type: object``. Wrap scalars."""
    if schema.get("type") == "object":
        return schema
    return {
        "type": "object",
        "properties": {"result": schema},
        "required": ["result"],
    }


def tool_call_result(spec: SkillSpec, value: Any) -> dict[str, Any]:
    """Wrap a skill return value as an MCP CallToolResult."""
    output_schema = spec.output_schema if spec else None
    if output_schema and output_schema.get("type") == "object":
        structured = value
    else:
        structured = {"result": value}
    text = json.dumps(value, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": False,
    }


def tool_call_error(message: str) -> dict[str, Any]:
    """Wrap a tool-side failure as an MCP CallToolResult with isError=true."""
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


class MCPServer:
    """Transport-agnostic MCP request handler for an :class:`A2AAgent`."""

    def __init__(
        self,
        agent: A2AAgent,
        *,
        context_builder: ContextBuilder | None = None,
        extra_tools: list[dict[str, Any]] | None = None,
        extra_tool_handler: ExtraToolHandler | None = None,
        tool_call_handler: ToolCallHandler | None = None,
        tools_provider: ToolsProvider | None = None,
    ) -> None:
        self.agent = agent
        self._build_ctx = context_builder or _default_context_builder(agent)
        self._extra_tools = list(extra_tools or [])
        self._extra_tool_names = {
            str(tool.get("name"))
            for tool in self._extra_tools
            if isinstance(tool.get("name"), str)
        }
        self._extra_tool_handler = extra_tool_handler
        self._tool_call_handler = tool_call_handler
        self._tools_provider = tools_provider
        self._initialized = False

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC message. Returns response or ``None`` for notifications."""
        if not isinstance(message, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")

        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message

        if not isinstance(method, str):
            return _error(msg_id, _INVALID_REQUEST, "missing method")

        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(msg_id, _INVALID_PARAMS, "params must be an object")

        try:
            result = await self._dispatch(method, params)
        except _MCPError as exc:
            if is_notification:
                return None
            return _error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            log.exception("mcp dispatch failed")
            if is_notification:
                return None
            return _error(msg_id, _INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._on_initialize(params)
        if method == "notifications/initialized":
            self._initialized = True
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            if self._tools_provider is not None:
                return {"tools": self._tools_provider()}
            return {"tools": skills_to_tools(self.agent, extra_tools=self._extra_tools)}
        if method == "tools/call":
            return await self._on_tool_call(params)
        raise _MCPError(_METHOD_NOT_FOUND, f"unknown method: {method}")

    def _on_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        agent_cls = type(self.agent)
        client_version = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
        return {
            "protocolVersion": client_version,
            "capabilities": {
                "tools": {"listChanged": False},
                # Server may issue ``elicitation/create`` requests mid
                # ``tools/call`` (driven by skills using ``ctx.collect`` /
                # ``ctx.ask``). The HTTP transport delivers them inline
                # on the SSE response and routes client responses back to
                # the pending future via session id. See mcp/http.py.
                "elicitation": {},
            },
            "serverInfo": {
                "name": agent_cls.name,
                "version": agent_cls.version,
            },
            "instructions": agent_cls.description or "",
        }

    async def _on_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _MCPError(_INVALID_PARAMS, "tools/call: missing name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _MCPError(_INVALID_PARAMS, "tools/call: arguments must be object")
        # Optional caller context — set by MCP gateways that have a CP login
        # available (a2amcp). When present, hand the skill a workspace
        # scoped to the user's bucket and the CP JWT so agents that need
        # to act on behalf of the user (agent-builder, file tools, …) can
        # do so from an MCP client the same way they do from the platform
        # orchestrator. Unknown clients omit _meta and get the original
        # unauthenticated context.
        meta = params.get("_meta") or {}
        if not isinstance(meta, dict):
            meta = {}

        if self._tool_call_handler is not None:
            handled = self._tool_call_handler(name, arguments, meta)
            if hasattr(handled, "__await__"):
                handled = await handled  # type: ignore[assignment]
            if handled is not None:
                return handled

        if name in self._extra_tool_names:
            if self._extra_tool_handler is None:
                return tool_call_error(f"unknown tool: {name}")
            result = self._extra_tool_handler(name, arguments)
            if hasattr(result, "__await__"):
                return await result  # type: ignore[return-value]
            return result

        spec = self.agent.skills.get(name)
        if spec is None:
            # Per MCP, unknown tools surface as isError content, not JSON-RPC error.
            return tool_call_error(f"unknown tool: {name}")

        ctx_or_awaitable = self._build_ctx(name)
        if hasattr(ctx_or_awaitable, "__await__"):
            ctx = await ctx_or_awaitable  # type: ignore[assignment]
        else:
            ctx = ctx_or_awaitable

        _apply_caller_meta(ctx, meta)
        await _apply_consumer_setup(agent=self.agent, ctx=ctx, meta=meta)
        await _apply_llm_creds(agent=self.agent, ctx=ctx, meta=meta)

        # Report this MCP tool-call to the control-plane so it shows up in the
        # "Runs" tab. Best-effort: a tracking failure must never change the
        # tool's return/raise behavior. Only real agent skills reach here (the
        # extra-tools / intercept branches return earlier).
        cp_jwt = (
            (meta.get("cp_jwt") if isinstance(meta.get("cp_jwt"), str) else None)
            or os.environ.get("A2A_CP_JWT")
        )
        cp_url = (
            (meta.get("cp_url") if isinstance(meta.get("cp_url"), str) else None)
            or os.environ.get("A2A_CP_URL")
            or os.environ.get("A2A_CP_URL_INTERNAL")
        )
        tracking_id = f"mcp-{secrets.token_hex(8)}"
        agent_name = type(self.agent).name
        track = bool(cp_jwt and cp_url)
        started = 0.0
        if track:
            try:
                await _post_mcp_tracking(
                    cp_url,  # type: ignore[arg-type]
                    cp_jwt,  # type: ignore[arg-type]
                    {
                        "type": "agent_invoke_started",
                        "grant_id": tracking_id,
                        "to": agent_name,
                        "agent": agent_name,
                        "skill": name,
                        "args_json": json.dumps(
                            arguments, separators=(",", ":"), ensure_ascii=False
                        )[:8000],
                        "source": "agent_mcp",
                    },
                )
                started = time.monotonic()
            except Exception:  # noqa: BLE001
                log.debug("mcp tracking (started) failed", exc_info=True)

        try:
            value = await self.agent.invoke_json(name, ctx, arguments)
        except SkillNotFound:
            if track:
                await self._track_mcp_error(
                    cp_url, cp_jwt, tracking_id, agent_name, name,
                    "unknown tool", started,
                )
            return tool_call_error(f"unknown tool: {name}")
        except SkillInputError as exc:
            if track:
                await self._track_mcp_error(
                    cp_url, cp_jwt, tracking_id, agent_name, name,
                    f"invalid arguments: {exc}", started,
                )
            return tool_call_error(f"invalid arguments: {exc}")
        except MissingScopes as exc:
            if track:
                await self._track_mcp_error(
                    cp_url, cp_jwt, tracking_id, agent_name, name,
                    f"permission denied: {exc}", started,
                )
            return tool_call_error(f"permission denied: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("tool call failed: %s", name)
            if track:
                await self._track_mcp_error(
                    cp_url, cp_jwt, tracking_id, agent_name, name,
                    f"{type(exc).__name__}: {exc}", started,
                )
            return tool_call_error(f"{type(exc).__name__}: {exc}")

        if track:
            try:
                await _post_mcp_tracking(
                    cp_url,  # type: ignore[arg-type]
                    cp_jwt,  # type: ignore[arg-type]
                    {
                        "type": "agent_invoke_complete",
                        "grant_id": tracking_id,
                        "to": agent_name,
                        "agent": agent_name,
                        "skill": name,
                        "ok": True,
                        "summary": f"{agent_name}.{name} ok",
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "source": "agent_mcp",
                    },
                )
            except Exception:  # noqa: BLE001
                log.debug("mcp tracking (complete) failed", exc_info=True)

        return tool_call_result(spec, value)

    async def _track_mcp_error(
        self,
        cp_url: str | None,
        cp_jwt: str | None,
        tracking_id: str,
        agent_name: str,
        skill_name: str,
        error: str,
        started: float,
    ) -> None:
        """Best-effort ``agent_invoke_error`` post; never raises."""
        if not (cp_url and cp_jwt):
            return
        try:
            await _post_mcp_tracking(
                cp_url,
                cp_jwt,
                {
                    "type": "agent_invoke_error",
                    "grant_id": tracking_id,
                    "to": agent_name,
                    "agent": agent_name,
                    "skill": skill_name,
                    "ok": False,
                    "summary": f"{agent_name}.{skill_name} error",
                    "error": str(error)[:500],
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "source": "agent_mcp",
                },
            )
        except Exception:  # noqa: BLE001
            log.debug("mcp tracking (error) failed", exc_info=True)


class _MCPError(Exception):
    """Internal sentinel for JSON-RPC error returns."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _error(
    msg_id: Any, code: int, message: str, data: Any | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def parse_message(raw: str | bytes) -> dict[str, Any]:
    """Parse a JSON-RPC frame; raises :class:`_MCPError` with PARSE_ERROR."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _MCPError(_PARSE_ERROR, f"parse error: {exc}") from exc
    if not isinstance(msg, dict):
        raise _MCPError(_INVALID_REQUEST, "request must be a JSON object")
    return msg
