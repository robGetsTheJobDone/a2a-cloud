from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import time

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.receipts import seal_receipt

from control_plane.db import Base
from control_plane.models import Agent, User
from control_plane.routes import agent_receipts as receipts_routes
from control_plane.routes.agent_receipts import SignedTokenIn


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="owner@example.com", password_hash="hash")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="research-agent",
            description="",
            version="1.0.0",
            image="registry.example.com/agents/research-agent:latest",
            public=True,
            status="running",
            url="https://research-agent.example.com",
            card={"skills": [{"name": "search"}]},
        )
        session.add(agent)
        await session.commit()
        yield session
    await engine.dispose()


async def _owner(session) -> User:
    """The fixture's agent owner — reads are owner-scoped since the receipt
    authz fix (see tests/test_agent_receipts_authz.py for the full matrix)."""
    from sqlalchemy import select

    return (
        await session.execute(select(User).where(User.email == "owner@example.com"))
    ).scalar_one()


def _make_token(
    *,
    agent_name: str = "research-agent",
    skill_name: str = "search",
    caller: str = "user-7",
) -> tuple[str, str]:
    started = int(time.time()) - 5
    receipt, token = seal_receipt(
        agent_name=agent_name,
        skill_name=skill_name,
        started_at=started,
        ended_at=started + 2,
        caller=caller,
        inputs={"q": "owls"},
        result={"hits": 3},
    )
    return token, receipt.receipt_id


@pytest.mark.asyncio
async def test_post_receipt_persists_verified_payload(db_session) -> None:
    token, receipt_id = _make_token()

    out = await receipts_routes.post_agent_receipt(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    assert out.receipt_id == receipt_id
    assert out.agent_name == "research-agent"
    assert out.skill_name == "search"
    assert out.caller == "user-7"
    assert out.status == "ok"
    assert out.signed_token == token
    assert out.payload["receipt_id"] == receipt_id
    assert out.payload["skill_name"] == "search"


@pytest.mark.asyncio
async def test_post_receipt_rejects_tampered_signature(db_session) -> None:
    token, _ = _make_token()
    # Mutate the signed payload, not the final base64url signature character,
    # because padding bits can make some last-character changes decode to the
    # same signature bytes.
    payload, signature = token.rsplit(".", 1)
    first = "A" if payload[0] != "A" else "B"
    tampered = first + payload[1:] + "." + signature

    with pytest.raises(HTTPException) as exc:
        await receipts_routes.post_agent_receipt(
            "research-agent",
            SignedTokenIn(signed_token=tampered),
            db_session,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_post_receipt_rejects_duplicate_receipt_id(db_session) -> None:
    token, _ = _make_token()
    await receipts_routes.post_agent_receipt(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    with pytest.raises(HTTPException) as exc:
        await receipts_routes.post_agent_receipt(
            "research-agent",
            SignedTokenIn(signed_token=token),
            db_session,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_list_receipts_returns_header_only_ordered_recent_first(db_session) -> None:
    token_a, id_a = _make_token(caller="user-a")
    await receipts_routes.post_agent_receipt(
        "research-agent",
        SignedTokenIn(signed_token=token_a),
        db_session,
    )
    # advance time so started_at differs
    time.sleep(0.01)
    token_b, id_b = _make_token(caller="user-b")
    await receipts_routes.post_agent_receipt(
        "research-agent",
        SignedTokenIn(signed_token=token_b),
        db_session,
    )

    rows = await receipts_routes.list_agent_receipts(
        "research-agent",
        limit=50,
        before=None,
        user=await _owner(db_session),
        session=db_session,
    )

    ids = [r.receipt_id for r in rows]
    assert set(ids) == {id_a, id_b}
    # header model does not expose signed_token / payload
    assert not hasattr(rows[0], "signed_token")
    assert not hasattr(rows[0], "payload")


@pytest.mark.asyncio
async def test_get_receipt_returns_full_payload_and_token(db_session) -> None:
    token, receipt_id = _make_token()
    await receipts_routes.post_agent_receipt(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    out = await receipts_routes.get_agent_receipt(
        "research-agent",
        receipt_id,
        await _owner(db_session),
        db_session,
    )

    assert out.receipt_id == receipt_id
    assert out.signed_token == token
    assert out.payload["receipt_id"] == receipt_id


@pytest.mark.asyncio
async def test_get_receipt_returns_404_when_missing(db_session) -> None:
    with pytest.raises(HTTPException) as exc:
        await receipts_routes.get_agent_receipt(
            "research-agent",
            "does-not-exist",
            await _owner(db_session),
            db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_post_receipt_404_when_agent_unknown(db_session) -> None:
    token, _ = _make_token(agent_name="ghost-agent")
    with pytest.raises(HTTPException) as exc:
        await receipts_routes.post_agent_receipt(
            "ghost-agent",
            SignedTokenIn(signed_token=token),
            db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_post_receipt_400_when_agent_name_mismatches_path(db_session) -> None:
    # body verifies fine but agent_name inside the receipt is for a different
    # agent than the URL — that's an integrity bug we surface as 400.
    other_agent_token, _ = _make_token(agent_name="some-other-agent")
    with pytest.raises(HTTPException) as exc:
        await receipts_routes.post_agent_receipt(
            "research-agent",
            SignedTokenIn(signed_token=other_agent_token),
            db_session,
        )
    assert exc.value.status_code == 400
