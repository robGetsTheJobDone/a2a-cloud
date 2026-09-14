from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from .thread_events import append_thread_event

DEFAULT_SSE_HEARTBEAT_SECONDS = 15.0
DEFAULT_REDIS_EVENT_TTL_SECONDS = 60 * 60
DEFAULT_PERSIST_BATCH_EVENTS = 25
DEFAULT_PERSIST_BATCH_WINDOW_SECONDS = 0.1
_SSE_HEARTBEAT = object()
log = logging.getLogger(__name__)


def _sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _sse_comment(comment: str = "ping") -> bytes:
    return f": {comment}\n\n".encode()


async def _queue_get_or_heartbeat(
    queue: asyncio.Queue[dict[str, Any] | None],
    timeout: float = DEFAULT_SSE_HEARTBEAT_SECONDS,
) -> dict[str, Any] | None | object:
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return _SSE_HEARTBEAT


class ChatRunStream:
    """Run-scoped live event stream with async replay persistence.

    The stream's first job is to keep the browser hot: publish to the SSE
    queue immediately, then persist replay events on a separate queue. The
    class also owns wire-level compatibility behavior, so orchestrator code
    publishes plain chat events instead of formatting SSE chunks directly.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        thread_id: str,
        user_id: int,
        job_id: str | None,
        db_lock: asyncio.Lock,
        redis_url: str | None = None,
        redis_client: Any | None = None,
        heartbeat_seconds: float = DEFAULT_SSE_HEARTBEAT_SECONDS,
        persist_batch_events: int = DEFAULT_PERSIST_BATCH_EVENTS,
        persist_batch_window_seconds: float = DEFAULT_PERSIST_BATCH_WINDOW_SECONDS,
    ) -> None:
        self.session = session
        self.thread_id = thread_id
        self.user_id = user_id
        self.job_id = job_id
        self.db_lock = db_lock
        self.redis_url = redis_url
        self._redis_client = redis_client
        self._owns_redis_client = redis_client is None
        self._redis_pubsub: Any | None = None
        self._redis_listener_task: asyncio.Task[None] | None = None
        self._redis_enabled = False
        self.heartbeat_seconds = heartbeat_seconds
        self.persist_batch_events = max(1, persist_batch_events)
        self.persist_batch_window_seconds = max(0.0, persist_batch_window_seconds)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._persist_queue: asyncio.Queue[list[dict[str, Any]] | None] = (
            asyncio.Queue()
        )
        self._persist_task: asyncio.Task[None] | None = None
        self._last_event_seq: int | None = None
        self._wire_seq = 0
        self._sent_delta = False
        self._closed = False
        stream_key = job_id or thread_id
        self.channel = f"control-plane:chat-runs:{stream_key}:events"
        self._seq_key = f"control-plane:chat-runs:{stream_key}:seq"

    async def start(self) -> None:
        if self._persist_task is None:
            self._persist_task = asyncio.create_task(self._persist_events())
        await self._start_redis_feed()

    async def publish(
        self,
        event: dict[str, Any],
        *,
        persist: bool = True,
    ) -> None:
        await self.publish_many([event], persist=persist)

    async def publish_many(
        self,
        events: list[dict[str, Any]],
        *,
        persist: bool = True,
    ) -> None:
        if not events:
            return
        for event in events:
            wire_event = await self._wire_event(event)
            if self._redis_enabled:
                await self._publish_redis(wire_event)
            else:
                await self._queue.put(wire_event)
        if persist:
            await self._persist_queue.put([dict(event) for event in events])

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._redis_enabled:
            await self._publish_redis({"type": "__close__"})
        else:
            await self._queue.put(None)
        await self._persist_queue.put(None)
        if self._persist_task is not None:
            await self._persist_task

    async def iter_sse(self) -> AsyncIterator[bytes]:
        try:
            while True:
                ev = await _queue_get_or_heartbeat(
                    self._queue,
                    timeout=self.heartbeat_seconds,
                )
                if ev is _SSE_HEARTBEAT:
                    yield _sse_comment()
                    continue
                if ev is None:
                    break
                if ev.get("type") == "delta":
                    self._sent_delta = True
                    yield _sse(ev)
                    continue
                if ev.get("type") == "final":
                    if not self._sent_delta:
                        yield _sse({"choices": [{"delta": {"content": ev["content"]}}]})
                    yield _sse(ev)
                    continue
                yield _sse(ev)
            yield b"data: [DONE]\n\n"
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._redis_listener_task is not None:
            self._redis_listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._redis_listener_task
            self._redis_listener_task = None
        if self._redis_pubsub is not None:
            with suppress(Exception):
                await self._redis_pubsub.unsubscribe(self.channel)
            with suppress(Exception):
                await self._redis_pubsub.aclose()
            self._redis_pubsub = None
        if self._redis_client is not None and self._owns_redis_client:
            await _close_redis_client(self._redis_client)
            self._redis_client = None

    async def _wire_event(self, event: dict[str, Any]) -> dict[str, Any]:
        out = dict(event)
        out.setdefault("thread_id", self.thread_id)
        if self.job_id:
            out.setdefault("job_id", self.job_id)
        out.setdefault("stream_seq", await self._next_wire_seq())
        return out

    async def _next_wire_seq(self) -> int:
        if self._redis_enabled and self._redis_client is not None:
            try:
                seq = int(await self._redis_client.incr(self._seq_key))
                if seq == 1:
                    with suppress(Exception):
                        await self._redis_client.expire(
                            self._seq_key,
                            DEFAULT_REDIS_EVENT_TTL_SECONDS,
                        )
                return seq
            except Exception:  # noqa: BLE001
                log.warning("chat stream redis seq failed; falling back", exc_info=True)
                self._redis_enabled = False
        self._wire_seq += 1
        return self._wire_seq

    async def _start_redis_feed(self) -> None:
        if self._redis_client is None and not self.redis_url:
            return
        try:
            if self._redis_client is None:
                self._redis_client = _redis_client(self.redis_url)
            if self._redis_client is None:
                return
            self._redis_pubsub = self._redis_client.pubsub()
            await self._redis_pubsub.subscribe(self.channel)
            self._redis_enabled = True
            self._redis_listener_task = asyncio.create_task(self._redis_listener())
        except Exception:  # noqa: BLE001
            log.warning("chat stream redis subscribe failed; using in-process queue", exc_info=True)
            self._redis_enabled = False
            if self._redis_pubsub is not None:
                with suppress(Exception):
                    await self._redis_pubsub.aclose()
                self._redis_pubsub = None
            if self._redis_client is not None and self._owns_redis_client:
                await _close_redis_client(self._redis_client)
                self._redis_client = None

    async def _publish_redis(self, event: dict[str, Any]) -> None:
        if self._redis_client is None:
            await self._queue.put(None if event.get("type") == "__close__" else event)
            self._redis_enabled = False
            return
        try:
            await self._redis_client.publish(
                self.channel,
                json.dumps(event, separators=(",", ":"), ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001
            log.warning("chat stream redis publish failed; using in-process queue", exc_info=True)
            self._redis_enabled = False
            await self._queue.put(None if event.get("type") == "__close__" else event)

    async def _redis_listener(self) -> None:
        if self._redis_pubsub is None:
            return
        try:
            while True:
                message = await self._redis_pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    if self._closed:
                        await self._queue.put(None)
                        return
                    continue
                data = message.get("data") if isinstance(message, dict) else None
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                if not isinstance(data, str):
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "__close__":
                    await self._queue.put(None)
                    return
                await self._queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("chat stream redis listener failed", exc_info=True)
            await self._queue.put({
                "type": "error",
                "message": "chat stream redis listener failed",
            })
            await self._queue.put(None)

    async def _persist_events(self) -> None:
        while True:
            batch = await self._persist_queue.get()
            if batch is None:
                return
            events = list(batch)
            deadline = (
                asyncio.get_running_loop().time()
                + self.persist_batch_window_seconds
            )
            while len(events) < self.persist_batch_events:
                timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                if timeout <= 0:
                    break
                try:
                    next_batch = await asyncio.wait_for(
                        self._persist_queue.get(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    break
                if next_batch is None:
                    await self._persist_batch(events)
                    return
                events.extend(next_batch)
            await self._persist_batch(events)

    async def _persist_batch(self, events: list[dict[str, Any]]) -> None:
        async with self.db_lock:
            for event in events:
                try:
                    row = await append_thread_event(
                        self.session,
                        thread_id=self.thread_id,
                        user_id=self.user_id,
                        event=event,
                        seq_hint=self._last_event_seq,
                    )
                    self._last_event_seq = int(row.seq)
                except Exception:  # noqa: BLE001
                    self._last_event_seq = None
                    with suppress(Exception):
                        await self.session.rollback()


def _redis_client(redis_url: str | None) -> Any | None:
    if not redis_url:
        return None
    import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

    return redis_asyncio.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=5.0,
    )


async def _close_redis_client(client: Any) -> None:
    close = getattr(client, "aclose", None)
    if callable(close):
        await close()
        return
    close = getattr(client, "close", None)
    if callable(close):
        maybe = close()
        if asyncio.iscoroutine(maybe):
            await maybe
