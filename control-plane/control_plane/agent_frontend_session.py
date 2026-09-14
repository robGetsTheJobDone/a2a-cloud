"""Origin-bound browser sessions for hosted agent frontends.

Hosted agents live on user-controlled ``<name>.a2acloud.io`` origins, so they
must never see the dashboard's ``__Host-`` platform session cookie: agent code
could read it and act as the user across the whole platform. That is why
``settings.allow_platform_frontend_auth`` stayed off and packed UIs could
render but never invoke.

This module is the gateway that closes the gap. The dashboard origin mints a
single-use code bound to one agent; the agent exchanges it server-side for an
*agent-scoped* token that carries ``aud="agent:<name>"``. The scoped token
proves "this user is present at this agent" and nothing more:
``decode_token`` rejects it, so it is useless against the rest of the API.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

import jwt

from .config import settings

_KEY_PREFIX = "control-plane:agent-session-exchange"
_CODE_BYTES = 32

#: ``typ`` claim marking a token as agent-scoped. Platform authentication
#: rejects this value, which is what keeps the scoped token from being replayed
#: against ``/v1/agents``, files, or any other user-wide endpoint.
AGENT_SESSION_TOKEN_TYPE = "agent_session"


class AgentSessionExchangeUnavailable(RuntimeError):
    """The shared exchange-code store could not be reached."""


@dataclass(frozen=True)
class AgentSessionExchange:
    user_id: int
    agent: str
    redirect_to: str


def agent_token_audience(agent: str) -> str:
    return f"agent:{agent}"


def mint_agent_session_token(
    *, user_id: int, agent: str, ttl_seconds: int
) -> tuple[str, int]:
    """Return ``(token, expires_at)`` for one user at one agent origin."""
    now = int(time.time())
    expires_at = now + max(1, ttl_seconds)
    payload = {
        "sub": str(user_id),
        "aud": agent_token_audience(agent),
        "typ": AGENT_SESSION_TOKEN_TYPE,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)
    return token, expires_at


def decode_agent_session_token(token: str, *, agent: str) -> int | None:
    """Return the user id iff ``token`` is a live session for exactly ``agent``.

    The audience is supplied by the caller rather than read from the token, so
    a token minted for one agent can never satisfy another agent's check.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
            audience=agent_token_audience(agent),
        )
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != AGENT_SESSION_TOKEN_TYPE:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def _key(code: str) -> str:
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


def _encode(exchange: AgentSessionExchange) -> str:
    return json.dumps(
        {
            "user_id": exchange.user_id,
            "agent": exchange.agent,
            "redirect_to": exchange.redirect_to,
        },
        separators=(",", ":"),
    )


def _decode(raw: str | bytes) -> AgentSessionExchange | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
        user_id = payload["user_id"]
        agent = payload["agent"]
        redirect_to = payload["redirect_to"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    if not isinstance(agent, str) or not agent:
        return None
    if not isinstance(redirect_to, str):
        return None
    return AgentSessionExchange(user_id=user_id, agent=agent, redirect_to=redirect_to)


class AgentSessionExchangeStore(Protocol):
    async def issue(
        self,
        *,
        user_id: int,
        agent: str,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str: ...

    async def consume(self, code: str) -> AgentSessionExchange | None: ...


class RedisAgentSessionExchangeStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def issue(
        self,
        *,
        user_id: int,
        agent: str,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str:
        payload = _encode(
            AgentSessionExchange(user_id=user_id, agent=agent, redirect_to=redirect_to)
        )
        try:
            for _ in range(3):
                code = secrets.token_urlsafe(_CODE_BYTES)
                created = await self._client.set(
                    _key(code), payload, ex=max(1, ttl_seconds), nx=True
                )
                if created:
                    return code
        except Exception as exc:  # noqa: BLE001 - normalize Redis client failures
            raise AgentSessionExchangeUnavailable from exc
        raise AgentSessionExchangeUnavailable("could not allocate a unique code")

    async def consume(self, code: str) -> AgentSessionExchange | None:
        # GETDEL is what makes the code single-use even if a redirect is
        # replayed or two tabs race the same URL.
        try:
            raw = await self._client.getdel(_key(code))
        except Exception as exc:  # noqa: BLE001 - normalize Redis client failures
            raise AgentSessionExchangeUnavailable from exc
        return None if raw is None else _decode(raw)


class InMemoryAgentSessionExchangeStore:
    """Process-local backend for tests and Redis-disabled development."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        user_id: int,
        agent: str,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str:
        payload = _encode(
            AgentSessionExchange(user_id=user_id, agent=agent, redirect_to=redirect_to)
        )
        async with self._lock:
            now = time.monotonic()
            self._values = {
                key: value for key, value in self._values.items() if value[0] > now
            }
            while True:
                code = secrets.token_urlsafe(_CODE_BYTES)
                key = _key(code)
                if key not in self._values:
                    self._values[key] = (now + max(1, ttl_seconds), payload)
                    return code

    async def consume(self, code: str) -> AgentSessionExchange | None:
        async with self._lock:
            item = self._values.pop(_key(code), None)
            if item is None:
                return None
            expires_at, raw = item
            if time.monotonic() >= expires_at:
                return None
            return _decode(raw)


_in_memory_store = InMemoryAgentSessionExchangeStore()


async def get_agent_session_exchange_store() -> AsyncIterator[AgentSessionExchangeStore]:
    if not settings.redis_url:
        yield _in_memory_store
        return

    import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

    client = redis_asyncio.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        yield RedisAgentSessionExchangeStore(client)
    finally:
        await client.aclose()
