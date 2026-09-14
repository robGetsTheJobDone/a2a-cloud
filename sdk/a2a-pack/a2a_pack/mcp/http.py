"""Streamable HTTP transport for MCP (with elicitation support).

Exposes ``POST /mcp`` for client→server requests. When a tool call drives
the skill into ``ctx.collect`` / ``ctx.ask``, the server pushes
``elicitation/create`` requests back to the client interleaved on the SSE
response, and routes the client's responses (delivered via a follow-up
``POST /mcp`` on the same ``Mcp-Session-Id``) to the in-flight future.

Content negotiation:
  * ``Accept: application/json`` (or no Accept) — one-shot JSON response.
    Skills that call ``ctx.collect``/``ctx.ask`` over this path will time
    out, because there is no channel to deliver the request to the client.
  * ``Accept`` includes ``text/event-stream`` — SSE response. Elicitation
    works end-to-end.

Mountable into an existing FastAPI app via :func:`mount_http`, or
serveable standalone via :func:`build_http_app`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import oauth as _oauth
from ..agent import A2AAgent, SkillInputError, SkillNotFound
from ..auth import (
    APIKeyAuth,
    APIKeyAuthResolver,
    AuthError,
    NoAuth,
    NoAuthResolver,
    PlatformUserAuth,
    PlatformUserAuthResolver,
    resolve_auth,
)
from ..context import LocalRunContext, MissingScopes, RunContext, ScopeDenied
from .server import (
    MCPServer,
    _INVALID_REQUEST,
    _PARSE_ERROR,
    _apply_caller_meta,
    _apply_consumer_setup,
    _apply_llm_creds,
    _error,
)

log = logging.getLogger(__name__)


# Caller identity cache: token-hash → (user_id, expires_at). Avoids hammering
# CP's /v1/me on every tools/call. This cache is only an identity lookup cache;
# stateless JWT revocation is still bounded by the token's own expiry unless the
# issuer/control-plane starts doing token introspection.
_ME_CACHE: dict[str, tuple[int, float]] = {}
_ME_CACHE_TTL_S = 60.0
_CLIENT_META_KEYS = frozenset({"progressToken"})
_CONNECTOR_ASYNC_AFTER_SECONDS = 60.0
_CONNECTOR_POLL_AFTER_SECONDS = 2
_CONNECTOR_JOB_TTL_SECONDS = 6 * 60 * 60


@dataclass
class _ConnectorJob:
    job_id: str
    skill_name: str
    args: dict[str, Any]
    created_at: float
    updated_at: float
    status: str = "queued"
    result: Any = None
    error: str | None = None
    interrupt: dict[str, Any] | None = None
    ctx: LocalRunContext[Any] | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    interrupted: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def touch(self) -> None:
        self.updated_at = asyncio.get_event_loop().time()


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(None, 1)[1].strip() or None


def _oauth_resource(request: Request | None) -> str:
    """This agent's canonical OAuth resource id (public base URL)."""
    base = str(request.base_url).rstrip("/") if request is not None else ""
    if request is not None and (
        request.url.path == "/_a2a" or request.url.path.startswith("/_a2a/")
    ):
        base = f"{base}/_a2a"
    return _oauth.resource_id(base)


def _challenge_headers(request: Request | None, *, error: str | None = None) -> dict[str, str]:
    """``WWW-Authenticate`` header that bootstraps MCP OAuth discovery (RFC 9728
    / MCP authorization spec): points the client at this resource's metadata."""
    if request is None:
        return {}
    prm_url = f"{_oauth_resource(request)}/.well-known/oauth-protected-resource"
    return {"WWW-Authenticate": _oauth.www_authenticate(prm_url, error=error)}


def _check_key(authorization: str | None, *, request: Request | None = None) -> None:
    """Optional platform shared-bearer gate for self-hosted deployments."""
    api_key = os.environ.get("A2A_API_KEY")
    if api_key is None:
        return
    token = _extract_bearer(authorization)
    if token is None:
        raise HTTPException(401, "missing bearer token", headers=_challenge_headers(request))
    if token != api_key:
        raise HTTPException(401, "invalid bearer token", headers=_challenge_headers(request))


def _me_cache_ttl_seconds(token: str) -> float:
    raw = os.environ.get("A2A_CP_ME_CACHE_TTL_SECONDS", "").strip()
    ttl = _ME_CACHE_TTL_S
    if raw:
        try:
            ttl = max(0.0, float(raw))
        except ValueError:
            log.warning("Ignoring invalid A2A_CP_ME_CACHE_TTL_SECONDS=%r", raw)
            ttl = _ME_CACHE_TTL_S
    if ttl <= 0:
        return 0.0

    try:
        import jwt

        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_exp": False,
                "verify_aud": False,
            },
        )
    except Exception:  # noqa: BLE001 - malformed/opaque tokens still use the CP result.
        return ttl

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return ttl
    return max(0.0, min(ttl, float(exp) - time.time()))


