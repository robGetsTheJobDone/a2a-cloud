from __future__ import annotations

import asyncio

import pytest

from control_plane.pending_actions import TIMEOUT, create_pending_actions_store


@pytest.mark.asyncio
async def test_pending_actions_can_resolve_before_wait_starts() -> None:
    store = create_pending_actions_store(None)

    assert await store.resolve(
        kind="handoff",
        request_id="abc",
        user_id=7,
        response="approve",
        ttl_seconds=120.0,
    )

    assert await store.wait(
        kind="handoff",
        request_id="abc",
        user_id=7,
        timeout_seconds=0.2,
        ttl_seconds=120.0,
    ) == "approve"


@pytest.mark.asyncio
async def test_pending_actions_times_out_and_blocks_late_resolve() -> None:
    store = create_pending_actions_store(None)

    assert await store.wait(
        kind="scope",
        request_id="late",
        user_id=3,
        timeout_seconds=0.01,
        ttl_seconds=1.0,
        poll_interval=0.001,
    ) is TIMEOUT

    assert not await store.resolve(
        kind="scope",
        request_id="late",
        user_id=3,
        response="approve",
        ttl_seconds=60.0,
    )


@pytest.mark.asyncio
async def test_pending_actions_wait_reacts_to_late_resolution() -> None:
    store = create_pending_actions_store(None)

    async def resolve_later() -> None:
        await asyncio.sleep(0.02)
        await store.resolve(
            kind="question",
            request_id="q-1",
            user_id=11,
            response="yes",
            ttl_seconds=180.0,
        )

    task = asyncio.create_task(resolve_later())
    try:
        assert await store.wait(
            kind="question",
            request_id="q-1",
            user_id=11,
            timeout_seconds=0.2,
            ttl_seconds=180.0,
            poll_interval=0.001,
        ) == "yes"
    finally:
        await task
