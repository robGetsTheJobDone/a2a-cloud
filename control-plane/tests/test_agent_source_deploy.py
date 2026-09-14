from __future__ import annotations

import os
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane.db import Base
from control_plane.models import Agent, User
from control_plane.routes import agents


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deploy_agent_source_uses_current_repo_head_and_manual_trigger(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    async with session_factory() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="invoice-bot",
            description="Invoice bot",
            version="0.1.0",
            image="registry.example.com/agents/invoice-bot:latest",
            public=True,
            status="running",
            url="https://invoice-bot.example.com",
            card={},
            gitea_owner="a2a-acme",
        )
        session.add(agent)
        await session.commit()
        await session.refresh(user)

        monkeypatch.setattr(
            agents,
            "repo_head_sha",
            lambda name, *, owner=None: calls.setdefault("repo_head_args", (name, owner))
            and "c" * 40,
        )

        async def _deploy_source_push(*_args, **kwargs):
            calls["deploy_kwargs"] = kwargs
            return {
                "summary": "Queued source deployment for invoice-bot",
                "agent_name": "invoice-bot",
                "source_sha": "c" * 40,
            }

        monkeypatch.setattr(agents, "deploy_source_push", _deploy_source_push)

        result = await agents.deploy_agent_source(
            "invoice-bot",
            SimpleNamespace(app=SimpleNamespace()),  # type: ignore[arg-type]
            user,
            session,
        )

    assert calls["repo_head_args"] == ("invoice-bot", "a2a-acme")
    assert calls["deploy_kwargs"] == {
        "owner": "a2a-acme",
        "repo": "invoice-bot",
        "source_sha": "c" * 40,
        "changed_paths": ["<explicit-source-deploy>"],
        "delivery_id": None,
        "ref": "refs/heads/main",
        "trigger": "source_manual",
    }
    assert result["source_sha"] == "c" * 40