async def _resolve_agent_auth(
    agent: A2AAgent,
    authorization: str | None,
    *,
    headers: dict[str, str],
) -> Any:
    token = _extract_bearer(authorization)
    resolver = type(agent).auth_resolver
    if resolver is not None:
        try:
            return await resolve_auth(
                resolver,
                token,
                headers=headers,
                agent=agent,
            )
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc

    auth_cls = type(agent).auth_model
    if auth_cls is APIKeyAuth:
        try:
            return await APIKeyAuthResolver(
                accepted_key=os.environ.get("A2A_API_KEY"),
                api_key_id=os.environ.get("A2A_API_KEY_ID", "configured"),
            ).resolve(token, headers=headers, agent=agent)
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc
    if auth_cls is NoAuth:
        return await NoAuthResolver().resolve(token, headers=headers, agent=agent)
    if auth_cls is PlatformUserAuth:
        try:
            return await PlatformUserAuthResolver().resolve(
                token,
                headers=headers,
                agent=agent,
            )
        except AuthError as exc:
            raise HTTPException(exc.status_code, exc.message) from exc
    try:
        return auth_cls()
    except Exception:  # noqa: BLE001
        return NoAuth()


async def _require_account_access(
    agent: A2AAgent,
    authorization: str | None,
    *,
    headers: dict[str, str],
) -> None:
    access = getattr(type(agent), "account_access", None)
    if not bool(getattr(access, "required", False)):
        return
    try:
        await PlatformUserAuthResolver().resolve(
            _extract_bearer(authorization),
            headers=headers,
            agent=agent,
        )
    except AuthError as exc:
        raise HTTPException(
            401,
            {
                "error": "account_required",
                "message": "Sign in to A2A Cloud to use this agent.",
                "agent": type(agent).name,
            },
        ) from exc


async def _verify_cp_jwt(token: str, cp_url: str) -> int | None:
    """Return the caller's user_id by hitting ``GET {cp_url}/v1/me`` with
    the bearer token. Cached briefly. Returns None on any failure so the
    caller decides between "anonymous OK" (public agent) or 401 (private)."""
    import hashlib

    import httpx

    h = hashlib.sha256(token.encode()).hexdigest()
    now = asyncio.get_event_loop().time()
    ttl = _me_cache_ttl_seconds(token)
    cached = _ME_CACHE.get(h)
    if ttl > 0 and cached and cached[1] > now:
        return cached[0]
    if cached:
        _ME_CACHE.pop(h, None)
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{cp_url.rstrip('/')}/v1/me",
                headers={"authorization": f"bearer {token}"},
            )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    body = r.json() or {}
    uid = body.get("id")
    if not isinstance(uid, int):
        return None
    if ttl > 0:
        _ME_CACHE[h] = (uid, now + ttl)
    return uid


async def _enforce_agent_auth(
    authorization: str | None,
    *,
    request: Request | None = None,
    required_scopes: tuple[str, ...] = (),
) -> int | None:
    """Apply platform-level auth (called in addition to ``_check_key``).

    Returns the verified caller's user_id, or None for an unauthenticated
    request to a public agent. Raises 401/403 for unauthenticated calls
    to a private agent or for owner mismatches.

    Env vars are read on every call so operators can hot-swap deployment
    policy without restarting the pod. Public agents remain unauthenticated;
    private agents require a verified control-plane caller.

    OAuth resource server (E1-P3): a Keycloak RS256 bearer is validated against
    the realm JWKS first — signature/issuer/expiry, and (when enforced) ``aud``
    must name this resource so a token for another agent can't be replayed here.
    Identity still resolves through the control plane's ``/v1/me``.
    """
    cp_url = os.environ.get("A2A_CP_URL")
    public_env = os.environ.get("A2A_AGENT_PUBLIC", "").strip().lower()
    owner_env = os.environ.get("A2A_AGENT_OWNER_ID", "").strip()
    is_public = public_env in {"", "true", "1", "yes"}

    token = _extract_bearer(authorization)
    if token is None:
        if not is_public:
            raise HTTPException(
                401,
                "agent is private; missing bearer token",
                headers=_challenge_headers(request),
            )
        return None

    # OAuth resource-server validation for Keycloak (RS256) tokens. CP-issued
    # HS256 session JWTs skip this and go straight to /v1/me as before.
    if _oauth.oauth_enabled() and _oauth.is_keycloak_token(token):
        try:
            claims = _oauth.validate_keycloak_token(
                token,
                audience=_oauth_resource(request),
            )
        except _oauth.OAuthError as exc:
            raise HTTPException(
                401,
                f"oauth: {exc.message}",
                headers=_challenge_headers(request, error="invalid_token"),
            ) from exc
        missing_scopes = []
        if required_scopes and _oauth.require_scopes():
            missing_scopes = [
                scope for scope in required_scopes if not _oauth.has_scope(claims, scope)
            ]
        if missing_scopes:
            scope_text = ", ".join(repr(scope) for scope in missing_scopes)
            noun = "scope" if len(missing_scopes) == 1 else "scopes"
            raise HTTPException(
                403,
                f"oauth token missing required {noun} {scope_text}",
            )

    if not cp_url:
        # No control plane configured for verification — allow with no
        # identity (preserves self-hosted deployments).
        return None
    uid = await _verify_cp_jwt(token, cp_url)
    if uid is None:
        if not is_public:
            raise HTTPException(
                401,
                "invalid bearer token",
                headers=_challenge_headers(request, error="invalid_token"),
            )
        return None
    if not is_public and owner_env:
        try:
            owner_id = int(owner_env)
        except ValueError:
            raise HTTPException(500, "invalid A2A_AGENT_OWNER_ID env") from None
        if uid != owner_id:
            raise HTTPException(403, "not the owner of this private agent")
    return uid


