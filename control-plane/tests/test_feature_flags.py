from __future__ import annotations

import os

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

from control_plane.auth import current_user
from control_plane.db import Base, get_session
from control_plane.models import FeatureFlag, User, UserFeatureFlag
from control_plane.routes import feature_flags


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def alice(session_factory) -> User:
    async with session_factory() as session:
        user = User(email="alice@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
async def client(session_factory, alice: User):
    app = FastAPI()
    app.include_router(feature_flags.router)
    app.include_router(feature_flags.admin_router)

    async def _override_session():
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> User:
        return alice

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_user] = _override_current_user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_current_feature_flags_default_closed(client: AsyncClient) -> None:
    resp = await client.get("/v1/me/feature-flags")
    assert resp.status_code == 200
    assert resp.json() == {"enabled_keys": []}


@pytest.mark.asyncio
async def test_admin_can_assign_user_feature_flags(
    client: AsyncClient,
    alice: User,
    session_factory,
) -> None:
    list_resp = await client.get(
        "/v1/admin/feature-flags",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert list_resp.status_code == 200
    assert {row["key"] for row in list_resp.json()} >= {
        "dashboard.simulations",
        "dashboard.bounties",
        "dashboard.organization",
    }

    update_resp = await client.put(
        f"/v1/admin/users/{alice.id}/feature-flags",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"enabled_keys": ["dashboard.simulations", "dashboard.bounties"]},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["enabled_keys"] == [
        "dashboard.bounties",
        "dashboard.simulations",
    ]

    current_resp = await client.get("/v1/me/feature-flags")
    assert current_resp.status_code == 200
    assert current_resp.json()["enabled_keys"] == [
        "dashboard.bounties",
        "dashboard.simulations",
    ]

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(UserFeatureFlag).where(UserFeatureFlag.user_id == alice.id)
            )
        ).scalars().all()
        assert sorted(row.flag_key for row in rows) == [
            "dashboard.bounties",
            "dashboard.simulations",
        ]


@pytest.mark.asyncio
async def test_admin_can_create_and_remove_feature_flag(
    client: AsyncClient,
    alice: User,
    session_factory,
) -> None:
    create_resp = await client.post(
        "/v1/admin/feature-flags",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={
            "key": "dashboard.experimental",
            "label": "Dashboard experimental nav",
            "description": "A test flag",
            "default_enabled": False,
        },
    )
    assert create_resp.status_code == 200
    assert create_resp.json()["key"] == "dashboard.experimental"

    assign_resp = await client.put(
        f"/v1/admin/users/{alice.id}/feature-flags",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"enabled_keys": ["dashboard.experimental"]},
    )
    assert assign_resp.status_code == 200
    assert assign_resp.json()["enabled_keys"] == ["dashboard.experimental"]

    delete_resp = await client.delete(
        "/v1/admin/feature-flags/dashboard.experimental",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert delete_resp.status_code == 204

    read_resp = await client.get(
        f"/v1/admin/users/{alice.id}/feature-flags",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert read_resp.status_code == 200
    assert "dashboard.experimental" not in read_resp.json()["enabled_keys"]

    async with session_factory() as session:
        flag = await session.get(FeatureFlag, "dashboard.experimental")
        assert flag is not None
        assert flag.deleted_at is not None
        assignments = (
            await session.execute(
                select(UserFeatureFlag).where(
                    UserFeatureFlag.flag_key == "dashboard.experimental"
                )
            )
        ).scalars().all()
        assert assignments == []
