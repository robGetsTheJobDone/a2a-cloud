from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane import template_updates
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentCodeEditorOptIn,
    AgentDeployment,
    User,
    WorkEvent,
    WorkJob,
)


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


async def _seed_agent(session: AsyncSession) -> Agent:
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
        card={
            "template_lineage": {
                "template_ref": "a2acloud/templates/invoice-bot",
                "template_version": "0.3.0",
                "source_agent": "code-editor-agent",
                "source_revision": "abc123",
                "instance_version": "0.1.0",
                "update_policy": "propose",
                "migration_skill": "turn",
            },
            "skills": [],
        },
        gitea_owner="a2a-acme",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _enqueue(session: AsyncSession, agent: Agent) -> WorkJob:
    return await template_updates.enqueue_template_update_job(
        session,
        agent,
        lineage=agent.card["template_lineage"],
    )


@pytest.mark.asyncio
async def test_template_update_worker_invokes_code_editor_and_completes_job(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(template_updates, "repo_exists", lambda *a, **kw: True)

    async def fake_runner(
        session: AsyncSession,
        user: User,
        agent: Agent,
        job: WorkJob,
        target_agent: str,
        skill: str,
        args: dict[str, object],
    ) -> dict[str, object]:
        calls.append({
            "user_id": user.id,
            "agent_name": agent.name,
            "job_id": job.job_id,
            "target_agent": target_agent,
            "skill": skill,
            "args": args,
        })
        return {
            "ok": True,
            "grant_id": "grant-1",
            "result": {"summary": "patched", "head_sha": "f" * 40},
        }

    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent)

        processed = await template_updates.TemplateUpdateWorker(fake_runner).run_once(
            session
        )
        await session.refresh(job)
        opt_in = (await session.execute(select(AgentCodeEditorOptIn))).scalar_one()
        events = (
            await session.execute(select(WorkEvent).order_by(WorkEvent.id.asc()))
        ).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["target_agent"] == "code-editor-agent"
    assert job.output_payload["skill"] == "turn"
    assert job.output_payload["migration_result"]["grant_id"] == "grant-1"
    assert opt_in.status == "enabled"
    assert opt_in.workspace_key == "a2a-acme/invoice-bot"
    assert calls[0]["target_agent"] == "code-editor-agent"
    assert calls[0]["skill"] == "turn"
    args = calls[0]["args"]
    assert args["agent_name"] == "invoice-bot"
    assert args["dry_run"] is False
    assert args["max_turns"] == 50
    assert "a2acloud/templates/invoice-bot" in str(args["prompt"])
    assert "template_update_invoking" in {event.event_type for event in events}
    assert "template_update_completed" in {event.event_type for event in events}


@pytest.mark.asyncio
async def test_template_update_worker_defers_during_active_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(template_updates, "repo_exists", lambda *a, **kw: True)

    async def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("migration should be deferred")

    async with session_factory() as session:
        agent = await _seed_agent(session)
        session.add(AgentDeployment(
            deploy_id="dpl_active",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="runtime_upgrade",
            status="building",
            created_at=template_updates.utcnow() + timedelta(seconds=1),
        ))
        await session.commit()
        job = await _enqueue(session, agent)

        processed = await template_updates.TemplateUpdateWorker(should_not_run).run_once(
            session
        )
        await session.refresh(job)

    assert processed is True
    assert job.status == "queued"
    assert job.summary == "Template update waiting for active deployment"
    assert job.queued_at is not None
    assert job.leased_until is None


@pytest.mark.asyncio
async def test_template_update_worker_failure_marks_job_error(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(template_updates, "repo_exists", lambda *a, **kw: True)

    async def fail_runner(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("code editor down")

    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent)

        processed = await template_updates.TemplateUpdateWorker(fail_runner).run_once(
            session
        )
        await session.refresh(job)

    assert processed is True
    assert job.status == "error"
    assert "code editor down" in (job.error or "")


@pytest.mark.asyncio
async def test_next_template_update_job_claim_marks_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        await _enqueue(session, agent)
        worker = template_updates.TemplateUpdateWorker()

        first = await worker._next_job(session)
        second = await worker._next_job(session)

    assert first is not None
    assert first.status == "running"
    assert first.attempt == 1
    assert first.started_at is not None
    assert first.leased_until is not None
    assert second is None


@pytest.mark.asyncio
async def test_template_update_notify_policy_completes_without_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        agent.card["template_lineage"]["update_policy"] = "notify"
        await session.commit()
        job = await _enqueue(session, agent)

        processed = await template_updates.TemplateUpdateWorker().run_once(session)
        await session.refresh(job)
        opt_ins = (await session.execute(select(AgentCodeEditorOptIn))).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["mutated"] is False
    assert opt_ins == []
