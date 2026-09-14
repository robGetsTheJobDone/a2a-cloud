from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

_TIMEOUT = object()
_DEFAULT_TOMBSTONE_SECONDS = 60.0
# With an event-driven watch, polling is only a safety net for missed
# notifications, so it can be much slower than the bare poll interval.
_WATCH_FALLBACK_POLL_SECONDS = 2.0


class PendingActionsBackend(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, payload: dict[str, Any], ttl_seconds: float) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def close(self) -> None: ...


class _NullKeyWatch:
    """No-op watch: callers fall back to plain interval polling."""

    async def __aenter__(self) -> "_NullKeyWatch":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def wait(self, timeout: float) -> None:
        await asyncio.sleep(timeout)


class _InMemoryKeyWatch:
    def __init__(self, backend: "_InMemoryPendingActionsBackend", key: str) -> None:
        self._backend = backend
        self._key = key

    async def __aenter__(self) -> "_InMemoryKeyWatch":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def wait(self, timeout: float) -> None:
        event = asyncio.Event()
        async with self._backend._lock:
            self._backend._waiters.setdefault(self._key, []).append(event)
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), timeout)
        finally:
            async with self._backend._lock:
                waiters = self._backend._waiters.get(self._key)
                if waiters is not None:
                    with suppress(ValueError):
                        waiters.remove(event)
                    if not waiters:
                        self._backend._waiters.pop(self._key, None)


class _InMemoryPendingActionsBackend:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float | None, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._waiters: dict[str, list[asyncio.Event]] = {}

    def _notify(self, key: str) -> None:
        for event in self._waiters.get(key, []):
            event.set()

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, payload = item
            if expires_at is not None and time.monotonic() >= expires_at:
                self._values.pop(key, None)
                return None
            return json.loads(json.dumps(payload))

    async def set(self, key: str, payload: dict[str, Any], ttl_seconds: float) -> None:
        async with self._lock:
            expires_at = time.monotonic() + ttl_seconds if ttl_seconds > 0 else None
            self._values[key] = (expires_at, json.loads(json.dumps(payload)))
            self._notify(key)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)
            self._notify(key)

    def watch(self, key: str) -> _InMemoryKeyWatch:
        return _InMemoryKeyWatch(self, key)

    async def close(self) -> None:
        return None


