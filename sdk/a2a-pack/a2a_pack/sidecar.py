"""Common sidecar runtime for DSL-compiled agents.

The sidecar owns the wire protocols and delegates native handler execution to
language workers. Target-language SDKs only need to compile declarations to
``AgentDsl`` and expose the small worker invoke endpoint.
"""
from __future__ import annotations

import asyncio
import hashlib
from http.cookies import CookieError, SimpleCookie
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, AsyncIterator, Protocol
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from .auth import AuthError, PlatformUserAuthResolver
from .consumer_setup_runtime import (
    ConsumerSetupLookupError,
    resolve_invoke_consumer_setup,
)
from .dsl import AgentDsl, AgentDslSkill
from .frontend import (
    FRONTEND_AGENT_API_PREFIX,
    PackedFrontend,
    frontend_agent_api_path,
    frontend_auth_metadata,
    frontend_ui_metadata,
    frontend_requires_session,
    packed_frontend_from_env,
    proxy_frontend_request,
    start_frontend_process,
)
from .mcp import MCP_PROTOCOL_VERSION
from .openapi import agent_dsl_openapi_spec
from .runtime import AgentEndpoint

_SIDECAR_FRONTEND_CLIENT_JS = """\
export function unwrapInvokeResponse(payload) {
  if (payload && typeof payload === "object" && "result" in payload) {
    return payload.result;
  }
  return payload;
}

export async function createA2AClient(configUrl = "./config.json") {
  const response = await fetch(configUrl, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`failed to load A2A config: ${response.status}`);
  const config = await response.json();
  return {
    config,
    async callSkill(skillName, args = {}, options = {}) {
      if (config.auth && config.auth.invokeRequiresSession) {
        const session = await fetch(config.endpoints.session, { credentials: "same-origin" });
        const sessionPayload = session.ok ? await session.json() : null;
        if (!sessionPayload || !sessionPayload.authenticated) {
          const target = config.auth.authorizeUrl || config.auth.loginUrl;
          if (target && typeof window !== "undefined") {
            const next = encodeURIComponent(
              window.location.pathname + window.location.search + window.location.hash,
            );
            window.location.assign(`${target}${target.includes("?") ? "&" : "?"}next=${next}`);
          }
          throw new Error("sign in required");
        }
      }
      const url = `${config.endpoints.invoke}/${encodeURIComponent(skillName)}`;
      const invoke = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ arguments: args }),
      });
      const text = await invoke.text();
      const payload = text ? JSON.parse(text) : null;
      if (!invoke.ok) {
        const detail = payload && (payload.detail || payload.message);
        throw new Error(detail || `skill invoke failed: ${invoke.status}`);
      }
      return options.rawResponse ? payload : unwrapInvokeResponse(payload);
    },
    async callSkillEnvelope(skillName, args = {}) {
      return this.callSkill(skillName, args, { rawResponse: true });
    },
  };
}
"""


class SidecarInvokeIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    caller: dict[str, Any] | None = None
    grant_ids: list[str] | None = None
    random_seed: str | int | None = None
    grant: str | None = None
    llm_creds: dict[str, Any] | None = None
    composition: dict[str, Any] | None = None
    consumer_config: dict[str, Any] | None = None
    consumer_secrets: dict[str, str] | None = None
    cp_jwt: str | None = None
    cp_url: str | None = None
    auth: dict[str, Any] | None = None


class SidecarWorkerRequest(BaseModel):
    agent: str
    skill: str
    handler: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    caller: dict[str, Any] | None = None
    grant_ids: list[str] | None = None
    random_seed: str | int | None = None
    grant: str | None = None
    llm_creds: dict[str, Any] | None = None
    composition: dict[str, Any] | None = None
    consumer_config: dict[str, Any] | None = None
    consumer_secrets: dict[str, str] | None = None
    cp_jwt: str | None = None
    cp_url: str | None = None
    auth: dict[str, Any] | None = None
    scope_expansion_allowed: bool | None = None


class SidecarWorkerResponse(BaseModel):
    result: Any
    events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class SidecarWorker(Protocol):
    async def call(self, skill: AgentDslSkill, request: SidecarWorkerRequest) -> Any:
        ...


