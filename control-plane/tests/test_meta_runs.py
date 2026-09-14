from __future__ import annotations

import os

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from control_plane.auth import issue_token
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.models import Agent, User
from control_plane.routes.meta_runs import router as meta_runs_router


@pytest.fixture
async def meta_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        session.add_all(
            [
                User(id=7, email="owner@example.com", password_hash="x"),
                User(id=8, email="other@example.com", password_hash="x"),
                Agent(
                    id=11,
                    owner_id=7,
                    name="meta-agent",
                    description="",
                    version="0.1.0",
                    image="example/meta-agent:latest",
                    public=False,
                    status="ready",
                    card={},
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(meta_runs_router)

    async def override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    await engine.dispose()


def _auth(user_id: int = 7) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


@pytest.mark.asyncio
async def test_meta_run_create_patch_get_and_list(meta_client: AsyncClient) -> None:
    create = await meta_client.post(
        "/v1/agents/meta-agent/meta-runs",
        headers=_auth(),
        json={
            "run_id": "run-1",
            "thread_id": "thread-1",
            "goal": "build a research meta-agent",
            "success_criteria": ["plan exists", "all nodes pass"],
            "current_plan": {"nodes": []},
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["run_id"] == "run-1"
    assert body["status"] == "planning"
    assert body["completed_at"] is None

    patch = await meta_client.patch(
        "/v1/agents/meta-agent/meta-runs/run-1",
        headers=_auth(),
        json={
            "status": "complete",
            "progress": [{"node_id": "draft", "status": "complete"}],
            "state": {"resume_from": "done"},
            "summary": "finished",
        },
    )
    assert patch.status_code == 200
    updated = patch.json()
    assert updated["status"] == "complete"
    assert updated["completed_at"] is not None
    assert updated["progress"][0]["node_id"] == "draft"

    read = await meta_client.get(
        "/v1/agents/meta-agent/meta-runs/run-1",
        headers=_auth(),
    )
    assert read.status_code == 200
    assert read.json()["state"] == {"resume_from": "done"}

    listed = await meta_client.get(
        "/v1/agents/meta-agent/meta-runs?thread_id=thread-1&status=complete",
        headers=_auth(),
    )
    assert listed.status_code == 200
    assert [item["run_id"] for item in listed.json()] == ["run-1"]


@pytest.mark.asyncio
async def test_meta_run_current_plan_is_typed_and_patchable(
    meta_client: AsyncClient,
) -> None:
    plan = {
        "ok": True,
        "goal": "build a report",
        "max_nodes": 4,
        "max_parallel": 2,
        "rounds": [["draft"]],
        "nodes": [
            {
                "id": "draft",
                "agent": "writer",
                "skill": "write",
                "args": {"topic": "kernel"},
                "deps": [],
                "expected_outputs": ["summary"],
                "score": 0.91,
            }
        ],
        "planner": "meta-dag",
    }
    created = await meta_client.post(
        "/v1/agents/meta-agent/meta-runs",
        headers=_auth(),
        json={"run_id": "typed-plan", "goal": "typed plan", "current_plan": plan},
    )

    assert created.status_code == 201
    current_plan = created.json()["current_plan"]
    assert current_plan["nodes"][0]["id"] == "draft"
    assert current_plan["nodes"][0]["agent"] == "writer"
    assert current_plan["nodes"][0]["score"] == 0.91
    assert current_plan["planner"] == "meta-dag"

    patched = await meta_client.patch(
        "/v1/agents/meta-agent/meta-runs/typed-plan",
        headers=_auth(),
        json={
            "current_plan": {
                "rounds": [["review"]],
                "nodes": [
                    {
                        "id": "review",
                        "agent": "critic",
                        "skill": "check",
                        "args": {},
                        "deps": ["draft"],
                    }
                ],
            }
        },
    )

    assert patched.status_code == 200
    assert patched.json()["current_plan"]["nodes"][0]["id"] == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_plan",
    [
        {"nodes": {"draft": {"agent": "writer", "skill": "write"}}},
        {"nodes": [{"agent": "writer", "skill": "write", "args": {}}]},
        {"nodes": [{"id": "draft", "agent": "writer", "skill": "write", "args": []}]},
        {"rounds": ["draft"]},
        {"max_nodes": 0},
    ],
)
async def test_meta_run_rejects_malformed_current_plan(
    meta_client: AsyncClient,
    current_plan: dict,
) -> None:
    created = await meta_client.post(
        "/v1/agents/meta-agent/meta-runs",
        headers=_auth(),
        json={"run_id": "bad-plan", "goal": "bad plan", "current_plan": current_plan},
    )

    assert created.status_code == 422

    seed = await meta_client.post(
        "/v1/agents/meta-agent/meta-runs",
        headers=_auth(),
        json={"run_id": f"seed-{abs(hash(str(current_plan)))}", "goal": "seed"},
    )
    assert seed.status_code == 201

    patched = await meta_client.patch(
        f"/v1/agents/meta-agent/meta-runs/{seed.json()['run_id']}",
        headers=_auth(),
        json={"current_plan": current_plan},
    )

    assert patched.status_code == 422


@pytest.mark.asyncio
async def test_meta_runs_are_owner_scoped(meta_client: AsyncClient) -> None:
    resp = await meta_client.post(
        "/v1/agents/meta-agent/meta-runs",
        headers=_auth(user_id=8),
        json={"goal": "steal state"},
    )

    assert resp.status_code == 404
