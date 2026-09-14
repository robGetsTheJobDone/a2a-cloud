from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.dag_runs import DagRunRecorder
from control_plane.db import Base
from control_plane.models import ChatThread, SubagentRun, User
from control_plane.routes.dag_runs import get_dag_run


@pytest.mark.asyncio
async def test_dag_run_recorder_persists_plan_progress_and_file_ops() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="planner@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=user.id, title="Plan")
            subagent = SubagentRun(
                grant_id="grant-1",
                user_id=user.id,
                thread_id=thread.id,
                agent_name="researcher",
                skill_name="search",
                args_json='{"topic":"markets"}',
                scopes={},
                status="complete",
                summary="created report",
                file_ops=[
                    {
                        "op": "create",
                        "path": "outputs/report.md",
                        "size": 42,
                        "content_type": "text/markdown",
                    },
                ],
            )
            session.add_all([user, thread, subagent])
            await session.commit()

            recorder = DagRunRecorder(
                session=session,
                user_id=user.id,
                thread_id=thread.id,
            )

            await recorder.record({
                "type": "dag_started",
                "dag_run_id": "dag-1",
                "goal": "ship market report",
                "nodes": [
                    {
                        "id": "research",
                        "agent": "researcher",
                        "skill": "search",
                        "deps": [],
                        "args": {"topic": "markets"},
                    },
                ],
            })
            await recorder.record({
                "type": "dag_node_started",
                "dag_run_id": "dag-1",
                "node_id": "research",
                "agent": "researcher",
                "skill": "search",
                "deps": [],
                "args_preview": {"topic": "markets"},
            })
            complete_event = {
                "type": "dag_node_complete",
                "dag_run_id": "dag-1",
                "node_id": "research",
                "agent": "researcher",
                "skill": "search",
                "ok": True,
                "grant_id": "grant-1",
                "summary": "created report",
                "result": {"path": "outputs/report.md"},
                "elapsed_ms": 120,
            }
            await recorder.record(complete_event)
            await recorder.record({
                "type": "dag_complete",
                "dag_run_id": "dag-1",
                "ok": True,
                "summary": "completed 1/1 nodes",
            })

            run = await get_dag_run("dag-1", user=user, session=session)

            assert run.thread_id == "thread-1"
            assert run.goal == "ship market report"
            assert run.status == "complete"
            assert run.error is None
            assert run.summary == "completed 1/1 nodes"
            assert len(run.nodes) == 1
            assert run.nodes[0].status == "complete"
            assert run.nodes[0].args_json == '{"topic": "markets"}'
            assert run.nodes[0].file_ops == [
                {
                    "op": "create",
                    "path": "outputs/report.md",
                    "size": 42,
                    "content_type": "text/markdown",
                },
            ]
            assert complete_event["file_ops"] == run.nodes[0].file_ops
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dag_run_response_surfaces_failed_node_error() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="planner@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            recorder = DagRunRecorder(
                session=session,
                user_id=user.id,
                thread_id=None,
            )

            await recorder.record({
                "type": "dag_started",
                "dag_run_id": "dag-error",
                "goal": "review ticket",
                "nodes": [
                    {
                        "id": "review",
                        "agent": "ticket-agent",
                        "skill": "review",
                        "deps": [],
                        "args": {"ticket": "REQ1"},
                    },
                ],
            })
            await recorder.record({
                "type": "dag_node_complete",
                "dag_run_id": "dag-error",
                "node_id": "review",
                "agent": "ticket-agent",
                "skill": "review",
                "ok": False,
                "grant_id": None,
                "summary": "validation failed",
                "result": {"error": "missing approval"},
                "elapsed_ms": 15,
            })
            await recorder.record({
                "type": "dag_complete",
                "dag_run_id": "dag-error",
                "ok": False,
                "summary": "completed 0/1 nodes",
            })

            run = await get_dag_run("dag-error", user=user, session=session)

            assert run.status == "error"
            assert run.error == "review: missing approval"
            assert run.nodes[0].status == "error"
            assert run.nodes[0].error == "missing approval"
    finally:
        await engine.dispose()
