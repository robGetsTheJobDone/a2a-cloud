from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.keycloak_auth import provision_user_from_claims
from control_plane.models import (
    Organization,
    OrganizationGiteaWorkspace,
    OrganizationLangfuseWorkspace,
    OrganizationMember,
    User,
    UserControlPolicy,
    UserLangfuseAccount,
    WorkJob,
)
from control_plane.routes.organizations import (
    OrganizationCreateIn,
    OrganizationMemberIn,
    add_organization_member,
    create_organization,
)


@pytest.mark.asyncio
async def test_keycloak_login_bootstraps_personal_org_and_langfuse_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = await provision_user_from_claims(
                session,
                {
                    "sub": "kc-owner-1",
                    "email": "Owner@Example.com",
                    "email_verified": True,
                },
            )

            user = (
                await session.execute(select(User).where(User.email == "owner@example.com"))
            ).scalar_one()
            org = (
                await session.execute(
                    select(Organization).where(Organization.created_by_id == user.id)
                )
            ).scalar_one()
            member = (
                await session.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == org.id,
                        OrganizationMember.user_id == user.id,
                    )
                )
            ).scalar_one()
            workspace = (
                await session.execute(
                    select(OrganizationLangfuseWorkspace).where(
                        OrganizationLangfuseWorkspace.organization_id == org.id
                    )
                )
            ).scalar_one()
            gitea_workspace = (
                await session.execute(
                    select(OrganizationGiteaWorkspace).where(
                        OrganizationGiteaWorkspace.organization_id == org.id
                    )
                )
            ).scalar_one()
            account = (
                await session.execute(
                    select(UserLangfuseAccount).where(
                        UserLangfuseAccount.organization_id == org.id,
                        UserLangfuseAccount.user_id == user.id,
                    )
                )
            ).scalar_one()
            policy = (
                await session.execute(
                    select(UserControlPolicy).where(UserControlPolicy.user_id == user.id)
                )
            ).scalar_one()
            jobs = (
                await session.execute(select(WorkJob).order_by(WorkJob.kind))
            ).scalars().all()

        assert org.slug == f"personal-{user.id}"
        assert org.name == "Personal workspace"
        assert member.role == "owner"
        assert member.active is True
        assert workspace.status == "pending"
        assert workspace.deployment_mode == "shared_instance"
        assert workspace.project_name == f"{org.slug}-default"
        assert workspace.public_key is None
        assert workspace.secret_key_ref is None
        assert gitea_workspace.status == "pending"
        assert gitea_workspace.gitea_org_name == f"owner-{user.id}"
        assert gitea_workspace.org_url.endswith(f"/owner-{user.id}")
        assert gitea_workspace.metadata_json["scope"] == "personal_user"
        assert gitea_workspace.metadata_json["user_email"] == "owner@example.com"
        assert account.email == "owner@example.com"
        assert account.role == "owner"
        assert account.status == "pending"
        assert policy.user_id == user.id
        assert [job.kind for job in jobs] == [
            "gitea.workspace.provision",
            "langfuse.account.provision",
            "langfuse.workspace.provision",
        ]
        assert jobs[0].queue == "source-control"
        assert jobs[1].user_id == user.id
        assert jobs[2].queue == "observability"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_repeated_keycloak_login_does_not_create_second_org() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            first = await provision_user_from_claims(
                session,
                {
                    "sub": "kc-owner-1",
                    "email": "owner@example.com",
                    "email_verified": True,
                },
            )
            second = await provision_user_from_claims(
                session,
                {
                    "sub": "kc-owner-1",
                    "email": "owner@example.com",
                    "email_verified": True,
                },
            )
            third = await provision_user_from_claims(
                session,
                {
                    "sub": "kc-owner-2",
                    "email": "owner@example.com",
                    "email_verified": True,
                },
            )
            with pytest.raises(HTTPException) as exc_info:
                await provision_user_from_claims(
                    session,
                    {
                        "sub": "kc-owner-3",
                        "email": "owner@example.com",
                        "email_verified": False,
                    },
                )

            org_count = await session.scalar(select(func.count()).select_from(Organization))
            workspace_count = await session.scalar(
                select(func.count()).select_from(OrganizationLangfuseWorkspace)
            )
            gitea_workspace_count = await session.scalar(
                select(func.count()).select_from(OrganizationGiteaWorkspace)
            )
            account_count = await session.scalar(
                select(func.count()).select_from(UserLangfuseAccount)
            )

        assert first.id == second.id == third.id
        assert exc_info.value.status_code == 403
        assert org_count == 1
        assert workspace_count == 1
        assert gitea_workspace_count == 1
        assert account_count == 1
    finally:
        await engine.dispose()


def test_legacy_password_and_saml_auth_routes_are_not_registered() -> None:
    from control_plane.main import app

    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/v1/auth/signup" not in paths
    assert "/v1/auth/login" not in paths
    assert not any(path.startswith("/v1/auth/saml") for path in paths)
    assert "/v1/me/organizations/{slug}/saml" not in paths


@pytest.mark.asyncio
async def test_manual_org_create_gets_langfuse_workspace_and_owner_account() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.commit()
            await session.refresh(user)

            out = await create_organization(
                OrganizationCreateIn(name="Acme Inc"),
                user=user,
                session=session,
            )
            org = (
                await session.execute(select(Organization).where(Organization.id == out.id))
            ).scalar_one()
            workspace = (
                await session.execute(
                    select(OrganizationLangfuseWorkspace).where(
                        OrganizationLangfuseWorkspace.organization_id == org.id
                    )
                )
            ).scalar_one()
            gitea_workspace = (
                await session.execute(
                    select(OrganizationGiteaWorkspace).where(
                        OrganizationGiteaWorkspace.organization_id == org.id
                    )
                )
            ).scalar_one()
            account = (
                await session.execute(
                    select(UserLangfuseAccount).where(
                        UserLangfuseAccount.organization_id == org.id,
                        UserLangfuseAccount.user_id == user.id,
                    )
                )
            ).scalar_one()
            member_out = await add_organization_member(
                org.slug,
                OrganizationMemberIn(email="Member@Example.com", role="admin"),
                user=user,
                session=session,
            )
            member_account = (
                await session.execute(
                    select(UserLangfuseAccount).where(
                        UserLangfuseAccount.organization_id == org.id,
                        UserLangfuseAccount.user_id == member_out.user_id,
                    )
                )
            ).scalar_one()

        assert out.role == "owner"
        assert gitea_workspace.gitea_org_name == "a2a-acme-inc"
        assert gitea_workspace.metadata_json["source"] == "organization_create"
        assert workspace.project_name == "acme-inc-default"
        assert workspace.metadata_json["source"] == "organization_create"
        assert account.role == "owner"
        assert account.metadata_json["source"] == "organization_create"
        assert member_account.email == "member@example.com"
        assert member_account.role == "admin"
        assert member_account.metadata_json["source"] == "member_add"
    finally:
        await engine.dispose()
