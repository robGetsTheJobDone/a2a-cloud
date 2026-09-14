from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import gitea_provisioning
from control_plane.db import Base
from control_plane.models import (
    KeycloakIdentity,
    Organization,
    OrganizationGiteaWorkspace,
    OrganizationMember,
    User,
    WorkJob,
)
from control_plane.work_ledger import create_job


@pytest.mark.asyncio
async def test_resolve_agent_gitea_workspace_provisions_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str | None]] = []

    def fake_ensure_organization(
        name: str,
        *,
        full_name: str | None = None,
        description: str = "",
    ) -> dict[str, object]:
        calls.append({
            "name": name,
            "full_name": full_name,
            "description": description,
        })
        return {"id": 42, "username": name}

    monkeypatch.setattr(gitea_provisioning, "ensure_organization", fake_ensure_organization)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            org = Organization(slug="acme-inc", name="Acme Inc", created_by_id=user.id)
            session.add(org)
            await session.flush()
            session.add(
                OrganizationMember(
                    organization_id=org.id,
                    user_id=user.id,
                    role="owner",
                    active=True,
                )
            )
            await session.commit()

            selected_org, workspace = await gitea_provisioning.resolve_agent_gitea_workspace(
                session,
                user,
                organization_slug="acme-inc",
                source="agent_from_tarball",
            )
            jobs = (
                await session.execute(
                    select(WorkJob).where(WorkJob.kind == "gitea.workspace.provision")
                )
            ).scalars().all()

        assert selected_org.id == org.id
        assert workspace.status == "provisioned"
        assert workspace.gitea_org_name == "a2a-acme-inc"
        assert workspace.gitea_org_id == "42"
        assert workspace.metadata_json["gitea_org_url"].endswith("/a2a-acme-inc")
        assert len(jobs) == 1
        assert jobs[0].queue == "source-control"
        assert calls == [
            {
                "name": "a2a-acme-inc",
                "full_name": "Acme Inc",
                "description": "A2A organization acme-inc",
            }
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gitea_workspace_row_is_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            org = Organization(slug="acme", name="Acme", created_by_id=user.id)
            session.add(org)
            await session.flush()

            first = await gitea_provisioning.ensure_gitea_workspace_row(
                session,
                org,
                source="keycloak_login",
            )
            second = await gitea_provisioning.ensure_gitea_workspace_row(
                session,
                org,
                source="member_add",
            )
            rows = (
                await session.execute(select(OrganizationGiteaWorkspace))
            ).scalars().all()
            jobs = (
                await session.execute(
                    select(WorkJob).where(WorkJob.kind == "gitea.workspace.provision")
                )
            ).scalars().all()

        assert first is second
        assert len(rows) == 1
        assert rows[0].gitea_org_name == "a2a-acme"
        assert len(jobs) == 1
        assert jobs[0].input_payload["gitea_org_name"] == "a2a-acme"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provisioned_personal_workspace_keeps_existing_gitea_owner() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            org = Organization(
                slug=f"personal-{user.id}",
                name="Personal workspace",
                created_by_id=user.id,
            )
            session.add(org)
            await session.flush()
            existing = OrganizationGiteaWorkspace(
                organization_id=org.id,
                status="provisioned",
                gitea_base_url="https://gitea.example.test",
                gitea_org_name="legacy-owner",
                org_url="https://gitea.example.test/legacy-owner",
                metadata_json={
                    "source": "keycloak_login",
                    "scope": "personal_user",
                    "interface": "gitea_api",
                    "user_id": user.id,
                    "user_email": user.email,
                },
            )
            session.add(existing)
            await session.flush()

            workspace = await gitea_provisioning.ensure_gitea_workspace_row(
                session,
                org,
                user=user,
                source="agent_from_tarball",
            )
            jobs = (
                await session.execute(
                    select(WorkJob).where(WorkJob.kind == "gitea.workspace.provision")
                )
            ).scalars().all()

        assert workspace is existing
        assert workspace.gitea_org_name == "legacy-owner"
        assert workspace.status == "provisioned"
        assert workspace.metadata_json["source"] == "agent_from_tarball"
        assert jobs == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_personal_workspace_rename_uses_owner_scoped_idempotency() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="Owner@Example.com", password_hash="x")
            session.add(user)
            await session.flush()
            org = Organization(
                slug=f"personal-{user.id}",
                name="Personal workspace",
                created_by_id=user.id,
            )
            session.add(org)
            await session.flush()
            existing = OrganizationGiteaWorkspace(
                organization_id=org.id,
                status="pending",
                gitea_base_url="https://gitea.example.test",
                gitea_org_name="legacy-owner",
                org_url="https://gitea.example.test/legacy-owner",
                metadata_json={
                    "source": "keycloak_login",
                    "scope": "personal_user",
                    "interface": "gitea_api",
                    "user_id": user.id,
                    "user_email": user.email,
                },
            )
            session.add(existing)
            await session.flush()
            await create_job(
                session,
                user_id=None,
                kind="gitea.workspace.provision",
                payload={"organization_id": org.id, "gitea_org_name": "legacy-owner"},
                idempotency_key=f"gitea-workspace:{org.id}",
                idempotency_scope="gitea_provisioning",
                request_hash="legacy-request",
                commit=False,
            )

            workspace = await gitea_provisioning.ensure_gitea_workspace_row(
                session,
                org,
                user=user,
                source="agent_from_tarball",
            )
            jobs = (
                await session.execute(
                    select(WorkJob).where(WorkJob.kind == "gitea.workspace.provision")
                )
            ).scalars().all()

        assert workspace is existing
        assert workspace.gitea_org_name == f"owner-{user.id}"
        assert len(jobs) == 2
        assert {job.input_payload["gitea_org_name"] for job in jobs} == {
            "legacy-owner",
            f"owner-{user.id}",
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_personal_workspace_provisions_gitea_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str | None]] = []
    oauth_links: list[dict[str, str | None]] = []

    def fake_ensure_user_account(
        username: str,
        *,
        email: str,
        full_name: str | None = None,
    ) -> dict[str, object]:
        calls.append({"username": username, "email": email, "full_name": full_name})
        return {"id": 77, "username": username}

    monkeypatch.setattr(
        gitea_provisioning,
        "ensure_user_account",
        fake_ensure_user_account,
    )
    monkeypatch.setattr(
        gitea_provisioning,
        "ensure_user_oauth_link",
        lambda username, *, email, keycloak_sub: oauth_links.append(
            {
                "username": username,
                "email": email,
                "keycloak_sub": keycloak_sub,
            }
        )
        or True,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="Owner@Example.com", password_hash="x")
            session.add(user)
            await session.flush()
            session.add(
                KeycloakIdentity(
                    keycloak_sub="kc-owner-1",
                    user_id=user.id,
                    email="owner@example.com",
                )
            )
            org = Organization(
                slug=f"personal-{user.id}",
                name="Personal workspace",
                created_by_id=user.id,
            )
            session.add(org)
            await session.flush()
            session.add(
                OrganizationMember(
                    organization_id=org.id,
                    user_id=user.id,
                    role="owner",
                    active=True,
                )
            )
            await session.commit()

            selected_org, workspace = await gitea_provisioning.resolve_agent_gitea_workspace(
                session,
                user,
                source="agent_from_tarball",
            )

        assert selected_org.id == org.id
        assert workspace.status == "provisioned"
        assert workspace.gitea_org_name == f"owner-{user.id}"
        assert workspace.gitea_org_id == "77"
        assert workspace.metadata_json["scope"] == "personal_user"
        assert workspace.metadata_json["user_email"] == "Owner@Example.com"
        assert workspace.metadata_json["gitea_oauth_linked"] is True
        assert calls == [
            {
                "username": f"owner-{user.id}",
                "email": "Owner@Example.com",
                "full_name": "Owner@Example.com",
            }
        ]
        assert oauth_links == [
            {
                "username": f"owner-{user.id}",
                "email": "Owner@Example.com",
                "keycloak_sub": "kc-owner-1",
            }
        ]
    finally:
        await engine.dispose()
