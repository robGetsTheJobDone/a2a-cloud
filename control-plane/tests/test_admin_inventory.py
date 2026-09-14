"""Tests for the admin inventory + purge endpoints."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)
os.environ["A2A_CP_ADMIN_TOKEN"] = "test-admin-secret"

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth import decode_token
from control_plane.db import Base, get_session
from control_plane.models import (
    Agent,
    AgentDeploymentEvent,
    GiteaTokenAudit,
    Organization,
    OrganizationAuditLog,
    OrganizationDomain,
    OrganizationMember,
    OrganizationScimToken,
    PlatformSetting,
    User,
    UserControlPolicy,
)
from control_plane.routes import admin as admin_routes


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(admin_routes.router)

    async def _override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_user(
    factory,
    *,
    email: str,
    created_at: datetime | None = None,
) -> User:
    async with factory() as session:
        user = User(email=email, password_hash="x")
        if created_at is not None:
            user.created_at = created_at
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _make_agent(
    factory,
    *,
    name: str,
    owner_id: int,
    gitea_owner: str | None = None,
) -> Agent:
    async with factory() as session:
        agent = Agent(
            owner_id=owner_id,
            gitea_owner=gitea_owner,
            name=name,
            description="",
            version="0.1.0",
            image="registry.a2acloud.io/agents/test:latest",
            public=True,
            status="ready",
            card={},
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


@pytest.mark.asyncio
async def test_list_users_returns_counts(client, session_factory) -> None:
    now = datetime.now(timezone.utc)
    alice = await _make_user(session_factory, email="alice@example.com", created_at=now)
    await _make_user(session_factory, email="bob@example.com")
    await _make_agent(session_factory, name="alpha", owner_id=alice.id, gitea_owner="gitea_admin")
    await _make_agent(session_factory, name="beta", owner_id=alice.id)

    resp = await client.get(
        "/v1/admin/users",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totals"] == {"users": 2, "agents": 2, "repos": 1}
    assert body["signups"] == {"last_24h": 2, "last_7d": 2, "last_30d": 2}
    assert [row["email"] for row in body["users"]] == ["alice@example.com", "bob@example.com"]
    alice_row = body["users"][0]
    assert alice_row["agent_count"] == 2
    assert alice_row["repo_count"] == 1
    bob_row = body["users"][1]
    assert bob_row["agent_count"] == 0
    assert bob_row["repo_count"] == 0


@pytest.mark.asyncio
async def test_admin_can_read_and_update_user_control_policy(client, session_factory) -> None:
    alice = await _make_user(session_factory, email="alice@example.com")

    read_resp = await client.get(
        f"/v1/admin/users/{alice.id}/control-policy",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert read_resp.status_code == 200
    assert read_resp.json()["policy"]["max_agent_calls_per_run"] == 8

    update_resp = await client.put(
        f"/v1/admin/users/{alice.id}/control-policy",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={
            "monthly_budget_cents": 12345,
            "run_budget_cents": 250,
            "max_agent_calls_per_run": 99,
            "require_approval_for_file_writes": True,
            "deny_external_network": True,
            "only_approved_agents": True,
            "pii_safe_mode": True,
            "approved_agents": [" search-agent ", "writer"],
        },
    )
    assert update_resp.status_code == 200
    policy = update_resp.json()["policy"]
    assert policy == {
        "monthly_budget_cents": 12345,
        "run_budget_cents": 250,
        "max_agent_calls_per_run": 99,
        "require_approval_for_file_writes": True,
        "deny_external_network": True,
        "only_approved_agents": True,
        "pii_safe_mode": True,
        "approved_agents": ["search-agent", "writer"],
    }

    list_resp = await client.get(
        "/v1/admin/users",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert list_resp.status_code == 200
    assert list_resp.json()["users"][0]["control_policy"] == policy

    async with session_factory() as session:
        row = (
            await session.execute(
                select(UserControlPolicy).where(UserControlPolicy.user_id == alice.id)
            )
        ).scalar_one()
        assert row.max_agent_calls_per_run == 99


@pytest.mark.asyncio
async def test_admin_can_mint_platform_token(client, session_factory) -> None:
    alice = await _make_user(session_factory, email="alice@example.com")

    resp = await client.post(
        f"/v1/admin/users/{alice.id}/platform-token",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"ttl_seconds": 600, "label": "route testing"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == alice.id
    assert body["email"] == "alice@example.com"
    assert body["token_type"] == "Bearer"
    assert body["ttl_seconds"] == 600
    assert body["label"] == "route testing"
    assert decode_token(body["token"]) == alice.id


@pytest.mark.asyncio
async def test_keycloak_admin_auth_requires_admin_flag(
    client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = await _make_user(session_factory, email="admin@example.com")
    async with session_factory() as session:
        row = await session.get(User, admin.id)
        assert row is not None
        row.is_admin = True
        await session.commit()

    def fake_verify(token: str, *, audience: str) -> dict:
        assert token == "admin-id-token"
        assert audience == "a2acloud-admin"
        return {
            "sub": "kc-admin",
            "email": "admin@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(admin_routes, "verify_keycloak_id_token", fake_verify)

    resp = await client.post(
        "/v1/admin/auth/keycloak",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"id_token": "admin-id-token"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "id": admin.id,
        "email": "admin@example.com",
        "is_admin": True,
    }


@pytest.mark.asyncio
async def test_keycloak_admin_auth_rejects_non_admin(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_verify(token: str, *, audience: str) -> dict:
        assert token == "user-id-token"
        assert audience == "a2acloud-admin"
        return {
            "sub": "kc-user",
            "email": "user@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(admin_routes, "verify_keycloak_id_token", fake_verify)

    resp = await client.post(
        "/v1/admin/auth/keycloak",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"id_token": "user-id-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_purge_users_and_agents_clears_records(
    client,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = await _make_user(session_factory, email="alice@example.com")
    bob = await _make_user(session_factory, email="bob@example.com")
    await _make_agent(session_factory, name="alpha", owner_id=alice.id, gitea_owner="gitea_admin")
    await _make_agent(session_factory, name="beta", owner_id=bob.id)

    async with session_factory() as session:
        session.add(
            PlatformSetting(
                key="reviewer_enabled",
                value=True,
                description="keep me",
                updated_by="admin",
            )
        )
        await session.commit()

    async with session_factory() as session:
        org = Organization(slug="acme", name="Acme", created_by_id=alice.id)
        session.add(org)
        await session.flush()
        session.add_all(
            [
                OrganizationMember(organization_id=org.id, user_id=alice.id, role="owner"),
                OrganizationScimToken(
                    organization_id=org.id,
                    label="bootstrap",
                    token_hash="hash",
                    token_last4="abcd",
                    created_by_id=alice.id,
                ),
                OrganizationAuditLog(
                    organization_id=org.id,
                    actor_user_id=alice.id,
                    actor="alice@example.com",
                    action="created",
                    target_type="organization",
                    target_id="acme",
                    target_email=None,
                    data={},
                ),
                OrganizationDomain(
                    organization_id=org.id,
                    domain="acme.example.com",
                    verification_token="token",
                ),
                AgentDeploymentEvent(
                    deployment_id=1,
                    deploy_id="deploy-1",
                    agent_name="alpha",
                    stage="source",
                    status="passed",
                    message="ok",
                    data={},
                ),
                GiteaTokenAudit(
                    token_name="token-1",
                    username="reader",
                    scopes=["read:repository"],
                    owner="gitea_admin",
                    repo="alpha",
                    permission="read",
                    issued_by_user_id=alice.id,
                    purpose=None,
                    token_secret_hash="hash",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            ]
        )
        await session.commit()

    monkeypatch.setattr(admin_routes, "_cleanup_agent_resources", lambda *args, **kwargs: [])
    monkeypatch.setattr(admin_routes, "_cleanup_external_agent_resources", lambda *args, **kwargs: [])

    resp = await client.delete(
        "/v1/admin/users",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted_users": 2, "deleted_agents": 2}

    async with session_factory() as session:
        assert (await session.execute(select(User))).scalars().all() == []
        assert (await session.execute(select(Agent))).scalars().all() == []
        assert (await session.execute(select(Organization))).scalars().all() == []
        assert (await session.execute(select(OrganizationMember))).scalars().all() == []
        assert (await session.execute(select(OrganizationScimToken))).scalars().all() == []
        assert (await session.execute(select(OrganizationAuditLog))).scalars().all() == []
        assert (await session.execute(select(OrganizationDomain))).scalars().all() == []
        assert (await session.execute(select(AgentDeploymentEvent))).scalars().all() == []
        assert (await session.execute(select(GiteaTokenAudit))).scalars().all() == []
        settings = (await session.execute(select(PlatformSetting))).scalars().all()
        assert len(settings) == 1
        assert settings[0].key == "reviewer_enabled"
