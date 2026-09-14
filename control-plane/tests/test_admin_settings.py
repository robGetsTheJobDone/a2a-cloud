"""Tests for the admin platform-settings endpoints + helpers."""
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import platform_settings
from control_plane.db import Base, get_session
from control_plane.routes.admin import router as admin_router


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
    app.include_router(admin_router)

    async def _override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- helpers ----

@pytest.mark.asyncio
async def test_get_setting_returns_default_when_missing(session_factory) -> None:
    async with session_factory() as session:
        value = await platform_settings.get_setting(session, "missing", default="fallback")
    assert value == "fallback"


@pytest.mark.asyncio
async def test_set_setting_upserts(session_factory) -> None:
    async with session_factory() as session:
        row = await platform_settings.set_setting(
            session, "reviewer_enabled", False, actor="alice"
        )
        assert row.value is False
        # Update path
        row2 = await platform_settings.set_setting(
            session, "reviewer_enabled", True, actor="bob"
        )
        assert row2.id == row.id and row2.value is True
        assert row2.updated_by == "bob"


@pytest.mark.asyncio
async def test_is_reviewer_enabled_defaults_true_then_follows_db(
    session_factory,
) -> None:
    async with session_factory() as session:
        assert (await platform_settings.is_reviewer_enabled(session)) is True
        await platform_settings.set_setting(session, "reviewer_enabled", False)
    async with session_factory() as session:
        assert (await platform_settings.is_reviewer_enabled(session)) is False


@pytest.mark.asyncio
async def test_is_collective_runtime_enabled_defaults_true_then_follows_db(
    session_factory,
) -> None:
    async with session_factory() as session:
        assert (await platform_settings.is_collective_runtime_enabled(session)) is True
        await platform_settings.set_setting(session, "collective_runtime_enabled", False)
    async with session_factory() as session:
        assert (await platform_settings.is_collective_runtime_enabled(session)) is False


# ---- endpoints ----

@pytest.mark.asyncio
async def test_admin_endpoints_require_token(client) -> None:
    resp = await client.get("/v1/admin/settings")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_endpoints_reject_wrong_token(client) -> None:
    resp = await client.get(
        "/v1/admin/settings",
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_settings_returns_known_keys_with_defaults(client) -> None:
    resp = await client.get(
        "/v1/admin/settings",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert resp.status_code == 200
    keys = [s["key"] for s in resp.json()]
    assert "reviewer_enabled" in keys
    assert "collective_runtime_enabled" in keys
    item = next(s for s in resp.json() if s["key"] == "reviewer_enabled")
    assert item["value"] is True  # default
    assert item["updated_at"] is None


@pytest.mark.asyncio
async def test_put_setting_changes_value(client) -> None:
    resp = await client.put(
        "/v1/admin/settings/reviewer_enabled",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"value": False, "actor": "admin-ui"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] is False
    assert body["updated_by"] == "admin-ui"

    resp = await client.get(
        "/v1/admin/settings/reviewer_enabled",
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] is False


@pytest.mark.asyncio
async def test_put_setting_rejects_unknown_key(client) -> None:
    resp = await client.put(
        "/v1/admin/settings/not-a-real-key",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"value": True},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_setting_rejects_wrong_type(client) -> None:
    resp = await client.put(
        "/v1/admin/settings/reviewer_enabled",
        headers={"Authorization": "Bearer test-admin-secret"},
        json={"value": "yes"},
    )
    assert resp.status_code == 400