def _normalize_required_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(scope for scope in value.replace(",", " ").split() if scope)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(scope) for scope in value if str(scope))
    return ()


def _agent_required_scopes(agent: A2AAgent, method: Any) -> tuple[str, ...]:
    mapping = getattr(type(agent), "mcp_required_scopes", None)
    if not isinstance(mapping, dict):
        mapping = getattr(agent, "mcp_required_scopes", None)
    if not isinstance(mapping, dict):
        return ()
    return _normalize_required_scopes(mapping.get(method))


def _required_scopes_for_method(agent: A2AAgent, method: Any) -> tuple[str, ...]:
    scopes: list[str] = []
    if method == "tools/list":
        scopes.append("agent:read")
    elif method == "tools/call":
        scopes.append("mcp:invoke")
    scopes.extend(_agent_required_scopes(agent, method))
    return tuple(dict.fromkeys(scopes))


def _wants_sse(accept: str | None) -> bool:
    if not accept:
        return False
    return "text/event-stream" in accept.lower()


def _is_jsonrpc_response(body: dict[str, Any]) -> bool:
    """A JSON-RPC message is a response (vs request/notification) when it
    has an id and a result/error but no method."""
    if "method" in body:
        return False
    if body.get("id") is None:
        return False
    return "result" in body or "error" in body


def _sanitize_tools_call_meta(body: dict[str, Any]) -> None:
    """Strip privileged transport metadata supplied by an MCP client.

    The HTTP transport is the trust boundary. MCP clients may send
    ``params._meta`` for protocol bookkeeping, but platform context such as
    CP JWTs, workspace buckets, consumer setup, and LLM grants must be
    generated by this server only after auth has been verified.
    """
    params = body.get("params")
    if not isinstance(params, dict):
        return
    sanitized = dict(params)
    meta = params.get("_meta")
    if isinstance(meta, dict):
        client_meta = {
            key: meta[key]
            for key in _CLIENT_META_KEYS
            if key in meta
        }
        if client_meta:
            sanitized["_meta"] = client_meta
        else:
            sanitized.pop("_meta", None)
    else:
        sanitized.pop("_meta", None)
    body["params"] = sanitized


class ElicitError(Exception):
    """Raised when the client declines/cancels an elicitation request or
    returns a JSON-RPC error in response to one."""


