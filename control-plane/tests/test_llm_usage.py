from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import ChatThread, LLMUsageEvent, User
from control_plane.routes.llm_usage import list_llm_usage


@pytest.mark.asyncio
async def test_list_llm_usage_filters_by_thread() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="ops@example.com", password_hash="x")
            session.add(user)
            session.add_all([
                ChatThread(id="thread-1", user_id=1, title="One"),
                ChatThread(id="thread-2", user_id=1, title="Two"),
                LLMUsageEvent(
                    user_id=1,
                    thread_id="thread-1",
                    source="control_plane_chat",
                    provider="openai",
                    model="gpt-test",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost_usd=0.01,
                ),
                LLMUsageEvent(
                    user_id=1,
                    thread_id="thread-2",
                    source="agent_handoff",
                    provider="openai",
                    model="gpt-other",
                    prompt_tokens=20,
                    completion_tokens=10,
                    total_tokens=30,
                    cost_usd=0.02,
                ),
            ])
            await session.commit()

            rows = await list_llm_usage(
                user=user,
                session=session,
                thread_id="thread-1",
                grant_id=None,
                dag_run_id=None,
                agent=None,
                limit=50,
            )

            assert [row.thread_id for row in rows] == ["thread-1"]
            assert rows[0].model == "gpt-test"
            assert rows[0].total_tokens == 15
            assert rows[0].status == "complete"
    finally:
        await engine.dispose()
