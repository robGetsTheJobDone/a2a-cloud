from __future__ import annotations

import os

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from control_plane.db import Base
from control_plane.models import Agent, AgentCodeEditorOptIn, User, WorkJob
from control_plane.routes import agents as agent_routes


async def _session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_agent(
    session: AsyncSession,
    *,
    owner_email: str = "owner@example.com",
    name: str = "invoice-bot",
    image: str | None = None,
    gitea_owner: str | None = "a2a-acme",
) -> tuple[User, Agent]:
    user = User(email=owner_email, password_hash="hash")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name=name,
        description="Invoice bot",
        version="0.1.0",
        image=image or f"registry.example.com/agents/{name}:latest",
        public=True,
        status="running",
        url=f"https://{name}.example.com",
        card={},
        gitea_owner=gitea_owner,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(user)
    await session.refresh(agent)
    return user, agent


@pytest.mark.asyncio
async def test_enable_code_editor_is_idempotent_and_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _session_factory()
    monkeypatch.setattr(agent_routes, "repo_exists", lambda *a, **kw: True)

    try:
        async with factory() as session:
            user, agent = await _seed_agent(session)

            first = await agent_routes.enable_agent_code_editor(agent.name, user, session)
            second = await agent_routes.enable_agent_code_editor(agent.name, user, session)
            listed = await agent_routes.list_my_agents(user, session)

            rows = (await session.execute(select(AgentCodeEditorOptIn))).scalars().all()
    finally:
        await engine.dispose()

    assert first.code_editor.enabled is True
    assert first.code_editor.shared_agent_name == "code-editor-agent"
    assert first.code_editor.workspace_key == "a2a-acme/invoice-bot"
    assert second.code_editor.enabled is True
    assert len(rows) == 1
    assert rows[0].agent_id == agent.id
    assert listed[0].code_editor.enabled is True
    assert listed[0].code_editor.workspace_key == "a2a-acme/invoice-bot"


@pytest.mark.asyncio
async def test_disable_code_editor_removes_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = await _session_factory()
    monkeypatch.setattr(agent_routes, "repo_exists", lambda *a, **kw: True)

    try:
        async with factory() as session:
            user, agent = await _seed_agent(session)
            await agent_routes.enable_agent_code_editor(agent.name, user, session)

            disabled = await agent_routes.disable_agent_code_editor(agent.name, user, session)
            rows = (await session.execute(select(AgentCodeEditorOptIn))).scalars().all()
    finally:
        await engine.dispose()

    assert disabled.code_editor.enabled is False
    assert disabled.code_editor.status == "disabled"
    assert rows == []


@pytest.mark.asyncio
async def test_enable_code_editor_rejects_unowned_agent() -> None:
    engine, factory = await _session_factory()

    try:
        async with factory() as session:
            _owner, agent = await _seed_agent(session)
            caller = User(email="caller@example.com", password_hash="hash")
            session.add(caller)
            await session.commit()
            await session.refresh(caller)

            with pytest.raises(HTTPException) as exc_info:
                await agent_routes.enable_agent_code_editor(agent.name, caller, session)
    finally:
        await engine.dispose()

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_enable_code_editor_rejects_external_imported_agent() -> None:
    engine, factory = await _session_factory()

    try:
        async with factory() as session:
            user, agent = await _seed_agent(
                session,
                image="external-a2a:https://example.com/.well-known/agent-card.json",
                gitea_owner=None,
            )

            with pytest.raises(HTTPException) as exc_info:
                await agent_routes.enable_agent_code_editor(agent.name, user, session)
    finally:
        await engine.dispose()

    assert exc_info.value.status_code == 409
    assert "managed source repo" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_request_template_update_queues_work_job() -> None:
    engine, factory = await _session_factory()

    try:
        async with factory() as session:
            user, agent = await _seed_agent(session)
            agent.card = {
                "template_lineage": {
                    "template_ref": "a2acloud/templates/invoice-bot",
                    "template_version": "0.3.0",
                    "source_agent": "invoice-template",
                    "source_revision": "abc123",
                    "instance_version": "0.1.0",
                    "update_policy": "propose",
                }
            }
            await session.commit()

            out = await agent_routes.request_agent_template_update(
                agent.name,
                user,
                session,
            )
            jobs = (await session.execute(select(WorkJob))).scalars().all()
    finally:
        await engine.dispose()

    assert out.agent_name == "invoice-bot"
    assert out.status == "queued"
    assert out.update_policy == "propose"
    assert len(jobs) == 1
    assert jobs[0].kind == "agent.template_update"
    assert jobs[0].queue == "template_updates"
    assert jobs[0].input_payload["template_lineage"]["template_ref"] == (
        "a2acloud/templates/invoice-bot"
    )


@pytest.mark.asyncio
async def test_request_template_update_rejects_missing_lineage() -> None:
    engine, factory = await _session_factory()

    try:
        async with factory() as session:
            user, agent = await _seed_agent(session)

            with pytest.raises(HTTPException) as exc_info:
                await agent_routes.request_agent_template_update(
                    agent.name,
                    user,
                    session,
                )
    finally:
        await engine.dispose()

    assert exc_info.value.status_code == 409
    assert "template_lineage" in str(exc_info.value.detail)
