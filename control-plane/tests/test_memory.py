from __future__ import annotations

import os
from types import SimpleNamespace

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
from control_plane.routes import memory as memory_routes
from control_plane.routes.memory import router as memory_router


@pytest.fixture
async def memory_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(
        memory_routes,
        "memory_vector_search",
        SimpleNamespace(configured=False),
    )
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
    app.include_router(memory_router)

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
async def test_agent_memory_crud_and_list(memory_client: AsyncClient) -> None:
    create = await memory_client.put(
        "/v1/agents/meta-agent/memory",
        headers=_auth(),
        json={
            "namespace": "plans",
            "key": "current",
            "value": {"goal": "ship kv memory"},
            "metadata": {"source": "test"},
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["agent_name"] == "meta-agent"
    assert body["value"] == {"goal": "ship kv memory"}
    assert body["metadata"] == {"source": "test"}

    read = await memory_client.get(
        "/v1/agents/meta-agent/memory/plans/current",
        headers=_auth(),
    )
    assert read.status_code == 200
    assert read.json()["key"] == "current"

    listed = await memory_client.get(
        "/v1/agents/meta-agent/memory?namespace=plans&q=kv",
        headers=_auth(),
    )
    assert listed.status_code == 200
    assert [item["key"] for item in listed.json()] == ["current"]

    update = await memory_client.post(
        "/v1/agents/meta-agent/memory",
        headers=_auth(),
        json={
            "namespace": "plans",
            "key": "current",
            "value": {"goal": "ship kv memory", "status": "done"},
        },
    )
    assert update.status_code == 201
    assert update.json()["value"]["status"] == "done"

    delete = await memory_client.delete(
        "/v1/agents/meta-agent/memory/plans/current",
        headers=_auth(),
    )
    assert delete.status_code == 204

    missing = await memory_client.get(
        "/v1/agents/meta-agent/memory/plans/current",
        headers=_auth(),
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_agent_memory_semantic_search_ranks_embedded_notes(
    memory_client: AsyncClient,
) -> None:
    for key, value in (
        ("python-agent", {"summary": "Python code generation and pytest fixes"}),
        ("billing-agent", {"summary": "Invoices, payments, and account balances"}),
    ):
        resp = await memory_client.put(
            "/v1/agents/meta-agent/memory",
            headers=_auth(),
            json={
                "namespace": "notes",
                "key": key,
                "value": value,
                "metadata": {"source": "test"},
            },
        )
        assert resp.status_code == 200

    search = await memory_client.get(
        "/v1/agents/meta-agent/memory/search?namespace=notes&q=python%20pytest&limit=2",
        headers=_auth(),
    )

    assert search.status_code == 200
    body = search.json()
    assert [item["key"] for item in body] == ["python-agent", "billing-agent"]
    assert body[0]["score"] > body[1]["score"]


@pytest.mark.asyncio
async def test_agent_memory_vector_search_uses_qdrant_index(
    memory_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMemoryVectorSearch:
        configured = True

        def __init__(self) -> None:
            self.upserts: list[tuple[int, str]] = []
            self.deletes: list[int] = []
            self.query_kwargs: dict[str, object] = {}
            self.matches: list[SimpleNamespace] = []

        async def upsert_memory(self, row) -> None:
            self.upserts.append((row.id, row.key))

        async def delete_memory(self, memory_id: int) -> None:
            self.deletes.append(memory_id)

        async def query_memory(self, query: str, **kwargs):
            self.query_kwargs = {"query": query, **kwargs}
            return self.matches

    fake = FakeMemoryVectorSearch()
    monkeypatch.setattr(memory_routes, "memory_vector_search", fake)

    first = await memory_client.put(
        "/v1/agents/meta-agent/memory",
        headers=_auth(),
        json={
            "namespace": "notes",
            "key": "python-agent",
            "value": {"summary": "Python code generation"},
        },
    )
    second = await memory_client.put(
        "/v1/agents/meta-agent/memory",
        headers=_auth(),
        json={
            "namespace": "notes",
            "key": "billing-agent",
            "value": {"summary": "Invoices and payments"},
        },
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    fake.matches = [
        SimpleNamespace(memory_id=second_id, score=0.93),
        SimpleNamespace(memory_id=first_id, score=0.82),
    ]

    search = await memory_client.get(
        "/v1/agents/meta-agent/memory/search?namespace=notes&q=payment%20agent&limit=2",
        headers=_auth(),
    )

    assert search.status_code == 200
    body = search.json()
    assert [item["key"] for item in body] == ["billing-agent", "python-agent"]
    assert [item["score"] for item in body] == [0.93, 0.82]
    assert fake.upserts == [(first_id, "python-agent"), (second_id, "billing-agent")]
    assert fake.query_kwargs == {
        "query": "payment agent",
        "agent_id": 11,
        "user_id": 7,
        "namespace": "notes",
        "limit": 2,
        "score_threshold": settings.memory_semantic_score_threshold,
    }

    delete = await memory_client.delete(
        "/v1/agents/meta-agent/memory/notes/python-agent",
        headers=_auth(),
    )

    assert delete.status_code == 204
    assert fake.deletes == [first_id]


@pytest.mark.asyncio
async def test_agent_memory_is_owner_scoped(memory_client: AsyncClient) -> None:
    resp = await memory_client.put(
        "/v1/agents/meta-agent/memory",
        headers=_auth(user_id=8),
        json={"namespace": "notes", "key": "x", "value": "nope"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_memory_rejects_unsafe_keys(memory_client: AsyncClient) -> None:
    resp = await memory_client.put(
        "/v1/agents/meta-agent/memory",
        headers=_auth(),
        json={"namespace": "notes", "key": "../secret", "value": "nope"},
    )

    assert resp.status_code == 400
    assert "safe relative path" in resp.text
