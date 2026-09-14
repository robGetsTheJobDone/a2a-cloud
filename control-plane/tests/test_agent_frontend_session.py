"""The agent-origin session gateway.

Hosted agents run user-controlled code on ``<name>.example.com``. The whole
point of this gateway is that an agent learns *who is visiting it* without ever
holding a credential that works anywhere else, so these tests are mostly about
what the scoped token must NOT be able to do.
"""
from __future__ import annotations

from time import monotonic as real_monotonic, time as real_time

import pytest
from fastapi import HTTPException

from control_plane import agent_frontend_session
from control_plane.agent_frontend_session import (
    InMemoryAgentSessionExchangeStore,
    decode_agent_session_token,
    mint_agent_session_token,
)
from control_plane.auth import decode_token, user_for_agent_audience
from control_plane.models import User


def test_scoped_token_resolves_only_for_its_own_agent() -> None:
    token, _ = mint_agent_session_token(user_id=7, agent="alpha", ttl_seconds=300)

    assert decode_agent_session_token(token, agent="alpha") == 7
    # A neighbouring agent on the same wildcard domain must not be able to
    # accept a token minted for someone else.
    assert decode_agent_session_token(token, agent="beta") is None


def test_scoped_token_is_rejected_by_platform_authentication() -> None:
    token, _ = mint_agent_session_token(user_id=7, agent="alpha", ttl_seconds=300)

    # This is the invariant that lets us hand the token to agent code at all:
    # it is signed by the platform, but useless against the platform API.
    with pytest.raises(HTTPException) as excinfo:
        decode_token(token)
    assert excinfo.value.status_code == 401


def test_expired_scoped_token_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mint as though it were issued yesterday; TTLs are clamped to >=1s, so
    # travel the clock rather than passing a negative TTL.
    monkeypatch.setattr(
        agent_frontend_session.time, "time", lambda: real_time() - 86_400
    )
    token, _ = mint_agent_session_token(user_id=7, agent="alpha", ttl_seconds=300)
    monkeypatch.undo()

    assert decode_agent_session_token(token, agent="alpha") is None


@pytest.mark.asyncio
async def test_user_for_agent_audience_rejects_other_agents_token() -> None:
    token, _ = mint_agent_session_token(user_id=7, agent="alpha", ttl_seconds=300)

    class _Session:
        async def get(self, _model: type, user_id: int) -> User:
            return User(id=user_id, email="user@example.test", password_hash="hash")

    session = _Session()
    user = await user_for_agent_audience(session, token, "alpha")  # type: ignore[arg-type]
    assert user.id == 7

    # Presenting alpha's session while claiming to be beta must not resolve;
    # it falls through to platform auth, which rejects scoped tokens.
    with pytest.raises(HTTPException) as excinfo:
        await user_for_agent_audience(session, token, "beta")  # type: ignore[arg-type]
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_exchange_code_is_single_use() -> None:
    store = InMemoryAgentSessionExchangeStore()
    code = await store.issue(user_id=7, agent="alpha", redirect_to="/app/", ttl_seconds=60)

    first = await store.consume(code)
    assert first is not None
    assert (first.user_id, first.agent, first.redirect_to) == (7, "alpha", "/app/")
    # A replayed redirect (back button, duplicated tab) must not mint a second
    # session.
    assert await store.consume(code) is None


@pytest.mark.asyncio
async def test_exchange_code_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryAgentSessionExchangeStore()
    code = await store.issue(user_id=7, agent="alpha", redirect_to="/", ttl_seconds=60)

    # Walk past the TTL instead of relying on a negative one, which clamps.
    monkeypatch.setattr(
        agent_frontend_session.time, "monotonic", lambda: real_monotonic() + 3_600
    )
    assert await store.consume(code) is None


@pytest.mark.asyncio
async def test_exchange_code_binds_the_agent_it_was_issued_for() -> None:
    store = InMemoryAgentSessionExchangeStore()
    code = await store.issue(user_id=7, agent="alpha", redirect_to="/", ttl_seconds=60)

    exchange = await store.consume(code)
    assert exchange is not None
    # The exchange endpoint compares this against the caller's claimed
    # audience, which is what stops one agent redeeming another's code.
    assert exchange.agent == "alpha"


@pytest.mark.asyncio
async def test_consumer_setup_invocation_accepts_the_agents_own_session() -> None:
    """A hosted agent serves its visitor using the origin-bound cookie.

    Real invokes failed here first: the agent forwards its session token to the
    control plane, which rejected it because platform auth (correctly) refuses
    agent-scoped tokens. The invocation lookup is bound to the agent named in
    the path instead.
    """
    from control_plane.routes.consumer_setup import _invocation_caller

    token, _ = mint_agent_session_token(user_id=7, agent="alpha", ttl_seconds=300)

    class _Session:
        async def get(self, _model: type, user_id: int) -> User:
            return User(id=user_id, email="user@example.test", password_hash="hash")

    session = _Session()
    user = await _invocation_caller("alpha", credential=token, session=session)  # type: ignore[arg-type]
    assert user.id == 7

    # The same cookie presented while invoking a different agent must not
    # resolve that visitor's setup.
    with pytest.raises(HTTPException) as excinfo:
        await _invocation_caller("beta", credential=token, session=session)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 401