class _Session:
    """Per-client MCP session. Tracks server→client requests in flight so
    the client's response POSTs can resolve the right pending future."""

    def __init__(self, agent: A2AAgent) -> None:
        self.id = secrets.token_urlsafe(16)
        self.agent = agent
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._next_id = 0

    def _new_request_id(self) -> str:
        self._next_id += 1
        return f"srv-{self.id[:8]}-{self._next_id}"

    async def send_request(
        self,
        queue: "asyncio.Queue[dict[str, Any] | None]",
        method: str,
        params: dict[str, Any],
    ) -> Any:
        """Push a JSON-RPC request onto ``queue`` and await the response
        the client delivers via :meth:`deliver_response`."""
        rid = self._new_request_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[rid] = fut
        await queue.put(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        try:
            return await fut
        finally:
            self._pending.pop(rid, None)

    def deliver_response(self, message: dict[str, Any]) -> bool:
        """Route a JSON-RPC response from the client to the matching
        pending server→client future. Returns True on hit."""
        rid = message.get("id")
        if not isinstance(rid, str):
            return False
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            return False
        if "error" in message:
            err = message["error"] or {}
            fut.set_exception(
                ElicitError(err.get("message") or "client returned error")
            )
        else:
            fut.set_result(message.get("result"))
        return True


def _ask_schema() -> dict[str, Any]:
    """JSON Schema used to elicit a plain-text answer for ``ctx.ask``."""
    return {
        "type": "object",
        "required": ["answer"],
        "properties": {
            "answer": {
                "type": "string",
                "description": "Your answer",
            }
        },
    }


def _build_elicit_context(
    agent: A2AAgent, skill_name: str, session: _Session,
    queue: "asyncio.Queue[dict[str, Any] | None]",
    progress_token: Any = None,
    auth: Any | None = None,
) -> LocalRunContext[Any]:
    """Build a per-call context whose ``on_event`` translates skill
    events into MCP JSON-RPC traffic on the SSE stream:

    * ``input_request`` / ``question`` → ``elicitation/create`` request
      (server→client RPC; resolved when the client responds).
    * ``progress`` / ``text_delta`` / any other event → MCP
      ``notifications/progress`` notification. Lets long-running skills
      reset the client's tool-call idle timer; without these, clients
      like Claude Code abort the call after ~60s even though the HTTP
      stream stays open.

    ``progress_token`` is what the MCP client passed in
    ``params._meta.progressToken``; we echo it on every progress
    notification so the client can correlate with the originating
    tools/call. If the client didn't supply one we synthesize a token
    (the upstream tools/call request id) — most clients accept this and
    use it as a keep-alive signal.
    """
    if auth is None:
        auth_cls = type(agent).auth_model
        try:
            auth = auth_cls()
        except Exception:  # noqa: BLE001
            auth = NoAuth()
    ctx = LocalRunContext(auth=auth, task_id=f"mcp-{skill_name}")

    async def on_event(ev: Any) -> None:
        kind = getattr(ev, "kind", None)
        payload = getattr(ev, "payload", {}) or {}
        if kind == "input_request":
            request_id = payload.get("request_id")
            if not request_id:
                return
            title = payload.get("title") or ""
            reason = payload.get("reason") or ""
            message = f"{title}\n\n{reason}".strip() if reason else title
            schema = payload.get("schema") or {"type": "object"}
            try:
                result = await session.send_request(
                    queue,
                    "elicitation/create",
                    {"message": message, "requestedSchema": schema},
                )
            except ElicitError as exc:
                log.warning("elicitation/create failed: %s", exc)
                return
            if not isinstance(result, dict):
                return
            if result.get("action") == "accept":
                content = result.get("content")
                if isinstance(content, dict):
                    RunContext.submit_input(request_id, content)
            # decline / cancel: leave the registry future unresolved;
            # skill's own asyncio.wait_for timeout will fire.
            return
        if kind == "question":
            question_id = payload.get("question_id")
            if not question_id:
                return
            prompt = payload.get("prompt") or ""
            try:
                result = await session.send_request(
                    queue,
                    "elicitation/create",
                    {"message": prompt, "requestedSchema": _ask_schema()},
                )
            except ElicitError as exc:
                log.warning("elicitation/create failed: %s", exc)
                return
            if not isinstance(result, dict):
                return
            if result.get("action") == "accept":
                content = result.get("content") or {}
                answer = content.get("answer", "")
                if isinstance(answer, str):
                    RunContext.answer(question_id, answer)
            return
        # Infrastructure / audit events that aren't user-visible progress.
        # The MCP transport carries skill-level traffic only; receipts and
        # replay sessions are handed off to the platform via HTTP, not the
        # MCP stream.
        if kind in {"receipt_sealed", "replay_sealed", "receipt_error"}:
            return
        # Any other event kind → forward as notifications/progress so
        # the MCP client gets liveness signals and doesn't abort the
        # tool call on its idle timer. Fire-and-forget — we don't wait
        # for a response from the client.
        message: str | None = None
        if isinstance(payload, dict):
            for key in ("message", "text", "summary"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    message = value
                    break
        progress_params: dict[str, Any] = {
            "progressToken": progress_token,
            "kind": kind,
        }
        if message is not None:
            progress_params["message"] = message
        await queue.put({
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": progress_params,
        })

    ctx._on_event = on_event  # noqa: SLF001
    return ctx


def _connector_status_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "ok",
                    "queued",
                    "running",
                    "approval_required",
                    "approval_pending",
                    "approval_denied",
                    "input_required",
                    "auth_required",
                    "expired",
                    "failed",
                ],
            },
            "job_id": {"type": "string"},
            "poll_after_ms": {"type": "integer"},
            "approval_token": {"type": "string"},
            "approval_id": {"type": "string"},
            "request_id": {"type": "string"},
            "grant_id": {"type": "string"},
            "message": {"type": "string"},
            "result": {},
        },
        "required": ["status"],
    }


def _connector_call_result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "structuredContent": value,
        "isError": False,
    }


