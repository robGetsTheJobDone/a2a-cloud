from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth import current_user
from control_plane.db import Base, get_session
from control_plane.models import User, UserLLMCreds, UserOnboardingState
from control_plane.routes import onboarding


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def alice(session_factory) -> User:
    async with session_factory() as session:
        user = User(email="alice@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def client(session_factory, alice: User):
    app = FastAPI()
    app.include_router(onboarding.router)

    async def _override_session():
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> User:
        return alice

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_user] = _override_current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_onboarding_defaults_to_llm_key_step(client: AsyncClient) -> None:
    resp = await client.get("/v1/me/onboarding")

    assert resp.status_code == 200
    assert resp.json() == {
        "completed": False,
        "dismissed": False,
        "current_step": "llm_key",
        "llm_key_configured": False,
        "llm_key_step_completed": False,
        "walkthrough_completed": False,
        "started_at": None,
        "llm_key_completed_at": None,
        "walkthrough_started_at": None,
        "walkthrough_completed_at": None,
        "last_seen_step": None,
        "tour_step_index": 0,
        "tour_step_total": 0,
        "dismissed_at": None,
        "completed_at": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_onboarding_requires_llm_key_before_completion(
    client: AsyncClient,
) -> None:
    resp = await client.patch("/v1/me/onboarding", json={"completed": True})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "LLM key is required before onboarding can continue"


@pytest.mark.asyncio
async def test_onboarding_can_be_dismissed_without_an_llm_key(
    client: AsyncClient,
) -> None:
    """Skipping BYOK is accepted with no key; completing it still is not.

    `dismissed` only takes the wall down. It must not claim the walkthrough or
    the key step happened, so the just-in-time key prompt still applies.
    """
    resp = await client.patch("/v1/me/onboarding", json={"dismissed": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dismissed"] is True
    assert body["dismissed_at"] is not None
    assert body["completed"] is False
    assert body["completed_at"] is None
    assert body["llm_key_configured"] is False
    assert body["llm_key_step_completed"] is False
    assert body["walkthrough_completed"] is False
    assert body["current_step"] == "llm_key"

    # Dismissal persists across reads, so the wizard does not reappear.
    assert (await client.get("/v1/me/onboarding")).json()["dismissed"] is True

    # And it is not a back door into completion.
    blocked = await client.patch("/v1/me/onboarding", json={"completed": True})
    assert blocked.status_code == 400


@pytest.mark.asyncio
async def test_onboarding_dismissal_can_be_undone(client: AsyncClient) -> None:
    await client.patch("/v1/me/onboarding", json={"dismissed": True})

    resp = await client.patch("/v1/me/onboarding", json={"dismissed": False})

    assert resp.status_code == 200
    assert resp.json()["dismissed"] is False
    assert resp.json()["dismissed_at"] is None


@pytest.mark.asyncio
async def test_onboarding_reads_a_row_it_did_not_create_as_not_dismissed(
    client: AsyncClient,
    session_factory,
    alice: User,
) -> None:
    """A row this API never wrote (dismissed_at NULL) reads back undismissed.

    Written straight to the table so the read path, not the create path, is
    what is under test — a user part-way through the old walkthrough must not
    be treated as having skipped it.
    """
    async with session_factory() as session:
        session.add(
            UserOnboardingState(
                user_id=alice.id,
                current_step="tour",
                llm_key_step_completed=True,
                tour_step_index=17,
                tour_step_total=24,
            )
        )
        await session.commit()

    resp = await client.get("/v1/me/onboarding")

    assert resp.status_code == 200
    assert resp.json()["dismissed"] is False
    assert resp.json()["dismissed_at"] is None
    assert resp.json()["tour_step_index"] == 17


@pytest.mark.asyncio
async def test_onboarding_updates_do_not_dismiss_as_a_side_effect(
    client: AsyncClient,
) -> None:
    """Only an explicit `dismissed` flips dismissal — no other field does."""
    seed = await client.patch(
        "/v1/me/onboarding",
        json={"current_step": "llm_key", "tour_step_index": 3},
    )

    assert seed.status_code == 200
    assert seed.json()["dismissed"] is False
    assert seed.json()["dismissed_at"] is None

    await client.patch("/v1/me/onboarding", json={"dismissed": True})
    kept = await client.patch("/v1/me/onboarding", json={"tour_step_index": 4})

    assert kept.status_code == 200
    assert kept.json()["dismissed"] is True
    assert kept.json()["tour_step_index"] == 4


@pytest.mark.asyncio
async def test_onboarding_can_complete_after_llm_key(
    client: AsyncClient,
    session_factory,
    alice: User,
) -> None:
    async with session_factory() as session:
        session.add(
            UserLLMCreds(
                user_id=alice.id,
                name="default",
                base_url="https://api.openai.com/v1",
                api_key="legacy-test-key",
                model="gpt-5",
            )
        )
        await session.commit()

    key_resp = await client.patch(
        "/v1/me/onboarding",
        json={
            "current_step": "tour",
            "llm_key_step_completed": True,
            "last_seen_step": "workspace-chat",
            "tour_step_index": 2,
            "tour_step_total": 24,
        },
    )
    assert key_resp.status_code == 200
    key_body = key_resp.json()
    assert key_body["current_step"] == "tour"
    assert key_body["llm_key_configured"] is True
    assert key_body["started_at"] is not None
    assert key_body["llm_key_completed_at"] is not None
    assert key_body["walkthrough_started_at"] is not None
    assert key_body["last_seen_step"] == "workspace-chat"
    assert key_body["tour_step_index"] == 2
    assert key_body["tour_step_total"] == 24

    done_resp = await client.patch("/v1/me/onboarding", json={"completed": True})
    assert done_resp.status_code == 200
    body = done_resp.json()
    assert body["completed"] is True
    assert body["current_step"] == "done"
    assert body["walkthrough_completed"] is True
    assert body["walkthrough_completed_at"] is not None
    assert body["completed_at"] is not None
