from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane.auth import current_user
from control_plane.db import Base, get_session
from control_plane.models import (
    Agent,
    AgentMemoryEntry,
    AgentReviewRun,
    DagRunNode,
    User,
    UserControlPolicy,
    WorkEvent,
    WorkJob,
)
from control_plane.platform_settings import set_setting
from control_plane.routes.collective_runtime import router


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def seeded_client(session_factory):
    async with session_factory() as session:
        user = User(email="local@example.com", password_hash="x", is_admin=True)
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="code-editor-agent",
            description="Code editing, tests, review, and repo refactors",
            version="0.1.0",
            image="registry.example/code-editor-agent:latest",
            public=False,
            status="running",
            url="https://code-editor-agent.example",
            card={
                "skills": [{"name": "code"}, {"name": "review"}],
                "runtime": {"tools_used": ["git", "pytest"]},
            },
        )
        session.add(agent)
        await session.flush()
        session.add_all([
            WorkJob(
                job_id="job-ok",
                kind="agent_api_invoke",
                status="complete",
                user_id=user.id,
                worker_name=agent.name,
                queue="agents",
                input_payload={},
                output_payload={},
                metadata_json={},
            ),
            WorkEvent(
                event_id="evt-job-ok-1",
                job_id="job-ok",
                event_seq=1,
                event_type="job_verified",
                status="complete",
                payload={"file_ops": [{"path": "app.py", "op": "update"}]},
                metrics={
                    "tests_passed": 4,
                    "tests_failed": 0,
                    "review_result": "approved",
                    "task_outcome": "accepted",
                },
            ),
            WorkJob(
                job_id="job-failed",
                kind="agent_api_invoke",
                status="failed",
                error="tests failed",
                user_id=user.id,
                worker_name=agent.name,
                queue="agents",
                input_payload={},
                output_payload={},
                metadata_json={},
            ),
            WorkEvent(
                event_id="evt-job-failed-1",
                job_id="job-failed",
                event_seq=1,
                event_type="job_verified",
                status="failed",
                payload={},
                metrics={
                    "tests_passed": 1,
                    "tests_failed": 2,
                    "review_result": "blocked",
                },
            ),
            AgentReviewRun(
                review_id="review-1",
                agent_id=agent.id,
                agent_name=agent.name,
                user_id=user.id,
                status="warning",
                warning_count=1,
            ),
            AgentMemoryEntry(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                namespace="collective-runtime",
                key="lesson/1",
                value={"lesson": "run tests before handoff"},
                metadata_json={},
            ),
            DagRunNode(
                dag_run_id="dag-1",
                node_id="coder",
                user_id=user.id,
                agent_name=agent.name,
                skill_name="turn",
                deps=[],
                args_json="{}",
                status="complete",
                result={},
                file_ops=[],
            ),
        ])
        await session.commit()
        user_id = user.id

    app = FastAPI()
    app.include_router(router)

    async def _override_session():
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> User:
        return User(id=user_id, email="local@example.com", password_hash="x", is_admin=True)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_user] = _override_current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, session_factory, user_id


@pytest.mark.asyncio
async def test_collective_runtime_protocols_and_registry(seeded_client) -> None:
    client, _session_factory, _user_id = seeded_client
    protocols = await client.get("/v1/me/collective-runtime/protocols")
    assert protocols.status_code == 200
    assert {item["id"] for item in protocols.json()} >= {
        "code_change_with_review",
        "research_parallel_scouts",
        "security_review",
    }

    registry = await client.get("/v1/me/collective-runtime/registry")
    assert registry.status_code == 200
    [agent] = registry.json()["agents"]
    assert agent["name"] == "code-editor-agent"
    assert agent["total_jobs"] == 2
    assert agent["completed_jobs"] == 1
    assert agent["failed_jobs"] == 1
    assert agent["memory_count"] == 1
    assert agent["dag_node_count"] == 1
    assert agent["outcome_score"] > 0
    assert "code" in agent["capabilities"]
    assert "pytest" in agent["tools"]

    scorecards = await client.get("/v1/me/collective-runtime/scorecards")
    assert scorecards.status_code == 200
    body = scorecards.json()
    assert len(body["recent_jobs"]) == 2
    job_ok = next(item for item in body["recent_jobs"] if item["job_id"] == "job-ok")
    assert job_ok["score"] > 0.8
    assert job_ok["metrics"]["tests_passed"] == 4
    assert body["protocols"][0]["id"] == "code_change_with_review"

    extracted = await client.post(
        "/v1/me/collective-runtime/memories/extract",
        json={"limit": 10},
    )
    assert extracted.status_code == 200
    assert extracted.json()["created_count"] == 2

    memories = await client.get("/v1/me/collective-runtime/memories")
    assert memories.status_code == 200
    assert any(item["key"] == "work-jobs/job-ok" for item in memories.json())

    plan = await client.post(
        "/v1/me/collective-runtime/plan",
        json={"goal": "Fix code tests using lessons from prior work"},
    )
    assert plan.status_code == 200
    assert any(item["key"] == "work-jobs/job-ok" for item in plan.json()["memory_refs"])


