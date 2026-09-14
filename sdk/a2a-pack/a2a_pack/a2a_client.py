"""Agent-to-agent invocation surface available via ``ctx.call(...)``.

An agent never speaks raw HTTP to another agent. It calls
``ctx.call(target, skill, args, grant=...)`` and the runtime-attached
:class:`A2AClient` handles transport: HTTP for cross-pod, in-memory for
local tests, anything else (gRPC, message bus) for future runtimes.

The grant token (see :mod:`a2a_pack.grants`) is the *only* way to hand
workspace access across agents. Callee-side runtime validates it before
materializing a :class:`WorkspaceClient`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import A2AAgent
    from .context import RunContext


@dataclass(frozen=True)
class CallResult:
    """What an A2A invocation returns to the calling skill."""

    result: Any
    events: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    grant_id: str | None = None  # echoed for audit


class A2AError(RuntimeError):
    """A cross-agent call returned an HTTP error.

    Carries the status code and the parsed ``detail`` (a dict when the control
    plane returns a structured error, else the raw text) so callers can branch
    on it instead of string-matching. Subclasses cover well-known cases.
    """

    def __init__(
        self,
        url: str,
        status_code: int,
        detail: Any = None,
        *,
        body: str | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.detail = detail
        self.body = body
        reason = ""
        if isinstance(detail, dict):
            reason = str(detail.get("message") or detail.get("reason") or "")
        super().__init__(
            f"a2a {url} -> {status_code}: {reason or body or detail or ''}".rstrip(": ")
        )


def _raise_for_a2a(url: str, status_code: int, body: str) -> None:
    """Turn a >=400 agent response into the most specific A2AError available."""
    import json

    detail: Any = body
    try:
        parsed = json.loads(body)
        # FastAPI wraps errors as {"detail": ...}; unwrap when present.
        detail = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
    except (ValueError, TypeError):
        pass
    raise A2AError(url, status_code, detail, body=body)


class A2AClient(ABC):
    """Transport-shaped agent-to-agent client."""

    @abstractmethod
    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        cp_jwt: str | None = None,
        cp_url: str | None = None,
        llm_creds: dict[str, Any] | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        timeout: float | None = None,
        composition: dict[str, Any] | None = None,
    ) -> CallResult:
        """Invoke ``skill`` on ``target`` and return its :class:`CallResult`.

        ``target`` is opaque to this layer — for the HTTP impl it's an agent
        URL; for the in-memory impl it's an agent name.
        """


# ---------------------------------------------------------------------------
# In-memory: routes calls to A2AAgent instances in the same process. Useful
# for the demo + tests.
# ---------------------------------------------------------------------------


@dataclass
class InMemoryA2AClient(A2AClient):
    """Routes calls to agent instances registered by name.

    The receiving agent gets a *new* :class:`RunContext` built by the
    ``ctx_factory`` callable, so caller and callee don't share state.
    Pass ``ctx_factory=lambda agent, grant: ...`` to control how scoped
    workspaces / sandboxes are wired in.
    """

    agents: dict[str, "A2AAgent"]
    ctx_factory: Any = None  # Callable[[A2AAgent, str | None], RunContext]

    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        cp_jwt: str | None = None,
        cp_url: str | None = None,
        llm_creds: dict[str, Any] | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        timeout: float | None = None,
        composition: dict[str, Any] | None = None,
    ) -> CallResult:
        if target not in self.agents:
            raise KeyError(f"no agent registered: {target!r}")
        agent = self.agents[target]
        ctx = self.ctx_factory(agent, grant) if self.ctx_factory else None
        if ctx is None:
            from .context import LocalRunContext
            from .auth import NoAuth

            ctx = LocalRunContext(auth=NoAuth(), task_id=f"a2a-{target}")
        if cp_jwt:
            setattr(ctx, "_cp_jwt", cp_jwt)
        if cp_url:
            setattr(ctx, "_cp_url", cp_url)
        _merge_consumer_setup(
            ctx,
            consumer_config=consumer_config,
            consumer_secrets=consumer_secrets,
        )
        if llm_creds:
            from .context import LLMCreds

            setattr(ctx, "_llm_creds", LLMCreds(
                base_url=str(llm_creds.get("base_url") or ""),
                api_key=str(llm_creds.get("api_key") or ""),
                model=str(llm_creds.get("model") or ""),
                source=str(llm_creds.get("source") or "caller"),
                temperature_mode=str(llm_creds.get("temperature_mode") or "default"),
                temperature=(
                    float(llm_creds["temperature"])
                    if llm_creds.get("temperature") is not None else None
                ),
                extra_body=dict(llm_creds.get("extra_body") or {}),
            ))
        if composition:
            from .composition import ensure_composition_budget

            setattr(
                ctx,
                "_composition_budget",
                ensure_composition_budget(
                    composition,
                    current_agent=getattr(type(agent), "name", target),
                ),
            )
        result = await agent.invoke_json(skill, ctx, args or {})
        events = tuple(
            {"kind": e.kind, "payload": e.payload}
            for e in getattr(ctx, "events", ())
        )
        # surface artifacts captured by LocalRunContext, if present
        artifacts: tuple[dict[str, Any], ...] = ()
        local_arts = getattr(ctx, "artifacts", None)
        if isinstance(local_arts, dict):
            artifacts = tuple(
                {"name": name, "size_bytes": len(data)}
                for name, data in local_arts.items()
            )
        return CallResult(
            result=result,
            events=events,
            artifacts=artifacts,
            grant_id=_grant_id_or_none(grant),
        )


# ---------------------------------------------------------------------------
# HTTP: posts to <target>/invoke/<skill> with {arguments, grant} body.
# ---------------------------------------------------------------------------


@dataclass
class HttpA2AClient(A2AClient):
    """A2A client that POSTs to the standard /invoke/{skill} endpoint.

    ``target`` may be a concrete URL or a platform registry name. Hosted
    runtimes attach discovery so meta-agents can use stable agent names while
    the transport still calls the live Agent Card URL.
    """

    default_timeout: float = 60.0
    discovery: Any | None = None
    _resolved_urls: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    async def _resolve_target(self, target: str) -> str:
        clean = target.rstrip("/")
        if clean.startswith(("http://", "https://")):
            return clean
        if clean in self._resolved_urls:
            return self._resolved_urls[clean]
        if self.discovery is None:
            raise RuntimeError(
                f"a2a target {target!r} is not a URL and no discovery client is attached"
            )
        discovered = await self.discovery.get_agent(clean)
        url = str(getattr(discovered, "url", "") or "").rstrip("/")
        if not url:
            raise RuntimeError(f"a2a target {target!r} resolved without a URL")
        self._resolved_urls[clean] = url
        return url

    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        cp_jwt: str | None = None,
        cp_url: str | None = None,
        llm_creds: dict[str, Any] | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        timeout: float | None = None,
        composition: dict[str, Any] | None = None,
    ) -> CallResult:
        import httpx  # late import: server-side needs no client

        body: dict[str, Any] = {"arguments": args or {}}
        if grant is not None:
            body["grant"] = grant
        if cp_jwt:
            body["cp_jwt"] = cp_jwt
        if cp_url:
            body["cp_url"] = cp_url
        if llm_creds:
            body["llm_creds"] = llm_creds
        if consumer_config:
            body["consumer_config"] = consumer_config
        if consumer_secrets:
            body["consumer_secrets"] = consumer_secrets
        if composition:
            body["composition"] = composition
        headers = {"content-type": "application/json"}
        if cp_jwt:
            headers["authorization"] = f"bearer {cp_jwt}"
        base_url = await self._resolve_target(target)
        url = f"{base_url}/invoke/{skill}"
        async with httpx.AsyncClient(timeout=timeout or self.default_timeout) as c:
            resp = await c.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            _raise_for_a2a(url, resp.status_code, resp.text)
        data = resp.json()
        return CallResult(
            result=data.get("result"),
            events=tuple(data.get("events") or ()),
            artifacts=tuple(data.get("artifacts") or ()),
            grant_id=_grant_id_or_none(grant),
        )


def _grant_id_or_none(grant: str | None) -> str | None:
    """Extract grant_id without re-validating the signature (audit only)."""
    if not grant or "." not in grant:
        return None
    try:
        from .grants import _b64decode

        payload = _b64decode(grant.rsplit(".", 1)[0])
        import json

        return json.loads(payload).get("grant_id")
    except Exception:  # noqa: BLE001
        return None


def _merge_consumer_setup(
    ctx: Any,
    *,
    consumer_config: dict[str, Any] | None,
    consumer_secrets: dict[str, str] | None,
) -> None:
    if consumer_config:
        current = getattr(ctx, "_consumer_config", {})
        if not isinstance(current, dict):
            current = {}
        setattr(ctx, "_consumer_config", {**current, **dict(consumer_config)})
    if consumer_secrets:
        current = getattr(ctx, "_consumer_secrets", {})
        if not isinstance(current, dict):
            current = {}
        setattr(
            ctx,
            "_consumer_secrets",
            {
                **current,
                **{str(key): str(value) for key, value in consumer_secrets.items()},
            },
        )


__all__ = [
    "A2AClient",
    "CallResult",
    "HttpA2AClient",
    "InMemoryA2AClient",
]