def _connector_tools(agent: A2AAgent) -> list[dict[str, Any]]:
    status_schema = _connector_status_schema()
    tools: list[dict[str, Any]] = []
    for spec in agent.skills.values():
        tools.append({
            "name": spec.name,
            "description": (
                spec.description
                or f"{type(agent).name}.{spec.name}"
            ) + (
                " Connector endpoint: returns status ok with result when it "
                "finishes inline. Poll job_result only when the response "
                "includes a job_id with running, approval_required, "
                "input_required, or auth_required status."
            ),
            "inputSchema": spec.input_schema,
            "outputSchema": status_schema,
        })
    tools.extend([
        {
            "name": "job_result",
            "description": "Poll a connector job started by any skill call.",
            "inputSchema": {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
            },
            "outputSchema": status_schema,
        },
        {
            "name": "submit_interaction",
            "description": (
                "Resume a connector job after approval_required or input_required."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["job_id", "approval_token"],
                "properties": {
                    "job_id": {"type": "string"},
                    "approval_token": {"type": "string"},
                    "decision": {"type": "string", "enum": ["approve", "deny"]},
                    "answer": {"type": "string"},
                    "value": {"type": "object"},
                    "grant_token": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
            "outputSchema": status_schema,
        },
    ])
    return tools


class _ConnectorJobManager:
    def __init__(self, agent: A2AAgent) -> None:
        self.agent = agent
        self.jobs: dict[str, _ConnectorJob] = {}

    def prune(self) -> None:
        now = asyncio.get_event_loop().time()
        for job_id, job in list(self.jobs.items()):
            if now - job.updated_at <= _CONNECTOR_JOB_TTL_SECONDS:
                continue
            if job.task is not None and not job.task.done():
                job.task.cancel()
            self.jobs.pop(job_id, None)

    async def handle_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        meta: dict[str, Any],
        auth: Any | None,
    ) -> dict[str, Any] | None:
        self.prune()
        if name == "job_result":
            job_id = str(arguments.get("job_id") or "")
            job = self.jobs.get(job_id)
            if job is None:
                return _connector_call_result({
                    "status": "expired",
                    "job_id": job_id,
                    "message": "Connector job not found or expired.",
                })
            return _connector_call_result(self.job_payload(job))
        if name == "submit_interaction":
            return _connector_call_result(await self.submit_interaction(arguments))
        if name not in self.agent.skills:
            return None
        job = _ConnectorJob(
            job_id=f"job_{secrets.token_hex(8)}",
            skill_name=name,
            args=dict(arguments),
            created_at=asyncio.get_event_loop().time(),
            updated_at=asyncio.get_event_loop().time(),
        )
        self.jobs[job.job_id] = job
        job.task = asyncio.create_task(
            self._run_job(job, meta=meta, auth=auth)
        )
        payload = await self._wait_initial(job)
        return _connector_call_result(payload)

    async def _wait_initial(self, job: _ConnectorJob) -> dict[str, Any]:
        done_task = asyncio.create_task(job.done.wait())
        interrupt_task = asyncio.create_task(job.interrupted.wait())
        try:
            done, _ = await asyncio.wait(
                {done_task, interrupt_task},
                timeout=_connector_async_after_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done_task in done:
                self.jobs.pop(job.job_id, None)
                return self.job_payload(job, include_job_handle=False)
            if interrupt_task in done:
                return self.job_payload(job)
            job.status = "running"
            job.touch()
            return self.job_payload(job)
        finally:
            for task in (done_task, interrupt_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _run_job(
        self,
        job: _ConnectorJob,
        *,
        meta: dict[str, Any],
        auth: Any | None,
    ) -> None:
        job.status = "running"
        ctx = _build_connector_context(self.agent, job, auth=auth)
        job.ctx = ctx
        _apply_caller_meta(ctx, meta)
        await _apply_consumer_setup(agent=self.agent, ctx=ctx, meta=meta)
        await _apply_llm_creds(agent=self.agent, ctx=ctx, meta=meta)
        try:
            job.result = await self.agent.invoke_json(job.skill_name, ctx, job.args)
            job.status = "ok"
        except SkillNotFound:
            job.status = "failed"
            job.error = f"unknown tool: {job.skill_name}"
        except SkillInputError as exc:
            job.status = "failed"
            job.error = f"invalid arguments: {exc}"
        except MissingScopes as exc:
            job.status = "failed"
            job.error = f"permission denied: {exc}"
        except ScopeDenied as exc:
            job.status = "approval_denied"
            job.error = str(exc) or "Approval denied."
        except Exception as exc:  # noqa: BLE001
            log.exception("connector job failed: %s", job.skill_name)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.touch()
            job.done.set()

    async def submit_interaction(self, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = str(arguments.get("job_id") or "")
        token = str(arguments.get("approval_token") or "")
        job = self.jobs.get(job_id)
        if job is None:
            return {"status": "expired", "job_id": job_id, "message": "Job expired."}
        interrupt = job.interrupt if isinstance(job.interrupt, dict) else None
        if interrupt is None:
            return self.job_payload(job)
        if token != interrupt.get("approval_token"):
            return {
                "status": "failed",
                "job_id": job_id,
                "message": "Invalid approval token.",
            }
        kind = str(interrupt.get("kind") or "")
        request_id = str(interrupt.get("request_id") or interrupt.get("approval_id") or "")
        ctx = job.ctx
        if ctx is None:
            job.status = "expired"
            return self.job_payload(job)
        if kind == "question":
            answer = arguments.get("answer")
            if isinstance(answer, str):
                RunContext.answer(request_id, answer)
                job.status = "running"
        elif kind == "input":
            value = arguments.get("value")
            if isinstance(value, dict):
                RunContext.submit_input(request_id, value)
                job.status = "running"
        elif kind == "scope":
            decision = str(arguments.get("decision") or "").lower()
            if decision == "deny":
                RunContext.deny_scope(
                    request_id,
                    str(arguments.get("reason") or "user denied approval"),
                )
                job.status = "approval_denied"
            elif decision == "approve":
                grant_token = arguments.get("grant_token")
                if isinstance(grant_token, str) and grant_token:
                    RunContext.resolve_scope_grant(request_id, grant_token)
                    job.status = "approval_pending"
                else:
                    return {
                        "status": "approval_required",
                        "job_id": job_id,
                        "message": "grant_token is required to approve scope.",
                        **interrupt,
                    }
            else:
                return {
                    "status": "approval_required",
                    "job_id": job_id,
                    "message": "decision must be approve or deny.",
                    **interrupt,
                }
        else:
            return self.job_payload(job)
        job.interrupt = None
        job.interrupted.clear()
        job.touch()
        return self.job_payload(job)

    def job_payload(
        self,
        job: _ConnectorJob,
        *,
        include_job_handle: bool = True,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"status": job.status}
        if include_job_handle:
            base.update({
                "job_id": job.job_id,
                "poll_after_ms": _CONNECTOR_POLL_AFTER_SECONDS * 1000,
            })
        if job.interrupt is not None and job.status in {
            "approval_required",
            "input_required",
            "auth_required",
        }:
            base.update(job.interrupt)
            return base
        if job.status == "ok":
            base["result"] = job.result
            return base
        if job.status == "failed":
            base["message"] = job.error or "Connector job failed."
            return base
        if job.status == "approval_denied":
            base["message"] = "Approval denied."
            return base
        base["message"] = "Connector job is still running."
        return base


def _connector_async_after_seconds() -> float:
    raw = os.environ.get("A2A_CONNECTOR_MCP_ASYNC_AFTER_SECONDS")
    try:
        value = float(raw) if raw is not None else _CONNECTOR_ASYNC_AFTER_SECONDS
    except ValueError:
        value = _CONNECTOR_ASYNC_AFTER_SECONDS
    return max(0.01, value)


def _build_connector_context(
    agent: A2AAgent,
    job: _ConnectorJob,
    *,
    auth: Any | None,
) -> LocalRunContext[Any]:
    if auth is None:
        auth_cls = type(agent).auth_model
        try:
            auth = auth_cls()
        except Exception:  # noqa: BLE001
            auth = NoAuth()
    ctx = LocalRunContext(auth=auth, task_id=f"connector-mcp-{job.skill_name}")

    async def on_event(ev: Any) -> None:
        kind = getattr(ev, "kind", None)
        payload = getattr(ev, "payload", {}) or {}
        if kind == "scope_request":
            request_id = str(payload.get("request_id") or "")
            if not request_id:
                return
            token = secrets.token_urlsafe(18)
            job.status = "approval_required"
            job.interrupt = {
                "status": "approval_required",
                "kind": "scope",
                "approval_type": "a2a_grant_extension",
                "approval_id": request_id,
                "request_id": request_id,
                "approval_token": token,
                "message": str(payload.get("reason") or "Agent requested more access."),
                "requested_scope": {
                    "read_patterns": payload.get("read_patterns") or [],
                    "write_prefix": payload.get("write_prefix"),
                    "write_prefixes": payload.get("write_prefixes") or [],
                    "mode": payload.get("mode"),
                    "ttl_seconds": payload.get("ttl_seconds"),
                },
                "retry": {
                    "tool": "submit_interaction",
                    "job_id": job.job_id,
                    "approval_token": token,
                },
            }
            job.interrupted.set()
            job.touch()
            return
        if kind == "input_request":
            request_id = str(payload.get("request_id") or "")
            if not request_id:
                return
            token = secrets.token_urlsafe(18)
            job.status = "input_required"
            job.interrupt = {
                "status": "input_required",
                "kind": "input",
                "request_id": request_id,
                "approval_token": token,
                "title": payload.get("title") or "More information needed",
                "message": payload.get("reason") or "Agent requested input.",
                "schema": payload.get("schema") or {"type": "object"},
                "ui_schema": payload.get("ui_schema") or {},
                "retry": {
                    "tool": "submit_interaction",
                    "job_id": job.job_id,
                    "approval_token": token,
                },
            }
            job.interrupted.set()
            job.touch()
            return
        if kind == "question":
            question_id = str(payload.get("question_id") or "")
            if not question_id:
                return
            token = secrets.token_urlsafe(18)
            job.status = "input_required"
            job.interrupt = {
                "status": "input_required",
                "kind": "question",
                "request_id": question_id,
                "approval_token": token,
                "message": payload.get("prompt") or "Agent asked a question.",
                "schema": _ask_schema(),
                "retry": {
                    "tool": "submit_interaction",
                    "job_id": job.job_id,
                    "approval_token": token,
                },
            }
            job.interrupted.set()
            job.touch()
            return

    ctx._on_event = on_event  # noqa: SLF001
    return ctx


def mount_http(app: FastAPI, agent: A2AAgent, *, prefix: str = "/mcp") -> None:
    """Mount the MCP endpoint onto an existing FastAPI app."""
    # Sessions live for the lifetime of this app instance. The header
    # value is opaque to clients — they just echo whatever we returned
    # on the response.
    sessions: dict[str, _Session] = {}

    router = APIRouter()

    def _get_or_create_session(session_id: str | None) -> _Session:
        if session_id and session_id in sessions:
            return sessions[session_id]
        s = _Session(agent)
        sessions[s.id] = s
        return s

    @router.post(prefix)
    async def mcp_endpoint(
        request: Request,
        authorization: str | None = Header(default=None),
        accept: str | None = Header(default=None),
        mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
    ) -> Any:
        custom_auth = (
            type(agent).auth_resolver is not None
            or type(agent).auth_model is PlatformUserAuth
        )
        if not custom_auth:
            _check_key(authorization, request=request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return _error(None, _PARSE_ERROR, f"parse error: {exc}")
        if not isinstance(body, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")

        method = body.get("method")
        caller_user_id = (
            None
            if custom_auth
            else await _enforce_agent_auth(
                authorization,
                request=request,
                required_scopes=_required_scopes_for_method(agent, method),
            )
        )

        # JSON-RPC response from the client (e.g. an elicitation result)?
        if _is_jsonrpc_response(body):
            session = sessions.get(mcp_session_id or "")
            if session is None:
                raise HTTPException(404, "unknown Mcp-Session-Id")
            if not session.deliver_response(body):
                raise HTTPException(404, "no pending server-initiated request with that id")
            return Response(status_code=202)

        # Client request / notification.
        session = _get_or_create_session(mcp_session_id)
        request_headers = dict(request.headers)
        agent_auth: Any | None = None
        if method == "tools/call":
            _sanitize_tools_call_meta(body)
            await _require_account_access(
                agent,
                authorization,
                headers=request_headers,
            )
            agent_auth = await _resolve_agent_auth(
                agent,
                authorization,
                headers=request_headers,
            )
            # PlatformUserAuth is already a verified control-plane identity.
            # The generic ``custom_auth`` branch intentionally skips the
            # separate /v1/me lookup above, so recover that verified user id
            # here. Without it, account-gated MCP calls never receive cp_jwt,
            # cp_url, or their workspace bucket and therefore cannot redeem
            # platform-funded LLM trial calls.
            if caller_user_id is None and isinstance(agent_auth, PlatformUserAuth):
                caller_user_id = agent_auth.user_id

        # When the caller is authenticated (has a verified CP JWT), inject
        # _meta.cp_jwt + cp_url + bucket onto tools/call params so skills
        # see the same caller context they'd get via the orchestrator.
        # Client-supplied privileged _meta was stripped above; only this
        # verified branch may attach platform context.
        if method == "tools/call" and caller_user_id is not None:
            token = _extract_bearer(authorization)
            if token:
                params = body.get("params") or {}
                if isinstance(params, dict):
                    meta = dict(params.get("_meta") or {})
                    meta["cp_jwt"] = token
                    meta["cp_url"] = os.environ.get("A2A_CP_URL", "")
                    meta["bucket"] = f"user-{caller_user_id}-files"
                    params["_meta"] = meta
                    body["params"] = params

        # Tools/call gets the elicit-capable path when the client accepts
        # SSE. Other methods (initialize, tools/list, ping, notifications)
        # never elicit; keep them as one-shot JSON.
        if method == "tools/call" and _wants_sse(accept):
            return _stream_tools_call(agent, session, body, auth=agent_auth)

        if method == "tools/call":
            def build_ctx(skill_name: str) -> LocalRunContext[Any]:
                return LocalRunContext(
                    auth=agent_auth if agent_auth is not None else NoAuth(),
                    task_id=f"mcp-{skill_name}",
                )

            server = MCPServer(agent, context_builder=build_ctx)
        else:
            server = MCPServer(agent)
        response = await server.handle(body)
        if response is None:
            return Response(
                status_code=202, headers={"Mcp-Session-Id": session.id}
            )
        return JSONResponse(response, headers={"Mcp-Session-Id": session.id})

    @router.get(prefix)
    async def mcp_get_unsupported() -> Any:
        # Streamable HTTP allows GET for standalone server-initiated
        # streams. We attach them inline to POST responses instead.
        raise HTTPException(405, "GET not supported; use POST /mcp")

    app.include_router(router)


def mount_connector_http(
    app: FastAPI,
    agent: A2AAgent,
    *,
    prefix: str = "/connector-mcp",
) -> None:
    """Mount connector-optimized MCP with async jobs and structured interrupts."""
    sessions: dict[str, _Session] = {}
    manager = _ConnectorJobManager(agent)
    router = APIRouter()

    def _get_or_create_session(session_id: str | None) -> _Session:
        if session_id and session_id in sessions:
            return sessions[session_id]
        s = _Session(agent)
        sessions[s.id] = s
        return s

    @router.post(prefix)
    async def connector_mcp_endpoint(
        request: Request,
        authorization: str | None = Header(default=None),
        mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
    ) -> Any:
        custom_auth = (
            type(agent).auth_resolver is not None
            or type(agent).auth_model is PlatformUserAuth
        )
        if not custom_auth:
            _check_key(authorization, request=request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            return _error(None, _PARSE_ERROR, f"parse error: {exc}")
        if not isinstance(body, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")

        method = body.get("method")
        caller_user_id = (
            None
            if custom_auth
            else await _enforce_agent_auth(
                authorization,
                request=request,
                required_scopes=_required_scopes_for_method(agent, method),
            )
        )

        if _is_jsonrpc_response(body):
            session = sessions.get(mcp_session_id or "")
            if session is None:
                raise HTTPException(404, "unknown Mcp-Session-Id")
            if not session.deliver_response(body):
                raise HTTPException(404, "no pending server request with that id")
            return Response(status_code=202)

        session = _get_or_create_session(mcp_session_id)
        request_headers = dict(request.headers)
        agent_auth: Any | None = None
        if method == "tools/call":
            _sanitize_tools_call_meta(body)
            await _require_account_access(
                agent,
                authorization,
                headers=request_headers,
            )
            agent_auth = await _resolve_agent_auth(
                agent,
                authorization,
                headers=request_headers,
            )
            if caller_user_id is not None:
                token = _extract_bearer(authorization)
                if token:
                    params = body.get("params") or {}
                    if isinstance(params, dict):
                        meta = dict(params.get("_meta") or {})
                        meta["cp_jwt"] = token
                        meta["cp_url"] = os.environ.get("A2A_CP_URL", "")
                        meta["bucket"] = f"user-{caller_user_id}-files"
                        params["_meta"] = meta
                        body["params"] = params

        async def handle_connector_call(
            name: str,
            arguments: dict[str, Any],
            meta: dict[str, Any],
        ) -> dict[str, Any] | None:
            return await manager.handle_tool_call(name, arguments, meta, agent_auth)

        server = MCPServer(
            agent,
            tool_call_handler=handle_connector_call,
            tools_provider=lambda: _connector_tools(agent),
        )
        response = await server.handle(body)
        if response is None:
            return Response(
                status_code=202,
                headers={"Mcp-Session-Id": session.id},
            )
        return JSONResponse(response, headers={"Mcp-Session-Id": session.id})

    @router.get(prefix)
    async def connector_mcp_get_unsupported() -> Any:
        raise HTTPException(405, "GET not supported; use POST")

    app.include_router(router)


def _stream_tools_call(
    agent: A2AAgent,
    session: _Session,
    body: dict[str, Any],
    *,
    auth: Any | None = None,
) -> StreamingResponse:
    """SSE response for ``tools/call``. Interleaves any
    ``elicitation/create`` requests the skill emits via
    ``ctx.collect`` / ``ctx.ask`` before yielding the final tool result."""
    queue: "asyncio.Queue[dict[str, Any] | None]" = asyncio.Queue()
    msg_id = body.get("id")
    params = body.get("params") or {}
    skill_name = params.get("name") if isinstance(params, dict) else None
    # Prefer the progressToken the MCP client supplied (per spec). Fall
    # back to the tools/call id so clients that don't opt in still get a
    # consistent token they can map to the call.
    progress_token: Any = msg_id
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict) and "progressToken" in meta:
            progress_token = meta.get("progressToken")

    # Build a context that turns local events into elicit-create requests
    # plus progress notifications.
    def build_ctx(name: str) -> LocalRunContext[Any]:
        return _build_elicit_context(
            agent,
            name,
            session,
            queue,
            progress_token=progress_token,
            auth=auth,
        )

    server = MCPServer(agent, context_builder=build_ctx)

    async def run_dispatch() -> dict[str, Any] | None:
        try:
            return await server.handle(body)
        except Exception as exc:  # noqa: BLE001
            log.exception("mcp tools/call dispatch failed")
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }

    async def gen() -> AsyncIterator[bytes]:
        dispatch_task = asyncio.create_task(run_dispatch())
        queue_task: asyncio.Task[dict[str, Any] | None] | None = None
        try:
            while True:
                queue_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queue_task, dispatch_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task in done:
                    msg = queue_task.result()
                    queue_task = None
                    if msg is not None:
                        yield (b"data: " + json.dumps(msg).encode() + b"\n\n")
                else:
                    queue_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_task
                    queue_task = None
                if dispatch_task in done:
                    final = dispatch_task.result()
                    # Drain any elicit requests still sitting in the queue.
                    while not queue.empty():
                        leftover = queue.get_nowait()
                        if leftover is not None:
                            yield (
                                b"data: "
                                + json.dumps(leftover).encode()
                                + b"\n\n"
                            )
                    if final is not None:
                        yield (b"data: " + json.dumps(final).encode() + b"\n\n")
                    break
        finally:
            if queue_task is not None and not queue_task.done():
                queue_task.cancel()
                with suppress(asyncio.CancelledError):
                    await queue_task
            if not dispatch_task.done():
                dispatch_task.cancel()
                with suppress(asyncio.CancelledError):
                    await dispatch_task
        _ = skill_name  # silence unused-var warning when debug logging is off

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Mcp-Session-Id": session.id,
        },
    )


def build_http_app(agent: A2AAgent) -> FastAPI:
    """Build a minimal FastAPI app exposing only the MCP endpoint."""
    app = FastAPI(title=f"{type(agent).name} (MCP)", version=type(agent).version)
    mount_http(app, agent)
    return app