@pytest.mark.asyncio
async def test_collective_runtime_plan_uses_protocol_and_disabled_note(
    seeded_client,
) -> None:
    client, session_factory, user_id = seeded_client
    async with session_factory() as session:
        await set_setting(session, "collective_runtime_enabled", False, actor="test")
        session.add(
            UserControlPolicy(
                user_id=user_id,
                require_approval_for_file_writes=True,
                only_approved_agents=True,
                approved_agents=[],
            )
        )
        await session.commit()

    resp = await client.post(
        "/v1/me/collective-runtime/plan",
        json={"goal": "Fix the auth permission bug and review the change", "risk": "high"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol_id"] == "security_review"
    assert body["selected_agents"][0]["name"] == "code-editor-agent"
    assert body["topology"][0]["role"] == "planner"
    gate_ids = {item["id"] for item in body["approval_gates"] if item["required"]}
    assert {"high_risk_change", "file_write_approval", "approved_agent_policy"} <= gate_ids
    assert "Collective runtime is disabled; this plan is a preview only." in body["notes"]


@pytest.mark.asyncio
async def test_collective_runtime_run_queues_ready_nodes_and_advances(
    seeded_client,
) -> None:
    client, session_factory, _user_id = seeded_client

    resp = await client.post(
        "/v1/me/collective-runtime/runs",
        json={"goal": "Fix code tests using lessons from prior work"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["protocol_id"] == "code_change_with_review"
    assert body["blocked_gate_ids"] == []
    assert len(body["child_jobs"]) == 1
    assert body["child_jobs"][0]["node_id"] == "planner"
    planner = next(item for item in body["nodes"] if item["node_id"] == "planner")
    coder = next(item for item in body["nodes"] if item["node_id"] == "coder")
    assert planner["status"] == "queued"
    assert planner["child_job_id"] == body["child_jobs"][0]["job_id"]
    assert coder["status"] == "pending"

    async with session_factory() as session:
        child = (
            await session.execute(
                select(WorkJob).where(WorkJob.job_id == body["child_jobs"][0]["job_id"])
            )
        ).scalar_one()
        child.status = "complete"
        child.summary = "planner complete"
        child.output_payload = {"summary": "planner complete", "file_ops": []}
        child.completed_at = datetime.now(timezone.utc)
        await session.commit()

    advanced = await client.post(
        f"/v1/me/collective-runtime/runs/{body['dag_run_id']}/advance"
    )

    assert advanced.status_code == 200
    advanced_body = advanced.json()
    assert len(advanced_body["child_jobs"]) == 2
    assert {item["node_id"] for item in advanced_body["child_jobs"]} == {
        "planner",
        "coder",
    }
    nodes = {item["node_id"]: item for item in advanced_body["nodes"]}
    assert nodes["planner"]["status"] == "complete"
    assert nodes["coder"]["status"] == "queued"


@pytest.mark.asyncio
async def test_collective_runtime_run_blocks_for_approval_then_queues(
    seeded_client,
) -> None:
    client, session_factory, user_id = seeded_client
    async with session_factory() as session:
        session.add(
            UserControlPolicy(
                user_id=user_id,
                require_approval_for_file_writes=True,
                only_approved_agents=True,
                approved_agents=[],
            )
        )
        await session.commit()

    resp = await client.post(
        "/v1/me/collective-runtime/runs",
        json={"goal": "Fix the auth permission bug and review the change", "risk": "high"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "blocked"
    assert body["child_jobs"] == []
    assert {
        "high_risk_change",
        "file_write_approval",
        "approved_agent_policy",
    } <= set(body["blocked_gate_ids"])

    approved = await client.post(
        f"/v1/me/collective-runtime/runs/{body['dag_run_id']}/approve",
        json={"approved_gate_ids": body["blocked_gate_ids"]},
    )

    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["status"] == "running"
    assert approved_body["blocked_gate_ids"] == []
    assert len(approved_body["child_jobs"]) == 1
    assert approved_body["child_jobs"][0]["node_id"] == "planner"


@pytest.mark.asyncio
async def test_disabled_collective_runtime_rejects_run_creation(
    seeded_client,
) -> None:
    client, session_factory, _user_id = seeded_client
    async with session_factory() as session:
        await set_setting(session, "collective_runtime_enabled", False, actor="test")
        await session.commit()

    resp = await client.post(
        "/v1/me/collective-runtime/runs",
        json={"goal": "Fix code tests"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "collective runtime is disabled by platform admin"
