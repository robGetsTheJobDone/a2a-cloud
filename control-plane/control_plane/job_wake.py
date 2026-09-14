"""Best-effort Redis pub/sub wake channel for the agent-api worker.

The worker polls Postgres for queued jobs on a fixed interval (default 2s).
Publishing a wake message when a job is enqueued lets the worker pick it up
immediately instead of waiting out the poll interval. Everything here is
best-effort: if Redis is unavailable the worker simply falls back to its
poll interval, so no caller may treat a failed nudge as an error.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

AGENT_API_JOBS_CHANNEL = "control-plane:agent-api-jobs:wake"

_publisher: Any | None = None


def _redis_client() -> Any | None:
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

        return redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    except Exception:  # noqa: BLE001
        log.exception("redis unavailable for job wake channel")
        return None


async def nudge_agent_api_worker() -> None:
    """Signal the agent-api worker that a job was enqueued. Best-effort."""
    global _publisher
    if _publisher is None:
        _publisher = _redis_client()
    if _publisher is None:
        return
    try:
        await _publisher.publish(AGENT_API_JOBS_CHANNEL, "1")
    except Exception:  # noqa: BLE001
        log.warning("agent-api worker wake publish failed", exc_info=True)
        _publisher = None


class AgentApiJobWake:
    """Worker-side listener: wait for a wake message or a timeout."""

    def __init__(self) -> None:
        self._pubsub: Any | None = None

    async def connect(self) -> None:
        if self._pubsub is not None:
            return
        client = _redis_client()
        if client is None:
            return
        try:
            pubsub = client.pubsub()
            await pubsub.subscribe(AGENT_API_JOBS_CHANNEL)
        except Exception:  # noqa: BLE001
            log.warning("agent-api worker wake subscribe failed", exc_info=True)
            return
        self._pubsub = pubsub

    async def wait(self, timeout: float) -> None:
        """Sleep until a wake message arrives or ``timeout`` elapses."""
        await self.connect()
        if self._pubsub is None:
            await asyncio.sleep(timeout)
            return
        try:
            await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=timeout
            )
        except Exception:  # noqa: BLE001
            log.warning("agent-api worker wake wait failed", exc_info=True)
            with suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None
            await asyncio.sleep(timeout)
