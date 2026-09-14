from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane.auth import current_user
from control_plane.db import Base, get_session
from control_plane.models import (
    Agent,
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
    Organization,
    OrganizationMember,
    User,
)
from control_plane.routes.databases import router as databases_router


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


@pytest.fixture
async def user(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session:
        row = User(email="owner@example.com", password_hash="x")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(databases_router)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_user() -> User:
        return user

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_user] = _override_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _add_org_member(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    role: str = "owner",
    slug: str = "acme",
) -> Organization:
    async with session_factory() as session:
        org = Organization(slug=slug, name="Acme", created_by_id=user.id)
        session.add(org)
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role=role,
            )
        )
        await session.commit()
        await session.refresh(org)
        return org


@pytest.mark.asyncio
async def test_create_personal_database_queues_project_branch_and_event(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    response = await client.post(
        "/v1/me/databases",
        json={"name": "App", "display_name": "Application DB"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "app"
    assert body["display_name"] == "Application DB"
    assert body["scope"] == "user"
    assert body["status"] == "pending"
    assert body["default_branch_name"] == "main"

    async with session_factory() as session:
        project = (
            await session.execute(select(DatabaseProject).where(DatabaseProject.name == "app"))
        ).scalar_one()
        branch = (
            await session.execute(
                select(DatabaseBranch).where(DatabaseBranch.project_id == project.id)
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(DatabaseProvisionEvent).where(
                    DatabaseProvisionEvent.database_project_id == project.id
                )
            )
        ).scalar_one()
        assert project.owner_id == user.id
        assert project.organization_id is None
        assert branch.name == "main"
        assert branch.protected is True
        assert event.event_type == "database_project_requested"
        assert event.status == "queued"


@pytest.mark.asyncio
async def test_create_org_database_requires_admin_member(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    await _add_org_member(session_factory, user, role="member")

    denied = await client.post(
        "/v1/me/databases",
        json={"name": "analytics", "scope": "org", "organization_slug": "acme"},
    )
    assert denied.status_code == 403

    async with session_factory() as session:
        member = (
            await session.execute(
                select(OrganizationMember).where(OrganizationMember.user_id == user.id)
            )
        ).scalar_one()
        member.role = "admin"
        await session.commit()

    allowed = await client.post(
        "/v1/me/databases",
        json={"name": "analytics", "scope": "org", "organization_slug": "acme"},
    )
    assert allowed.status_code == 201
    assert allowed.json()["scope"] == "org"
    assert allowed.json()["organization_slug"] == "acme"


@pytest.mark.asyncio
async def test_list_database_projects_returns_visible_user_and_org_projects(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    org = await _add_org_member(session_factory, user, role="admin")
    async with session_factory() as session:
        session.add_all(
            [
                DatabaseProject(
                    owner_id=user.id,
                    created_by_id=user.id,
                    name="personal",
                    display_name="Personal",
                    status="ready",
                ),
                DatabaseProject(
                    organization_id=org.id,
                    created_by_id=user.id,
                    name="shared",
                    display_name="Shared",
                    status="pending",
                ),
            ]
        )
        await session.commit()

    all_response = await client.get("/v1/me/databases")
    assert all_response.status_code == 200
    by_name = {item["name"]: item for item in all_response.json()}
    assert by_name["personal"]["scope"] == "user"
    assert by_name["shared"]["scope"] == "org"
    assert by_name["shared"]["organization_slug"] == "acme"

    org_response = await client.get("/v1/me/databases?organization_slug=acme")
    assert org_response.status_code == 200
    assert [item["name"] for item in org_response.json()] == ["shared"]


@pytest.mark.asyncio
async def test_get_database_project_returns_operational_detail(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    async with session_factory() as session:
        agent = Agent(
            owner_id=user.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/db-agent:latest",
            public=False,
            status="ready",
            card={},
        )
        project = DatabaseProject(
            owner_id=user.id,
            created_by_id=user.id,
            name="app",
            display_name="App",
            status="ready",
            project_ref="tenant-1",
        )
        session.add_all([agent, project])
        await session.flush()
        branch = DatabaseBranch(
            project_id=project.id,
            name="main",
            status="ready",
            branch_ref="timeline-1",
            protected=True,
        )
        role = DatabaseRole(
            project_id=project.id,
            name="agent-app-rw",
            username="agent_app_rw",
            database_name="app",
            access_mode="read_write",
            status="ready",
            secret_ref="db-agent-agent-secrets",
        )
        session.add_all([branch, role])
        await session.flush()
        session.add_all(
            [
                AgentDatabaseBinding(
                    agent_id=agent.id,
                    user_id=user.id,
                    database_project_id=project.id,
                    database_branch_id=branch.id,
                    database_role_id=role.id,
                    agent_name=agent.name,
                    binding_name="app",
                    env_var="DATABASE_URL",
                    access_mode="read_write",
                    status="ready",
                    metadata_json={"runtime_secret": "db-agent-agent-secrets"},
                ),
                DatabaseProvisionEvent(
                    database_project_id=project.id,
                    agent_id=agent.id,
                    event_type="database_binding_ready",
                    status="ok",
                    message="ready",
                    data={"runtime_secret": "db-agent-agent-secrets"},
                ),
            ]
        )
        await session.commit()
        project_id = project.id

    response = await client.get(f"/v1/me/databases/{project_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["project_ref"] == "tenant-1"
    assert body["branches"][0]["branch_ref"] == "timeline-1"
    assert body["roles"][0]["secret_ref"] == "db-agent-agent-secrets"
    assert body["bindings"][0]["env_var"] == "DATABASE_URL"
    assert body["events"][0]["event_type"] == "database_binding_ready"


@pytest.mark.asyncio
async def test_database_provisioner_status_is_exposed(client: AsyncClient) -> None:
    response = await client.get("/v1/me/databases/provisioner-status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"enabled", "configured", "ok", "metrics"}
    assert "queue_depth" in body["metrics"]


@pytest.mark.asyncio
async def test_create_database_rejects_duplicate_user_project(
    client: AsyncClient,
) -> None:
    first = await client.post("/v1/me/databases", json={"name": "app"})
    second = await client.post("/v1/me/databases", json={"name": "app"})

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "database project already exists"


@pytest.mark.asyncio
async def test_create_database_rejects_bad_scope_and_names(client: AsyncClient) -> None:
    missing_org = await client.post(
        "/v1/me/databases",
        json={"name": "app", "scope": "org"},
    )
    bad_name = await client.post("/v1/me/databases", json={"name": "Bad_Name"})

    assert missing_org.status_code == 400
    assert bad_name.status_code == 400