@dataclass
class PendingActionsStore:
    backend: PendingActionsBackend
    prefix: str = "control-plane:pending-actions"
    tombstone_seconds: float = _DEFAULT_TOMBSTONE_SECONDS

    def _key(self, kind: str, request_id: str) -> str:
        return f"{self.prefix}:{kind}:{request_id}"

    async def register(
        self,
        *,
        kind: str,
        request_id: str,
        user_id: int,
        ttl_seconds: float,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        key = self._key(kind, request_id)
        current = await self.backend.get(key)
        if current is not None:
            return current
        record = {
            "kind": kind,
            "request_id": request_id,
            "user_id": user_id,
            "state": "pending",
            "payload": payload or {},
            "response": None,
            "created_at": time.time(),
            "ttl_seconds": ttl_seconds,
        }
        await self.backend.set(key, record, ttl_seconds)
        return record

    async def resolve(
        self,
        *,
        kind: str,
        request_id: str,
        user_id: int,
        response: Any,
        ttl_seconds: float,
    ) -> bool:
        key = self._key(kind, request_id)
        current = await self.backend.get(key)
        if current is not None:
            if current.get("kind") != kind or current.get("user_id") != user_id:
                return False
            if current.get("state") == "timed_out":
                return False
        record = current or {
            "kind": kind,
            "request_id": request_id,
            "user_id": user_id,
            "payload": {},
            "created_at": time.time(),
            "ttl_seconds": ttl_seconds,
        }
        record["state"] = "resolved"
        record["response"] = response
        record["ttl_seconds"] = ttl_seconds
        await self.backend.set(key, record, ttl_seconds)
        return True

    async def mark_timed_out(
        self,
        *,
        kind: str,
        request_id: str,
        user_id: int,
    ) -> None:
        key = self._key(kind, request_id)
        current = await self.backend.get(key)
        if current is None:
            return
        if current.get("kind") != kind or current.get("user_id") != user_id:
            return
        current["state"] = "timed_out"
        current["response"] = None
        await self.backend.set(key, current, self.tombstone_seconds)

    async def wait(
        self,
        *,
        kind: str,
        request_id: str,
        user_id: int,
        timeout_seconds: float,
        ttl_seconds: float,
        payload: dict[str, Any] | None = None,
        poll_interval: float = 0.25,
    ) -> Any:
        key = self._key(kind, request_id)
        current = await self.register(
            kind=kind,
            request_id=request_id,
            user_id=user_id,
            ttl_seconds=ttl_seconds,
            payload=payload,
        )
        if current is not None and current.get("state") == "resolved":
            return current.get("response")
        deadline = time.monotonic() + timeout_seconds
        # Backends with a watch() wake this loop the moment the key changes;
        # the poll then only guards against missed notifications. Without
        # watch(), the loop degrades to the original interval polling.
        watch_factory = getattr(self.backend, "watch", None)
        if watch_factory is not None:
            watch_ctx = watch_factory(key)
            step = max(poll_interval, _WATCH_FALLBACK_POLL_SECONDS)
        else:
            watch_ctx = _NullKeyWatch()
            step = poll_interval
        async with watch_ctx as watch:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self.mark_timed_out(
                        kind=kind,
                        request_id=request_id,
                        user_id=user_id,
                    )
                    return _TIMEOUT
                current = await self.backend.get(key)
                if current is not None and current.get("kind") == kind and current.get("user_id") == user_id:
                    state = current.get("state")
                    if state == "resolved":
                        return current.get("response")
                    if state == "timed_out":
                        return _TIMEOUT
                await watch.wait(min(step, remaining))

    async def delete(self, *, kind: str, request_id: str) -> None:
        await self.backend.delete(self._key(kind, request_id))

    async def close(self) -> None:
        await self.backend.close()
class _RedisKeyWatch:
    def __init__(self, client: Any, key: str) -> None:
        self._client = client
        self._channel = _notify_channel(key)
        self._pubsub: Any | None = None

    async def __aenter__(self) -> "_RedisKeyWatch":
        try:
            pubsub = self._client.pubsub()
            await pubsub.subscribe(self._channel)
            self._pubsub = pubsub
        except Exception:  # noqa: BLE001
            log.warning("pending-actions watch subscribe failed", exc_info=True)
            self._pubsub = None
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pubsub is not None:
            with suppress(Exception):
                await self._pubsub.unsubscribe(self._channel)
            with suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None

    async def wait(self, timeout: float) -> None:
        if self._pubsub is None:
            await asyncio.sleep(timeout)
            return
        try:
            await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=timeout
            )
        except Exception:  # noqa: BLE001
            log.warning("pending-actions watch wait failed", exc_info=True)
            await self.__aexit__()
            await asyncio.sleep(timeout)


def _notify_channel(key: str) -> str:
    return f"{key}::notify"


class RedisPendingActionsBackend:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self._client.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def set(self, key: str, payload: dict[str, Any], ttl_seconds: float) -> None:
        await self._client.set(
            key,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            ex=max(1, int(ttl_seconds)),
        )
        with suppress(Exception):
            await self._client.publish(_notify_channel(key), "1")

    async def delete(self, key: str) -> None:
        await self._client.delete(key)
        with suppress(Exception):
            await self._client.publish(_notify_channel(key), "1")

    def watch(self, key: str) -> _RedisKeyWatch:
        return _RedisKeyWatch(self._client, key)

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None)
        if callable(close):
            await close()
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            maybe = close()
            if asyncio.iscoroutine(maybe):
                await maybe


def create_pending_actions_store(redis_url: str | None) -> PendingActionsStore:
    if redis_url:
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

            client = redis_asyncio.from_url(redis_url, decode_responses=True)
            return PendingActionsStore(RedisPendingActionsBackend(client))
        except Exception:  # noqa: BLE001
            log.exception("failed to initialize Redis pending-actions backend; using in-memory fallback")
    return PendingActionsStore(_InMemoryPendingActionsBackend())


TIMEOUT = _TIMEOUT
