from __future__ import annotations

import asyncio

import pytest

from control_plane.chat_stream import (
    _SSE_HEARTBEAT,
    _queue_get_or_heartbeat,
    _sse_comment,
)
from control_plane.routes.chat import (
    _cancel_and_await,
    _error_event,
)


def test_sse_comment_is_ignored_by_event_data_parsers() -> None:
    assert _sse_comment() == b": ping\n\n"


def test_error_event_includes_machine_readable_fields() -> None:
    event = _error_event(RuntimeError("boom"), phase="orchestrator")

    assert event == {
        "type": "error",
        "phase": "orchestrator",
        "error_type": "RuntimeError",
        "retryable": True,
        "message": "RuntimeError: boom",
    }


@pytest.mark.asyncio
async def test_queue_get_or_heartbeat_returns_event_before_timeout() -> None:
    q: asyncio.Queue[dict | None] = asyncio.Queue()
    await q.put({"type": "agent_progress"})

    assert await _queue_get_or_heartbeat(q, timeout=0.01) == {
        "type": "agent_progress",
    }


@pytest.mark.asyncio
async def test_queue_get_or_heartbeat_returns_heartbeat_on_idle() -> None:
    q: asyncio.Queue[dict | None] = asyncio.Queue()

    assert await _queue_get_or_heartbeat(q, timeout=0.001) is _SSE_HEARTBEAT


@pytest.mark.asyncio
async def test_cancel_and_await_waits_for_cancel_cleanup() -> None:
    cleaned = asyncio.Event()

    async def worker() -> None:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)

    await _cancel_and_await(task)

    assert task.done()
    assert cleaned.is_set()
