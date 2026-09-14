from __future__ import annotations

import asyncio

from control_plane import card_cache


def _use_in_memory(monkeypatch) -> card_cache._InMemoryCardCache:
    store = card_cache._InMemoryCardCache()
    monkeypatch.setattr(card_cache, "_store", store)
    return store


def test_warm_and_resolve_roundtrip(monkeypatch) -> None:
    _use_in_memory(monkeypatch)

    async def run() -> dict:
        await card_cache.warm_agent_card("alpha", {"skills": [{"name": "x"}]})
        return await card_cache.resolve_agent_card("alpha", fallback={"skills": []})

    got = asyncio.run(run())
    assert got["skills"][0]["name"] == "x"


def test_resolve_falls_back_to_db_copy_on_miss(monkeypatch) -> None:
    _use_in_memory(monkeypatch)

    async def run() -> dict | None:
        return await card_cache.resolve_agent_card("missing", fallback={"db": True})

    assert asyncio.run(run()) == {"db": True}


def test_warm_ignores_empty_or_missing_card(monkeypatch) -> None:
    _use_in_memory(monkeypatch)

    async def run() -> dict | None:
        await card_cache.warm_agent_card("alpha", {})
        await card_cache.warm_agent_card("beta", None)
        return await card_cache.resolve_agent_card("alpha", fallback=None)

    assert asyncio.run(run()) is None


def test_invalidate_removes_warmed_card(monkeypatch) -> None:
    _use_in_memory(monkeypatch)

    async def run() -> dict | None:
        await card_cache.warm_agent_card("alpha", {"skills": [{"name": "x"}]})
        await card_cache.invalidate_agent_card("alpha")
        return await card_cache.resolve_agent_card("alpha", fallback={"db": True})

    assert asyncio.run(run()) == {"db": True}
