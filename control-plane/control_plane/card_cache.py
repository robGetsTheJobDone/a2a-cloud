"""Redis-backed cache of agent cards.

Passive registry / discovery / handoff reads must never wake a scaled-to-zero
agent just to read its ``/.well-known/agent-card``. The durable source of
truth stays ``Agent.card`` in Postgres; this is a fast front layer that is
**warmed on every version deploy** (and on import / explicit refresh) so reads
can be served from cache and almost never need the pod or a card re-fetch.

Every operation is best-effort: a Redis outage degrades to a cache miss (read)
or a no-op (write), and callers fall back to the DB copy. A cache error must
never fail a deploy or a registry read.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

# Cards change only on deploy/import/refresh, all of which re-warm the cache,
# so a long TTL is fine; it only bounds how long a stale entry survives if a
# warm write was somehow missed.
_CARD_TTL_SECONDS = 7 * 24 * 3600
_KEY = "agent-card:{name}"


class _InMemoryCardCache:
    """Fallback used when no Redis URL is configured (tests, local dev)."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def get(self, name: str) -> dict[str, Any] | None:
        return self._data.get(name)

    async def set(self, name: str, card: dict[str, Any]) -> None:
        if isinstance(card, dict):
            self._data[name] = card

    async def delete(self, name: str) -> None:
        self._data.pop(name, None)


class _RedisCardCache:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self, name: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(_KEY.format(name=name))
        except Exception:  # noqa: BLE001 - cache miss on any backend error
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def set(self, name: str, card: dict[str, Any]) -> None:
        if not isinstance(card, dict):
            return
        try:
            await self._client.set(
                _KEY.format(name=name),
                json.dumps(card, separators=(",", ":"), ensure_ascii=False),
                ex=_CARD_TTL_SECONDS,
            )
        except Exception:  # noqa: BLE001 - best-effort warm
            log.debug("card cache warm failed for %s", name, exc_info=True)

    async def delete(self, name: str) -> None:
        try:
            await self._client.delete(_KEY.format(name=name))
        except Exception:  # noqa: BLE001
            log.debug("card cache delete failed for %s", name, exc_info=True)


_store: _InMemoryCardCache | _RedisCardCache | None = None


def _build(redis_url: str | None) -> _InMemoryCardCache | _RedisCardCache:
    if redis_url:
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

            client = redis_asyncio.from_url(redis_url, decode_responses=True)
            return _RedisCardCache(client)
        except Exception:  # noqa: BLE001
            log.exception("card cache: Redis init failed; using in-memory fallback")
    return _InMemoryCardCache()


def card_cache() -> _InMemoryCardCache | _RedisCardCache:
    global _store
    if _store is None:
        _store = _build(settings.redis_url)
    return _store


async def warm_agent_card(name: str, card: dict[str, Any] | None) -> None:
    """Write an agent's card into the cache (no-op for empty/invalid cards)."""
    if isinstance(card, dict) and card:
        await card_cache().set(name, card)


async def invalidate_agent_card(name: str) -> None:
    """Remove an agent's cached card so reads cannot serve an older deploy."""
    if name:
        await card_cache().delete(name)


async def resolve_agent_card(
    name: str, fallback: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return the cached card for ``name``, or ``fallback`` (e.g. the DB copy)."""
    cached = await card_cache().get(name)
    if isinstance(cached, dict) and cached:
        return cached
    return fallback


__all__ = [
    "card_cache",
    "warm_agent_card",
    "invalidate_agent_card",
    "resolve_agent_card",
]