class HttpSidecarWorker:
    """HTTP worker adapter used by native language SDK shims."""

    def __init__(self, base_url: str, *, timeout: float = 1800.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def call(self, skill: AgentDslSkill, request: SidecarWorkerRequest) -> Any:
        payload = await self.call_full(skill, request)
        return payload.result

    async def call_full(
        self,
        skill: AgentDslSkill,
        request: SidecarWorkerRequest,
    ) -> SidecarWorkerResponse:
        async with httpx.AsyncClient(timeout=_worker_timeout(skill, self.timeout)) as client:
            response = await client.post(
                f"{self.base_url}/_a2a/invoke/{skill.handler}",
                json=request.model_dump(mode="json"),
            )
        if response.status_code >= 400:
            raise HTTPException(
                response.status_code,
                f"worker invoke failed: {response.text[:500]}",
            )
        try:
            payload = SidecarWorkerResponse.model_validate(response.json())
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                502,
                f"worker response must match SidecarWorkerResponse: {exc}",
            ) from exc
        return payload

    async def stream(
        self,
        skill: AgentDslSkill,
        request: SidecarWorkerRequest,
    ) -> AsyncIterator[bytes]:
        async with httpx.AsyncClient(timeout=_worker_timeout(skill, self.timeout)) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/_a2a/invoke-stream/{skill.handler}",
                json=request.model_dump(mode="json"),
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode(errors="replace")
                    yield _sse_payload({
                        "type": "error",
                        "status": response.status_code,
                        "detail": f"worker invoke failed: {text[:500]}",
                    })
                    yield _sse_done()
                    return
                async for chunk in response.aiter_bytes():
                    yield chunk

    async def callback(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload)
        if response.status_code >= 400:
            raise HTTPException(
                response.status_code,
                f"worker callback failed: {response.text[:500]}",
            )
        try:
            data = response.json()
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {"result": data}


def load_agent_dsl(path: Path) -> AgentDsl:
    return AgentDsl.model_validate_json(path.read_text(encoding="utf-8"))


