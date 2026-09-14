from __future__ import annotations

import os
import json
from types import SimpleNamespace

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import langfuse_provisioner as provisioner_module
from control_plane.config import settings
from control_plane.db import Base
from control_plane.keycloak_auth import provision_user_from_claims
from control_plane.langfuse_provisioner import LangfuseProvisioner
from control_plane.models import (
    OrganizationLangfuseWorkspace,
    User,
    UserLangfuseAccount,
    WorkJob,
)
from control_plane.org_provisioning import user_langfuse_routing_context
from control_plane.secret_crypto import decrypt_secret, encrypt_secret


class _FakeLangfuseDb:
    def __init__(self) -> None:
        self.password_updates: list[tuple[str, str, str]] = []
        self.project_memberships: list[tuple[str, str, str, str]] = []

    async def set_user_password(
        self,
        *,
        user_id: str,
        email: str,
        password: str | None = None,
        password_hash: str | None = None,
    ) -> None:
        del password_hash
        self.password_updates.append((user_id, email, password or ""))

    async def ensure_project_membership(
        self,
        *,
        organization_id: str,
        project_id: str,
        user_id: str,
        role: str,
    ) -> None:
        self.project_memberships.append((organization_id, project_id, user_id, role))


@pytest.mark.asyncio
async def test_provisioner_creates_langfuse_project_litellm_key_and_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(settings, "langfuse_org_public_key", "pk-org")
    monkeypatch.setattr(settings, "langfuse_org_secret_key", "sk-org")
    monkeypatch.setattr(settings, "langfuse_provisioning_enabled", True)
    monkeypatch.setattr(settings, "litellm_team_logging_enabled", True)
    monkeypatch.setattr(
        provisioner_module,
        "load_runtime_settings",
        lambda: SimpleNamespace(litellm_url="http://litellm.test", litellm_key="master"),
    )
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        calls.append((request.method, str(request.url), body))
        path = request.url.path
        if request.method == "GET" and path == "/api/public/projects":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and path == "/api/public/projects":
            return httpx.Response(200, json={"id": "proj_1", "name": "personal-1-default"})
        if request.method == "POST" and path == "/api/public/projects/proj_1/apiKeys":
            return httpx.Response(
                200,
                json={"id": "key_1", "publicKey": "pk-lf-org", "secretKey": "sk-lf-org"},
            )
        if request.method == "GET" and path == "/team/info":
            return httpx.Response(404, json={"error": {"message": "team not found"}})
        if request.method == "POST" and path == "/team/new":
            return httpx.Response(200, json={"team_id": "a2a-org-1"})
        if request.method == "POST" and path == "/team/a2a-org-1/callback":
            return httpx.Response(200, json={"status": "success"})
        if request.method == "POST" and path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-litellm-org"})
        if request.method == "POST" and path == "/api/public/scim/Users":
            return httpx.Response(200, json={"id": "lf-user-1"})
        return httpx.Response(404, json={"error": "not found"})

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        transport = httpx.MockTransport(handler)
        direct_db = _FakeLangfuseDb()
        async with httpx.AsyncClient(transport=transport) as client:
            provisioner = LangfuseProvisioner(
                langfuse_base_url="https://langfuse.test",
                litellm_url="http://litellm.test",
                litellm_key="master",
                client=client,
                direct_db=direct_db,  # type: ignore[arg-type]
            )
            async with Session() as session:
                await provision_user_from_claims(
                    session,
                    {
                        "sub": "kc-owner-1",
                        "email": "owner@example.com",
                        "email_verified": True,
                    },
                )

                assert await provisioner.run_once(session) is True
                assert await provisioner.run_once(session) is True

                user = (
                    await session.execute(select(User).where(User.email == "owner@example.com"))
                ).scalar_one()
                workspace = (
                    await session.execute(select(OrganizationLangfuseWorkspace))
                ).scalar_one()
                account = (await session.execute(select(UserLangfuseAccount))).scalar_one()
                jobs = (
                    await session.execute(
                        select(WorkJob)
                        .where(
                            WorkJob.kind.in_(
                                (
                                    "langfuse.account.provision",
                                    "langfuse.workspace.provision",
                                )
                            )
                        )
                        .order_by(WorkJob.kind)
                    )
                ).scalars().all()
                routing = await user_langfuse_routing_context(session, user)

        assert workspace.status == "provisioned"
        assert workspace.langfuse_project_id == "proj_1"
        assert workspace.public_key == "pk-lf-org"
        assert workspace.secret_key_ref == f"db:organization_langfuse_workspaces:{workspace.id}:secret_key"
        assert decrypt_secret(workspace.secret_key_ciphertext or "") == "sk-lf-org"
        assert workspace.litellm_team_id == "a2a-org-1"
        assert workspace.litellm_key_ref == f"db:organization_langfuse_workspaces:{workspace.id}:litellm_api_key"
        assert decrypt_secret(workspace.litellm_key_ciphertext or "") == "sk-litellm-org"
        assert routing.litellm_api_key == "sk-litellm-org"
        assert routing.langfuse_project_id == "proj_1"
        assert account.status == "provisioned"
        assert account.langfuse_user_id == "lf-user-1"
        assert account.login_password_ref == (
            f"db:user_langfuse_accounts:{account.id}:login_password"
        )
        assert account.login_password_ciphertext
        assert direct_db.password_updates == [
            (
                "lf-user-1",
                "owner@example.com",
                decrypt_secret(account.login_password_ciphertext),
            )
        ]
        assert direct_db.project_memberships == [
            ("a2a", "proj_1", "lf-user-1", "owner")
        ]
        assert [job.status for job in jobs] == ["complete", "complete"]
        assert ("POST", "http://litellm.test/team/new", {
            "team_id": "a2a-org-1",
            "team_alias": "personal-1",
            "models": ["*"],
            "metadata": {
                "a2a_org_id": 1,
                "a2a_org_slug": "personal-1",
                "purpose": "org_langfuse_routing",
            },
        }) in calls
        assert ("POST", "http://litellm.test/key/generate", {
            "team_id": "a2a-org-1",
            "models": ["*"],
            "key_alias": "a2a-org-1-observability",
            "metadata": {
                "a2a_org_id": 1,
                "a2a_org_slug": "personal-1",
                "purpose": "org_langfuse_routing",
            },
        }) in calls
        assert ("POST", "http://litellm.test/team/a2a-org-1/callback", None) not in calls
        assert any(call[1].endswith("/team/a2a-org-1/callback") for call in calls)
        assert ("POST", "https://langfuse.test/api/public/scim/Users", {
            "userName": "owner@example.com",
            "active": True,
            "emails": [{"value": "owner@example.com", "primary": True}],
            "roles": ["OWNER"],
        }) in calls
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_team_callback_duplicate_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        provisioner_module,
        "load_runtime_settings",
        lambda: SimpleNamespace(litellm_url="http://litellm.test", litellm_key="master"),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/team/a2a-org-31/callback":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "callback_name = langfuse already exists in "
                            "team_callback_settings"
                        ),
                        "param": "callback_name",
                    }
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provisioner = LangfuseProvisioner(
            langfuse_base_url="https://langfuse.test",
            litellm_url="http://litellm.test",
            litellm_key="master",
            client=client,
        )
        await provisioner._configure_litellm_team_callback(
            team_id="a2a-org-31",
            public_key="pk-lf-org",
            secret_key_ciphertext=encrypt_secret("sk-lf-org"),
        )
