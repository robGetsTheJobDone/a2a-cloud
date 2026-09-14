from __future__ import annotations

import asyncio

import pytest

from a2a_pack.serve.asgi import (
    SSE_HEARTBEAT,
    queue_get_or_heartbeat,
    sse_comment,
)


def test_sse_comment_is_wire_compatible_keepalive() -> None:
    assert sse_comment() == b": ping\n\n"


@pytest.mark.asyncio
async def test_queue_get_or_heartbeat_returns_event_before_timeout() -> None:
    q: asyncio.Queue[dict | None] = asyncio.Queue()
    await q.put({"type": "result"})

    assert await queue_get_or_heartbeat(q, timeout=0.01) == {"type": "result"}


@pytest.mark.asyncio
async def test_queue_get_or_heartbeat_returns_heartbeat_on_idle() -> None:
    q: asyncio.Queue[dict | None] = asyncio.Queue()

    assert await queue_get_or_heartbeat(q, timeout=0.001) is SSE_HEARTBEAT