def build_sidecar_app(
    dsl: AgentDsl,
    *,
    worker: SidecarWorker | None = None,
    worker_url: str | None = None,
) -> FastAPI:
    """Build a FastAPI sidecar app from a compiled Agent DSL."""

    resolved_worker = worker or HttpSidecarWorker(
        worker_url or "http://127.0.0.1:9001"
    )
    app = FastAPI(title=dsl.name, version=dsl.version)
    skills = {skill.name: skill for skill in dsl.skills}
    stream_operations: dict[str, _SidecarStreamOperation] = {}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "agent": dsl.name,
            "version": dsl.version,
            "sidecar": True,
        }

    @app.get("/.well-known/agent-card")
    async def agent_card(request: Request) -> dict[str, Any]:
        card = dsl.to_agent_card().model_dump(mode="json")
        if _uses_frontend_agent_api_prefix(request):
            _rewrite_sidecar_mcp_paths_for_prefix(card, FRONTEND_AGENT_API_PREFIX)
        return card

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card(request: Request) -> dict[str, Any]:
        card = dsl.to_agent_card().model_dump(mode="json")
        if _uses_frontend_agent_api_prefix(request):
            _rewrite_sidecar_mcp_paths_for_prefix(card, FRONTEND_AGENT_API_PREFIX)
        return card

    @app.get("/.well-known/a2a-skills.json")
    async def a2a_skills(request: Request) -> dict[str, Any]:
        return _skills_payload(dsl, request)

    @app.get("/.well-known/openapi.json")
    async def openapi_spec(request: Request) -> dict[str, Any]:
        return agent_dsl_openapi_spec(
            dsl,
            base_url=str(request.base_url).rstrip("/"),
        )

    @app.post("/invoke/{skill_name}", response_model=None)
    async def invoke(
        skill_name: str,
        body: SidecarInvokeIn,
        request: Request,
        accept: str | None = Header(default=None),
    ) -> dict[str, Any] | StreamingResponse:
        await _sidecar_require_account_access(dsl, body, request)
        if accept and "text/event-stream" in accept.lower():
            return await _invoke_sse(skill_name, body, request)
        skill = _skill_or_404(skills, skill_name)
        _validate_arguments(skill, body.arguments)
        body = await _sidecar_body_with_consumer_setup(dsl, body, request)
        worker_request = _worker_request(dsl, skill, body)
        payload = await _call_worker_full(resolved_worker, skill, worker_request)
        response: dict[str, Any] = {"result": payload.result}
        if payload.events:
            response["events"] = payload.events
        if payload.artifacts:
            response["artifacts"] = payload.artifacts
        return response

    @app.post("/answers/{question_id}")
    async def answer(question_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await _worker_callback(
            resolved_worker,
            f"/_a2a/answers/{question_id}",
            body,
        )

    @app.post("/input-requests/{request_id}")
    async def input_request(request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await _worker_callback(
            resolved_worker,
            f"/_a2a/input-requests/{request_id}",
            body,
        )

    @app.post("/scope-grants/{request_id}")
    async def scope_grant(request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await _worker_callback(
            resolved_worker,
            f"/_a2a/scope-grants/{request_id}",
            body,
        )

    @app.post("/scope-denials/{request_id}")
    async def scope_denial(request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await _worker_callback(
            resolved_worker,
            f"/_a2a/scope-denials/{request_id}",
            body,
        )

    async def _invoke_sse(
        skill_name: str,
        body: SidecarInvokeIn,
        request: Request,
    ) -> StreamingResponse:
        try:
            skill = _skill_or_404(skills, skill_name)
            _validate_arguments(skill, body.arguments)
            body = await _sidecar_body_with_consumer_setup(dsl, body, request)
            worker_request = _worker_request(dsl, skill, body)
        except HTTPException as exc:
            status_code = exc.status_code
            detail = exc.detail

            async def err() -> AsyncIterator[bytes]:
                yield _sse_payload({
                    "type": "error",
                    "status": status_code,
                    "detail": detail,
                })
                yield _sse_done()
            return _streaming_response(err())

        async def gen() -> AsyncIterator[bytes]:
            operation = _sidecar_stream_operation(
                stream_operations,
                dsl=dsl,
                skill=skill,
                request=worker_request,
                run=lambda op: _run_sidecar_stream_operation(
                    op,
                    resolved_worker=resolved_worker,
                    skill=skill,
                    request=worker_request,
                ),
            )
            async for frame in operation.subscribe():
                yield _sse_payload(frame) if isinstance(frame, dict) else _sse_done()
        return _streaming_response(gen())

    @app.post("/mcp")
    async def mcp(body: dict[str, Any], request: Request) -> dict[str, Any]:
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if method == "initialize":
            return _jsonrpc_result(request_id, _mcp_initialize(dsl, params))
        if method == "notifications/initialized":
            return _jsonrpc_result(request_id, {})
        if method == "tools/list":
            return _jsonrpc_result(request_id, {"tools": _mcp_tools(dsl)})
        if method == "tools/call":
            await _sidecar_require_account_access(
                dsl,
                SidecarInvokeIn(),
                request,
            )
            try:
                result = await _mcp_tool_call(dsl, skills, resolved_worker, params)
            except HTTPException as exc:
                return _jsonrpc_error(request_id, -32602, str(exc.detail))
            except Exception as exc:  # noqa: BLE001
                return _jsonrpc_error(request_id, -32603, str(exc))
            return _jsonrpc_result(request_id, result)
        return _jsonrpc_error(request_id, -32601, f"method not found: {method}")

    @app.get("/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        return await _sidecar_auth_session_payload(dsl, request)

    def _make_raw_endpoint_handler(
        endpoint: AgentEndpoint,
        skill: AgentDslSkill,
    ) -> Any:
        async def raw_endpoint(request: Request) -> Any:
            arguments = await _raw_endpoint_arguments(endpoint, request)
            _validate_arguments(skill, arguments)
            body = SidecarInvokeIn(
                arguments=arguments,
                task_id=f"http-endpoint:{endpoint.name or endpoint.path}",
            )
            await _sidecar_require_account_access(dsl, body, request)
            body = _sidecar_body_with_env_consumer_setup(dsl, body)
            body = await _sidecar_body_with_consumer_setup(dsl, body, request)
            worker_request = _worker_request(dsl, skill, body)
            payload = await _call_worker_full(resolved_worker, skill, worker_request)
            return _raw_endpoint_result(payload.result)

        return raw_endpoint

    for endpoint in dsl.runtime.endpoints:
        skill = _skill_or_404(skills, endpoint.skill)
        app.add_api_route(
            endpoint.path,
            _make_raw_endpoint_handler(endpoint, skill),
            methods=list(endpoint.methods),
            include_in_schema=False,
            response_model=None,
        )

    frontend = packed_frontend_from_env()
    if frontend is not None and frontend.mount == "/":
        for path, endpoint, methods in (
            ("/.well-known/agent-card", agent_card, ["GET"]),
            ("/.well-known/agent-card.json", a2a_agent_card, ["GET"]),
            ("/.well-known/a2a-skills.json", a2a_skills, ["GET"]),
            ("/.well-known/openapi.json", openapi_spec, ["GET"]),
            ("/auth/session", auth_session, ["GET"]),
            ("/invoke/{skill_name}", invoke, ["POST"]),
            ("/answers/{question_id}", answer, ["POST"]),
            ("/input-requests/{request_id}", input_request, ["POST"]),
            ("/scope-grants/{request_id}", scope_grant, ["POST"]),
            ("/scope-denials/{request_id}", scope_denial, ["POST"]),
            ("/mcp", mcp, ["POST"]),
        ):
            app.add_api_route(
                f"{FRONTEND_AGENT_API_PREFIX}{path}",
                endpoint,
                methods=methods,
                include_in_schema=False,
                response_model=None,
            )
    if frontend is not None:
        _mount_sidecar_frontend(app, dsl, frontend)

    return app


def serve_sidecar(
    *,
    dsl_path: Path,
    worker_url: str,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    import uvicorn

    app = build_sidecar_app(load_agent_dsl(dsl_path), worker_url=worker_url)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _skill_or_404(skills: dict[str, AgentDslSkill], name: str) -> AgentDslSkill:
    skill = skills.get(name)
    if skill is None:
        raise HTTPException(404, f"unknown skill: {name}")
    return skill


def _validate_arguments(skill: AgentDslSkill, arguments: dict[str, Any]) -> None:
    schema = skill.input_schema
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    missing = [name for name in required if name not in arguments]
    if missing:
        raise HTTPException(422, f"missing required arguments: {missing}")
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise HTTPException(422, f"unknown arguments: {unknown}")


async def _raw_endpoint_arguments(
    endpoint: AgentEndpoint,
    request: Request,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        endpoint.body_arg: await _raw_endpoint_body(request),
    }
    if endpoint.headers_arg:
        arguments[endpoint.headers_arg] = dict(request.headers)
    if endpoint.query_arg:
        arguments[endpoint.query_arg] = dict(request.query_params)
    return arguments


async def _raw_endpoint_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        return None
    content_type = request.headers.get("content-type", "")
    if _is_json_content_type(content_type):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "endpoint body must be valid JSON") from exc
    return raw.decode("utf-8", errors="replace")


def _is_json_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _raw_endpoint_result(result: Any) -> Any:
    return {"ok": True} if result is None else result


def _worker_request(
    dsl: AgentDsl,
    skill: AgentDslSkill,
    body: SidecarInvokeIn,
) -> SidecarWorkerRequest:
    return SidecarWorkerRequest(
        agent=dsl.name,
        skill=skill.name,
        handler=skill.handler,
        arguments=body.arguments,
        task_id=body.task_id,
        caller=body.caller,
        grant_ids=body.grant_ids,
        random_seed=body.random_seed,
        grant=body.grant,
        llm_creds=body.llm_creds,
        composition=body.composition,
        consumer_config=body.consumer_config,
        consumer_secrets=body.consumer_secrets,
        cp_jwt=body.cp_jwt,
        cp_url=body.cp_url,
        auth=body.auth,
        scope_expansion_allowed=skill.policy.allow_scope_expansion,
    )


async def _worker_callback(
    worker: SidecarWorker,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    callback = getattr(worker, "callback", None)
    if not callable(callback):
        raise HTTPException(501, "worker does not support callbacks")
    return await callback(path, payload)


def _worker_timeout(skill: AgentDslSkill, default_timeout: float) -> httpx.Timeout:
    policy_timeout = getattr(getattr(skill, "policy", None), "timeout_seconds", None)
    total = max(default_timeout, float(policy_timeout or 0) + 60.0)
    return httpx.Timeout(total, connect=30.0, write=30.0, pool=30.0)


def _sse_payload(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def _streaming_response(body: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class _SidecarStreamOperation:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any] | str] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any] | str]] = set()
        self.done = False
        self.task: asyncio.Task[None] | None = None

    def publish(self, frame: dict[str, Any] | str) -> None:
        if self.done:
            return
        self.frames.append(frame)
        if frame == "[DONE]":
            self.done = True
        for queue in tuple(self.subscribers):
            queue.put_nowait(frame)

    async def subscribe(self) -> AsyncIterator[dict[str, Any] | str]:
        queue: asyncio.Queue[dict[str, Any] | str] = asyncio.Queue()
        for frame in self.frames:
            yield frame
            if frame == "[DONE]":
                return
        if self.done:
            yield "[DONE]"
            return
        self.subscribers.add(queue)
        try:
            while True:
                frame = await queue.get()
                yield frame
                if frame == "[DONE]":
                    return
        finally:
            self.subscribers.discard(queue)


def _sidecar_stream_operation(
    operations: dict[str, _SidecarStreamOperation],
    *,
    dsl: AgentDsl,
    skill: AgentDslSkill,
    request: SidecarWorkerRequest,
    run: Any,
) -> _SidecarStreamOperation:
    key = _sidecar_stream_key(dsl, skill, request)
    operation = operations.get(key)
    if operation is not None and not operation.done:
        return operation
    if operation is not None and operation.done:
        return operation
    operation = _SidecarStreamOperation()
    operations[key] = operation

    async def runner() -> None:
        try:
            await run(operation)
        finally:
            operation.publish("[DONE]")
            await asyncio.sleep(15)
            if operations.get(key) is operation:
                operations.pop(key, None)

    operation.task = asyncio.create_task(runner())
    return operation


def _sidecar_stream_key(
    dsl: AgentDsl,
    skill: AgentDslSkill,
    request: SidecarWorkerRequest,
) -> str:
    payload = {
        "agent": dsl.name,
        "version": dsl.version,
        "skill": skill.name,
        "handler": skill.handler,
        "request": request.model_dump(mode="json"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def _run_sidecar_stream_operation(
    operation: _SidecarStreamOperation,
    *,
    resolved_worker: SidecarWorker,
    skill: AgentDslSkill,
    request: SidecarWorkerRequest,
) -> None:
    stream = getattr(resolved_worker, "stream", None)
    try:
        if callable(stream):
            async for frame in _iter_sse_frames(stream(skill, request)):
                operation.publish(frame)
                if frame == "[DONE]":
                    return
            return
        payload = await _call_worker_full(resolved_worker, skill, request)
        operation.publish({
            "type": "result",
            "result": payload.result,
            "events": payload.events,
            "artifacts": payload.artifacts,
        })
    except HTTPException as exc:
        operation.publish({
            "type": "error",
            "status": exc.status_code,
            "detail": exc.detail,
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        operation.publish({
            "type": "error",
            "status": 500,
            "detail": str(exc),
        })


async def _iter_sse_frames(
    chunks: AsyncIterator[bytes],
) -> AsyncIterator[dict[str, Any] | str]:
    buffer = ""
    async for chunk in chunks:
        buffer += chunk.decode(errors="replace")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            frame = _parse_sse_block(block)
            if frame is not None:
                yield frame
    frame = _parse_sse_block(buffer)
    if frame is not None:
        yield frame


def _parse_sse_block(block: str) -> dict[str, Any] | str | None:
    data_lines = [
        line.removeprefix("data: ").removeprefix("data:")
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return None
    raw = "\n".join(data_lines).strip()
    if raw == "[DONE]":
        return raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "event", "kind": "message", "payload": {"message": raw}}
    if isinstance(parsed, dict):
        return parsed
    return {"type": "event", "kind": "message", "payload": {"value": parsed}}


async def _call_worker_full(
    worker: SidecarWorker,
    skill: AgentDslSkill,
    request: SidecarWorkerRequest,
) -> SidecarWorkerResponse:
    call_full = getattr(worker, "call_full", None)
    if callable(call_full):
        payload = await call_full(skill, request)
        return SidecarWorkerResponse.model_validate(payload)
    result = await worker.call(skill, request)
    if isinstance(result, SidecarWorkerResponse):
        return result
    if isinstance(result, dict) and "result" in result:
        return SidecarWorkerResponse.model_validate(result)
    return SidecarWorkerResponse(result=result)


def _skills_payload(dsl: AgentDsl, request: Request | None = None) -> dict[str, Any]:
    card = dsl.to_agent_card().model_dump(mode="json")
    invoke_path = _sidecar_request_agent_api_path(request, "/invoke")
    mcp_path = _sidecar_request_agent_api_path(request, "/mcp")
    return {
        "agent": {"name": dsl.name, "version": dsl.version},
        "skills": card.get("skills") if isinstance(card.get("skills"), list) else [],
        "invokeUrl": _request_url(request, invoke_path),
        "mcpUrl": _request_url(request, mcp_path),
    }


def _request_url(request: Request | None, path: str) -> str:
    if request is None:
        return path
    return str(request.url_for("healthz")).removesuffix("/healthz") + path


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _runtime_cp_jwt() -> str | None:
    token = os.environ.get("A2A_CP_JWT")
    return token.strip() if isinstance(token, str) and token.strip() else None


def _trusted_cp_jwt(body: SidecarInvokeIn, request: Request) -> str | None:
    return (
        body.cp_jwt
        or _session_token_from_cookie_header(
            request.headers.get("cookie") or request.headers.get("Cookie")
        )
        or _runtime_cp_jwt()
    )


async def _sidecar_body_with_consumer_setup(
    dsl: AgentDsl,
    body: SidecarInvokeIn,
    request: Request,
) -> SidecarInvokeIn:
    cp_jwt = _trusted_cp_jwt(body, request)
    cp_url = (
        os.environ.get("A2A_CP_URL")
        or "http://control-plane.control-plane.svc.cluster.local"
    ).rstrip("/")
    try:
        consumer_config, consumer_secrets = await resolve_invoke_consumer_setup(
            setup=dsl.consumer_setup,
            cp_url=cp_url,
            cp_jwt=cp_jwt,
            agent_name=dsl.name,
            consumer_config=body.consumer_config,
            consumer_secrets=body.consumer_secrets,
        )
    except ConsumerSetupLookupError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    update: dict[str, Any] = {
        "consumer_config": consumer_config,
        "consumer_secrets": consumer_secrets,
        "cp_url": cp_url,
    }
    runtime = getattr(dsl, "runtime", None)
    if getattr(runtime, "wants_cp_jwt", False) and cp_jwt:
        update["cp_jwt"] = body.cp_jwt or cp_jwt
        update["cp_url"] = cp_url
    return body.model_copy(
        update=update
    )


def _sidecar_body_with_env_consumer_setup(
    dsl: AgentDsl,
    body: SidecarInvokeIn,
) -> SidecarInvokeIn:
    consumer_config = dict(body.consumer_config or {})
    consumer_secrets = dict(body.consumer_secrets or {})
    for field in dsl.consumer_setup.fields:
        value = os.environ.get(field.name)
        if value is None:
            continue
        if field.kind == "secret":
            consumer_secrets.setdefault(field.name, value)
        else:
            consumer_config.setdefault(field.name, value)
    return body.model_copy(
        update={
            "consumer_config": consumer_config or None,
            "consumer_secrets": consumer_secrets or None,
        }
    )


def _session_token_from_cookie_header(cookie_header: str | None) -> str | None:
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return None
    cookie_name = os.environ.get("A2A_SESSION_COOKIE_NAME") or "a2a_session"
    morsel = jar.get(cookie_name)
    if morsel is None:
        return None
    token = str(morsel.value or "").strip()
    return token or None


async def _sidecar_platform_session(dsl: AgentDsl, request: Request) -> Any | None:
    try:
        return await PlatformUserAuthResolver().resolve(
            _extract_bearer(request.headers.get("authorization")),
            headers=dict(request.headers),
            agent=dsl,
        )
    except AuthError:
        return None


async def _sidecar_require_account_access(
    dsl: AgentDsl,
    body: SidecarInvokeIn,
    request: Request,
) -> None:
    access = dsl.runtime.account_access
    if not access.required:
        return
    token = _trusted_cp_jwt(body, request) or _extract_bearer(
        request.headers.get("authorization")
    )
    try:
        await PlatformUserAuthResolver().resolve(
            token,
            headers=dict(request.headers),
            agent=dsl,
        )
    except AuthError as exc:
        raise HTTPException(
            401,
            {
                "error": "account_required",
                "message": "Sign in to A2A Cloud to use this agent.",
                "agent": dsl.name,
            },
        ) from exc


async def _sidecar_auth_session_payload(dsl: AgentDsl, request: Request) -> dict[str, Any]:
    local_dev = os.environ.get("A2A_LOCAL_DEV") == "1"
    platform_user = await _sidecar_platform_session(dsl, request)
    authenticated = platform_user is not None
    return {
        "authenticated": authenticated,
        "local": local_dev,
        "user": {
            "id": platform_user.user_id,
            "sub": platform_user.sub,
            "email": platform_user.email,
        }
        if authenticated
        else None,
        "org": {
            "id": platform_user.org_id,
            "slug": platform_user.org_slug,
        }
        if authenticated and (platform_user.org_id or platform_user.org_slug)
        else None,
        "scopes": platform_user.scopes if platform_user else [],
    }


async def _sidecar_require_frontend_session(
    dsl: AgentDsl,
    request: Request,
) -> Response | None:
    if await _sidecar_platform_session(dsl, request) is not None:
        return None
    login_url = os.environ.get("A2A_LOGIN_URL", "").strip()
    accept = request.headers.get("accept", "")
    if login_url and "text/html" in accept.lower():
        separator = "&" if "?" in login_url else "?"
        return RedirectResponse(
            f"{login_url}{separator}next={quote(str(request.url), safe='')}",
            status_code=303,
        )
    return JSONResponse({"detail": "platform session required"}, status_code=401)


def _mount_sidecar_frontend(app: FastAPI, dsl: AgentDsl, frontend: PackedFrontend) -> None:
    app.state.a2a_frontend = frontend

    async def authorize(request: Request) -> Response | None:
        if frontend_requires_session(frontend):
            return await _sidecar_require_frontend_session(dsl, request)
        return None

    @app.get(frontend.config_path, include_in_schema=False, response_model=None)
    async def sidecar_frontend_config(request: Request) -> dict[str, Any] | Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return _sidecar_frontend_config_payload(dsl, frontend, request)

    @app.get(frontend.client_path, include_in_schema=False)
    async def sidecar_frontend_client(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return Response(_SIDECAR_FRONTEND_CLIENT_JS, media_type="text/javascript; charset=utf-8")

    if frontend.is_server_rendered:
        start_frontend_process(app, frontend)

        async def sidecar_frontend_proxy(request: Request, path: str = "") -> Response:
            auth_response = await authorize(request)
            if auth_response is not None:
                return auth_response
            return await proxy_frontend_request(frontend, request)

        async def sidecar_frontend_redirect(request: Request) -> Response:
            auth_response = await authorize(request)
            if auth_response is not None:
                return auth_response
            return RedirectResponse(f"{frontend.mount}/", status_code=307)

        if frontend.mount != "/":
            app.add_api_route(
                frontend.mount,
                sidecar_frontend_redirect,
                methods=["GET"],
                include_in_schema=False,
                response_model=None,
            )
        catchall = "/{path:path}" if frontend.mount == "/" else f"{frontend.mount}/{{path:path}}"
        app.add_api_route(
            catchall,
            sidecar_frontend_proxy,
            methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            include_in_schema=False,
            response_model=None,
        )
        return

    if frontend.dist_dir is None:
        raise ValueError("static frontend requires dist_dir")
    root = frontend.dist_dir.resolve()

    async def sidecar_frontend_index(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return _sidecar_index_response(root)

    async def sidecar_frontend_redirect(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return RedirectResponse(f"{frontend.mount}/", status_code=307)

    async def sidecar_frontend_asset(request: Request, path: str) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        if path in {"", "/", "index.html"}:
            return _sidecar_index_response(root)
        target = _safe_frontend_asset_path(root, path)
        if target.is_file():
            return FileResponse(
                target,
                media_type=mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
            )
        return _sidecar_index_response(root)

    if frontend.mount == "/":
        app.add_api_route(
            "/",
            sidecar_frontend_index,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
    else:
        app.add_api_route(
            frontend.mount,
            sidecar_frontend_redirect,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
        app.add_api_route(
            f"{frontend.mount}/",
            sidecar_frontend_index,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
    catchall = "/{path:path}" if frontend.mount == "/" else f"{frontend.mount}/{{path:path}}"
    app.add_api_route(
        catchall,
        sidecar_frontend_asset,
        methods=["GET"],
        include_in_schema=False,
        response_model=None,
    )


def _sidecar_frontend_config_payload(
    dsl: AgentDsl,
    frontend: PackedFrontend,
    request: Request,
) -> dict[str, Any]:
    card = dsl.to_agent_card().model_dump(mode="json")
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    return {
        "agent": {
            "name": dsl.name,
            "description": dsl.description,
            "version": dsl.version,
        },
        "mount": frontend.mount,
        "ui": frontend_ui_metadata(frontend, request),
        "auth": {
            **frontend_auth_metadata(frontend, request),
            "invokeRequiresSession": (
                dsl.auth.required or dsl.runtime.account_access.required
            ),
        },
        "endpoints": {
            "agentCard": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/agent-card.json"),
            ),
            "legacyAgentCard": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/agent-card"),
            ),
            "skills": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/a2a-skills.json"),
            ),
            "invoke": _request_url(
                request,
                frontend_agent_api_path(frontend, "/invoke"),
            ),
            "mcp": _request_url(request, frontend_agent_api_path(frontend, "/mcp")),
            "session": _request_url(
                request,
                frontend_agent_api_path(frontend, "/auth/session"),
            ),
        },
        "skills": skills,
        "docs": {"base": frontend.docs_url, "install": f"{frontend.docs_url.rstrip('/')}/"},
    }


def _sidecar_request_agent_api_path(request: Request | None, path: str) -> str:
    clean = path if path.startswith("/") else f"/{path}"
    if request is not None and _uses_frontend_agent_api_prefix(request):
        return f"{FRONTEND_AGENT_API_PREFIX}{clean}"
    return clean


def _uses_frontend_agent_api_prefix(request: Request) -> bool:
    return request.url.path == FRONTEND_AGENT_API_PREFIX or request.url.path.startswith(
        f"{FRONTEND_AGENT_API_PREFIX}/"
    )


def _rewrite_sidecar_mcp_paths_for_prefix(card: dict[str, Any], prefix: str) -> None:
    card["mcp_endpoint"] = f"{prefix}/mcp"
    capabilities = card.get("capabilities")
    endpoint_sets = [card.get("mcp_endpoints")]
    if isinstance(capabilities, dict):
        endpoint_sets.append(capabilities.get("mcp"))
    for endpoints in endpoint_sets:
        if not isinstance(endpoints, dict):
            continue
        standard = endpoints.get("standard")
        if isinstance(standard, dict):
            standard["path"] = f"{prefix}/mcp"


def _sidecar_index_response(root: Path) -> Response:
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(404, "frontend index.html not found")
    return FileResponse(index, media_type="text/html; charset=utf-8")


def _safe_frontend_asset_path(root: Path, relpath: str) -> Path:
    clean = relpath.replace("\\", "/").lstrip("/")
    target = (root / clean).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(404, "asset not found")
    return target


def _mcp_initialize(dsl: AgentDsl, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": dsl.name, "version": dsl.version},
        "instructions": dsl.description,
    }


def _mcp_tools(dsl: AgentDsl) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for skill in dsl.skills:
        tool: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description or f"{dsl.name}.{skill.name}",
            "inputSchema": skill.input_schema,
        }
        if skill.output_schema:
            tool["outputSchema"] = _ensure_object_schema(skill.output_schema)
        tools.append(tool)
    return tools


async def _mcp_tool_call(
    dsl: AgentDsl,
    skills: dict[str, AgentDslSkill],
    worker: SidecarWorker,
    params: dict[str, Any],
) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "tools/call: missing name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPException(400, "tools/call: arguments must be object")
    skill = _skill_or_404(skills, name)
    _validate_arguments(skill, arguments)
    result = await worker.call(
        skill,
        SidecarWorkerRequest(
            agent=dsl.name,
            skill=skill.name,
            handler=skill.handler,
            arguments=arguments,
            auth=params.get("auth") if isinstance(params.get("auth"), dict) else None,
        ),
    )
    structured = result if skill.output_schema.get("type") == "object" else {"result": result}
    return {
        "content": [{"type": "text", "text": json.dumps(result, default=str)}],
        "structuredContent": structured,
        "isError": False,
    }


def _ensure_object_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        return schema
    return {
        "type": "object",
        "properties": {"result": schema},
        "required": ["result"],
    }


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
