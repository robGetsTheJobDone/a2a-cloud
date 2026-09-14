from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.chat_stream import ChatRunStream
from control_plane.db import Base
from control_plane.models import ChatThread, ChatThreadEvent, User


class FakeRedis:
    def __init__(self) -> None:
        self.channels: dict[str, list[asyncio.Queue[dict[str, str]]]] = {}
        self.values: dict[str, int] = {}
        self.published = 0

    def pubsub(self) -> "FakePubSub":
        return FakePubSub(self)

    async def publish(self, channel: str, data: str) -> None:
        self.published += 1
        for queue in list(self.channels.get(channel, [])):
            await queue.put({"type": "message", "channel": channel, "data": data})

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, _key: str, _seconds: int) -> None:
        return None


class FakePubSub:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.channel: str | None = None
        self.queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.client.channels.setdefault(channel, []).append(self.queue)

    async def unsubscribe(self, channel: str) -> None:
        queues = self.client.channels.get(channel, [])
        if self.queue in queues:
            queues.remove(self.queue)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,
    ) -> dict[str, str] | None:
        del ignore_subscribe_messages
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def aclose(self) -> None:
        if self.channel is not None:
            await self.unsubscribe(self.channel)


@pytest.mark.asyncio
async def test_chat_run_stream_keeps_deltas_live_and_persists_replay_events() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            session.add(User(id=1, email="stream@example.com", password_hash="x"))
            session.add(ChatThread(id="thread-1", user_id=1, title="Stream"))
            await session.commit()

            stream = ChatRunStream(
                session=session,
                thread_id="thread-1",
                user_id=1,
                job_id="job-1",
                db_lock=asyncio.Lock(),
            )
            await stream.start()

            await stream.publish({"type": "delta", "content": "hel"}, persist=False)
            await stream.publish({"type": "delta", "content": "lo"}, persist=False)
            await stream.publish({"type": "final", "content": "hello"})
            await stream.close()

            chunks: list[bytes] = []
            async for chunk in stream.iter_sse():
                chunks.append(chunk)
            body = b"".join(chunks).decode()

            assert '"type": "delta"' in body
            assert '"stream_seq": 1' in body
            assert '"stream_seq": 3' in body
            assert '"choices"' not in body
            assert body.endswith("data: [DONE]\n\n")

            rows = (
                await session.execute(
                    select(ChatThreadEvent).order_by(ChatThreadEvent.seq.asc())
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].event_type == "final"
            assert rows[0].payload == {"type": "final", "content": "hello"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chat_run_stream_uses_redis_pubsub_for_live_events() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            session.add(User(id=1, email="redis-stream@example.com", password_hash="x"))
            session.add(ChatThread(id="thread-redis", user_id=1, title="Redis"))
            await session.commit()

            fake_redis = FakeRedis()
            stream = ChatRunStream(
                session=session,
                thread_id="thread-redis",
                user_id=1,
                job_id="job-redis",
                db_lock=asyncio.Lock(),
                redis_client=fake_redis,
            )
            await stream.start()

            await stream.publish({"type": "delta", "content": "hi"}, persist=False)
            await stream.publish({"type": "final", "content": "hi"})
            await stream.close()

            chunks: list[bytes] = []
            async for chunk in stream.iter_sse():
                chunks.append(chunk)
            body = b"".join(chunks).decode()

            assert fake_redis.published == 3
            assert stream.channel == "control-plane:chat-runs:job-redis:events"
            assert '"type": "delta"' in body
            assert '"type": "final"' in body
            assert '"stream_seq": 1' in body
            assert '"stream_seq": 2' in body

            rows = (
                await session.execute(
                    select(ChatThreadEvent).order_by(ChatThreadEvent.seq.asc())
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].event_type == "final"
    finally:
        await engine.dispose()
