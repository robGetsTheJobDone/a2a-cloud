from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .config import settings

_KEY_PREFIX = "control-plane:cli-session-exchange"
_CODE_BYTES = 32


class AuthExchangeUnavailable(RuntimeError):
    """The shared exchange-code store could not be reached."""


@dataclass(frozen=True)
class CliSessionExchange:
    user_id: int
    redirect_to: str


class CliSessionExchangeStore(Protocol):
    async def issue(
        self,
        *,
        user_id: int,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str: ...

    async def consume(self, code: str) -> CliSessionExchange | None: ...


def _key(code: str) -> str:
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


def _encode(exchange: CliSessionExchange) -> str:
    return json.dumps(
        {"user_id": exchange.user_id, "redirect_to": exchange.redirect_to},
        separators=(",", ":"),
    )


def _decode(raw: str | bytes) -> CliSessionExchange | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(raw)
        user_id = payload["user_id"]
        redirect_to = payload["redirect_to"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return None
    if not isinstance(redirect_to, str) or not redirect_to.startswith("/"):
        return None
    return CliSessionExchange(user_id=user_id, redirect_to=redirect_to)


class RedisCliSessionExchangeStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def issue(
        self,
        *,
        user_id: int,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str:
        payload = _encode(CliSessionExchange(user_id=user_id, redirect_to=redirect_to))
        try:
            for _ in range(3):
                code = secrets.token_urlsafe(_CODE_BYTES)
                created = await self._client.set(
                    _key(code), payload, ex=max(1, ttl_seconds), nx=True
                )
                if created:
                    return code
        except Exception as exc:  # noqa: BLE001 - normalize Redis client failures
            raise AuthExchangeUnavailable from exc
        raise AuthExchangeUnavailable("could not allocate a unique exchange code")

    async def consume(self, code: str) -> CliSessionExchange | None:
        try:
            raw = await self._client.getdel(_key(code))
        except Exception as exc:  # noqa: BLE001 - normalize Redis client failures
            raise AuthExchangeUnavailable from exc
        return None if raw is None else _decode(raw)


class InMemoryCliSessionExchangeStore:
    """Process-local backend for tests and Redis-disabled development."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    async def issue(
        self,
        *,
        user_id: int,
        redirect_to: str,
        ttl_seconds: int,
    ) -> str:
        payload = _encode(CliSessionExchange(user_id=user_id, redirect_to=redirect_to))
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

    async def consume(self, code: str) -> CliSessionExchange | None:
        async with self._lock:
            item = self._values.pop(_key(code), None)
            if item is None:
                return None
            expires_at, raw = item
            if time.monotonic() >= expires_at:
                return None
            return _decode(raw)


_in_memory_store = InMemoryCliSessionExchangeStore()


async def get_cli_session_exchange_store() -> AsyncIterator[CliSessionExchangeStore]:
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
        yield RedisCliSessionExchangeStore(client)
    finally:
        await client.aclose()
