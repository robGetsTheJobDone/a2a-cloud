from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.harness_cleanup import HarnessCleanupWorker
from control_plane.models import Agent, User
from control_plane.routes import agents as agent_routes


async def _get_or_create_user(session, email: str) -> User:
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(email=email, password_hash="x")
    session.add(user)
    await session.flush()
    return user


async def _seed_agent(
    session,
    *,
    email: str,
    name: str,
    created_at: datetime,
    public: bool = False,
) -> Agent:
    user = await _get_or_create_user(session, email)
    agent = Agent(
        owner_id=user.id,
        name=name,
        description="Harness test",
        version="0.1.0",
        image=f"registry.example/{name}:latest",
        public=public,
        status="running",
        url=None,
        card={"skills": []},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


@pytest.mark.asyncio
async def test_harness_cleanup_deletes_only_stale_private_harness_agents(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    deleted_external: list[tuple[str, str | None]] = []
    deleted_search: list[tuple[int, str]] = []

    def fake_cleanup(name: str, *, repo_owner: str | None = None) -> list[str]:
        deleted_external.append((name, repo_owner))
        return []

    async def fake_delete_search(agent_id: int, agent_name: str) -> None:
        deleted_search.append((agent_id, agent_name))

    monkeypatch.setattr(agent_routes, "_cleanup_agent_resources", fake_cleanup)
    monkeypatch.setattr(agent_routes, "_delete_agent_from_search", fake_delete_search)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            old = datetime.now(UTC) - timedelta(days=2)
            fresh = datetime.now(UTC) - timedelta(hours=1)
            stale = await _seed_agent(
                session,
                email="agent-studio-harness@a2acloud.io",
                name="studio-harness-0604l",
                created_at=old,
            )
            await _seed_agent(
                session,
                email="agent-studio-harness@a2acloud.io",
                name="studio-harness-fresh",
                created_at=fresh,
            )
            await _seed_agent(
                session,
                email="owner@example.com",
                name="studio-harness-user",
                created_at=old,
            )
            await _seed_agent(
                session,
                email="agent-studio-harness@a2acloud.io",
                name="animation-engineer",
                created_at=old,
            )
            await _seed_agent(
                session,
                email="agent-studio-harness@a2acloud.io",
                name="studio-harness-public",
                created_at=old,
                public=True,
            )

            result = await HarnessCleanupWorker(ttl_seconds=24 * 60 * 60).run_once(session)

            assert result.deleted == 1
            assert result.skipped == 0
            assert result.failed == 0
            assert deleted_external == [("studio-harness-0604l", None)]
            assert deleted_search == [(stale.id, "studio-harness-0604l")]
            names = [row.name for row in (await session.execute(select(Agent))).scalars()]
            assert sorted(names) == [
                "animation-engineer",
                "studio-harness-fresh",
                "studio-harness-public",
                "studio-harness-user",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_harness_cleanup_keeps_record_when_external_cleanup_fails(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    def fake_cleanup(name: str, *, repo_owner: str | None = None) -> list[str]:
        return ["argocd application: unavailable"]

    monkeypatch.setattr(agent_routes, "_cleanup_agent_resources", fake_cleanup)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_agent(
                session,
                email="agent-studio-harness@a2acloud.io",
                name="studio-harness-0604l",
                created_at=datetime.now(UTC) - timedelta(days=2),
            )

            result = await HarnessCleanupWorker(ttl_seconds=24 * 60 * 60).run_once(session)

            assert result.deleted == 0
            assert result.failed == 1
            remaining = (await session.execute(select(Agent))).scalars().all()
            assert [row.name for row in remaining] == ["studio-harness-0604l"]
    finally:
        await engine.dispose()
