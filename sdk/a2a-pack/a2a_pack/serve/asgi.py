"""HTTP adapter that turns any :class:`A2AAgent` into a service.

This is intentionally minimal: it covers the surface needed to plug into
the wider A2A ecosystem and the platform's control plane.

Endpoints:
  GET  /healthz                     -> liveness
  GET  /.well-known/agent-card      -> Agent Card JSON
  POST /invoke/{skill}              -> invoke skill (JSON in, JSON out)

Auth: by default a single bearer token is read from the ``A2A_API_KEY`` env
var. Agents can instead set ``auth_resolver`` to validate caller bearer tokens
against their own API, OIDC userinfo/introspection endpoint, or other identity
bridge. The resolved principal is materialized into the declared
``auth_model``.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import oauth as _oauth
from ..agent import A2AAgent, SkillInputError, SkillNotFound
from ..a2a_client import HttpA2AClient
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
from ..context import AgentEvent, LLMCreds, LocalRunContext, MissingScopes, RunContext
from ..composition import ensure_composition_budget
from ..consumer_setup_runtime import (
    ConsumerSetupLookupError,
    resolve_invoke_consumer_setup,
)
from ..discovery import ControlPlaneDiscovery
from ..frontend import (
    FRONTEND_AGENT_API_PREFIX,
    PackedFrontend,
    frontend_ui_metadata,
    mount_packed_frontend,
    packed_frontend_from_env,
    skills_payload,
)
from ..grants import Grant, GrantInvalid, verify_grant
from ..openapi import agent_openapi_spec
from ..runtime import AgentEndpoint, LLMProvisioning
from ..workspace import _normalize_write_prefixes

_log = logging.getLogger(__name__)
SSE_HEARTBEAT_SECONDS = 15.0
SSE_HEARTBEAT = object()
A2A_PROTOCOL_VERSION = "1.0"
_PLATFORM_LLM_GRANT_GRACE_SECONDS = 30


class _PlatformLLMGrantError(RuntimeError):
    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class _LLMCredsIn(BaseModel):
    base_url: str
    api_key: str
    model: str
    temperature_mode: str = "default"
    temperature: float | None = None
    extra_body: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class _InvokeIn(BaseModel):
    arguments: dict[str, Any] = {}
    grant: str | None = None
    llm_creds: _LLMCredsIn | None = None
    composition: dict[str, Any] | None = None
    consumer_config: dict[str, Any] | None = None
    consumer_secrets: dict[str, str] | None = None
    # The caller's CP JWT, forwarded by the orchestrator when this
    # agent's Card declares ``wants_cp_jwt=True``. Lets the skill call
    # back into /v1/me/* endpoints on the user's behalf.
    cp_jwt: str | None = None
    cp_url: str | None = None


def _llm_accepts_caller_creds(provisioning: LLMProvisioning | str) -> bool:
    value = (
        provisioning.value
        if isinstance(provisioning, LLMProvisioning)
        else str(provisioning)
    )
    return value in {
        LLMProvisioning.CALLER_PROVIDED.value,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED.value,
    }


def _llm_accepts_platform_creds(provisioning: LLMProvisioning | str) -> bool:
    value = (
        provisioning.value
        if isinstance(provisioning, LLMProvisioning)
        else str(provisioning)
    )
    return value in {
        LLMProvisioning.PLATFORM.value,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED.value,
    }


def _llm_field(creds: Any, name: str, default: Any = None) -> Any:
    if isinstance(creds, dict):
        return creds.get(name, default)
    return getattr(creds, name, default)


def _set_ctx_llm_creds(
    ctx: LocalRunContext[Any],
    llm_creds: Any,
    *,
    source: str,
) -> bool:
    api_key = str(_llm_field(llm_creds, "api_key") or "")
    base_url = str(_llm_field(llm_creds, "base_url") or "")
    model = str(_llm_field(llm_creds, "model") or "")
    if not (api_key and base_url and model):
        return False
    temperature = _llm_field(llm_creds, "temperature")
    try:
        parsed_temperature = float(temperature) if temperature is not None else None
    except (TypeError, ValueError):
        parsed_temperature = None
    object.__setattr__(
        ctx,
        "_llm_creds",
        LLMCreds(
            base_url=base_url,
            api_key=api_key,
            model=model,
            source=source,
            temperature_mode=str(
                _llm_field(llm_creds, "temperature_mode", "default")
            ),
            temperature=parsed_temperature,
            extra_body=dict(_llm_field(llm_creds, "extra_body") or {}),
            metadata=dict(_llm_field(llm_creds, "metadata") or {}),
        ),
    )
    return True


def _platform_llm_creds_from_grant(grant: Grant, token: str) -> dict[str, Any]:
    models = tuple(str(item).strip() for item in grant.llm_models if str(item).strip())
    return {
        "base_url": (
            os.environ.get(
                "A2A_LITELLM_URL",
                "http://litellm.llm.svc.cluster.local:4000",
            ).rstrip("/")
            + "/v1"
        ),
        "api_key": token,
        "model": (
            models[0]
            if models
            else os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5")
        ),
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": {
            "a2a_grant_id": grant.grant_id,
            "a2a_agent_name": grant.audience,
            "a2a_llm_source": "platform_grant",
        },
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


def _safe_next_path(raw: str | None) -> str:
    """Clamp a post-login redirect to a path on this agent's own origin.

    Anything absolute, scheme-relative, or control-character bearing collapses
    to ``/`` so the hand-off can never be pointed at another site.
    """
    candidate = str(raw or "").strip()
    if not candidate:
        return "/"
    if len(candidate) > 512:
        return "/"
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return "/"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate


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


async def _fetch_platform_llm_grant_from_cp(
    *,
    cp_url: str,
    cp_token: str,
    agent_name: str,
    ttl_seconds: int,
) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{cp_url.rstrip('/')}/v1/platform/llm-grant",
            headers={"authorization": f"bearer {cp_token}"},
            json={"audience": agent_name, "ttl_seconds": ttl_seconds},
        )
    if response.status_code >= 400:
        try:
            error_body = response.json()
        except ValueError:
            error_body = None
        detail = (
            error_body.get("detail")
            if isinstance(error_body, dict) and "detail" in error_body
            else error_body
        )
        raise _PlatformLLMGrantError(
            response.status_code,
            detail
            or (
                f"LLM credential lookup failed: HTTP {response.status_code}: "
                f"{response.text[:500]}"
            ),
        )
    data = response.json()
    return data if isinstance(data, dict) else {}


def _append_ctx_grant_id(ctx: LocalRunContext[Any], grant_id: str | None) -> None:
    clean = str(grant_id or "").strip()
    if not clean:
        return
    existing = tuple(getattr(ctx, "grant_ids", ()) or ())
    if clean in existing:
        return
    ctx.grant_ids = (*existing, clean)


class _AnswerIn(BaseModel):
    answer: str


class _InputResponseIn(BaseModel):
    value: dict[str, Any]


class _ScopeGrantIn(BaseModel):
    grant: str


class _ScopeDenyIn(BaseModel):
    reason: str


def _accepted_grant_audiences(agent: A2AAgent) -> frozenset[str]:
    audiences = {type(agent).name}
    for env_name in ("A2A_AGENT_AUDIENCE", "A2A_AGENT_AUDIENCE_ALIASES"):
        raw = os.environ.get(env_name, "")
        audiences.update(part.strip() for part in raw.split(",") if part.strip())
    return frozenset(audiences)


def _assert_grant_audience(grant: Grant, agent: A2AAgent) -> None:
    accepted = _accepted_grant_audiences(agent)
    if grant.audience in accepted:
        return
    expected = ", ".join(sorted(accepted))
    raise HTTPException(
        403,
        f"grant audience mismatch: expected one of [{expected}], got {grant.audience!r}",
    )


def sse_comment(comment: str = "ping") -> bytes:
    return f": {comment}\n\n".encode()


async def queue_get_or_heartbeat(
    queue: asyncio.Queue[dict[str, Any] | None],
    timeout: float = SSE_HEARTBEAT_SECONDS,
) -> dict[str, Any] | None | object:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return SSE_HEARTBEAT


def build_app(agent: A2AAgent, *, frontend: PackedFrontend | None = None) -> FastAPI:
    """Build a FastAPI app for the given agent instance."""
    app = FastAPI(title=type(agent).name, version=type(agent).version)
    api_key = os.environ.get("A2A_API_KEY")
    a2a_tasks: dict[str, dict[str, Any]] = {}
    a2a_push_configs: dict[str, dict[str, dict[str, Any]]] = {}
    packed_frontend = frontend if frontend is not None else packed_frontend_from_env()

    # Expose the same skills over MCP at POST /mcp so the agent is
    # callable from Claude Code, Cursor, and any other MCP client.
    from ..mcp import mount_http as _mount_mcp
    from ..mcp import mount_connector_http as _mount_connector_mcp
    _mount_mcp(app, agent)
    _mount_connector_mcp(app, agent)
    if packed_frontend is not None and packed_frontend.mount == "/":
        _mount_mcp(app, agent, prefix=f"{FRONTEND_AGENT_API_PREFIX}/mcp")
        _mount_connector_mcp(
            app,
            agent,
            prefix=f"{FRONTEND_AGENT_API_PREFIX}/connector-mcp",
        )

    def _a2a_request_auth_error(request: Request) -> JSONResponse | None:
        return _a2a_auth_error_response(
            request,
            allow_noauth_without_api_key=(
                type(agent).auth_resolver is None and type(agent).auth_model is NoAuth
            ),
        )

    async def _build_auth(
        provided_key: str | None,
        *,
        bearer_token: str | None,
        headers: dict[str, str],
    ) -> tuple[Any, Any]:
        """Return ``(resolver, auth_principal)``.

        Returning the resolver lets callers derive the caller id via
        ``resolver.principal_id(auth)`` for receipts + replay sessions.
        """
        resolver = type(agent).auth_resolver
        if resolver is not None:
            try:
                auth = await resolve_auth(
                    resolver,
                    bearer_token,
                    headers=headers,
                    agent=agent,
                )
            except AuthError as exc:
                raise HTTPException(exc.status_code, exc.message) from exc
            return resolver, auth
        auth_cls = type(agent).auth_model
        if auth_cls is APIKeyAuth:
            apikey_resolver = APIKeyAuthResolver(
                accepted_key=os.environ.get("A2A_API_KEY"),
                api_key_id=os.environ.get("A2A_API_KEY_ID", "configured"),
            )
            return apikey_resolver, await apikey_resolver.resolve(
                provided_key, headers=headers, agent=agent
            )
        if auth_cls is NoAuth:
            no_auth = NoAuthResolver()
            return no_auth, await no_auth.resolve(
                bearer_token,
                headers=headers,
                agent=agent,
            )
        if auth_cls is PlatformUserAuth:
            platform_resolver = PlatformUserAuthResolver()
            try:
                auth = await platform_resolver.resolve(
                    bearer_token,
                    headers=headers,
                    agent=agent,
                )
            except AuthError as exc:
                raise HTTPException(exc.status_code, exc.message) from exc
            return platform_resolver, auth
        # Unknown auth model: try default-construct, else fail loudly.
        try:
            return None, auth_cls()
        except Exception as exc:  # pragma: no cover - depends on user model
            raise HTTPException(
                status_code=500,
                detail=f"cannot materialize auth_model {auth_cls.__name__}: {exc}",
            ) from exc

    def _local_consumer_setup_from_env(
        body: _InvokeIn,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        consumer_config = dict(body.consumer_config or {})
        consumer_secrets = dict(body.consumer_secrets or {})
        if os.environ.get("A2A_LOCAL_DEV") == "1":
            for field in type(agent).consumer_setup.fields:
                value = os.environ.get(field.name)
                if value is None:
                    continue
                if field.kind == "secret":
                    consumer_secrets.setdefault(field.name, value)
                else:
                    consumer_config.setdefault(field.name, value)
        return consumer_config or None, consumer_secrets or None

    async def _consumer_setup_for_invoke(
        body: _InvokeIn,
        request_headers: dict[str, str],
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        consumer_config, consumer_secrets = _local_consumer_setup_from_env(body)
        cp_jwt = _trusted_cp_jwt(body, request_headers)
        cp_url = _control_plane_url(body)
        try:
            return await resolve_invoke_consumer_setup(
                setup=type(agent).consumer_setup,
                cp_url=cp_url,
                cp_jwt=cp_jwt,
                agent_name=type(agent).name,
                consumer_config=consumer_config,
                consumer_secrets=consumer_secrets,
            )
        except ConsumerSetupLookupError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    def _check_key(authorization: str | None) -> str | None:
        if api_key is None:
            return None
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.split(None, 1)[1].strip()
        if token != api_key:
            raise HTTPException(401, "invalid bearer token")
        return token

    def _extract_bearer(authorization: str | None) -> str | None:
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        return authorization.split(None, 1)[1].strip() or None

    def _runtime_cp_jwt() -> str | None:
        token = os.environ.get("A2A_CP_JWT")
        return token.strip() if isinstance(token, str) and token.strip() else None

    def _trusted_cp_jwt(
        body: _InvokeIn,
        request_headers: dict[str, str],
    ) -> str | None:
        return (
            body.cp_jwt
            or _session_token_from_cookie_header(
                request_headers.get("cookie") or request_headers.get("Cookie")
            )
            or _runtime_cp_jwt()
        )

    def _invoke_token(authorization: str | None) -> str | None:
        if type(agent).auth_resolver is not None:
            return _extract_bearer(authorization)
        if type(agent).auth_model is PlatformUserAuth:
            return _extract_bearer(authorization)
        return _check_key(authorization)

    def _control_plane_url(body: _InvokeIn) -> str:
        del body
        return (
            os.environ.get("A2A_CP_URL")
            or "http://control-plane.control-plane.svc.cluster.local"
        ).rstrip("/")

    def _control_plane_token(
        body: _InvokeIn,
        authorization: str | None,
        request_headers: dict[str, str],
    ) -> str | None:
        return _trusted_cp_jwt(body, request_headers) or _extract_bearer(authorization)

    async def _platform_session(request: Request) -> PlatformUserAuth | None:
        try:
            return await PlatformUserAuthResolver().resolve(
                _extract_bearer(request.headers.get("authorization")),
                headers=dict(request.headers),
                agent=agent,
            )
        except AuthError:
            return None

    async def _require_frontend_session(request: Request) -> Response | None:
        if await _platform_session(request) is None:
            login_url = os.environ.get("A2A_LOGIN_URL", "").strip()
            accept = request.headers.get("accept", "")
            if login_url and "text/html" in accept.lower():
                separator = "&" if "?" in login_url else "?"
                return RedirectResponse(
                    f"{login_url}{separator}next={quote(str(request.url), safe='')}",
                    status_code=303,
                )
            return JSONResponse(
                {"detail": "platform session required"},
                status_code=401,
            )
        return None

    def _tracking_id(grant_obj: Grant | None) -> str:
        return grant_obj.grant_id if grant_obj is not None else f"invoke-{secrets.token_hex(8)}"

    def _grant_scopes(grant_obj: Grant | None) -> dict[str, Any]:
        if grant_obj is None:
            return {}
        write_prefixes = _normalize_write_prefixes(
            grant_obj.outputs_prefix,
            grant_obj.write_prefixes,
        )
        return {
            "bucket": grant_obj.bucket,
            "mode": str(grant_obj.mode.value if hasattr(grant_obj.mode, "value") else grant_obj.mode),
            "allow_patterns": list(grant_obj.allow_patterns),
            "deny_patterns": list(grant_obj.deny_patterns),
            "outputs_prefix": grant_obj.outputs_prefix,
            "write_prefixes": list(write_prefixes),
            "ttl_seconds": max(0, int(grant_obj.expires_at or 0) - int(grant_obj.issued_at or 0)),
        }

    async def _attach_platform_llm_grant(
        ctx: LocalRunContext[Any],
        grant_obj: Grant | None,
        *,
        body: _InvokeIn,
        authorization: str | None,
        request_headers: dict[str, str],
    ) -> Grant | None:
        if isinstance(getattr(ctx, "_llm_creds", None), LLMCreds):
            return grant_obj
        if "llm_provisioning" not in type(agent).__dict__:
            return grant_obj
        if not _llm_accepts_platform_creds(type(agent).llm_provisioning):
            return grant_obj

        cp_url = _control_plane_url(body).strip()
        cp_token = _trusted_cp_jwt(body, request_headers) or _extract_bearer(
            authorization
        )
        if not (cp_url and cp_token):
            raise RuntimeError(
                "LLM key required. Add an LLM credential in Settings > "
                "LLM credentials before running this agent."
            )

        try:
            payload = await _fetch_platform_llm_grant_from_cp(
                cp_url=cp_url,
                cp_token=cp_token,
                agent_name=type(agent).name,
                ttl_seconds=_agent_platform_llm_ttl_seconds(agent),
            )
        except _PlatformLLMGrantError as exc:
            if exc.status_code == 402:
                raise HTTPException(402, exc.detail) from exc
            raise RuntimeError(
                "LLM key required. Add an LLM credential in Settings > "
                "LLM credentials before running this agent."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "user LLM credential lookup failed for %s: %s",
                type(agent).name,
                exc,
            )
            raise RuntimeError(
                "LLM key required. Add an LLM credential in Settings > "
                "LLM credentials before running this agent."
            ) from exc

        token = payload.get("grant") if isinstance(payload.get("grant"), str) else None
        fetched_grant: Grant | None = None
        if token:
            try:
                fetched_grant = verify_grant(token)
                _assert_grant_audience(fetched_grant, agent)
            except GrantInvalid as exc:
                _log.warning(
                    "LLM credential grant verification failed for %s: %s",
                    type(agent).name,
                    exc,
                )
                fetched_grant = None

        if fetched_grant is None:
            return grant_obj

        llm_creds = payload.get("llm_creds") if isinstance(payload, dict) else None
        if isinstance(llm_creds, dict):
            _set_ctx_llm_creds(ctx, llm_creds, source=str(llm_creds.get("source") or "user"))

        _append_ctx_grant_id(ctx, fetched_grant.grant_id)
        if grant_obj is not None:
            return grant_obj

        object.__setattr__(
            ctx,
            "_workspace",
            _grant_to_workspace(fetched_grant, agent, token),
        )
        _bind_minio_artifacts(ctx, fetched_grant)
        return fetched_grant

    def _args_preview(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str) and len(value) > limit:
                out[key] = value[:limit] + f"... (+{len(value) - limit} chars)"
            else:
                out[key] = value
        return out

    def _result_preview(result: Any) -> Any:
        if isinstance(result, dict):
            selected = {
                key: value
                for key, value in result.items()
                if key
                in {
                    "ok",
                    "status",
                    "summary",
                    "message",
                    "path",
                    "url",
                    "output_path",
                    "chart_path",
                    "error",
                    "stop_reason",
                }
            }
            if selected:
                return selected
        try:
            raw = json.dumps(result, ensure_ascii=False)
        except TypeError:
            raw = str(result)
        return {"preview": raw[:4000], "truncated": len(raw) > 4000}

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _llm_usage_from_message(message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            dumper = getattr(message, "model_dump", None)
            if callable(dumper):
                try:
                    message = dumper(mode="json")
                except TypeError:
                    message = dumper()
        if not isinstance(message, dict):
            return None

        usage = message.get("usage_metadata")
        response = message.get("response_metadata")
        if not isinstance(usage, dict):
            usage = {}
        if not isinstance(response, dict):
            response = {}

        token_usage = response.get("token_usage")
        if isinstance(token_usage, dict):
            usage = {
                **usage,
                "input_tokens": usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or token_usage.get("prompt_tokens"),
                "output_tokens": usage.get("output_tokens")
                or usage.get("completion_tokens")
                or token_usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens")
                or token_usage.get("total_tokens"),
            }

        prompt = _int(
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_token_count")
        )
        completion = _int(
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("output_token_count")
        )
        total = _int(usage.get("total_tokens")) or prompt + completion
        cost = _float(
            response.get("response_cost")
            or response.get("cost")
            or usage.get("response_cost")
            or usage.get("cost")
            or usage.get("cost_usd")
        )
        model = response.get("model_name") or response.get("model") or usage.get("model")
        provider = response.get("provider") or usage.get("provider")
        if total <= 0 and cost <= 0 and not (model or provider):
            return None
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cost_usd": cost,
            "model": str(model or ""),
            "provider": str(provider) if provider else None,
            "metadata": {
                "finish_reason": response.get("finish_reason"),
                "system_fingerprint": response.get("system_fingerprint"),
            },
        }

    def _merge_llm_usage(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        metadata: dict[str, Any] = {"call_count": len(items)}
        models = [str(item.get("model") or "") for item in items if item.get("model")]
        providers = [
            str(item.get("provider") or "") for item in items if item.get("provider")
        ]
        if models:
            metadata["models"] = sorted(set(models))
        if providers:
            metadata["providers"] = sorted(set(providers))
        return {
            "prompt_tokens": sum(_int(item.get("prompt_tokens")) for item in items),
            "completion_tokens": sum(
                _int(item.get("completion_tokens")) for item in items
            ),
            "total_tokens": sum(_int(item.get("total_tokens")) for item in items),
            "cost_usd": sum(_float(item.get("cost_usd")) for item in items),
            "model": models[-1] if models else "",
            "provider": providers[-1] if providers else None,
            "metadata": metadata,
        }

    def _extract_llm_usage(result: Any) -> dict[str, Any] | None:
        messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(messages, list):
            return None
        usages = [
            usage
            for message in messages
            if (usage := _llm_usage_from_message(message)) is not None
        ]
        return _merge_llm_usage(usages)

    def _summary(result: Any, error: str | None = None) -> str:
        if error:
            return error[:240]
        if isinstance(result, dict):
            if isinstance(result.get("error"), str):
                return f"error: {result['error'][:200]}"
            if result.get("ok") is False:
                reason = result.get("stop_reason") or result.get("status") or "returned ok=false"
                return f"error: {str(reason)[:200]}"
            for key in ("summary", "message", "path", "url", "output_path", "chart_path"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return f"{key}={value[:200]}"
        return "ok"

    def _result_failed(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("ok") is False:
            return True
        status = result.get("status")
        return isinstance(status, str) and status.lower() in {
            "failed",
            "failure",
            "error",
            "partial",
            "blocked",
            "canceled",
            "cancelled",
        }

    def _exception_payload(exc: BaseException) -> dict[str, Any]:
        serializer = getattr(exc, "to_error_payload", None)
        if callable(serializer):
            try:
                payload = serializer()
                if isinstance(payload, dict):
                    return payload
            except Exception:  # noqa: BLE001
                pass
        message = f"{type(exc).__name__}: {exc}"
        return {"type": type(exc).__name__, "message": message}

    def _exception_message(exc: BaseException) -> str:
        payload = _exception_payload(exc)
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
        return f"{type(exc).__name__}: {exc}"

    async def _post_tracking(
        body: _InvokeIn,
        event: dict[str, Any],
    ) -> None:
        cp_jwt = body.cp_jwt or _runtime_cp_jwt()
        if not cp_jwt:
            return
        cp_url = _control_plane_url(body)
        if not cp_url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{cp_url.rstrip('/')}/v1/me/subagent-runs/track",
                    json=event,
                    headers={"authorization": f"bearer {cp_jwt}"},
                )
        except Exception:  # noqa: BLE001
            _log.debug("failed to post subagent tracking event", exc_info=True)

    async def _track_started(
        tracking_id: str,
        skill_name: str,
        body: _InvokeIn,
        grant_obj: Grant | None,
    ) -> float:
        await _post_tracking(
            body,
            {
                "type": "agent_invoke_started",
                "grant_id": tracking_id,
                "to": type(agent).name,
                "agent": type(agent).name,
                "skill": skill_name,
                "scopes": _grant_scopes(grant_obj),
                "args_preview": _args_preview(body.arguments or {}),
                "args_json": json.dumps(
                    body.arguments or {},
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                "source": "agent_server",
            },
        )
        return time.monotonic()

    async def _track_complete(
        tracking_id: str,
        skill_name: str,
        body: _InvokeIn,
        grant_obj: Grant | None,
        started: float,
        *,
        ok: bool,
        result: Any = None,
        error: str | None = None,
        error_detail: dict[str, Any] | None = None,
    ) -> None:
        recorded_ok = ok and not _result_failed(result)
        result_payload = _result_preview(result) if ok else {"error": error}
        if error_detail is not None and not recorded_ok:
            result_payload["details"] = error_detail
        llm_usage = _extract_llm_usage(result) if ok else None
        if llm_usage is not None:
            llm_usage.setdefault("metadata", {})["transport_runtime"] = "a2a_pack"
            llm_usage["metadata"]["usage_authority"] = "litellm_response"
        await _post_tracking(
            body,
            {
                "type": "agent_invoke_complete" if recorded_ok else "agent_invoke_error",
                "grant_id": tracking_id,
                "to": type(agent).name,
                "agent": type(agent).name,
                "skill": skill_name,
                "ok": recorded_ok,
                "summary": _summary(result, error),
                "result": result_payload,
                **({"llm_usage": llm_usage} if llm_usage is not None else {}),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "scopes": _grant_scopes(grant_obj),
                "source": "agent_server",
            },
        )

    def _tracking_heartbeat_seconds() -> float:
        raw = os.environ.get("A2A_INVOKE_TRACKING_HEARTBEAT_SECONDS", "60")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 60.0
        return max(0.1, value)

    async def _track_heartbeat_loop(
        tracking_id: str,
        skill_name: str,
        body: _InvokeIn,
        grant_obj: Grant | None,
        started: float,
    ) -> None:
        interval = _tracking_heartbeat_seconds()
        while True:
            await asyncio.sleep(interval)
            await _post_tracking(
                body,
                {
                    "type": "agent_invoke_heartbeat",
                    "grant_id": tracking_id,
                    "to": type(agent).name,
                    "agent": type(agent).name,
                    "skill": skill_name,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "scopes": _grant_scopes(grant_obj),
                    "source": "agent_server",
                },
            )

    async def _cancel_tracking_heartbeat(task: asyncio.Task[None]) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _tracking_event_type(event: AgentEvent) -> str:
        if event.kind == "progress":
            return "agent_progress"
        if event.kind == "artifact":
            return "agent_artifact"
        if event.kind == "error":
            return "agent_error"
        return "agent_event"

    def _tracking_event_summary(event: AgentEvent) -> str:
        message = event.payload.get("message")
        if isinstance(message, str) and message:
            return message
        text = event.payload.get("text")
        if isinstance(text, str) and text:
            return text
        name = event.payload.get("name")
        if isinstance(name, str) and name:
            return name
        return event.kind

    async def _track_agent_event(
        tracking_id: str,
        skill_name: str,
        body: _InvokeIn,
        grant_obj: Grant | None,
        event: AgentEvent,
    ) -> None:
        await _post_tracking(
            body,
            {
                "type": _tracking_event_type(event),
                "grant_id": tracking_id,
                "to": type(agent).name,
                "agent": type(agent).name,
                "skill": skill_name,
                "kind": event.kind,
                "summary": _tracking_event_summary(event),
                "payload": event.payload,
                "scopes": _grant_scopes(grant_obj),
                "source": "agent_server",
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        ok = await agent.health()
        return {"ok": ok, "agent": type(agent).name, "version": type(agent).version}

    @app.get("/.well-known/agent-card")
    async def agent_card(request: Request) -> dict[str, Any]:
        payload = agent.card().model_dump(mode="json")
        if _uses_frontend_agent_api_prefix(request):
            _rewrite_mcp_paths_for_prefix(payload, FRONTEND_AGENT_API_PREFIX)
        if packed_frontend is not None:
            payload["ui"] = frontend_ui_metadata(packed_frontend, request)
            capabilities = payload.setdefault("capabilities", {})
            if isinstance(capabilities, dict):
                capabilities["ui"] = payload["ui"]
        return payload

    @app.get("/.well-known/agent-card.json")
    async def a2a_agent_card(request: Request) -> dict[str, Any]:
        return _a2a_protocol_card(agent, request)

    @app.get("/.well-known/a2a-skills.json")
    async def a2a_skills(request: Request) -> dict[str, Any]:
        return skills_payload(agent, request)

    @app.get("/.well-known/openapi.json")
    async def openapi_spec(request: Request) -> dict[str, Any]:
        return agent_openapi_spec(
            agent,
            base_url=str(request.base_url).rstrip("/"),
            require_bearer_auth=api_key is not None,
        )

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource(request: Request) -> dict[str, Any]:
        """RFC 9728 Protected Resource Metadata — the anchor ChatGPT/Claude
        fetch (via the /mcp 401 WWW-Authenticate challenge) to discover the
        Keycloak authorization server."""
        base = _request_base_url(request)
        return _oauth.protected_resource_metadata(_oauth.resource_id(base))

    if packed_frontend is not None and packed_frontend.mount == "/":
        app.add_api_route(
            f"{FRONTEND_AGENT_API_PREFIX}/.well-known/agent-card",
            agent_card,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            f"{FRONTEND_AGENT_API_PREFIX}/.well-known/agent-card.json",
            a2a_agent_card,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            f"{FRONTEND_AGENT_API_PREFIX}/.well-known/a2a-skills.json",
            a2a_skills,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            f"{FRONTEND_AGENT_API_PREFIX}/.well-known/openapi.json",
            openapi_spec,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            f"{FRONTEND_AGENT_API_PREFIX}/.well-known/oauth-protected-resource",
            oauth_protected_resource,
            methods=["GET"],
            include_in_schema=False,
        )

    @app.get("/auth/callback")
    async def auth_callback(request: Request) -> Response:
        """Redeem a dashboard hand-off code for this origin's own session.

        The platform session cookie is host-locked to the dashboard and never
        reaches an agent. Instead the dashboard sends the browser here with a
        single-use code, which we exchange server-side for a token scoped to
        this agent alone, then store in our own host-only cookie.
        """
        code = str(request.query_params.get("code") or "").strip()
        next_path = _safe_next_path(request.query_params.get("next"))
        if not code:
            return JSONResponse({"detail": "missing code"}, status_code=400)

        cp_url = (
            os.environ.get("A2A_CP_URL")
            or "http://control-plane.control-plane.svc.cluster.local"
        ).rstrip("/")
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{cp_url}/v1/auth/agent-session/exchange",
                    json={"code": code, "audience": type(agent).name},
                )
        except httpx.HTTPError:
            return JSONResponse(
                {"detail": "agent session exchange is unreachable"}, status_code=503
            )
        if resp.status_code != 200:
            # 410 == code already used or expired; make the browser retry
            # cleanly from the dashboard rather than loop on a dead code.
            return JSONResponse(
                {"detail": "agent session exchange failed"},
                status_code=401 if resp.status_code in {403, 410} else 502,
            )
        try:
            data = resp.json()
            token = str(data["token"])
            expires_at = int(data["expires_at"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "malformed exchange response"}, status_code=502)

        redirect = RedirectResponse(next_path, status_code=303)
        max_age = max(1, expires_at - int(time.time()))
        redirect.set_cookie(
            os.environ.get("A2A_SESSION_COOKIE_NAME") or "a2a_session",
            token,
            max_age=max_age,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        redirect.headers["Cache-Control"] = "no-store"
        redirect.headers["Referrer-Policy"] = "no-referrer"
        return redirect

    @app.get("/auth/session")
    async def auth_session(request: Request) -> dict[str, Any]:
        local_dev = os.environ.get("A2A_LOCAL_DEV") == "1"
        platform_user = await _platform_session(request)
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

    @app.post("/")
    async def jsonrpc_root(request: Request) -> Any:
        request_id: str | int | None = None
        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return _jsonrpc_error_response(None, -32700, "Parse error")

        if not isinstance(payload, dict):
            return _jsonrpc_error_response(None, -32600, "Invalid Request")

        request_id = payload.get("id")
        if not _valid_jsonrpc_id(request_id):
            return _jsonrpc_error_response(None, -32600, "Invalid Request")
        if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return _jsonrpc_error_response(request_id, -32600, "Invalid Request")

        params = payload.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _jsonrpc_error_response(request_id, -32602, "Invalid params")

        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _jsonrpc_error_response(
                request_id,
                service_param_error.code,
                service_param_error.message,
                service_param_error.data,
            )

        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error

        if _a2a_is_streaming_method(payload["method"]):
            if not _a2a_streaming_enabled():
                exc = _A2AJsonRpcError(-32004, "This operation is not supported")
                return _jsonrpc_error_response(request_id, exc.code, exc.message, exc.data)
            preflight_error = _a2a_stream_preflight_error(a2a_tasks, payload["method"], params)
            if preflight_error is not None:
                return _a2a_jsonrpc_error_stream_response(request_id, preflight_error)
            return _a2a_jsonrpc_stream_response(
                agent=agent,
                request=request,
                tasks=a2a_tasks,
                push_configs=a2a_push_configs,
                method=payload["method"],
                params=params,
                request_id=request_id,
            )

        try:
            result = _handle_a2a_jsonrpc(
                agent=agent,
                request=request,
                tasks=a2a_tasks,
                push_configs=a2a_push_configs,
                method=payload["method"],
                params=params,
            )
        except _A2AJsonRpcError as exc:
            return _jsonrpc_error_response(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            _log.exception("A2A JSON-RPC method %r failed", payload.get("method"))
            return _jsonrpc_error_response(
                request_id,
                -32603,
                f"Internal error: {type(exc).__name__}",
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "result": result, "id": request_id},
            headers={"A2A-Version": A2A_PROTOCOL_VERSION},
        )

    @app.post("/message:send")
    async def a2a_rest_send_message(request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise _A2AJsonRpcError(-32602, "Invalid params")
            result = _a2a_send_message(
                a2a_tasks,
                a2a_push_configs,
                {
                    "message": _normalize_a2a_message(payload.get("message")),
                    "configuration": payload.get("configuration") or {},
                },
            )
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except json.JSONDecodeError:
            return _a2a_rest_error_response(_A2AJsonRpcError(-32700, "Parse error"))
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.post("/message:stream")
    async def a2a_rest_stream_message(request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        if not _a2a_streaming_enabled():
            return _a2a_rest_error_response(_A2AJsonRpcError(-32004, "This operation is not supported"))
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise _A2AJsonRpcError(-32602, "Invalid params")
            params = {
                "message": _normalize_a2a_message(payload.get("message")),
                "configuration": payload.get("configuration") or {},
            }
        except json.JSONDecodeError:
            return _a2a_rest_error_response(_A2AJsonRpcError(-32700, "Parse error"))
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)
        return _a2a_rest_stream_response(a2a_tasks, a2a_push_configs, params)

    @app.get("/tasks/{task_id}")
    async def a2a_rest_get_task(task_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            result = _a2a_get_task(a2a_tasks, {"id": task_id, **dict(request.query_params)})
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.get("/tasks")
    async def a2a_rest_list_tasks(request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            params = _a2a_rest_list_params(dict(request.query_params))
            result = _a2a_list_tasks(a2a_tasks, params)
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.post("/tasks/{task_id}:cancel")
    async def a2a_rest_cancel_task(task_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            result = _a2a_cancel_task(a2a_tasks, {"id": task_id})
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.post("/tasks/{task_id}:subscribe")
    async def a2a_rest_subscribe_task(task_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        if not _a2a_streaming_enabled():
            return _a2a_rest_error_response(_A2AJsonRpcError(-32004, "This operation is not supported"))
        preflight_error = _a2a_stream_preflight_error(a2a_tasks, "SubscribeToTask", {"id": task_id})
        if preflight_error is not None:
            return _a2a_rest_error_response(preflight_error)
        return _a2a_rest_subscribe_response(a2a_tasks, task_id)

    @app.get("/extendedAgentCard")
    async def a2a_rest_extended_agent_card(request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            result = _a2a_get_extended_agent_card(agent, request)
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.post("/tasks/{task_id}/pushNotificationConfigs")
    async def a2a_rest_create_push_config(task_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise _A2AJsonRpcError(-32602, "Invalid params")
            payload = dict(payload)
            payload.setdefault("taskId", task_id)
            result = _a2a_create_push_config(a2a_tasks, a2a_push_configs, payload)
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except json.JSONDecodeError:
            return _a2a_rest_error_response(_A2AJsonRpcError(-32700, "Parse error"))
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.get("/tasks/{task_id}/pushNotificationConfigs")
    async def a2a_rest_list_push_configs(task_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            result = _a2a_list_push_configs(a2a_tasks, a2a_push_configs, {"task_id": task_id})
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.get("/tasks/{task_id}/pushNotificationConfigs/{config_id}")
    async def a2a_rest_get_push_config(task_id: str, config_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            result = _a2a_get_push_config(
                a2a_tasks,
                a2a_push_configs,
                {"task_id": task_id, "id": config_id},
            )
            return JSONResponse(result, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    @app.delete("/tasks/{task_id}/pushNotificationConfigs/{config_id}")
    async def a2a_rest_delete_push_config(task_id: str, config_id: str, request: Request) -> Any:
        service_param_error = _a2a_service_parameter_error(request)
        if service_param_error is not None:
            return _a2a_rest_error_response(service_param_error)
        auth_error = _a2a_request_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            _a2a_delete_push_config(
                a2a_tasks,
                a2a_push_configs,
                {"task_id": task_id, "id": config_id},
            )
            return JSONResponse({}, headers={"A2A-Version": A2A_PROTOCOL_VERSION})
        except _A2AJsonRpcError as exc:
            return _a2a_rest_error_response(exc)

    async def _prepare_invoke(
        body: _InvokeIn,
        token: str | None,
        skill_name: str,
        *,
        authorization: str | None,
        request_headers: dict[str, str],
    ) -> tuple[Grant | None, LocalRunContext[Any]]:
        granted_workspace = None
        grant_obj: Grant | None = None
        if body.grant is not None:
            try:
                grant_obj = verify_grant(body.grant)
            except GrantInvalid as exc:
                raise HTTPException(403, f"invalid grant: {exc}") from exc
            _assert_grant_audience(grant_obj, agent)
            granted_workspace = _grant_to_workspace(grant_obj, agent, body.grant)
        consumer_config, consumer_secrets = await _consumer_setup_for_invoke(
            body,
            request_headers,
        )
        resolver_used, auth_principal = await _build_auth(
            token,
            bearer_token=_extract_bearer(authorization),
            headers=request_headers,
        )
        account_access = getattr(type(agent), "account_access", None)
        if bool(getattr(account_access, "required", False)):
            try:
                await PlatformUserAuthResolver().resolve(
                    _trusted_cp_jwt(body, request_headers)
                    or _extract_bearer(authorization),
                    headers=request_headers,
                    agent=agent,
                )
            except AuthError as exc:
                authorize_url = os.environ.get(
                    "A2A_AGENT_SESSION_AUTHORIZE_URL",
                    os.environ.get("A2A_LOGIN_URL", ""),
                ).strip()
                detail: dict[str, Any] = {
                    "error": "account_required",
                    "message": "Sign in to A2A Cloud to use this agent.",
                    "agent": type(agent).name,
                }
                if authorize_url:
                    detail["authorize_url"] = authorize_url
                raise HTTPException(401, detail) from exc
        caller_id = (
            resolver_used.principal_id(auth_principal)
            if resolver_used is not None
            else ""
        )
        trusted_cp_jwt = _trusted_cp_jwt(body, request_headers)
        discovery = ControlPlaneDiscovery(
            _control_plane_url(body),
            token=_control_plane_token(body, authorization, request_headers),
        )
        ctx: LocalRunContext[Any] = LocalRunContext(
            auth=auth_principal,
            task_id=f"http-{skill_name}",
            secrets=_env_secrets(agent),
            workspace=granted_workspace or _local_dev_workspace(agent),
            sandbox=_sandbox_client(grant_obj, body.grant),
            a2a=HttpA2AClient(discovery=discovery),
            discover=discovery,
            consumer_config=consumer_config,
            consumer_secrets=consumer_secrets,
            caller=caller_id,
            grant_ids=(grant_obj.grant_id,) if grant_obj is not None else (),
            random_seed=secrets.token_hex(16),
            composition_budget=body.composition,
        )
        object.__setattr__(
            ctx,
            "_composition_budget",
            ensure_composition_budget(
                getattr(ctx, "_composition_budget", body.composition),
                current_agent=type(agent).name,
                llm_budget_usd=(
                    float(grant_obj.llm_max_budget_usd)
                    if grant_obj is not None
                    and grant_obj.llm_max_budget_usd is not None
                    else None
                ),
            ),
        )
        if grant_obj is not None:
            _bind_minio_artifacts(ctx, grant_obj)
        # JWT forwarding is gated by the agent's own declaration. Agents
        # that didn't opt in via ``wants_cp_jwt`` never see the token.
        if trusted_cp_jwt is not None and type(agent).wants_cp_jwt:
            object.__setattr__(ctx, "_cp_jwt", trusted_cp_jwt)
            object.__setattr__(
                ctx,
                "_cp_url",
                _control_plane_url(body),
            )

        if (
            grant_obj is not None
            and body.grant
            and grant_obj.llm_models
            and _llm_accepts_platform_creds(type(agent).llm_provisioning)
        ):
            _set_ctx_llm_creds(
                ctx,
                _platform_llm_creds_from_grant(grant_obj, body.grant),
                source="platform_grant",
            )

        # Inline credentials are accepted only when the Card explicitly opts
        # into a caller-provided mode. Platform-only agents use a signed grant
        # or fetch credentials from the configured control plane.
        if body.llm_creds is not None:
            provisioning = type(agent).llm_provisioning
            wants_caller = _llm_accepts_caller_creds(provisioning)
            if wants_caller:
                _set_ctx_llm_creds(ctx, body.llm_creds, source="caller")
        grant_obj = await _attach_platform_llm_grant(
            ctx,
            grant_obj,
            body=body,
            authorization=authorization,
            request_headers=request_headers,
        )
        return grant_obj, ctx

    @app.post("/invoke/{skill_name}")
    async def invoke(
        skill_name: str,
        body: _InvokeIn,
        request: Request,
        authorization: str | None = Header(default=None),
        accept: str | None = Header(default=None),
    ) -> Any:
        token = _invoke_token(authorization)
        # Stream when caller asks for SSE — control-plane sets this so the
        # dashboard can show progress events live.
        if accept and "text/event-stream" in accept.lower():
            return await _invoke_sse(skill_name, body, token, authorization, request)
        grant_obj, ctx = await _prepare_invoke(
            body,
            token,
            skill_name,
            authorization=authorization,
            request_headers=dict(request.headers),
        )
        tracking_id = _tracking_id(grant_obj)
        started = await _track_started(tracking_id, skill_name, body, grant_obj)

        async def on_event(ev: AgentEvent) -> None:
            await _track_agent_event(tracking_id, skill_name, body, grant_obj, ev)

        ctx._on_event = on_event  # noqa: SLF001
        heartbeat_task = asyncio.create_task(
            _track_heartbeat_loop(tracking_id, skill_name, body, grant_obj, started)
        )
        try:
            try:
                result = await agent.invoke_json(skill_name, ctx, body.arguments)
            except SkillNotFound:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=f"unknown skill: {skill_name}",
                )
                raise HTTPException(404, f"unknown skill: {skill_name}")
            except SkillInputError as exc:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=str(exc),
                )
                raise HTTPException(400, str(exc))
            except MissingScopes as exc:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=str(exc),
                )
                raise HTTPException(403, str(exc))
            except asyncio.CancelledError:
                error_message = "CancelledError: invoke request canceled before skill completed"
                error_detail = {
                    "type": "CancelledError",
                    "message": error_message,
                    "operation": "invoke",
                    "reason": "request_canceled",
                }
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=error_message, error_detail=error_detail,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                _log.exception("skill %r raised on /invoke (json path)", skill_name)
                error_detail = _exception_payload(exc)
                error_message = _exception_message(exc)
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=error_message, error_detail=error_detail,
                )
                raise HTTPException(
                    500,
                    f"skill {skill_name!r} raised {error_message}",
                ) from exc
            await _track_complete(
                tracking_id, skill_name, body, grant_obj, started,
                ok=True, result=result,
            )
            return {
                "result": result,
                "events": [
                    {"kind": e.kind, "payload": e.payload} for e in ctx.events
                ],
                "artifacts": [
                    {"name": name, "size_bytes": len(data)}
                    for name, data in (ctx.artifacts or {}).items()
                ],
                "grant_id": grant_obj.grant_id if grant_obj else None,
            }
        finally:
            await _cancel_tracking_heartbeat(heartbeat_task)

    async def _invoke_sse(
        skill_name: str,
        body: _InvokeIn,
        token: str | None,
        authorization: str | None,
        request: Request,
    ) -> StreamingResponse:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def on_event(ev: AgentEvent) -> None:
            await _track_agent_event(tracking_id, skill_name, body, grant_obj, ev)
            await queue.put({"type": "event", "kind": ev.kind, "payload": ev.payload})

        # Build context with the live callback wired in.
        try:
            grant_obj, base_ctx = await _prepare_invoke(
                body,
                token,
                skill_name,
                authorization=authorization,
                request_headers=dict(request.headers),
            )
        except HTTPException as exc:
            status_code = exc.status_code
            detail = exc.detail

            async def err() -> AsyncIterator[bytes]:
                yield b"data: " + json.dumps(
                    {"type": "error", "status": status_code, "detail": detail}
                ).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"
            return StreamingResponse(err(), media_type="text/event-stream")
        # Mutate the context to use our callback.
        base_ctx._on_event = on_event  # noqa: SLF001
        tracking_id = _tracking_id(grant_obj)
        started = await _track_started(tracking_id, skill_name, body, grant_obj)

        async def run_skill() -> None:
            heartbeat_task = asyncio.create_task(
                _track_heartbeat_loop(tracking_id, skill_name, body, grant_obj, started)
            )
            try:
                result = await agent.invoke_json(skill_name, base_ctx, body.arguments)
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=True, result=result,
                )
                await queue.put({"type": "result", "result": result,
                                  "grant_id": grant_obj.grant_id if grant_obj else None})
            except SkillNotFound:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=f"unknown skill: {skill_name}",
                )
                await queue.put({"type": "error", "status": 404,
                                  "detail": f"unknown skill: {skill_name}"})
            except SkillInputError as exc:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=str(exc),
                )
                await queue.put({"type": "error", "status": 400, "detail": str(exc)})
            except MissingScopes as exc:
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=str(exc),
                )
                await queue.put({"type": "error", "status": 403, "detail": str(exc)})
            except asyncio.CancelledError:
                error_message = "CancelledError: invoke stream closed before skill completed"
                error_detail = {
                    "type": "CancelledError",
                    "message": error_message,
                    "operation": "invoke",
                    "reason": "stream_closed",
                }
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=error_message, error_detail=error_detail,
                )
                raise
            except Exception as exc:  # noqa: BLE001
                _log.exception("skill %r raised on /invoke", skill_name)
                error_detail = _exception_payload(exc)
                error_message = _exception_message(exc)
                await _track_complete(
                    tracking_id, skill_name, body, grant_obj, started,
                    ok=False, error=error_message, error_detail=error_detail,
                )
                await queue.put({
                    "type": "error", "status": 500,
                    "detail": error_message,
                    "error": error_detail,
                })
            finally:
                await _cancel_tracking_heartbeat(heartbeat_task)
                await queue.put(None)

        async def gen() -> AsyncIterator[bytes]:
            task = asyncio.create_task(run_skill())
            try:
                while True:
                    ev = await queue_get_or_heartbeat(queue)
                    if ev is SSE_HEARTBEAT:
                        yield sse_comment()
                        continue
                    if ev is None:
                        break
                    yield b"data: " + json.dumps(ev).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def _endpoint_body_with_env_consumer_setup(body: _InvokeIn) -> _InvokeIn:
        consumer_config = dict(body.consumer_config or {})
        consumer_secrets = dict(body.consumer_secrets or {})
        for field in type(agent).consumer_setup.fields:
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

    def _make_raw_endpoint_handler(endpoint: AgentEndpoint) -> Any:
        async def raw_endpoint(request: Request) -> Any:
            body = _InvokeIn(
                arguments=await _raw_endpoint_arguments(endpoint, request),
            )
            body = _endpoint_body_with_env_consumer_setup(body)
            grant_obj, ctx = await _prepare_invoke(
                body,
                None,
                endpoint.skill,
                authorization=None,
                request_headers=dict(request.headers),
            )
            try:
                result = await agent.invoke_json(endpoint.skill, ctx, body.arguments)
            except SkillNotFound:
                raise HTTPException(404, f"unknown skill: {endpoint.skill}") from None
            except SkillInputError as exc:
                raise HTTPException(400, str(exc)) from exc
            except MissingScopes as exc:
                raise HTTPException(403, str(exc)) from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.exception("skill %r raised on raw endpoint", endpoint.skill)
                raise HTTPException(
                    500,
                    f"skill {endpoint.skill!r} raised {_exception_message(exc)}",
                ) from exc
            return _raw_endpoint_result(result)

        return raw_endpoint

    for endpoint in type(agent).runtime().endpoints:
        app.add_api_route(
            endpoint.path,
            _make_raw_endpoint_handler(endpoint),
            methods=list(endpoint.methods),
            include_in_schema=False,
            response_model=None,
        )

    @app.post("/answers/{question_id}")
    async def answer_question(
        question_id: str,
        body: _AnswerIn,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_key(authorization)
        ok = RunContext.answer(question_id, body.answer)
        if not ok:
            raise HTTPException(404, "no pending question with that id")
        return {"ok": True, "question_id": question_id}

    @app.post("/input-requests/{request_id}")
    async def submit_input_request(
        request_id: str,
        body: _InputResponseIn,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_key(authorization)
        ok = RunContext.submit_input(request_id, body.value)
        if not ok:
            raise HTTPException(404, "no pending input request with that id")
        return {"ok": True, "request_id": request_id}

    @app.post("/scope-grants/{request_id}")
    async def deliver_scope_grant(
        request_id: str,
        body: _ScopeGrantIn,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Platform delivers an approved superseding grant for ``request_id``."""
        _check_key(authorization)
        # verify_grant runs again inside resolve_scope_grant; we surface a
        # 403 here on bad signature so the platform sees the failure cleanly.
        try:
            grant_obj = verify_grant(body.grant)
        except GrantInvalid as exc:
            raise HTTPException(403, f"invalid grant: {exc}") from exc
        _assert_grant_audience(grant_obj, agent)
        ok = RunContext.resolve_scope_grant(request_id, body.grant)
        if not ok:
            raise HTTPException(404, "no pending scope request with that id")
        return {"ok": True, "request_id": request_id}

    @app.post("/scope-denials/{request_id}")
    async def deliver_scope_denial(
        request_id: str,
        body: _ScopeDenyIn,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Platform refused the scope request; deliver the reason."""
        _check_key(authorization)
        ok = RunContext.deny_scope(request_id, body.reason)
        if not ok:
            raise HTTPException(404, "no pending scope request with that id")
        return {"ok": True, "request_id": request_id}

    if packed_frontend is not None and packed_frontend.mount == "/":
        app.add_api_route(
            FRONTEND_AGENT_API_PREFIX,
            jsonrpc_root,
            methods=["POST"],
            include_in_schema=False,
        )
        for path, endpoint, methods in (
            ("/auth/session", auth_session, ["GET"]),
            ("/message:send", a2a_rest_send_message, ["POST"]),
            ("/message:stream", a2a_rest_stream_message, ["POST"]),
            ("/tasks", a2a_rest_list_tasks, ["GET"]),
            ("/tasks/{task_id}", a2a_rest_get_task, ["GET"]),
            ("/tasks/{task_id}:cancel", a2a_rest_cancel_task, ["POST"]),
            ("/tasks/{task_id}:subscribe", a2a_rest_subscribe_task, ["POST"]),
            ("/extendedAgentCard", a2a_rest_extended_agent_card, ["GET"]),
            (
                "/tasks/{task_id}/pushNotificationConfigs",
                a2a_rest_create_push_config,
                ["POST"],
            ),
            (
                "/tasks/{task_id}/pushNotificationConfigs",
                a2a_rest_list_push_configs,
                ["GET"],
            ),
            (
                "/tasks/{task_id}/pushNotificationConfigs/{config_id}",
                a2a_rest_get_push_config,
                ["GET"],
            ),
            (
                "/tasks/{task_id}/pushNotificationConfigs/{config_id}",
                a2a_rest_delete_push_config,
                ["DELETE"],
            ),
            ("/invoke/{skill_name}", invoke, ["POST"]),
            ("/answers/{question_id}", answer_question, ["POST"]),
            ("/input-requests/{request_id}", submit_input_request, ["POST"]),
            ("/scope-grants/{request_id}", deliver_scope_grant, ["POST"]),
            ("/scope-denials/{request_id}", deliver_scope_denial, ["POST"]),
        ):
            app.add_api_route(
                f"{FRONTEND_AGENT_API_PREFIX}{path}",
                endpoint,
                methods=methods,
                include_in_schema=False,
            )

    if packed_frontend is not None:
        mount_packed_frontend(
            app,
            agent,
            packed_frontend,
            require_session=_require_frontend_session,
        )

    return app


class _A2AJsonRpcError(Exception):
    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.metadata = metadata or {}
        self.data = data if data is not None else _a2a_error_details(code, self.metadata)


_A2A_TASK_STATES = frozenset(
    {
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_AUTH_REQUIRED",
    }
)
_A2A_TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_CANCELED",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
    }
)
_A2A_SUPPORTED_OUTPUT_MODES = frozenset(
    {
        "text",
        "text/plain",
        "application/json",
        "application/a2a+json",
        "file",
        "data",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
        "text/csv",
    }
)
_A2A_SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "application/json",
        "application/a2a+json",
        "application/octet-stream",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
        "text/csv",
    }
)
_A2A_SUPPORTED_PROTOCOL_VERSIONS = frozenset({A2A_PROTOCOL_VERSION})
_A2A_ERROR_REASONS = {
    -32001: "TASK_NOT_FOUND",
    -32002: "TASK_NOT_CANCELABLE",
    -32003: "PUSH_NOTIFICATION_NOT_SUPPORTED",
    -32004: "UNSUPPORTED_OPERATION",
    -32005: "CONTENT_TYPE_NOT_SUPPORTED",
    -32006: "INVALID_AGENT_RESPONSE",
    -32007: "EXTENDED_AGENT_CARD_NOT_CONFIGURED",
    -32008: "EXTENSION_SUPPORT_REQUIRED",
    -32009: "VERSION_NOT_SUPPORTED",
}
_A2A_REST_ERROR_STATUS = {
    -32001: (404, "NOT_FOUND"),
    -32002: (400, "FAILED_PRECONDITION"),
    -32003: (400, "FAILED_PRECONDITION"),
    -32004: (400, "FAILED_PRECONDITION"),
    -32005: (400, "INVALID_ARGUMENT"),
    -32006: (500, "INTERNAL"),
    -32007: (400, "FAILED_PRECONDITION"),
    -32008: (400, "FAILED_PRECONDITION"),
    -32009: (400, "FAILED_PRECONDITION"),
    -32601: (404, "NOT_FOUND"),
    -32602: (400, "INVALID_ARGUMENT"),
    -32700: (400, "INVALID_ARGUMENT"),
}


def _a2a_error_details(code: int, metadata: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    reason = _A2A_ERROR_REASONS.get(code)
    if reason is None:
        return None
    details: dict[str, Any] = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "a2a-protocol.org",
        "metadata": {
            "timestamp": _a2a_now(),
        },
    }
    if metadata:
        details["metadata"].update({str(key): str(value) for key, value in metadata.items()})
    return [details]


def _jsonrpc_error_response(
    request_id: str | int | float | None,
    code: int,
    message: str,
    data: Any = None,
) -> JSONResponse:
    return JSONResponse(
        _jsonrpc_error_payload(request_id, code, message, data),
        headers={"A2A-Version": A2A_PROTOCOL_VERSION},
    )


def _jsonrpc_error_payload(
    request_id: str | int | float | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "error": error, "id": request_id}


def _valid_jsonrpc_id(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int, float))


def _a2a_env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _a2a_push_notifications_enabled() -> bool:
    return _a2a_env_bool("A2A_ENABLE_PUSH_NOTIFICATIONS", True)


def _a2a_streaming_enabled() -> bool:
    return _a2a_env_bool("A2A_ENABLE_STREAMING", True)


def _a2a_extended_agent_card_enabled() -> bool:
    return _a2a_env_bool("A2A_ENABLE_EXTENDED_AGENT_CARD", True)


def _a2a_extended_agent_card_configured() -> bool:
    return _a2a_env_bool("A2A_EXTENDED_AGENT_CARD_CONFIGURED", True)


def _a2a_required_extensions() -> tuple[str, ...]:
    raw = os.environ.get("A2A_REQUIRED_EXTENSIONS", "")
    return tuple(
        extension.strip()
        for extension in raw.split(",")
        if extension.strip()
    )


def _a2a_declared_extensions(request: Request) -> set[str]:
    raw = request.headers.get("a2a-extensions", "")
    return {
        extension.strip()
        for extension in raw.split(",")
        if extension.strip()
    }


def _a2a_service_parameter_error(request: Request) -> _A2AJsonRpcError | None:
    requested_version = request.headers.get("a2a-version")
    if requested_version is not None:
        requested_version = requested_version.strip() or "0.3"
        if requested_version not in _A2A_SUPPORTED_PROTOCOL_VERSIONS:
            return _A2AJsonRpcError(
                -32009,
                f"A2A protocol version {requested_version} is not supported",
                metadata={
                    "requestedVersion": requested_version,
                    "supportedVersions": ",".join(sorted(_A2A_SUPPORTED_PROTOCOL_VERSIONS)),
                },
            )

    required_extensions = set(_a2a_required_extensions())
    if required_extensions:
        missing_extensions = sorted(required_extensions - _a2a_declared_extensions(request))
        if missing_extensions:
            return _A2AJsonRpcError(
                -32008,
                "Required A2A extension support was not declared",
                metadata={"missingExtensions": ",".join(missing_extensions)},
            )
    return None


def _a2a_get_extended_agent_card(agent: A2AAgent, request: Request) -> dict[str, Any]:
    if not _a2a_extended_agent_card_enabled():
        raise _A2AJsonRpcError(-32004, "This operation is not supported")
    if not _a2a_extended_agent_card_configured():
        raise _A2AJsonRpcError(-32007, "Extended agent card is not configured")
    return _a2a_protocol_card(agent, request, extended=True)


def _request_base_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if _uses_frontend_agent_api_prefix(request):
        return f"{base}{FRONTEND_AGENT_API_PREFIX}"
    return base


def _uses_frontend_agent_api_prefix(request: Request) -> bool:
    return request.url.path == FRONTEND_AGENT_API_PREFIX or request.url.path.startswith(
        f"{FRONTEND_AGENT_API_PREFIX}/"
    )


def _rewrite_mcp_paths_for_prefix(card: dict[str, Any], prefix: str) -> None:
    card["mcp_endpoint"] = f"{prefix}/mcp"
    card["connector_mcp_endpoint"] = f"{prefix}/connector-mcp"
    capabilities = card.get("capabilities")
    endpoint_sets = [card.get("mcp_endpoints")]
    if isinstance(capabilities, dict):
        endpoint_sets.append(capabilities.get("mcp"))
    for endpoints in endpoint_sets:
        if not isinstance(endpoints, dict):
            continue
        _rewrite_mcp_endpoint_set(endpoints, prefix)


def _rewrite_mcp_endpoint_set(endpoints: dict[str, Any], prefix: str) -> None:
    standard = endpoints.get("standard")
    if isinstance(standard, dict):
        standard["path"] = f"{prefix}/mcp"
    connector = endpoints.get("connector")
    if isinstance(connector, dict):
        connector["path"] = f"{prefix}/connector-mcp"


def _a2a_protocol_card(
    agent: A2AAgent,
    request: Request,
    *,
    extended: bool = False,
) -> dict[str, Any]:
    raw = agent.card().model_dump(mode="json")
    input_modes = _a2a_modes(raw.get("input_modes"), include_all=True)
    output_modes = _a2a_modes(raw.get("output_modes"), include_all=True)
    base_url = _request_base_url(request)
    skills = [
        {
            "id": str(skill.get("id") or skill.get("name") or "skill"),
            "name": str(skill.get("name") or skill.get("id") or "skill"),
            "description": str(skill.get("description") or ""),
            "tags": [str(tag) for tag in (skill.get("tags") or [])],
            "inputModes": input_modes,
            "outputModes": output_modes,
            "inputOutputModes": ["text", "file", "data"],
        }
        for skill in raw.get("skills", [])
        if isinstance(skill, dict)
    ]
    if not skills:
        skills = [
            {
                "id": "message",
                "name": "A2A message handling",
                "description": "Protocol-level message handling.",
                "tags": ["a2a", "protocol"],
                "inputModes": input_modes,
                "outputModes": output_modes,
                "inputOutputModes": ["text", "file", "data"],
            }
        ]

    _kc_issuer = _oauth.oauth_issuer()
    oauth2_scheme = {
        "oauth2SecurityScheme": {
            # Real self-hosted Keycloak realm (E1). Authorization-Code + PKCE is
            # the only interactive flow we support; implicit is disabled.
            "oauth2MetadataUrl": f"{_kc_issuer}/.well-known/openid-configuration",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": f"{_kc_issuer}/protocol/openid-connect/auth",
                    "tokenUrl": f"{_kc_issuer}/protocol/openid-connect/token",
                    "refreshUrl": f"{_kc_issuer}/protocol/openid-connect/token",
                    "scopes": {s: s for s in _oauth.oauth_scopes()},
                }
            },
        }
    }
    mutual_tls_scheme = {
        "mtlsSecurityScheme": {
            "description": "Mutual TLS client certificate authentication.",
        }
    }

    extensions = [
        {
            "uri": "https://a2a-protocol.org/extensions/protocol-fixture",
            "description": "Compatibility fixture extension metadata.",
            "required": False,
            "params": {"version": A2A_PROTOCOL_VERSION},
        }
    ]
    for extension_uri in _a2a_required_extensions():
        if extension_uri not in {extension["uri"] for extension in extensions}:
            extensions.append(
                {
                    "uri": extension_uri,
                    "description": "Required extension support for this agent.",
                    "required": True,
                    "params": {"version": A2A_PROTOCOL_VERSION},
                }
            )

    card: dict[str, Any] = {
        "name": raw.get("name") or type(agent).name,
        "description": raw.get("description") or type(agent).description,
        "version": raw.get("version") or type(agent).version,
        "capabilities": {
            "streaming": _a2a_streaming_enabled(),
            "pushNotifications": _a2a_push_notifications_enabled(),
            "extendedAgentCard": _a2a_extended_agent_card_enabled(),
            "extended_agent_card": _a2a_extended_agent_card_enabled(),
            "stateTransitionHistory": True,
            "extensions": extensions,
            "skills": skills,
        },
        "defaultInputModes": input_modes,
        "defaultOutputModes": output_modes,
        "skills": skills,
        "securitySchemes": {
            "bearer": {
                "httpAuthSecurityScheme": {
                    "scheme": "bearer",
                    "bearerFormat": "opaque",
                }
            },
            "oauth2": oauth2_scheme,
            "mutualTLS": mutual_tls_scheme,
        },
        "security": [{"bearer": []}],
        "authentication": [{"scheme": "bearer"}],
        "supportedInterfaces": [
            {
                "protocolBinding": "jsonrpc",
                "protocolVersion": A2A_PROTOCOL_VERSION,
                "url": base_url,
            },
            {
                "protocolBinding": "rest",
                "protocolVersion": A2A_PROTOCOL_VERSION,
                "url": base_url,
            }
        ],
        "additionalInterfaces": [
            {"transport": "HTTP+JSON", "url": base_url},
        ],
    }
    frontend = getattr(request.app.state, "a2a_frontend", None)
    if isinstance(frontend, PackedFrontend):
        ui = frontend_ui_metadata(frontend, request)
        card["ui"] = ui
        if isinstance(card.get("capabilities"), dict):
            card["capabilities"]["ui"] = ui
    if extended:
        card["metadata"] = {"card": "extended", "protocolVersion": A2A_PROTOCOL_VERSION}
    return card


def _a2a_modes(raw_modes: Any, *, include_all: bool = False) -> list[str]:
    modes: list[str] = ["text", "text/plain"]
    if include_all:
        modes.extend(["file", "data", "application/json", "application/a2a+json", "image/png"])
    if isinstance(raw_modes, list):
        for mode in raw_modes:
            if isinstance(mode, str) and mode.strip() and mode not in modes:
                modes.append(mode)
    return modes


def _handle_a2a_jsonrpc(
    *,
    agent: A2AAgent,
    request: Request,
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    method: str,
    params: dict[str, Any],
) -> Any:
    method_key = method.strip()
    method_lower = method_key.lower()

    if method_key in {"SendMessage", "message/send"}:
        return _a2a_send_message(tasks, push_configs, params)
    if method_key in {"GetTask", "tasks/get"}:
        return _a2a_get_task(tasks, params)
    if method_key in {"CancelTask", "tasks/cancel"}:
        return _a2a_cancel_task(tasks, params)
    if method_key in {"ListTasks", "tasks/list"}:
        return _a2a_list_tasks(tasks, params)
    if method_key in {"GetExtendedAgentCard", "agent/getAuthenticatedExtendedCard"}:
        return _a2a_get_extended_agent_card(agent, request)
    if method_key in {
        "CreateTaskPushNotificationConfig",
        "tasks/pushNotificationConfig/set",
        "tasks/pushNotificationConfig/create",
    }:
        return _a2a_create_push_config(tasks, push_configs, params)
    if method_key in {"GetTaskPushNotificationConfig", "tasks/pushNotificationConfig/get"}:
        return _a2a_get_push_config(tasks, push_configs, params)
    if method_key in {"ListTaskPushNotificationConfigs", "tasks/pushNotificationConfig/list"}:
        return _a2a_list_push_configs(tasks, push_configs, params)
    if method_key in {"DeleteTaskPushNotificationConfig", "tasks/pushNotificationConfig/delete"}:
        return _a2a_delete_push_config(tasks, push_configs, params)
    if "pushnotification" in method_lower or "pushnotificationconfig" in method_lower:
        raise _A2AJsonRpcError(-32601, "Method not found")
    if _a2a_is_streaming_method(method_key):
        raise _A2AJsonRpcError(-32004, "This operation requires an SSE response")
    raise _A2AJsonRpcError(-32601, "Method not found")


def _a2a_auth_error_response(
    request: Request,
    *,
    allow_noauth_without_api_key: bool = False,
) -> JSONResponse | None:
    token = os.environ.get("A2A_API_KEY")
    if not token:
        if allow_noauth_without_api_key:
            return None
        return JSONResponse(
            {"error": {"code": -32603, "message": "server bearer auth is not configured"}},
            status_code=500,
            headers={"A2A-Version": A2A_PROTOCOL_VERSION},
        )
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.lower().startswith("bearer "):
        return _a2a_unauthorized_response("missing bearer credentials")
    if authorization.split(None, 1)[1].strip() != token:
        return _a2a_unauthorized_response("invalid bearer credentials")
    return None


def _a2a_unauthorized_response(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": -32603, "message": message}},
        status_code=401,
        headers={
            "A2A-Version": A2A_PROTOCOL_VERSION,
            "WWW-Authenticate": 'Bearer realm="a2a"',
        },
    )


def _a2a_rest_error_response(exc: _A2AJsonRpcError) -> JSONResponse:
    status, status_name = _A2A_REST_ERROR_STATUS.get(exc.code, (400, "UNKNOWN"))
    error: dict[str, Any] = {
        "code": status,
        "status": status_name,
        "message": exc.message,
    }
    if exc.data:
        error["details"] = exc.data
    return JSONResponse(
        {
            "error": error,
            "message": exc.message,
            "code": exc.code,
        },
        status_code=status,
        headers={"A2A-Version": A2A_PROTOCOL_VERSION},
    )


def _a2a_is_streaming_method(method: str) -> bool:
    return method.strip() in {
        "SendStreamingMessage",
        "message/stream",
        "SubscribeToTask",
        "tasks/resubscribe",
        "tasks/subscribe",
    }


def _a2a_jsonrpc_stream_response(
    *,
    agent: A2AAgent,
    request: Request,
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    method: str,
    params: dict[str, Any],
    request_id: str | int | float | None,
) -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        try:
            if method in {"SendStreamingMessage", "message/stream"}:
                events = _a2a_stream_message_events(tasks, push_configs, params)
            elif method in {"SubscribeToTask", "tasks/resubscribe", "tasks/subscribe"}:
                events = _a2a_subscribe_events(tasks, params)
            else:
                raise _A2AJsonRpcError(-32601, "Method not found")
            for result in events:
                yield _sse_data({"jsonrpc": "2.0", "result": result, "id": request_id})
                await asyncio.sleep(0)
        except _A2AJsonRpcError as exc:
            yield _sse_data(_jsonrpc_error_payload(request_id, exc.code, exc.message, exc.data))
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "A2A-Version": A2A_PROTOCOL_VERSION,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _a2a_jsonrpc_error_stream_response(
    request_id: str | int | float | None,
    exc: _A2AJsonRpcError,
) -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        yield _sse_data(_jsonrpc_error_payload(request_id, exc.code, exc.message, exc.data))
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "A2A-Version": A2A_PROTOCOL_VERSION,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _a2a_stream_preflight_error(
    tasks: dict[str, dict[str, Any]],
    method: str,
    params: dict[str, Any],
) -> _A2AJsonRpcError | None:
    if method not in {"SubscribeToTask", "tasks/resubscribe", "tasks/subscribe"}:
        return None
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        return _A2AJsonRpcError(-32602, "Invalid params")
    task = tasks.get(task_id)
    if task is None:
        return _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    if task.get("status", {}).get("state") in _A2A_TERMINAL_TASK_STATES:
        return _A2AJsonRpcError(
            -32004,
            "This operation is not supported for terminal tasks",
            metadata={"taskId": task_id},
        )
    return None


def _a2a_rest_stream_response(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        try:
            for event in _a2a_stream_message_events(tasks, push_configs, params, wrap_rest=True):
                yield _sse_data(event)
                await asyncio.sleep(0)
        except _A2AJsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data:
                error["details"] = exc.data
            yield _sse_data({"error": error})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "A2A-Version": A2A_PROTOCOL_VERSION,
            "Cache-Control": "no-cache",
            "Connection": "close",
            "X-Accel-Buffering": "no",
        },
    )


def _a2a_rest_subscribe_response(
    tasks: dict[str, dict[str, Any]],
    task_id: str,
) -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        try:
            for event in _a2a_subscribe_events(tasks, {"id": task_id}, wrap_rest=True):
                yield _sse_data(event)
                await asyncio.sleep(0)
        except _A2AJsonRpcError as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data:
                error["details"] = exc.data
            yield _sse_data({"error": error})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "A2A-Version": A2A_PROTOCOL_VERSION,
            "Cache-Control": "no-cache",
            "Connection": "close",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_data(value: Any) -> bytes:
    return b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n"


def _a2a_send_message(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any]:
    message = params.get("message")
    if message is None and {"messageId", "role", "parts"} & set(params):
        message = params
    if (
        isinstance(message, dict)
        and isinstance(message.get("message"), dict)
        and not ({"messageId", "role", "parts"} & set(message))
    ):
        message = message["message"]
    _validate_a2a_message(message)
    _validate_a2a_send_configuration(params.get("configuration"))

    assert isinstance(message, dict)
    existing_task_id = message.get("taskId")
    if existing_task_id is not None:
        if not isinstance(existing_task_id, str) or not existing_task_id.strip():
            raise _A2AJsonRpcError(-32602, "Invalid params")
        if existing_task_id not in tasks:
            raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": existing_task_id})
        task = tasks[existing_task_id]
        if task.get("status", {}).get("state") in _A2A_TERMINAL_TASK_STATES:
            raise _A2AJsonRpcError(
                -32004,
                "This operation is not supported for terminal tasks",
                metadata={"taskId": existing_task_id},
            )
        task["history"].append(copy.deepcopy(message))
        task["status"] = {
            "state": "TASK_STATE_WORKING",
            "timestamp": _a2a_now(),
        }
        _store_push_config_from_send_config(task["id"], push_configs, params.get("configuration"))
        response = {"task": _copy_task_for_response(task)}
        _validate_a2a_send_message_response(response)
        return response

    task_id = f"task-{uuid4().hex}"
    context_id = message.get("contextId")
    if not isinstance(context_id, str) or not context_id.strip():
        context_id = f"context-{uuid4().hex}"
    auth_required = _a2a_requires_secondary_auth(message)
    status = {
        "state": "TASK_STATE_AUTH_REQUIRED" if auth_required else "TASK_STATE_WORKING",
        "timestamp": _a2a_now(),
    }
    if auth_required:
        status["message"] = {
            "messageId": f"auth-required-{uuid4().hex}",
            "role": "ROLE_AGENT",
            "parts": [{"text": "Additional authorization is required."}],
        }
    task = {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": status,
        "history": [copy.deepcopy(message)],
        "artifacts": [],
    }
    if auth_required:
        task["authChallenge"] = {
            "schemes": [{"scheme": "oauth2"}],
            "reason": "external account access",
        }
    tasks[task_id] = task
    _store_push_config_from_send_config(task_id, push_configs, params.get("configuration"))
    response = {"task": _copy_task_for_response(task)}
    _validate_a2a_send_message_response(response)
    return response


def _validate_a2a_send_message_response(response: Any) -> None:
    if not isinstance(response, dict):
        raise _A2AJsonRpcError(-32006, "Invalid agent response")
    has_task = isinstance(response.get("task"), dict)
    has_message = isinstance(response.get("message"), dict)
    if has_task == has_message:
        raise _A2AJsonRpcError(-32006, "Invalid agent response")
    if has_task:
        task = response["task"]
        status = task.get("status")
        if (
            not isinstance(task.get("id"), str)
            or not isinstance(status, dict)
            or status.get("state") not in _A2A_TASK_STATES
        ):
            raise _A2AJsonRpcError(-32006, "Invalid agent response")
    if has_message:
        message = response["message"]
        if (
            not isinstance(message.get("messageId"), str)
            or message.get("role") not in {"ROLE_AGENT", "ROLE_USER"}
            or not isinstance(message.get("parts"), list)
        ):
            raise _A2AJsonRpcError(-32006, "Invalid agent response")


def _a2a_stream_message_events(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
    *,
    wrap_rest: bool = False,
) -> list[dict[str, Any]]:
    result = _a2a_send_message(tasks, push_configs, params)
    task = result["task"] if "task" in result else result
    status_update = {
        "kind": "status-update",
        "taskId": task["id"],
        "contextId": task["contextId"],
        "status": task["status"],
        "final": False,
    }
    if wrap_rest:
        return [{"task": task}, {"status_update": status_update}]
    return [task, status_update]


def _a2a_subscribe_events(
    tasks: dict[str, dict[str, Any]],
    params: dict[str, Any],
    *,
    wrap_rest: bool = False,
) -> list[dict[str, Any]]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    task = tasks.get(task_id)
    if task is None:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    if task.get("status", {}).get("state") in _A2A_TERMINAL_TASK_STATES:
        raise _A2AJsonRpcError(
            -32004,
            "This operation is not supported for terminal tasks",
            metadata={"taskId": task_id},
        )
    out = _copy_task_for_response(task)
    if wrap_rest:
        return [{"task": out}]
    return [out]


def _a2a_get_task(
    tasks: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    task = tasks.get(task_id)
    if task is None:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    history_length = params.get("historyLength")
    if history_length is None:
        history_length = params.get("history_length")
    if isinstance(history_length, str) and history_length.isdecimal():
        history_length = int(history_length)
    if history_length is not None and (
        isinstance(history_length, bool)
        or not isinstance(history_length, int)
        or history_length < 0
    ):
        raise _A2AJsonRpcError(-32602, "Invalid params")
    return _copy_task_for_response(task, history_length=history_length)


def _a2a_cancel_task(
    tasks: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    task = tasks.get(task_id)
    if task is None:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    if task.get("status", {}).get("state") in _A2A_TERMINAL_TASK_STATES:
        raise _A2AJsonRpcError(-32002, "Task cannot be canceled", metadata={"taskId": task_id})
    task["status"] = {
        "state": "TASK_STATE_CANCELED",
        "timestamp": _a2a_now(),
    }
    return _copy_task_for_response(task)


def _a2a_list_tasks(
    tasks: dict[str, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    page_size = params.get("pageSize", 50)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise _A2AJsonRpcError(-32602, "Invalid params")

    history_length = params.get("historyLength", 0)
    if (
        isinstance(history_length, bool)
        or not isinstance(history_length, int)
        or history_length < 0
    ):
        raise _A2AJsonRpcError(-32602, "Invalid params")

    offset = 0
    page_token = params.get("pageToken")
    if page_token is not None:
        if not isinstance(page_token, str) or not page_token.startswith("offset:"):
            raise _A2AJsonRpcError(-32602, "Invalid params")
        try:
            offset = int(page_token.removeprefix("offset:"))
        except ValueError as exc:
            raise _A2AJsonRpcError(-32602, "Invalid params") from exc
        if offset < 0:
            raise _A2AJsonRpcError(-32602, "Invalid params")

    status = params.get("status")
    if status is not None and status not in _A2A_TASK_STATES:
        raise _A2AJsonRpcError(-32602, "Invalid params")

    context_id = params.get("contextId")
    if context_id is not None and not isinstance(context_id, str):
        raise _A2AJsonRpcError(-32602, "Invalid params")

    status_timestamp_after = params.get("statusTimestampAfter")
    parsed_after = None
    if status_timestamp_after is not None:
        parsed_after = _parse_a2a_timestamp(status_timestamp_after)

    filtered = list(tasks.values())
    if context_id is not None:
        filtered = [task for task in filtered if task.get("contextId") == context_id]
    if status is not None:
        filtered = [
            task
            for task in filtered
            if task.get("status", {}).get("state") == status
        ]
    if parsed_after is not None:
        filtered = [
            task
            for task in filtered
            if _task_timestamp(task) >= parsed_after
        ]

    filtered.sort(key=_task_timestamp, reverse=True)
    total_size = len(filtered)
    page = filtered[offset : offset + page_size]
    next_offset = offset + len(page)
    next_page_token = f"offset:{next_offset}" if next_offset < total_size else ""
    include_artifacts = bool(params.get("includeArtifacts", False))

    return {
        "tasks": [
            _copy_task_for_response(
                task,
                history_length=history_length,
                include_artifacts=include_artifacts,
            )
            for task in page
        ],
        "totalSize": total_size,
        "pageSize": len(page),
        "nextPageToken": next_page_token,
    }


def _a2a_create_push_config(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any]:
    if not _a2a_push_notifications_enabled():
        raise _A2AJsonRpcError(-32003, "Push notifications are not supported")
    config = _normalize_push_config(params)
    task_id = config["taskId"]
    if task_id not in tasks:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    task_configs = push_configs.setdefault(task_id, {})
    task_configs[config["id"]] = config
    return copy.deepcopy(config)


def _a2a_get_push_config(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any]:
    if not _a2a_push_notifications_enabled():
        raise _A2AJsonRpcError(-32003, "Push notifications are not supported")
    task_id = _extract_task_id(params)
    config_id = _extract_config_id(params)
    if task_id not in tasks:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    config = push_configs.get(task_id, {}).get(config_id)
    if config is None:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    return copy.deepcopy(config)


def _a2a_list_push_configs(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any]:
    if not _a2a_push_notifications_enabled():
        raise _A2AJsonRpcError(-32003, "Push notifications are not supported")
    task_id = _extract_task_id(params)
    if task_id not in tasks:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    configs = list(push_configs.get(task_id, {}).values())
    return {"configs": copy.deepcopy(configs), "nextPageToken": ""}


def _a2a_delete_push_config(
    tasks: dict[str, dict[str, Any]],
    push_configs: dict[str, dict[str, dict[str, Any]]],
    params: dict[str, Any],
) -> dict[str, Any]:
    if not _a2a_push_notifications_enabled():
        raise _A2AJsonRpcError(-32003, "Push notifications are not supported")
    task_id = _extract_task_id(params)
    config_id = _extract_config_id(params)
    if task_id not in tasks:
        raise _A2AJsonRpcError(-32001, "Task not found", metadata={"taskId": task_id})
    push_configs.get(task_id, {}).pop(config_id, None)
    return {}


def _normalize_push_config(params: dict[str, Any]) -> dict[str, Any]:
    candidate: Any = params
    for key in ("taskPushNotificationConfig", "task_push_notification_config", "config"):
        if isinstance(candidate, dict) and isinstance(candidate.get(key), dict):
            nested = dict(candidate[key])
            for id_key in ("taskId", "task_id", "id"):
                if id_key in candidate and id_key not in nested:
                    nested[id_key] = candidate[id_key]
            candidate = nested
            break
    if not isinstance(candidate, dict):
        raise _A2AJsonRpcError(-32602, "Invalid params")
    config = dict(candidate)
    task_id = config.pop("task_id", config.get("taskId", None))
    if "taskId" not in config and task_id is not None:
        config["taskId"] = task_id
    if not isinstance(config.get("taskId"), str) or not config["taskId"].strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    if not isinstance(config.get("url"), str) or not config["url"].strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    if not isinstance(config.get("id"), str) or not config["id"].strip():
        config["id"] = f"push-{uuid4().hex}"
    return config


def _extract_task_id(params: dict[str, Any]) -> str:
    task_id = params.get("taskId") or params.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    return task_id


def _extract_config_id(params: dict[str, Any]) -> str:
    config_id = params.get("id") or params.get("configId") or params.get("config_id")
    if not isinstance(config_id, str) or not config_id.strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    return config_id


def _store_push_config_from_send_config(
    task_id: str,
    push_configs: dict[str, dict[str, dict[str, Any]]],
    configuration: Any,
) -> None:
    if not isinstance(configuration, dict):
        return
    candidate = (
        configuration.get("taskPushNotificationConfig")
        or configuration.get("task_push_notification_config")
        or configuration.get("pushNotificationConfig")
    )
    if not isinstance(candidate, dict):
        return
    config = dict(candidate)
    config["taskId"] = task_id
    config.setdefault("id", f"push-{uuid4().hex}")
    try:
        normalized = _normalize_push_config(config)
    except _A2AJsonRpcError:
        return
    push_configs.setdefault(task_id, {})[normalized["id"]] = normalized


def _normalize_a2a_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    normalized = dict(message)
    field_map = {
        "message_id": "messageId",
        "context_id": "contextId",
        "task_id": "taskId",
        "reference_task_ids": "referenceTaskIds",
    }
    for source, target in field_map.items():
        if source in normalized and target not in normalized:
            normalized[target] = normalized.pop(source)
    if isinstance(normalized.get("parts"), list):
        normalized["parts"] = [_normalize_a2a_part(part) for part in normalized["parts"]]
    return normalized


def _normalize_a2a_part(part: Any) -> Any:
    if not isinstance(part, dict):
        return part
    out = dict(part)
    if "raw" in out and "bytes" not in out:
        out["bytes"] = out["raw"]
    return out


def _a2a_rest_list_params(raw: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = dict(raw)
    for key in ("pageSize", "historyLength"):
        if key in params and isinstance(params[key], str):
            if params[key].startswith("-") and params[key][1:].isdecimal():
                params[key] = int(params[key])
                continue
            if not params[key].isdecimal():
                raise _A2AJsonRpcError(-32602, "Invalid params")
            params[key] = int(params[key])
    if "includeArtifacts" in params and isinstance(params["includeArtifacts"], str):
        params["includeArtifacts"] = params["includeArtifacts"].lower() == "true"
    return params


def _validate_a2a_message(message: Any) -> None:
    if not isinstance(message, dict):
        raise _A2AJsonRpcError(-32602, "Invalid params")
    if not isinstance(message.get("messageId"), str) or not message["messageId"].strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    if not isinstance(message.get("role"), str) or not message["role"].strip():
        raise _A2AJsonRpcError(-32602, "Invalid params")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _A2AJsonRpcError(-32602, "Invalid params")
    if any(not isinstance(part, dict) for part in parts):
        raise _A2AJsonRpcError(-32602, "Invalid params")
    for part in parts:
        media_type = part.get("mediaType")
        if media_type is None and isinstance(part.get("file"), dict):
            media_type = part["file"].get("mediaType")
        if media_type is not None and media_type not in _A2A_SUPPORTED_MEDIA_TYPES:
            raise _A2AJsonRpcError(-32005, "Incompatible content types")


def _validate_a2a_send_configuration(configuration: Any) -> None:
    if configuration is None:
        return
    if not isinstance(configuration, dict):
        raise _A2AJsonRpcError(-32602, "Invalid params")
    allowed = {
        "acceptedOutputModes",
        "accepted_output_modes",
        "taskPushNotificationConfig",
        "task_push_notification_config",
        "pushNotificationConfig",
        "historyLength",
        "history_length",
        "returnImmediately",
        "return_immediately",
    }
    if set(configuration) - allowed:
        raise _A2AJsonRpcError(-32004, "This operation is not supported")

    history_length = configuration.get("historyLength")
    if history_length is None:
        history_length = configuration.get("history_length")
    if history_length is not None:
        if isinstance(history_length, bool) or not isinstance(history_length, int) or history_length < 0:
            raise _A2AJsonRpcError(-32602, "Invalid params")
        if history_length > 1000:
            raise _A2AJsonRpcError(-32004, "This operation is not supported")

    accepted_output_modes = configuration.get("acceptedOutputModes")
    if accepted_output_modes is None:
        accepted_output_modes = configuration.get("accepted_output_modes")
    if accepted_output_modes is not None:
        if (
            not isinstance(accepted_output_modes, list)
            or any(not isinstance(mode, str) for mode in accepted_output_modes)
        ):
            raise _A2AJsonRpcError(-32602, "Invalid params")
        if not any(mode in _A2A_SUPPORTED_OUTPUT_MODES for mode in accepted_output_modes):
            raise _A2AJsonRpcError(-32005, "Incompatible content types")


def _a2a_requires_secondary_auth(message: dict[str, Any]) -> bool:
    terms = ("authenticate", "external service", "email", "google drive", "calendar", "banking")
    for part in message.get("parts", []):
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str):
            lowered = text.lower()
            if any(term in lowered for term in terms):
                return True
    return False


def _copy_task_for_response(
    task: dict[str, Any],
    *,
    history_length: int | None = None,
    include_artifacts: bool = True,
) -> dict[str, Any]:
    out = copy.deepcopy(task)
    if history_length is not None:
        out["history"] = out.get("history", [])[-history_length:] if history_length else []
    if not include_artifacts:
        out.pop("artifacts", None)
    return out


def _a2a_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_a2a_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or value == "-1":
        raise _A2AJsonRpcError(-32602, "Invalid params")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _A2AJsonRpcError(-32602, "Invalid params") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _task_timestamp(task: dict[str, Any]) -> datetime:
    timestamp = task.get("status", {}).get("timestamp")
    try:
        return _parse_a2a_timestamp(timestamp)
    except _A2AJsonRpcError:
        return datetime.fromtimestamp(0, timezone.utc)


def _grant_to_workspace(grant: Grant, agent: A2AAgent, grant_token: str | None = None) -> Any:
    """Build a :class:`WorkspaceClient` bounded by the grant.

    When the agent pod has MinIO env, return a real MinIO-backed client
    scoped to the caller's bucket. Local/dev runtimes without those env vars
    keep the in-memory fallback used by tests.
    """
    from ..workspace import (
        ControlPlaneWorkspaceClient,
        LocalWorkspaceClient,
        MinIOWorkspaceClient,
        WorkspaceAccess,
        WorkspaceMode,
    )

    access = WorkspaceAccess.dynamic(
        max_files=64,
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
        require_reason=False,
        deny_patterns=tuple(grant.deny_patterns),
        require_human_approval=False,
    )
    endpoint = os.environ.get("A2A_MINIO_ENDPOINT")
    if endpoint:
        client = MinIOWorkspaceClient(
            bucket=grant.bucket,
            endpoint_url=endpoint,
            access_key_id=os.environ.get("A2A_MINIO_ACCESS_KEY", ""),
            secret_access_key=os.environ.get("A2A_MINIO_SECRET_KEY", ""),
            access=access,
            issuer=grant.audience,
        )
    elif os.environ.get("A2A_CP_URL") and grant_token:
        client = ControlPlaneWorkspaceClient(
            bucket=grant.bucket,
            control_plane_url=os.environ["A2A_CP_URL"],
            grant_token=grant_token,
            access=access,
            issuer=grant.audience,
        )
    else:
        client = LocalWorkspaceClient(
            files={}, access=access, bucket=grant.bucket, issuer=grant.audience
        )
    # Seed the client's capability state from the original grant so a later
    # ctx.request_scope() call sees the right baseline before installing the
    # superseding grant.
    client.install_grant(grant)
    return client


def _local_dev_workspace(agent: A2AAgent) -> Any:
    root = os.environ.get("A2A_LOCAL_WORKSPACE_DIR")
    if not root and os.environ.get("A2A_LOCAL_DEV"):
        root = ".a2a/workspace"
    if not root:
        return None
    from ..workspace import FileSystemWorkspaceClient, WorkspaceAccess, WorkspaceMode

    access = type(agent).workspace_access
    if not access.enabled:
        return None
    local_access = WorkspaceAccess(
        enabled=True,
        max_files=access.max_files or 64,
        allowed_modes=access.allowed_modes
        or (WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
        require_reason=access.require_reason,
        deny_patterns=access.deny_patterns,
        require_human_approval=False,
        max_total_size_bytes=access.max_total_size_bytes,
    )
    return FileSystemWorkspaceClient(
        Path(root),
        access=local_access,
        bucket="local-dev",
        issuer=type(agent).name,
        allow_patterns=("**",),
        outputs_prefix="outputs",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )


def _env_secrets(agent: A2AAgent) -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in type(agent).required_secrets
        if key in os.environ
    }


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


def _bind_minio_artifacts(ctx: LocalRunContext[Any], grant: Grant) -> None:
    """Persist ``ctx.write_artifact`` outputs into the granted workspace."""
    from ..context import ArtifactRef

    write_prefixes = _normalize_write_prefixes(
        grant.outputs_prefix,
        grant.write_prefixes,
    )
    prefix = (write_prefixes[0] if write_prefixes else "outputs/").strip("/")
    task_id = getattr(ctx, "task_id", "task")
    endpoint = os.environ.get("A2A_MINIO_ENDPOINT")
    if not endpoint:
        writer = getattr(getattr(ctx, "workspace", None), "write_bytes", None)
        if writer is None:
            return

        async def write_artifact(name: str, data: bytes, mime_type: str) -> ArtifactRef:
            clean_name = name.strip("/").replace("..", "_") or "artifact"
            key = f"{prefix}/{task_id}/{clean_name}" if prefix else f"{task_id}/{clean_name}"
            writer(key, data)
            try:
                ctx.artifacts[name] = data
            except Exception:  # noqa: BLE001
                pass
            return ArtifactRef(
                name=name,
                uri=f"s3://{grant.bucket}/{key}",
                mime_type=mime_type,
                size_bytes=len(data),
            )

        ctx.write_artifact = write_artifact  # type: ignore[assignment]
        return
    try:
        import boto3
        from botocore.config import Config as _BotoConfig
    except Exception:  # noqa: BLE001
        return

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("A2A_MINIO_ACCESS_KEY", ""),
        aws_secret_access_key=os.environ.get("A2A_MINIO_SECRET_KEY", ""),
        region_name="us-east-1",
        config=_BotoConfig(s3={"addressing_style": "path"}),
    )

    async def write_artifact(name: str, data: bytes, mime_type: str) -> ArtifactRef:
        clean_name = name.strip("/").replace("..", "_") or "artifact"
        key = f"{prefix}/{task_id}/{clean_name}" if prefix else f"{task_id}/{clean_name}"
        s3.put_object(Bucket=grant.bucket, Key=key, Body=data, ContentType=mime_type)
        try:
            ctx.artifacts[name] = data
        except Exception:  # noqa: BLE001
            pass
        return ArtifactRef(
            name=name,
            uri=f"s3://{grant.bucket}/{key}",
            mime_type=mime_type,
            size_bytes=len(data),
        )

    ctx.write_artifact = write_artifact  # type: ignore[assignment]


def _sandbox_client(grant: Grant | None, grant_token: str | None = None) -> Any:
    url = os.environ.get("A2A_SANDBOX_URL")
    if not url:
        return None
    from ..sandbox import HttpSandboxClient

    return HttpSandboxClient(
        url,
        default_workspace=grant.bucket if grant is not None else None,
        timeout_seconds=float(os.environ.get("A2A_SANDBOX_TIMEOUT_S", "1200")),
        auth_token=os.environ.get("A2A_SANDBOX_TOKEN"),
        grant_token=grant_token,
    )


class _DropHealthzAccessLog(logging.Filter):
    """Drop uvicorn access-log lines for the readiness probe.

    The in-pod Knative readiness probe hits ``/healthz`` every few seconds for
    the life of the pod, which otherwise floods the agent logs with
    ``GET /healthz ... 200 OK``. Real requests are still logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def serve(
    agent: A2AAgent,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    frontend: PackedFrontend | None = None,
) -> None:
    """Run the agent's HTTP server with uvicorn (blocking)."""
    import uvicorn

    logging.getLogger("uvicorn.access").addFilter(_DropHealthzAccessLog())
    uvicorn.run(build_app(agent, frontend=frontend), host=host, port=port, log_level="info")
