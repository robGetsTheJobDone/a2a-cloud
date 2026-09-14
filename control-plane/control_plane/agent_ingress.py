"""Trusted public ingress for hosted agents.

Public agent traffic used to flow straight from Traefik/Kourier into user
workloads.  The SDK can describe receipts, but hosted workloads deliberately do
not receive platform private signing keys, so a direct ``/invoke`` or MCP
``tools/call`` could not return platform-signed evidence.

This service is the trust boundary for that path.  It resolves the requested
host against the control-plane database, proxies only to the corresponding
cluster-local agent Service, and signs a gateway-observed receipt/replay record
after an execution response completes.  Signing keys never enter the agents
namespace.

Only operations that execute a skill are sealed:

* ``POST /invoke/{skill}`` (and the packed-frontend ``/_a2a`` alias)
* MCP ``tools/call`` on the standard endpoint
* runtime-declared raw HTTP endpoints that map to a named skill

Health checks, Agent Cards, frontends, MCP discovery, and the current formal
A2A task/message endpoints are proxied without execution receipts.  The latter
currently manage protocol tasks but do not invoke an SDK skill.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
import re
import secrets
import time
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from a2a_pack.grants import GrantInvalid, verify_grant
from a2a_pack.receipts import ExecutionReceipt, hash_input, sign_receipt
from a2a_pack.replay import ReplayEvent, ReplaySession, sign_replay_session
from a2a_pack.runtime import AgentEndpoint as RuntimeAgentEndpoint

from .config import settings
from .db import SessionLocal
from .models import Agent, AgentCustomDomain, AgentReceipt, AgentSession
from .object_store import get_default_store, session_events_key


log = logging.getLogger(__name__)

AGENT_INGRESS_GATEWAY_SERVICE = "agent-ingress-gateway"
AGENT_INGRESS_GATEWAY_HEALTH_PATH = "/__gateway/healthz"
AGENT_INGRESS_GATEWAY_LIVE_PATH = "/__gateway/livez"

_AGENT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_INVOKE_RE = re.compile(r"^/(?:_a2a/)?invoke/([^/]+)$")
_MCP_PATHS = frozenset(
    {
        "/mcp",
        "/_a2a/mcp",
    }
)
_EVIDENCE_EVENT_KINDS = frozenset(
    {"receipt_sealed", "replay_sealed", "receipt_error"}
)
_SIGNING_ENV_VARS = (
    "A2A_RECEIPT_SIGNING_KEY",
    "A2A_REPLAY_SIGNING_KEY",
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_EVIDENCE_RESPONSE_HEADERS = frozenset(
    {
        "x-a2a-receipt-id",
        "x-a2a-receipt-url",
        "x-a2a-receipt-token",
        "x-a2a-replay-session-id",
        "x-a2a-replay-url",
        "x-a2a-replay-token",
    }
)
_RESERVED_EVIDENCE_BODY_KEYS = frozenset({"a2a_evidence", "a2aCloudEvidence"})
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cp_jwt",
    "grant",
    "llm_creds",
    "password",
    "secret",
    "token",
)
# One home for "what a credential looks like", used two ways. ``_SECRET_*_RE``
# tests a *whole* value, which is all :func:`_redact` can do because it walks
# structure. ``_SECRET_INLINE_RE`` finds the same shapes *inside* a rendered
# string, which is where a real upstream error actually puts them - "OpenAI
# rejected the request for key sk-…" is one string value, not a credential
# field. Both are built from the same source so they cannot drift apart.
_SECRET_TOKEN_SHAPES = (
    r"sk-[A-Za-z0-9_-]{12,}"
    r"|gh[opsu]_[A-Za-z0-9_]{12,}"
    r"|xox[a-z]-[A-Za-z0-9-]{12,}"
    r"|AKIA[A-Z0-9]{12,}"
)
_JWT_SHAPE = r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
_JWT_RE = re.compile(rf"^{_JWT_SHAPE}$")
_SECRET_PREFIX_RE = re.compile(rf"^(?:{_SECRET_TOKEN_SHAPES})$", re.IGNORECASE)
_SECRET_INLINE_RE = re.compile(
    rf"(?:{_SECRET_TOKEN_SHAPES}"
    rf"|{_JWT_SHAPE}"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,})",
    re.IGNORECASE,
)
# ``key: value`` / ``key=value`` for any key :data:`_SENSITIVE_KEY_PARTS` calls
# a credential. This is the key-based rule expressed for *text*: without it a
# body that could not be parsed as JSON - malformed, or simply too large to be
# worth parsing - would silently fall back to shape rules only, and an agent
# could choose that weaker path just by padding its error body.
#
# Deliberately two expressions rather than one. Written as a single pattern the
# key needs a leading ``[\w.-]*`` to reach ``x_api_key``, and that made the
# scan quadratic: on 4 KiB of word characters - which an agent picks - one
# ``sub`` took 700 ms, turning a redactor into a denial of service. Instead the
# first expression finds a credential's *name* in one linear pass, and the
# second is only ever matched from where that name ended, with every quantifier
# bounded.
_SENSITIVE_KEY_RE = re.compile("|".join(_SENSITIVE_KEY_PARTS), re.IGNORECASE)
_SENSITIVE_VALUE_RE = re.compile(
    r"[\w.-]{0,64}[\"']?[ \t]{0,16}[:=][ \t]{0,16}"
    # ``[redacted]`` first so it is seen whole; otherwise the bare-word branch
    # would stop at the marker's own ``]`` and the pass would not be idempotent.
    r"(?P<value>\[redacted\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;)\]}]{1,4096})"
)
# C0 and C1. An agent that can put a raw ESC into a message the CLI prints can
# forge a line in someone's terminal; a raw newline can forge one in a log.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: What every rule here leaves behind, so a later pass can recognise its own work.
_REDACTED = "[redacted]"
_MAX_REQUEST_CAPTURE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_CAPTURE_BYTES = 512 * 1024
_MAX_INLINE_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_SSE_FRAME_BYTES = 1024 * 1024
_UPSTREAM_TOTAL_TIMEOUT_SECONDS = min(
    max(float(os.environ.get("A2A_AGENT_INGRESS_TIMEOUT_SECONDS", "1830")), 1.0),
    1830.0,
)
_EVIDENCE_PERSIST_TIMEOUT_SECONDS = min(
    max(float(os.environ.get("A2A_AGENT_INGRESS_PERSIST_TIMEOUT_SECONDS", "10")), 1.0),
    60.0,
)
_GLOBAL_CONCURRENCY_LIMIT = max(
    1,
    int(os.environ.get("A2A_AGENT_INGRESS_GLOBAL_CONCURRENCY", "200")),
)
_PER_AGENT_CONCURRENCY_LIMIT = max(
    1,
    int(os.environ.get("A2A_AGENT_INGRESS_PER_AGENT_CONCURRENCY", "50")),
)
_BACKGROUND_FINALIZERS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class AgentTarget:
    id: int
    name: str
    version: str
    execution_endpoints: tuple[RuntimeAgentEndpoint, ...] = ()


@dataclass(frozen=True)
class EvidenceOperation:
    transport: str
    skill_name: str
    inputs: Any | None
    input_captured: bool
    caller: str
    grant_ids: tuple[str, ...]
    task_id: str


@dataclass(frozen=True)
class EvidenceReservation:
    receipt_id: str
    session_id: str
    started_at: int
    started_monotonic: float


@dataclass(frozen=True)
class EvidenceBundle:
    receipt: ExecutionReceipt
    receipt_token: str
    replay: ReplaySession
    replay_token: str
    persisted: bool


@dataclass
class BodyCapture:
    limit: int
    data: bytearray
    total_bytes: int = 0
    truncated: bool = False
    keep_tail: bool = False

    @classmethod
    def with_limit(cls, limit: int, *, keep_tail: bool = False) -> "BodyCapture":
        return cls(limit=limit, data=bytearray(), keep_tail=keep_tail)

    def add(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if self.keep_tail:
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[: len(self.data) - self.limit]
                self.truncated = True
            return
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def bytes(self) -> bytes:
        return bytes(self.data)


SessionFactory = Callable[[], Any]
PersistEvidence = Callable[[AgentTarget, EvidenceBundle], Awaitable[bool]]


class AgentConcurrencyBulkhead:
    """Bound one tenant and the process before opening an upstream stream."""

    def __init__(self, *, global_limit: int, per_agent_limit: int) -> None:
        self.global_limit = global_limit
        self.per_agent_limit = min(per_agent_limit, global_limit)
        self._total = 0
        self._per_agent: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, agent_name: str) -> bool:
        async with self._lock:
            current = self._per_agent.get(agent_name, 0)
            if self._total >= self.global_limit or current >= self.per_agent_limit:
                return False
            self._total += 1
            self._per_agent[agent_name] = current + 1
            return True

    async def release(self, agent_name: str) -> None:
        async with self._lock:
            current = self._per_agent.get(agent_name, 0)
            if current <= 0:
                return
            self._total = max(0, self._total - 1)
            if current == 1:
                self._per_agent.pop(agent_name, None)
            else:
                self._per_agent[agent_name] = current - 1


def _normalized_host(host_header: str) -> str | None:
    """Return an ASCII DNS host without a port, or ``None`` when malformed."""
    value = host_header.strip().lower().rstrip(".")
    if not value or any(char in value for char in ("/", "\\", "@", "\x00")):
        return None
    if value.startswith("[") or value.count(":") > 1:
        return None
    if ":" in value:
        host, port = value.rsplit(":", 1)
        if not port.isdigit():
            return None
        value = host
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(value) > 253 or ".." in value:
        return None
    return value


def _platform_agent_name(host: str) -> str | None:
    suffix = settings.platform_host_suffix
    if not host.endswith(suffix):
        return None
    name = host[: -len(suffix)]
    if not _AGENT_NAME_RE.fullmatch(name):
        return None
    return name


def _runtime_execution_endpoints(card: dict[str, Any]) -> tuple[RuntimeAgentEndpoint, ...]:
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    raw = runtime.get("endpoints")
    if raw is None:
        raw = runtime.get("webhooks")
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    endpoints: list[RuntimeAgentEndpoint] = []
    for item in items:
        try:
            endpoints.append(RuntimeAgentEndpoint.model_validate(item))
        except (TypeError, ValueError):
            # Invalid declarations cannot be mounted by the SDK runtime either.
            # Ignore them without allowing untrusted card data to break ingress.
            log.warning("ignoring invalid raw execution endpoint in agent card")
    return tuple(endpoints)


async def _resolve_public_agent(
    host_header: str,
    session_factory: SessionFactory = SessionLocal,
) -> AgentTarget | None:
    """Resolve a hosted platform/custom host without using it as an upstream.

    ``Agent.public`` controls marketplace visibility; it is not an ingress
    authorization boundary. Private agents were historically reachable at
    their canonical host and enforce their declared auth mode in the runtime.
    Keeping routing independent from listing visibility preserves that access
    model while still sending executions through this trusted gateway.
    """
    host = _normalized_host(host_header)
    if host is None:
        return None
    platform_name = _platform_agent_name(host)
    async with session_factory() as session:
        if platform_name is not None:
            agent = (
                await session.execute(
                    select(Agent)
                    .where(Agent.name == platform_name)
                )
            ).scalar_one_or_none()
        else:
            agent = (
                await session.execute(
                    select(Agent)
                    .join(AgentCustomDomain, AgentCustomDomain.agent_id == Agent.id)
                    .where(AgentCustomDomain.hostname == host)
                    .where(AgentCustomDomain.status == "active")
                )
            ).scalar_one_or_none()
        if agent is None or not _AGENT_NAME_RE.fullmatch(str(agent.name)):
            return None
        card = agent.card if isinstance(agent.card, dict) else {}
        version = str(card.get("version") or agent.version or "")[:64]
        return AgentTarget(
            id=int(agent.id),
            name=str(agent.name),
            version=version,
            execution_endpoints=_runtime_execution_endpoints(card),
        )


def _raw_execution_endpoint(
    target: AgentTarget,
    *,
    method: str,
    path: str,
) -> RuntimeAgentEndpoint | None:
    normalized_method = method.upper()
    return next(
        (
            endpoint
            for endpoint in target.execution_endpoints
            if endpoint.path == path and normalized_method in endpoint.methods
        ),
        None,
    )


def _potential_evidence_path(method: str, path: str, *, target: AgentTarget) -> bool:
    if _raw_execution_endpoint(target, method=method, path=path) is not None:
        return True
    if method.upper() != "POST":
        return False
    return bool(_INVOKE_RE.fullmatch(path)) or path in _MCP_PATHS


def _json_object(capture: BodyCapture) -> dict[str, Any] | None:
    if capture.truncated:
        return None
    try:
        value = json.loads(capture.bytes())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_grant_metadata(
    token: Any,
    *,
    target: AgentTarget,
) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        grant = verify_grant(token)
    except (GrantInvalid, RuntimeError, ValueError):
        return None
    audience = str(grant.audience).rstrip("/")
    accepted = {
        target.name,
        f"https://{target.name}{settings.platform_host_suffix}",
        f"http://{target.name}.agents.svc.cluster.local",
    }
    if audience not in accepted:
        return None
    caller = str(grant.issuer or "").strip()[:255] or "grant-caller"
    return caller, (str(grant.grant_id),)


def _external_caller(request_headers: httpx.Headers | Any) -> str:
    authorization = str(request_headers.get("authorization") or "").strip()
    cookie = str(request_headers.get("cookie") or "").strip()
    # Presence is observable; successful authentication is enforced by the
    # agent and cannot be asserted by this transport observer.
    return "credential-present" if authorization or cookie else "anonymous"


def _raw_endpoint_body_value(body: BodyCapture, *, content_type: str) -> tuple[Any, bool]:
    if body.truncated:
        return None, False
    raw = body.bytes()
    if not raw:
        return None, True
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            return json.loads(raw), True
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The runtime will reject invalid JSON before invoking the skill,
            # but retain bounded raw evidence for the signed failed attempt.
            return raw.decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), True


def _operation_from_request(
    *,
    method: str,
    path: str,
    body: BodyCapture,
    headers: Any,
    target: AgentTarget,
    query_params: Any | None = None,
) -> EvidenceOperation | None:
    parsed = _json_object(body)
    invoke_match = _INVOKE_RE.fullmatch(path)
    raw_endpoint = _raw_execution_endpoint(target, method=method, path=path)
    if method.upper() == "POST" and invoke_match is not None:
        skill_name = invoke_match.group(1)[:128]
        inputs = parsed.get("arguments", {}) if parsed is not None else None
        grant_token = parsed.get("grant") if parsed is not None else None
        task_id = f"http-{skill_name}"[:128]
        transport = "invoke"
        input_captured = parsed is not None
    elif method.upper() == "POST" and path in _MCP_PATHS:
        if parsed is None or parsed.get("method") != "tools/call":
            return None
        params = parsed.get("params")
        if not isinstance(params, dict):
            params = {}
        skill_name = str(params.get("name") or "mcp.tools/call")[:128]
        inputs = params.get("arguments", {})
        # The standard MCP transport strips client-supplied privileged _meta
        # and does not consume delegation grants. Never sign provenance that
        # the skill execution did not actually receive.
        grant_token = None
        rpc_id = parsed.get("id")
        task_id = f"mcp-{rpc_id}"[:128] if rpc_id is not None else "mcp-tools-call"
        transport = "mcp"
        input_captured = True
    elif raw_endpoint is not None:
        raw_body, input_captured = _raw_endpoint_body_value(
            body,
            content_type=str(headers.get("content-type") or ""),
        )
        inputs = {raw_endpoint.body_arg: raw_body}
        if raw_endpoint.headers_arg:
            inputs[raw_endpoint.headers_arg] = dict(headers)
        if raw_endpoint.query_arg:
            inputs[raw_endpoint.query_arg] = dict(query_params or {})
        skill_name = raw_endpoint.skill[:128]
        grant_token = None
        task_id = f"http-endpoint:{raw_endpoint.name or raw_endpoint.path}"[:128]
        transport = "endpoint"
    else:
        return None

    caller = _external_caller(headers)
    grant_ids: tuple[str, ...] = ()
    grant_metadata = _valid_grant_metadata(
        grant_token,
        target=target,
    )
    if grant_metadata is not None:
        caller, grant_ids = grant_metadata
    return EvidenceOperation(
        transport=transport,
        skill_name=skill_name,
        inputs=inputs,
        input_captured=input_captured,
        caller=caller,
        grant_ids=grant_ids,
        task_id=task_id,
    )


def _reservation() -> EvidenceReservation:
    return EvidenceReservation(
        receipt_id=secrets.token_hex(8),
        # 128 bits. The id is *not* a capability — /v1/sessions/{id} is gated by
        # agent_authorization.authorize_session_read and has no anonymous branch
        # — but a replay handle travels in logs and referrers, so the entropy
        # stays as defence in depth rather than as the grant.
        session_id=secrets.token_hex(16),
        started_at=int(time.time()),
        started_monotonic=time.monotonic(),
    )


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[depth-limited]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                out["…"] = "[items-limited]"
                break
            out[str(key)] = "[redacted]" if _is_sensitive_key(key) else _redact(
                item, depth=depth + 1
            )
        return out
    if isinstance(value, (list, tuple)):
        items = [_redact(item, depth=depth + 1) for item in value[:50]]
        if len(value) > 50:
            items.append("[items-limited]")
        return items
    if isinstance(value, str):
        stripped = value.strip()
        if (
            stripped.lower().startswith("bearer ")
            or "-----BEGIN PRIVATE KEY-----" in stripped
            or _JWT_RE.fullmatch(stripped)
            or _SECRET_PREFIX_RE.fullmatch(stripped)
            or ("://" in stripped and "@" in stripped.split("/", 3)[2])
        ):
            return "[redacted]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _scrub_untrusted_text(text: str) -> str:
    """Redact credential shapes *inside* a rendered string, and defang it.

    :func:`_redact` works on structure, so it can only see a secret that is a
    whole value under a key it recognises. Untrusted text does not oblige: a
    provider quotes the key back inside a sentence, a traceback prints the
    header it sent, a body arrives too malformed or too large to parse at all.
    Those used to survive verbatim into a signed, durable receipt.

    This is the second stage, and it is deliberately structure-blind so that no
    input shape gets a weaker rule set than another: whatever produced the text,
    both the key-based rule and the value-shape rule get a chance at it.
    """
    scrubbed = _CONTROL_CHAR_RE.sub(" ", text)
    kept: list[str] = []
    cursor = 0
    for name in _SENSITIVE_KEY_RE.finditer(scrubbed):
        if name.start() < cursor:
            continue  # inside a value an earlier key already blanked
        assignment = _SENSITIVE_VALUE_RE.match(scrubbed, name.end())
        if assignment is None:
            continue  # the name is prose, not a field: nothing to blank
        start, end = assignment.span("value")
        if scrubbed[start:end].strip("\"'") == _REDACTED:
            continue  # already blanked, by _redact upstream or an earlier pass
        kept.append(scrubbed[cursor:start])
        kept.append(_REDACTED)
        cursor = end
    kept.append(scrubbed[cursor:])
    return _SECRET_INLINE_RE.sub(_REDACTED, "".join(kept))


def _preview(value: Any, *, limit: int = 240) -> str:
    try:
        rendered = json.dumps(
            _redact(value), separators=(",", ":"), ensure_ascii=False, default=str
        )
    except Exception:  # noqa: BLE001
        rendered = "[unavailable]"
    # Scrub before truncating, so a credential straddling the cut is still
    # matched; the head slice keeps a hostile body from becoming regex work.
    rendered = _scrub_untrusted_text(rendered[: limit * 4])
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 1] + "…"


def _response_observation(
    body: bytes,
    *,
    content_type: str,
    status_code: int,
    transport: str,
    truncated: bool,
) -> tuple[str, str, Any | None]:
    status = "error" if status_code >= 300 else "ok"
    error_type = f"HTTP{status_code}" if status_code >= 300 else ""
    result: Any | None = None
    text = ""
    with suppress(UnicodeDecodeError):
        text = body.decode("utf-8")

    if "text/event-stream" in content_type.lower():
        for raw_data in _sse_data_values(text):
            if raw_data == "[DONE]":
                continue
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            _strip_untrusted_evidence_values(event)
            if event.get("error") is not None:
                status = "error"
                error = event.get("error")
                error_type = (
                    str(error.get("code") or error.get("type") or "MCPError")[:128]
                    if isinstance(error, dict)
                    else "MCPError"
                )
                result = {"error": error}
            elif transport == "mcp" and isinstance(event.get("result"), dict):
                result = event.get("result")
                if result.get("isError") is True:
                    status = "error"
                    error_type = "MCPToolError"
            elif event.get("type") == "result":
                result = event.get("result")
            elif event.get("type") == "error":
                status = "error"
                error_type = str(event.get("detail") or "AgentError")[:128]
                result = {"error": event.get("detail")}
    elif "json" in content_type.lower() and text and not truncated:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            _strip_untrusted_evidence_values(payload)
            if "error" in payload and payload.get("error") is not None:
                status = "error"
                error = payload.get("error")
                if isinstance(error, dict):
                    error_type = str(error.get("code") or error.get("type") or "AgentError")[:128]
                elif not error_type:
                    error_type = "AgentError"
                result = {"error": error}
            elif transport == "invoke" and "result" in payload:
                result = payload.get("result")
            elif transport == "mcp" and "result" in payload:
                result = payload.get("result")
                if isinstance(result, dict) and result.get("isError") is True:
                    status = "error"
                    error_type = "MCPToolError"
            elif transport == "endpoint":
                result = payload
            elif status_code >= 400:
                result = {"error": payload.get("detail") or payload}
    if truncated and result is None:
        result = {"response_preview": "[omitted: response exceeded capture limit]"}
    return status, error_type, result


def _build_evidence(
    *,
    target: AgentTarget,
    operation: EvidenceOperation,
    reservation: EvidenceReservation,
    response_body: bytes,
    response_truncated: bool,
    response_content_type: str,
    response_status_code: int,
) -> EvidenceBundle:
    ended_at = int(time.time())
    elapsed_ms = max(0, int((time.monotonic() - reservation.started_monotonic) * 1000))
    input_hash = (
        hash_input(operation.inputs)
        if operation.input_captured and operation.inputs is not None
        else ""
    )
    input_preview = (
        _preview(operation.inputs)
        if operation.input_captured and operation.inputs is not None
        else "[omitted: request body unavailable or exceeded capture limit]"
    )
    status, error_type, result = _response_observation(
        response_body,
        content_type=response_content_type,
        status_code=response_status_code,
        transport=operation.transport,
        truncated=response_truncated,
    )
    receipt = ExecutionReceipt(
        receipt_id=reservation.receipt_id,
        agent_name=target.name,
        agent_version=target.version,
        caller=operation.caller,
        task_id=operation.task_id,
        skill_name=operation.skill_name,
        input_hash=input_hash,
        input_preview=input_preview,
        grant_ids=operation.grant_ids,
        status=status,
        error_type=error_type,
        result_preview=_preview(result) if result is not None else "",
        started_at=reservation.started_at,
        ended_at=ended_at,
        elapsed_ms=elapsed_ms,
    )
    receipt_token = sign_receipt(receipt)
    start_ms = reservation.started_at * 1000
    end_ms = max(start_ms, int(time.time() * 1000))
    terminal_kind = "skill_end" if status == "ok" else "error"
    replay = ReplaySession(
        session_id=reservation.session_id,
        agent_name=target.name,
        agent_version=target.version,
        caller=operation.caller,
        task_id=operation.task_id,
        skill_name=operation.skill_name,
        input_hash=input_hash,
        started_at=reservation.started_at,
        ended_at=ended_at,
        events=(
            ReplayEvent(
                idx=0,
                kind="skill_start",
                ts_ms=start_ms,
                payload={"observer": "agent-ingress-gateway", "transport": operation.transport},
            ),
            ReplayEvent(
                idx=1,
                kind=terminal_kind,
                ts_ms=end_ms,
                payload={"status": status, "http_status": response_status_code},
            ),
        ),
        receipt_id=receipt.receipt_id,
    )
    replay_token = sign_replay_session(replay)
    return EvidenceBundle(
        receipt=receipt,
        receipt_token=receipt_token,
        replay=replay,
        replay_token=replay_token,
        persisted=False,
    )


async def _persist_evidence(
    target: AgentTarget,
    bundle: EvidenceBundle,
    *,
    session_factory: SessionFactory = SessionLocal,
) -> bool:
    """Persist direct-ingress evidence."""
    receipt = bundle.receipt
    replay = bundle.replay
    object_key = session_events_key(target.id, replay.session_id)
    try:
        store = get_default_store()
        object_stored = True
        try:
            await asyncio.to_thread(
                store.put_jsonl,
                object_key,
                (event.model_dump(mode="json") for event in replay.events),
            )
        except Exception:  # noqa: BLE001
            # The signed replay token contains the complete minimal event list,
            # so keep the database row and token retrievable even if the
            # secondary JSONL object store is temporarily unavailable.
            object_stored = False
            log.exception(
                "failed to store replay object for direct-ingress evidence %s",
                receipt.receipt_id,
            )
        async with session_factory() as session:
            session.add(
                AgentReceipt(
                    receipt_id=receipt.receipt_id,
                    agent_id=target.id,
                    agent_name=receipt.agent_name,
                    agent_version=receipt.agent_version,
                    caller=receipt.caller,
                    task_id=receipt.task_id,
                    skill_name=receipt.skill_name,
                    status=receipt.status,
                    eval_score=receipt.eval_score,
                    started_at=_timestamp(receipt.started_at),
                    ended_at=_timestamp(receipt.ended_at),
                    elapsed_ms=int(receipt.elapsed_ms),
                    signed_token=bundle.receipt_token,
                    payload=receipt.model_dump(mode="json"),
                )
            )
            session.add(
                AgentSession(
                    session_id=replay.session_id,
                    agent_id=target.id,
                    agent_name=replay.agent_name,
                    agent_version=replay.agent_version,
                    caller=replay.caller,
                    task_id=replay.task_id,
                    skill_name=replay.skill_name,
                    receipt_id=replay.receipt_id or None,
                    started_at=int(replay.started_at),
                    ended_at=int(replay.ended_at),
                    event_count=len(replay.events),
                    signed_token=bundle.replay_token,
                    events_object_key=object_key,
                )
            )
            await session.commit()
            if receipt.status == "error":
                try:
                    from .self_healing import maybe_enqueue_runtime_failure

                    await maybe_enqueue_runtime_failure(
                        session,
                        agent_id=target.id,
                        receipt_id=receipt.receipt_id,
                        skill_name=receipt.skill_name,
                        error_type=receipt.error_type,
                        error_preview=receipt.result_preview,
                    )
                except Exception:  # noqa: BLE001
                    await session.rollback()
                    log.exception(
                        "failed to evaluate self-healing policy for receipt %s",
                        receipt.receipt_id,
                    )
        if not object_stored:
            log.warning(
                "direct-ingress evidence %s persisted without replay JSONL object",
                receipt.receipt_id,
            )
        return True
    except IntegrityError:
        # IDs are random and a duplicate can only be a retry/race, but a
        # foreign-key failure (for example an agent deleted mid-request) must
        # not be reported as persisted. Verify both rows in a fresh session.
        async with session_factory() as session:
            existing_receipt = await session.get(AgentReceipt, receipt.receipt_id)
            existing_session = await session.get(AgentSession, replay.session_id)
        if existing_receipt is not None and existing_session is not None:
            log.info("direct-ingress evidence %s already persisted", receipt.receipt_id)
            return True
        log.exception(
            "integrity failure persisting direct-ingress evidence %s",
            receipt.receipt_id,
        )
        return False
    except Exception:  # noqa: BLE001
        log.exception("failed to persist direct-ingress evidence %s", receipt.receipt_id)
        return False


def _timestamp(seconds: int) -> datetime | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def _public_cp_url() -> str:
    return str(settings.public_cp_url).rstrip("/")


def _evidence_payload(bundle: EvidenceBundle) -> dict[str, Any]:
    base = _public_cp_url()
    agent_name = bundle.receipt.agent_name
    return {
        "receipt": {
            "receipt_id": bundle.receipt.receipt_id,
            "signed_token": bundle.receipt_token,
            "url": f"{base}/v1/agents/{agent_name}/receipts/{bundle.receipt.receipt_id}",
        },
        "replay": {
            "session_id": bundle.replay.session_id,
            "signed_token": bundle.replay_token,
            "url": f"{base}/v1/sessions/{bundle.replay.session_id}",
        },
        "observer": "a2a-cloud-agent-ingress-gateway",
        "persisted": bundle.persisted,
    }


def _reservation_headers(
    reservation: EvidenceReservation,
    *,
    target: AgentTarget,
) -> dict[str, str]:
    base = _public_cp_url()
    return {
        "X-A2A-Receipt-ID": reservation.receipt_id,
        "X-A2A-Receipt-URL": (
            f"{base}/v1/agents/{target.name}/receipts/{reservation.receipt_id}"
        ),
        "X-A2A-Replay-Session-ID": reservation.session_id,
        "X-A2A-Replay-URL": f"{base}/v1/sessions/{reservation.session_id}",
    }


def _bundle_headers(bundle: EvidenceBundle) -> dict[str, str]:
    headers = _reservation_headers(
        EvidenceReservation(
            receipt_id=bundle.receipt.receipt_id,
            session_id=bundle.replay.session_id,
            started_at=bundle.receipt.started_at,
            started_monotonic=0.0,
        ),
        target=AgentTarget(
            id=0,
            name=bundle.receipt.agent_name,
            version=bundle.receipt.agent_version,
        ),
    )
    headers["X-A2A-Receipt-Token"] = bundle.receipt_token
    headers["X-A2A-Replay-Token"] = bundle.replay_token
    return headers


def _forward_request_headers(
    headers: Any,
    *,
    target: AgentTarget,
    original_host: str,
    scheme: str,
    client_ip: str,
) -> list[tuple[str, str]]:
    forwarded: list[tuple[str, str]] = []
    connection_tokens = {
        token.strip().lower()
        for token in str(headers.get("connection") or "").split(",")
        if token.strip()
    }
    for key, value in headers.items():
        lower = key.lower()
        if (
            lower == "host"
            or lower == "accept-encoding"
            or lower == "content-length"
            or lower == "forwarded"
            or lower.startswith("x-forwarded-")
            or lower in _HOP_BY_HOP_HEADERS
            or lower in connection_tokens
            or lower in _EVIDENCE_RESPONSE_HEADERS
        ):
            continue
        forwarded.append((key, value))
    # The destination is derived from the database target, not these headers.
    # ``original_host`` has already resolved to this public agent, so preserve
    # an active custom domain for redirects, cookie scope, and generated links
    # without allowing it to influence the cluster-local upstream address.
    forwarded.append(("Host", original_host))
    if client_ip:
        forwarded.append(("X-Forwarded-For", client_ip))
    forwarded.append(("X-Forwarded-Host", original_host))
    forwarded.append(("X-Forwarded-Proto", "https" if scheme == "https" else "http"))
    forwarded.append(("X-Forwarded-Port", "443" if scheme == "https" else "80"))
    forwarded.append(("Accept-Encoding", "identity"))
    return forwarded


def _response_header_pairs(
    upstream: httpx.Headers,
    *,
    extra: dict[str, str] | None = None,
    content_length: int | None = None,
) -> list[tuple[bytes, bytes]]:
    pairs: list[tuple[str, str]] = []
    connection_tokens = {
        token.strip().lower()
        for token in str(upstream.get("connection") or "").split(",")
        if token.strip()
    }
    for key, value in upstream.multi_items():
        lower = key.lower()
        if (
            lower in _HOP_BY_HOP_HEADERS
            or lower in connection_tokens
            or lower in _EVIDENCE_RESPONSE_HEADERS
            or (content_length is not None and lower == "content-length")
            or lower == "access-control-expose-headers"
        ):
            continue
        pairs.append((key, value))
    if content_length is not None:
        pairs.append(("Content-Length", str(content_length)))
    if extra:
        pairs.extend(extra.items())
        exposed = ", ".join(extra)
        existing = upstream.get("access-control-expose-headers", "")
        if existing:
            exposed = f"{existing}, {exposed}"
        pairs.append(("Access-Control-Expose-Headers", exposed))
    elif upstream.get("access-control-expose-headers"):
        pairs.append(
            (
                "Access-Control-Expose-Headers",
                upstream["access-control-expose-headers"],
            )
        )
    return [
        (key.encode("latin-1"), value.encode("latin-1")) for key, value in pairs
    ]


def _response_with_headers(
    body: bytes,
    *,
    status_code: int,
    upstream_headers: httpx.Headers,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    response = Response(content=body, status_code=status_code)
    response.raw_headers = _response_header_pairs(
        upstream_headers,
        extra=extra_headers,
        content_length=len(body),
    )
    return response


def _streaming_response_with_headers(
    body: AsyncIterator[bytes],
    *,
    status_code: int,
    upstream_headers: httpx.Headers,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    response = StreamingResponse(body, status_code=status_code)
    response.raw_headers = _response_header_pairs(
        upstream_headers,
        extra=extra_headers,
    )
    return response


def _strip_untrusted_invoke_evidence(payload: dict[str, Any]) -> None:
    events = payload.get("events")
    if not isinstance(events, list):
        return
    payload["events"] = [
        event
        for event in events
        if not (
            isinstance(event, dict)
            and str(event.get("kind") or "") in _EVIDENCE_EVENT_KINDS
        )
    ]


def _strip_untrusted_evidence_values(value: Any) -> bool:
    """Remove reserved evidence namespaces recursively from agent output."""
    changed = False
    if isinstance(value, dict):
        for key in list(value):
            if str(key) in _RESERVED_EVIDENCE_BODY_KEYS:
                del value[key]
                changed = True
                continue
            child_changed = _strip_untrusted_evidence_values(value[key])
            if child_changed and str(key) == "_meta" and value[key] == {}:
                del value[key]
            changed = child_changed or changed
    elif isinstance(value, list):
        for item in value:
            changed = _strip_untrusted_evidence_values(item) or changed
    return changed


def _inline_json_evidence(
    body: bytes,
    *,
    bundle: EvidenceBundle,
    operation: EvidenceOperation,
) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    _strip_untrusted_evidence_values(payload)
    evidence = _evidence_payload(bundle)
    if operation.transport == "invoke":
        _strip_untrusted_invoke_evidence(payload)
        payload["a2a_evidence"] = evidence
    elif operation.transport == "mcp" and isinstance(payload.get("result"), dict):
        result = payload["result"]
        metadata = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
        metadata["a2aCloudEvidence"] = evidence
        result["_meta"] = metadata
    else:
        payload["a2a_evidence"] = evidence
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sse_data_values(text: str) -> list[str]:
    values: list[str] = []
    data_lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if not line:
            if data_lines:
                values.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        values.append("\n".join(data_lines))
    return values


def _sanitize_sse_frame(frame: bytes) -> tuple[str, bytes]:
    try:
        text = frame.decode("utf-8")
    except UnicodeDecodeError:
        return "forward", frame
    values = _sse_data_values(text)
    if any(value == "[DONE]" for value in values):
        return "done", b""
    for value in values:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("type") == "a2a.evidence"
        ):
            return "drop", b""
        if (
            isinstance(payload, dict)
            and payload.get("type") == "event"
            and str(payload.get("kind") or "") in _EVIDENCE_EVENT_KINDS
        ):
            return "drop", b""
        if isinstance(payload, dict) and _strip_untrusted_evidence_values(payload):
            sanitized = json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            return "forward", b"data: " + sanitized + b"\n\n"
    return "forward", frame


def _pop_sse_frame(buffer: bytearray) -> bytes | None:
    raw = bytes(buffer)
    positions = [position for marker in (b"\n\n", b"\r\n\r\n") if (position := raw.find(marker)) >= 0]
    if not positions:
        return None
    position = min(positions)
    marker_len = 4 if raw[position : position + 4] == b"\r\n\r\n" else 2
    frame = raw[: position + marker_len]
    del buffer[: position + marker_len]
    return frame


async def _finalize_evidence(
    *,
    target: AgentTarget,
    operation: EvidenceOperation,
    reservation: EvidenceReservation,
    response_capture: BodyCapture,
    response_content_type: str,
    response_status_code: int,
    persister: PersistEvidence,
) -> EvidenceBundle:
    bundle = _build_evidence(
        target=target,
        operation=operation,
        reservation=reservation,
        response_body=response_capture.bytes(),
        response_truncated=response_capture.truncated,
        response_content_type=response_content_type,
        response_status_code=response_status_code,
    )
    try:
        persisted = await asyncio.wait_for(
            persister(target, bundle),
            timeout=_EVIDENCE_PERSIST_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        persisted = False
        log.error("timed out persisting direct-ingress evidence %s", bundle.receipt.receipt_id)
    except Exception:  # noqa: BLE001
        persisted = False
        log.exception("failed to persist direct-ingress evidence %s", bundle.receipt.receipt_id)
    return EvidenceBundle(
        receipt=bundle.receipt,
        receipt_token=bundle.receipt_token,
        replay=bundle.replay,
        replay_token=bundle.replay_token,
        persisted=persisted,
    )


async def _shield_background_finalizer(awaitable: Awaitable[Any]) -> None:
    """Keep post-disconnect evidence persistence alive under cancellation."""
    task = asyncio.create_task(awaitable)
    _BACKGROUND_FINALIZERS.add(task)
    task.add_done_callback(_BACKGROUND_FINALIZERS.discard)
    with suppress(asyncio.CancelledError, Exception):
        await asyncio.shield(task)


def _signing_ready() -> tuple[bool, str]:
    missing = [name for name in _SIGNING_ENV_VARS if not os.environ.get(name, "").strip()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    try:
        now = int(time.time())
        receipt = ExecutionReceipt(
            receipt_id="gateway-readiness",
            agent_name="gateway-readiness",
            skill_name="readiness",
            started_at=now,
            ended_at=now,
        )
        sign_receipt(receipt)
        sign_replay_session(
            ReplaySession(
                session_id="gateway-readiness",
                agent_name="gateway-readiness",
                skill_name="readiness",
                started_at=now,
                ended_at=now,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid signing material: {type(exc).__name__}"
    return True, "ready"


def _upstream_url(target: AgentTarget, request: Request) -> str:
    raw_path = request.scope.get("raw_path") or request.url.path.encode("ascii", "surrogateescape")
    path = raw_path.decode("ascii", "surrogateescape")
    url = f"http://{target.name}.agents.svc.cluster.local{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return url


def create_app(
    *,
    http_client: httpx.AsyncClient | None = None,
    session_factory: SessionFactory = SessionLocal,
    persister: PersistEvidence | None = None,
) -> FastAPI:
    owns_client = http_client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.http_client is None:
            app.state.http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=_UPSTREAM_TOTAL_TIMEOUT_SECONDS,
                    write=30.0,
                    pool=5.0,
                ),
                limits=httpx.Limits(
                    max_connections=_GLOBAL_CONCURRENCY_LIMIT,
                    max_keepalive_connections=min(100, _GLOBAL_CONCURRENCY_LIMIT),
                    keepalive_expiry=30.0,
                ),
                follow_redirects=False,
            )
        try:
            yield
        finally:
            if owns_client and app.state.http_client is not None:
                await app.state.http_client.aclose()

    app = FastAPI(
        title="A2A Cloud agent ingress gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.http_client = http_client
    app.state.session_factory = session_factory
    app.state.bulkhead = AgentConcurrencyBulkhead(
        global_limit=_GLOBAL_CONCURRENCY_LIMIT,
        per_agent_limit=_PER_AGENT_CONCURRENCY_LIMIT,
    )

    if persister is None:
        async def default_persister(target: AgentTarget, bundle: EvidenceBundle) -> bool:
            return await _persist_evidence(
                target,
                bundle,
                session_factory=app.state.session_factory,
            )

        app.state.persist_evidence = default_persister
    else:
        app.state.persist_evidence = persister

    @app.get(AGENT_INGRESS_GATEWAY_HEALTH_PATH)
    async def healthz() -> Response:
        ready, detail = _signing_ready()
        database_detail = "unchecked"
        if ready:
            try:
                async with app.state.session_factory() as session:
                    await asyncio.wait_for(session.execute(select(1)), timeout=2.0)
                database_detail = "ready"
            except Exception:  # noqa: BLE001
                ready = False
                database_detail = "unavailable"
        return JSONResponse(
            {
                "status": "ok" if ready else "unready",
                "signing": detail,
                "database": database_detail,
            },
            status_code=200 if ready else 503,
        )

    @app.get(AGENT_INGRESS_GATEWAY_LIVE_PATH)
    async def livez() -> Response:
        # Liveness answers only whether the process can serve requests. A
        # missing/invalid signer is a readiness failure and should not create
        # a restart loop that cannot repair operator configuration.
        return JSONResponse({"status": "ok"})

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy(path: str, request: Request) -> Response:
        request_path = request.url.path
        target = await _resolve_public_agent(
            request.headers.get("host", ""),
            app.state.session_factory,
        )
        if target is None:
            return JSONResponse({"detail": "agent not found"}, status_code=404)
        potential_evidence = _potential_evidence_path(
            request.method,
            request_path,
            target=target,
        )

        request_capture = BodyCapture.with_limit(_MAX_REQUEST_CAPTURE_BYTES)
        if potential_evidence:
            declared_length = request.headers.get("content-length", "")
            if declared_length.isdigit() and int(declared_length) > _MAX_REQUEST_CAPTURE_BYTES:
                # Buffer execution envelopes before forwarding. Otherwise a
                # chunked oversized MCP tools/call could evade classification
                # after the skill had already started upstream.
                return JSONResponse(
                    {"detail": "execution request body exceeds 8 MiB"},
                    status_code=413,
                )
            request_chunks: list[bytes] = []
            async for chunk in request.stream():
                if request_capture.total_bytes + len(chunk) > _MAX_REQUEST_CAPTURE_BYTES:
                    return JSONResponse(
                        {"detail": "execution request body exceeds 8 MiB"},
                        status_code=413,
                    )
                request_capture.add(chunk)
                request_chunks.append(chunk)
            request_bytes = b"".join(request_chunks)
            upstream_content: Any = request_bytes
        else:
            async def request_body() -> AsyncIterator[bytes]:
                async for chunk in request.stream():
                    request_capture.add(chunk)
                    yield chunk

            upstream_content = request_body()

        client: httpx.AsyncClient = app.state.http_client
        original_host = _normalized_host(request.headers.get("host", "")) or target.name
        upstream_request = client.build_request(
            request.method,
            _upstream_url(target, request),
            headers=_forward_request_headers(
                request.headers,
                target=target,
                original_host=original_host,
                scheme=request.url.scheme,
                client_ip=request.client.host if request.client is not None else "",
            ),
            content=upstream_content,
        )
        reservation = _reservation() if potential_evidence else None
        pre_operation = _operation_from_request(
            method=request.method,
            path=request_path,
            body=request_capture,
            headers=request.headers,
            target=target,
            query_params=request.query_params,
        )
        if pre_operation is not None:
            ready, detail = _signing_ready()
            if not ready:
                # Classification happens after the bounded request envelope is
                # read, but still before forwarding. MCP discovery therefore
                # remains available during a signer outage while tools/call
                # cannot execute without platform evidence.
                return JSONResponse(
                    {"detail": "execution evidence signer unavailable", "signing": detail},
                    status_code=503,
                )
        bulkhead: AgentConcurrencyBulkhead = app.state.bulkhead
        if not await bulkhead.acquire(target.name):
            return JSONResponse(
                {"detail": "agent ingress concurrency limit reached"},
                status_code=429,
                headers={"Retry-After": "1"},
            )
        bulkhead_released = False

        async def release_bulkhead() -> None:
            nonlocal bulkhead_released
            if bulkhead_released:
                return
            bulkhead_released = True
            await bulkhead.release(target.name)

        try:
            upstream = await asyncio.wait_for(
                client.send(upstream_request, stream=True),
                timeout=_UPSTREAM_TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if pre_operation is not None and reservation is not None:
                await _shield_background_finalizer(
                    _finalize_evidence(
                        target=target,
                        operation=pre_operation,
                        reservation=reservation,
                        response_capture=BodyCapture.with_limit(
                            _MAX_RESPONSE_CAPTURE_BYTES, keep_tail=True
                        ),
                        response_content_type="application/json",
                        response_status_code=499,
                        persister=app.state.persist_evidence,
                    )
                )
            await release_bulkhead()
            raise
        except TimeoutError:
            if pre_operation is None or reservation is None:
                await release_bulkhead()
                return JSONResponse({"detail": "agent upstream timed out"}, status_code=504)
            error_body = b'{"detail":"agent upstream timed out"}'
            timeout_capture = BodyCapture.with_limit(
                _MAX_RESPONSE_CAPTURE_BYTES,
                keep_tail=True,
            )
            timeout_capture.add(error_body)
            try:
                bundle = await _finalize_evidence(
                    target=target,
                    operation=pre_operation,
                    reservation=reservation,
                    response_capture=timeout_capture,
                    response_content_type="application/json",
                    response_status_code=504,
                    persister=app.state.persist_evidence,
                )
            finally:
                await release_bulkhead()
            error_body = _inline_json_evidence(
                error_body,
                bundle=bundle,
                operation=pre_operation,
            )
            return _response_with_headers(
                error_body,
                status_code=504,
                upstream_headers=httpx.Headers({"Content-Type": "application/json"}),
                extra_headers=_bundle_headers(bundle),
            )
        except httpx.RequestError as exc:
            log.warning("agent ingress upstream failed for %s: %s", target.name, exc)
            operation = pre_operation
            error_body = json.dumps(
                {"detail": "agent upstream unavailable"}, separators=(",", ":")
            ).encode()
            if operation is None or reservation is None:
                await release_bulkhead()
                return JSONResponse({"detail": "agent upstream unavailable"}, status_code=502)
            response_capture = BodyCapture.with_limit(
                _MAX_RESPONSE_CAPTURE_BYTES, keep_tail=True
            )
            response_capture.add(error_body)
            try:
                bundle = await _finalize_evidence(
                    target=target,
                    operation=operation,
                    reservation=reservation,
                    response_capture=response_capture,
                    response_content_type="application/json",
                    response_status_code=502,
                    persister=app.state.persist_evidence,
                )
            finally:
                await release_bulkhead()
            error_body = _inline_json_evidence(
                error_body,
                bundle=bundle,
                operation=operation,
            )
            return _response_with_headers(
                error_body,
                status_code=502,
                upstream_headers=httpx.Headers({"Content-Type": "application/json"}),
                extra_headers=_bundle_headers(bundle),
            )
        except Exception:
            await release_bulkhead()
            raise

        operation = pre_operation
        if operation is None or reservation is None:
            async def passthrough() -> AsyncIterator[bytes]:
                try:
                    async with asyncio.timeout(_UPSTREAM_TOTAL_TIMEOUT_SECONDS):
                        async for chunk in upstream.aiter_raw():
                            yield chunk
                finally:
                    await upstream.aclose()
                    await release_bulkhead()

            return _streaming_response_with_headers(
                passthrough(),
                status_code=upstream.status_code,
                upstream_headers=upstream.headers,
            )

        async def signed_gateway_error(detail: str, *, status_code: int = 502) -> Response:
            error_body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
            error_capture = BodyCapture.with_limit(
                _MAX_RESPONSE_CAPTURE_BYTES,
                keep_tail=True,
            )
            error_capture.add(error_body)
            try:
                bundle = await _finalize_evidence(
                    target=target,
                    operation=operation,
                    reservation=reservation,
                    response_capture=error_capture,
                    response_content_type="application/json",
                    response_status_code=status_code,
                    persister=app.state.persist_evidence,
                )
            finally:
                await release_bulkhead()
            signed_body = _inline_json_evidence(
                error_body,
                bundle=bundle,
                operation=operation,
            )
            return _response_with_headers(
                signed_body,
                status_code=status_code,
                upstream_headers=httpx.Headers({"Content-Type": "application/json"}),
                extra_headers=_bundle_headers(bundle),
            )

        response_capture = BodyCapture.with_limit(
            _MAX_RESPONSE_CAPTURE_BYTES, keep_tail=True
        )
        content_type = upstream.headers.get("content-type", "")
        reservation_headers = _reservation_headers(reservation, target=target)
        is_sse = "text/event-stream" in content_type.lower()
        is_json = "json" in content_type.lower()
        content_encoding = upstream.headers.get("content-encoding", "").strip().lower()
        response_started_monotonic = time.monotonic()
        if (is_json or is_sse) and content_encoding not in {"", "identity"}:
            # The gateway requested identity encoding. Refuse an encoded JSON
            # body rather than forwarding evidence fields it cannot inspect.
            await upstream.aclose()
            return await signed_gateway_error("encoded structured agent response is not supported")
        if is_sse:
            async def evidence_sse() -> AsyncIterator[bytes]:
                buffer = bytearray()
                saw_done = False
                finalized = False
                oversized_frame = False
                timed_out = False
                try:
                    async for chunk in upstream.aiter_raw():
                        response_capture.add(chunk)
                        buffer.extend(chunk)
                        if (
                            time.monotonic() - response_started_monotonic
                            > _UPSTREAM_TOTAL_TIMEOUT_SECONDS
                        ):
                            timed_out = True
                            buffer.clear()
                            break
                        while (frame := _pop_sse_frame(buffer)) is not None:
                            if len(frame) > _MAX_SSE_FRAME_BYTES:
                                oversized_frame = True
                                buffer.clear()
                                break
                            action, safe_frame = _sanitize_sse_frame(frame)
                            if action == "done":
                                saw_done = True
                                continue
                            if action == "forward":
                                yield safe_frame
                        if oversized_frame:
                            break
                        if len(buffer) > _MAX_SSE_FRAME_BYTES:
                            # Never stream an unclassified oversized frame: it
                            # could hide forged evidence across chunk boundaries.
                            oversized_frame = True
                            buffer.clear()
                            break
                    if buffer and not oversized_frame and not timed_out:
                        action, safe_frame = _sanitize_sse_frame(bytes(buffer))
                        if action == "forward":
                            yield safe_frame
                    observed_status = upstream.status_code
                    if oversized_frame or timed_out:
                        observed_status = 504 if timed_out else 502
                        detail = (
                            "agent SSE response timed out"
                            if timed_out
                            else "agent SSE frame exceeds 1 MiB"
                        )
                        error_event = json.dumps(
                            {
                                "type": "error",
                                "status": observed_status,
                                "detail": detail,
                            },
                            separators=(",", ":"),
                        ).encode()
                        yield b"data: " + error_event + b"\n\n"
                    bundle = await _finalize_evidence(
                        target=target,
                        operation=operation,
                        reservation=reservation,
                        response_capture=response_capture,
                        response_content_type=content_type,
                        response_status_code=observed_status,
                        persister=app.state.persist_evidence,
                    )
                    finalized = True
                    event = json.dumps(
                        {"type": "a2a.evidence", "evidence": _evidence_payload(bundle)},
                        separators=(",", ":"),
                    ).encode()
                    yield b"data: " + event + b"\n\n"
                    if saw_done or operation.transport in {"invoke", "mcp"}:
                        yield b"data: [DONE]\n\n"
                except asyncio.CancelledError:
                    if not finalized:
                        await _shield_background_finalizer(
                            _finalize_evidence(
                                target=target,
                                operation=operation,
                                reservation=reservation,
                                response_capture=response_capture,
                                response_content_type=content_type,
                                response_status_code=499,
                                persister=app.state.persist_evidence,
                            )
                        )
                    raise
                except Exception:
                    if not finalized:
                        await _shield_background_finalizer(
                            _finalize_evidence(
                                target=target,
                                operation=operation,
                                reservation=reservation,
                                response_capture=response_capture,
                                response_content_type=content_type,
                                response_status_code=502,
                                persister=app.state.persist_evidence,
                            )
                        )
                    raise
                finally:
                    await upstream.aclose()
                    await release_bulkhead()

            return _streaming_response_with_headers(
                evidence_sse(),
                status_code=upstream.status_code,
                upstream_headers=upstream.headers,
                extra_headers=reservation_headers,
            )

        iterator = upstream.aiter_raw()
        chunks: list[bytes] = []
        total = 0
        exceeded_inline_limit = False
        response_timed_out = False
        try:
            async for chunk in iterator:
                response_capture.add(chunk)
                chunks.append(chunk)
                total += len(chunk)
                if (
                    time.monotonic() - response_started_monotonic
                    > _UPSTREAM_TOTAL_TIMEOUT_SECONDS
                ):
                    response_timed_out = True
                    break
                if total > _MAX_INLINE_RESPONSE_BYTES:
                    exceeded_inline_limit = True
                    break
        except Exception:
            await upstream.aclose()
            await release_bulkhead()
            raise

        if response_timed_out:
            await upstream.aclose()
            return await signed_gateway_error(
                "agent response timed out",
                status_code=504,
            )

        if not exceeded_inline_limit:
            await upstream.aclose()
            body = b"".join(chunks)
            try:
                bundle = await _finalize_evidence(
                    target=target,
                    operation=operation,
                    reservation=reservation,
                    response_capture=response_capture,
                    response_content_type=content_type,
                    response_status_code=upstream.status_code,
                    persister=app.state.persist_evidence,
                )
            finally:
                await release_bulkhead()
            if is_json:
                body = _inline_json_evidence(body, bundle=bundle, operation=operation)
            return _response_with_headers(
                body,
                status_code=upstream.status_code,
                upstream_headers=upstream.headers,
                extra_headers=_bundle_headers(bundle),
            )

        # Headers have not been sent yet. Fail closed for every oversized
        # execution response so the client always receives retrievable signed
        # evidence and no reserved body namespace can bypass sanitization.
        await upstream.aclose()
        return await signed_gateway_error("agent response exceeds 4 MiB")

    return app


app = create_app()


__all__ = [
    "AGENT_INGRESS_GATEWAY_HEALTH_PATH",
    "AGENT_INGRESS_GATEWAY_LIVE_PATH",
    "AGENT_INGRESS_GATEWAY_SERVICE",
    "AgentTarget",
    "BodyCapture",
    "EvidenceBundle",
    "EvidenceOperation",
    "EvidenceReservation",
    "app",
    "create_app",
]
