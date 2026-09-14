"""Two invocations of the same skill with the same ``random_seed`` must
produce identical ``ctx.random()`` streams. This is the foundation deterministic
replay rests on — :mod:`a2a_pack.replay_doubles` reproduces side effects, but
random draws still come from ``ctx.random()``.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from a2a_pack import (
    A2AAgent,
    LocalRunContext,
    NoAuth,
    RunContext,
    skill,
)


class _Cfg(BaseModel):
    pass


class _RandomAgent(A2AAgent[_Cfg, NoAuth]):
    name = "random-agent"
    description = "Draws random values via ctx.random()"
    config_model = _Cfg
    auth_model = NoAuth

    @skill(description="returns a deterministic draw sequence")
    async def draws(self, ctx: RunContext[NoAuth], n: int = 5) -> list[int]:
        rng = ctx.random()
        return [rng.randint(0, 10_000) for _ in range(n)]


def test_default_local_random_seed_is_populated() -> None:
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth())
    assert ctx.random_seed != ""
    # 16 random hex bytes → 32 chars.
    assert len(ctx.random_seed) == 32


def test_ctx_random_is_cached() -> None:
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), random_seed="cafebabe",
    )
    rng1 = ctx.random()
    rng2 = ctx.random()
    assert rng1 is rng2


@pytest.mark.asyncio
async def test_same_seed_produces_identical_draws() -> None:
    agent = _RandomAgent()
    ctx_a: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), random_seed="seed-42",
    )
    ctx_b: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), random_seed="seed-42",
    )

    draws_a = await agent.invoke("draws", ctx_a, n=8)
    draws_b = await agent.invoke("draws", ctx_b, n=8)

    assert draws_a == draws_b
    # Sanity: not the trivial all-zero / all-same sequence.
    assert len(set(draws_a)) > 1


@pytest.mark.asyncio
async def test_different_seed_diverges() -> None:
    agent = _RandomAgent()
    ctx_a: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), random_seed="seed-A",
    )
    ctx_b: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(), random_seed="seed-B",
    )
    assert await agent.invoke("draws", ctx_a) != await agent.invoke("draws", ctx_b)
