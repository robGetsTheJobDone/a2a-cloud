from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.cron import next_cron_time, normalize_cron, validate_timezone
from control_plane.db import Base
from control_plane.models import Agent, AgentSchedule, User
from control_plane.routes.schedules import ScheduleIn, create_schedule


def test_next_cron_time_respects_timezone() -> None:
    after = datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc)

    out = next_cron_time("0 9 * * *", "America/Sao_Paulo", after=after)

    assert out == datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)


def test_cron_validation_accepts_macros_and_rejects_bad_timezone() -> None:
    assert normalize_cron("@daily") == "0 0 * * *"
    with pytest.raises(ValueError):
        validate_timezone("No/SuchZone")


@pytest.mark.asyncio
async def test_create_agent_schedule_validates_skill_and_sets_next_run() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            agent = Agent(
                owner_id=1,
                name="researcher",
                description="Research agent",
                version="0.1.0",
                image="image",
                public=False,
                status="ready",
                card={
                    "name": "researcher",
                    "description": "Research agent",
                    "version": "0.1.0",
                    "skills": [{"name": "brief", "description": "", "tags": []}],
                },
            )
            session.add_all([user, agent])
            await session.commit()

            out = await create_schedule(
                ScheduleIn(
                    name="Daily brief",
                    cron="@daily",
                    timezone="UTC",
                    target_type="agent",
                    agent_name="researcher",
                    skill_name="brief",
                    args_json='{"topic":"markets"}',
                ),
                user=user,
                session=session,
            )

            assert out.name == "Daily brief"
            assert out.target_type == "agent"
            assert out.args_json == '{"topic":"markets"}'
            assert out.next_run_at is not None

            row = (await session.execute(select(AgentSchedule))).scalar_one()
            assert row.schedule_id == out.schedule_id
            assert row.agent_name == "researcher"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_main_agent_schedule_requires_prompt() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await create_schedule(
                    ScheduleIn(
                        name="Empty prompt",
                        cron="@daily",
                        timezone="UTC",
                        target_type="main_agent",
                    ),
                    user=user,
                    session=session,
                )

            assert exc.value.status_code == 400
    finally:
        await engine.dispose()
