from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import (
    Agent,
    Organization,
    OrganizationGiteaWorkspace,
    OrganizationLangfuseWorkspace,
    OrganizationMember,
    User,
    UserLangfuseAccount,
)
from control_plane.routes.service_access import list_service_access
from control_plane.secret_crypto import encrypt_secret


@pytest.mark.asyncio
async def test_service_access_lists_links_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
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
                slug="acme",
                name="Acme",
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
            session.add(
                OrganizationLangfuseWorkspace(
                    organization_id=org.id,
                    status="provisioned",
                    deployment_mode="shared_instance",
                    langfuse_base_url="https://langfuse.example.test",
                    langfuse_project_id="project-123",
                    project_name="acme-default",
                    public_key="pk-lf-public",
                    secret_key_ref="db:organization_langfuse_workspaces:1:secret_key",
                    secret_key_ciphertext="fernet:not-returned",
                    litellm_team_id="a2a-org-1",
                    litellm_key_ref="db:organization_langfuse_workspaces:1:litellm_api_key",
                    litellm_key_ciphertext="fernet:not-returned",
                )
            )
            session.add(
                UserLangfuseAccount(
                    organization_id=org.id,
                    user_id=user.id,
                    email=user.email,
                    role="owner",
                    status="provisioned",
                    langfuse_user_id="lf-user-1",
                    login_password_ref="db:user_langfuse_accounts:1:login_password",
                    login_password_ciphertext=encrypt_secret("login-secret"),
                )
            )
            session.add(
                OrganizationGiteaWorkspace(
                    organization_id=org.id,
                    status="provisioned",
                    gitea_base_url="https://gitea.example.test",
                    gitea_org_name="a2a-acme",
                    org_url="https://gitea.example.test/a2a-acme",
                )
            )
            session.add(
                Agent(
                    owner_id=user.id,
                    organization_id=org.id,
                    gitea_owner="a2a-acme",
                    name="researcher",
                    description="",
                    version="0.1.0",
                    image="registry.example/researcher:latest",
                    public=True,
                    status="live",
                    url="https://researcher.a2acloud.io",
                    card={},
                )
            )
            await session.commit()

            out = await list_service_access(user=user, session=session)

        org_access = out.organizations[0]
        payload = out.model_dump()
        assert out.user.email == "owner@example.com"
        assert org_access.langfuse.project_url == (
            "https://langfuse.example.test/project/project-123"
        )
        assert org_access.langfuse.public_key == "pk-lf-public"
        assert org_access.langfuse.secret_key_ref.endswith(":secret_key")
        assert org_access.langfuse.login_password == "login-secret"
        assert org_access.langfuse.login_password_ref.endswith(":login_password")
        assert org_access.litellm.status == "configured"
        assert org_access.litellm.key_configured is True
        assert org_access.gitea.status == "provisioned"
        assert org_access.gitea.org_name == "a2a-acme"
        assert out.gitea.repositories[0].agent_name == "researcher"
        assert out.gitea.repositories[0].owner == "a2a-acme"
        assert out.gitea.repositories[0].repo_url.endswith("/a2a-acme/researcher")
        assert "fernet:not-returned" not in str(payload)
        assert "login_password_ciphertext" not in str(payload)
    finally:
        await engine.dispose()
