from __future__ import annotations

import pytest

from control_plane import auth_exchange
from control_plane.auth_exchange import (
    InMemoryCliSessionExchangeStore,
    RedisCliSessionExchangeStore,
)


@pytest.mark.asyncio
async def test_in_memory_exchange_expires_and_is_consumed_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(auth_exchange.time, "monotonic", lambda: now)
    store = InMemoryCliSessionExchangeStore()

    code = await store.issue(user_id=7, redirect_to="/work", ttl_seconds=30)
    exchange = await store.consume(code)
    assert exchange is not None
    assert exchange.user_id == 7
    assert exchange.redirect_to == "/work"
    assert await store.consume(code) is None

    expired_code = await store.issue(user_id=7, redirect_to="/work", ttl_seconds=30)
    now = 131.0
    assert await store.consume(expired_code) is None


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.getdel_calls: list[str] = []

    async def set(self, key: str, value: str, **_kwargs) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        self.getdel_calls.append(key)
        return self.values.pop(key, None)


@pytest.mark.asyncio
async def test_redis_exchange_hashes_code_and_uses_atomic_getdel() -> None:
    redis = _FakeRedis()
    store = RedisCliSessionExchangeStore(redis)

    code = await store.issue(user_id=11, redirect_to="/agents", ttl_seconds=60)
    stored_key = next(iter(redis.values))
    assert code not in stored_key

    exchange = await store.consume(code)
    assert exchange is not None
    assert exchange.user_id == 11
    assert redis.getdel_calls == [stored_key]
    assert await store.consume(code) is None
