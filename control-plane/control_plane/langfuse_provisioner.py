from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from main_agent.config import load_settings as load_runtime_settings

from .config import settings
from .db import SessionLocal, init_models
from .langfuse_db_fallback import LangfuseDirectDb
from .models import (
    Organization,
    OrganizationLangfuseWorkspace,
    User,
    UserLangfuseAccount,
    WorkJob,
)
from .org_provisioning import litellm_team_id, workspace_secret_ref
from .secret_crypto import decrypt_secret, encrypt_secret
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown
from .work_ledger import complete_job, fail_job

log = logging.getLogger(__name__)


class ProvisioningBlocked(RuntimeError):
    """Raised when external credentials/features are missing or unavailable."""


@dataclass(frozen=True)
class LangfuseProjectKey:
    public_key: str
    secret_key: str
    key_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        for key in ("data", "items", "projects", "apiKeys", "keys", "Resources"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_string(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _role_for_langfuse(role: str) -> str:
    return {
        "owner": "OWNER",
        "admin": "ADMIN",
        "member": "MEMBER",
    }.get(role, "MEMBER")


class LangfuseProvisioner:
    def __init__(
        self,
        *,
        langfuse_base_url: str | None = None,
        litellm_url: str | None = None,
        litellm_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        direct_db: LangfuseDirectDb | None = None,
    ) -> None:
        runtime_settings = load_runtime_settings()
        self.langfuse_base_url = (
            langfuse_base_url or settings.langfuse_base_url
        ).rstrip("/")
        self.litellm_url = (litellm_url or runtime_settings.litellm_url).rstrip("/")
        self.litellm_key = litellm_key if litellm_key is not None else runtime_settings.litellm_key
        self._client = client
        # Fallback DB writer: used only when Langfuse OSS's API gates a
        # request behind an EE license (401/403 → ProvisioningBlocked).
        if direct_db is not None:
            self._direct_db: LangfuseDirectDb | None = direct_db
        elif settings.langfuse_database_url:
            self._direct_db = LangfuseDirectDb(settings.langfuse_database_url)
        else:
            self._direct_db = None

    async def run_once(self, session: AsyncSession) -> bool:
        job = await self._next_job(session)
        if job is None:
            return False
        try:
            if job.kind == "langfuse.workspace.provision":
                result = await self.provision_workspace(session, job)
            elif job.kind == "langfuse.account.provision":
                result = await self.provision_account(session, job)
            else:
                return False
            await complete_job(
                session,
                job,
                result=result,
                summary=result.get("summary", "Langfuse provisioning complete"),
                status="complete",
                event_type="langfuse_provisioned",
                commit=True,
            )
            return True
        except ProvisioningBlocked as exc:
            await self._mark_blocked(session, job, str(exc))
            return True
        except Exception as exc:  # noqa: BLE001
            await fail_job(
                session,
                job,
                error=str(exc),
                summary="Langfuse provisioning failed",
                status="error",
                event_type="langfuse_provisioning_failed",
                commit=True,
            )
            return True

    async def provision_workspace(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> dict[str, Any]:
        workspace = await self._workspace_from_job(session, job)
        org = await session.get(Organization, workspace.organization_id)
        if org is None:
            raise ProvisioningBlocked("organization no longer exists")
        if not settings.langfuse_provisioning_enabled:
            workspace.status = "disabled"
            workspace.last_error = None
            return {"summary": "Langfuse provisioning disabled"}

        project = await self._ensure_langfuse_project(workspace)
        workspace.langfuse_project_id = project["id"]
        workspace.project_name = project["name"]
        workspace.langfuse_base_url = self.langfuse_base_url

        if not workspace.public_key or not workspace.secret_key_ciphertext:
            api_key = await self._create_langfuse_project_key(project["id"], org)
            workspace.public_key = api_key.public_key
            workspace.secret_key_ref = workspace_secret_ref(workspace, "secret_key")
            workspace.secret_key_ciphertext = encrypt_secret(api_key.secret_key)

        if settings.litellm_team_logging_enabled:
            team_id = workspace.litellm_team_id or litellm_team_id(org)
            workspace.litellm_team_id = team_id
            await self._ensure_litellm_team(team_id, org)
            await self._configure_litellm_team_callback(
                team_id=team_id,
                public_key=workspace.public_key or "",
                secret_key_ciphertext=workspace.secret_key_ciphertext or "",
            )
            if not workspace.litellm_key_ciphertext:
                virtual_key = await self._create_litellm_team_key(team_id, org)
                workspace.litellm_key_ref = workspace_secret_ref(
                    workspace, "litellm_api_key"
                )
                workspace.litellm_key_ciphertext = encrypt_secret(virtual_key)

        workspace.status = "provisioned"
        workspace.last_error = None
        workspace.provisioned_at = _utcnow()
        metadata = dict(workspace.metadata_json or {})
        metadata.update({
            "langfuse_project_id": workspace.langfuse_project_id,
            "litellm_team_id": workspace.litellm_team_id,
        })
        workspace.metadata_json = metadata
        return {
            "summary": f"Provisioned Langfuse project {workspace.project_name}",
            "organization_id": org.id,
            "organization_slug": org.slug,
            "workspace_id": workspace.id,
            "langfuse_project_id": workspace.langfuse_project_id,
            "litellm_team_id": workspace.litellm_team_id,
        }

    async def provision_account(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> dict[str, Any]:
        account = await self._account_from_job(session, job)
        org = await session.get(Organization, account.organization_id)
        user = await session.get(User, account.user_id)
        if org is None or user is None:
            raise ProvisioningBlocked("organization or user no longer exists")
        workspace = (
            await session.execute(
                select(OrganizationLangfuseWorkspace).where(
                    OrganizationLangfuseWorkspace.organization_id == org.id
                )
            )
        ).scalar_one_or_none()
        if workspace is None or workspace.status != "provisioned":
            raise ProvisioningBlocked("Langfuse workspace is not provisioned yet")
        login_password = (
            self._ensure_account_login_password(account)
            if self._direct_db is not None
            else None
        )
        langfuse_user_id = await self._ensure_langfuse_user(
            account,
            project_id=workspace.langfuse_project_id,
            login_password=login_password,
        )
        account.status = "provisioned"
        account.langfuse_user_id = langfuse_user_id
        account.last_error = None
        account.provisioned_at = _utcnow()
        metadata = dict(account.metadata_json or {})
        metadata.update({
            "langfuse_project_id": workspace.langfuse_project_id,
            "langfuse_workspace_id": workspace.id,
        })
        account.metadata_json = metadata
        return {
            "summary": f"Provisioned Langfuse login for {account.email}",
            "organization_id": org.id,
            "organization_slug": org.slug,
            "account_id": account.id,
            "user_id": user.id,
            "langfuse_user_id": langfuse_user_id,
        }

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        rows = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(
                    WorkJob.kind.in_(
                        ["langfuse.workspace.provision", "langfuse.account.provision"]
                    )
                )
                .where(or_(WorkJob.queued_at.is_(None), WorkJob.queued_at <= _utcnow()))
                .order_by(WorkJob.created_at.asc(), WorkJob.id.asc())
                .with_for_update(skip_locked=True)
                .limit(25)
            )
        ).scalars().all()
        job = next(
            (row for row in rows if row.kind == "langfuse.workspace.provision"),
            rows[0] if rows else None,
        )
        if job is None:
            return None
        now = _utcnow()
        job.status = "running"
        job.attempt = (job.attempt or 0) + 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.leased_until = now + timedelta(minutes=30)
        await session.commit()
        await session.refresh(job)
        return job

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = _utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind.in_(["langfuse.workspace.provision", "langfuse.account.provision"]))
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Langfuse provisioning lease expired; requeued",
                queued_at=now,
                heartbeat_at=None,
                leased_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if result.rowcount:
            session.expire_all()

    async def _workspace_from_job(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> OrganizationLangfuseWorkspace:
        workspace_id = int((job.input_payload or {}).get("workspace_id") or 0)
        workspace = await session.get(OrganizationLangfuseWorkspace, workspace_id)
        if workspace is None:
            raise ProvisioningBlocked("Langfuse workspace row no longer exists")
        return workspace

    async def _account_from_job(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> UserLangfuseAccount:
        account_id = int((job.input_payload or {}).get("account_id") or 0)
        account = await session.get(UserLangfuseAccount, account_id)
        if account is None:
            raise ProvisioningBlocked("Langfuse account row no longer exists")
        return account

    async def _ensure_langfuse_project(
        self,
        workspace: OrganizationLangfuseWorkspace,
    ) -> dict[str, str]:
        # API path first.
        try:
            existing = await self._find_langfuse_project(workspace.project_name)
            if existing is not None:
                return existing
            raw = await self._langfuse_request(
                "POST",
                "/api/public/projects",
                json={"name": workspace.project_name},
            )
        except ProvisioningBlocked:
            if self._direct_db is None:
                raise
            log.info(
                "langfuse API gated, falling back to direct DB for project=%s",
                workspace.project_name,
            )
            record = await self._direct_db.ensure_project(
                organization_id=settings.langfuse_shared_org_id,
                name=workspace.project_name,
            )
            return {"id": record.id, "name": record.name}
        project_id = _first_string(raw, "id", "projectId", "project_id")
        name = _first_string(raw, "name") or workspace.project_name
        if not project_id:
            raise ProvisioningBlocked("Langfuse project create response had no project id")
        return {"id": project_id, "name": name}

    async def _find_langfuse_project(self, name: str) -> dict[str, str] | None:
        raw = await self._langfuse_request("GET", "/api/public/projects")
        for item in _items(raw):
            item_name = _first_string(item, "name")
            if item_name != name:
                continue
            project_id = _first_string(item, "id", "projectId", "project_id")
            if project_id:
                return {"id": project_id, "name": item_name or name}
        return None

    async def _create_langfuse_project_key(
        self,
        project_id: str,
        org: Organization,
    ) -> LangfuseProjectKey:
        try:
            raw = await self._langfuse_request(
                "POST",
                f"/api/public/projects/{project_id}/apiKeys",
                json={"note": f"A2A {org.slug}"},
            )
        except ProvisioningBlocked:
            if self._direct_db is None:
                raise
            log.info(
                "langfuse API gated, falling back to direct DB for api-key project=%s",
                project_id,
            )
            record = await self._direct_db.create_project_api_key(
                project_id=project_id,
                note=f"A2A {org.slug}",
            )
            return LangfuseProjectKey(
                public_key=record.public_key,
                secret_key=record.secret_key,
                key_id=record.id,
            )
        public_key = _first_string(raw, "publicKey", "public_key", "publicKeyId")
        secret_key = _first_string(raw, "secretKey", "secret_key", "privateKey")
        key_id = _first_string(raw, "id", "apiKeyId", "keyId")
        if not public_key or not secret_key:
            raise ProvisioningBlocked("Langfuse API key response did not include keys")
        return LangfuseProjectKey(
            public_key=public_key,
            secret_key=secret_key,
            key_id=key_id,
        )

    def _ensure_account_login_password(self, account: UserLangfuseAccount) -> str:
        password_ref = f"db:user_langfuse_accounts:{account.id}:login_password"
        if account.login_password_ciphertext:
            if not account.login_password_ref:
                account.login_password_ref = password_ref
            return decrypt_secret(account.login_password_ciphertext)
        password = secrets.token_urlsafe(24)
        account.login_password_ref = password_ref
        account.login_password_ciphertext = encrypt_secret(password)
        return password

    async def _ensure_langfuse_user(
        self,
        account: UserLangfuseAccount,
        *,
        project_id: str | None,
        login_password: str | None = None,
    ) -> str | None:
        try:
            raw = await self._langfuse_request(
                "POST",
                "/api/public/scim/Users",
                json={
                    "userName": account.email,
                    "active": True,
                    "emails": [{"value": account.email, "primary": True}],
                    "roles": [_role_for_langfuse(account.role)],
                },
            )
        except ProvisioningBlocked:
            if self._direct_db is None:
                raise
            log.info(
                "langfuse API gated, falling back to direct DB for user=%s",
                account.email,
            )
            user_id = await self._direct_db.ensure_user(
                account.email,
                password=login_password,
            )
            if project_id:
                await self._direct_db.ensure_project_membership(
                    organization_id=settings.langfuse_shared_org_id,
                    project_id=project_id,
                    user_id=user_id,
                    role=account.role,
                )
            return user_id
        user_id = _first_string(raw, "id", "userId", "externalId")
        if user_id and self._direct_db is not None:
            if login_password:
                await self._direct_db.set_user_password(
                    user_id=user_id,
                    email=account.email,
                    password=login_password,
                )
            if project_id:
                await self._direct_db.ensure_project_membership(
                    organization_id=settings.langfuse_shared_org_id,
                    project_id=project_id,
                    user_id=user_id,
                    role=account.role,
                )
        return user_id

    async def _ensure_litellm_team(self, team_id: str, org: Organization) -> None:
        try:
            await self._litellm_request(
                "GET",
                "/team/info",
                params={"team_id": team_id},
                unavailable_statuses={401, 403, 405},
            )
            return
        except RuntimeError as exc:
            if "failed with 404" not in str(exc):
                raise

        try:
            await self._litellm_request(
                "POST",
                "/team/new",
                json={
                    "team_id": team_id,
                    "team_alias": org.slug,
                    "models": ["*"],
                    "metadata": {
                        "a2a_org_id": org.id,
                        "a2a_org_slug": org.slug,
                        "purpose": "org_langfuse_routing",
                    },
                },
            )
        except RuntimeError as exc:
            message = str(exc).lower()
            if "already exists" not in message and "duplicate" not in message:
                raise

    async def _configure_litellm_team_callback(
        self,
        *,
        team_id: str,
        public_key: str,
        secret_key_ciphertext: str,
    ) -> None:
        try:
            await self._litellm_request(
                "POST",
                f"/team/{team_id}/callback",
                json={
                    "callback_name": "langfuse",
                    "callback_type": "success_and_failure",
                    "callback_vars": {
                        "langfuse_public_key": public_key,
                        "langfuse_secret_key": decrypt_secret(secret_key_ciphertext),
                        "langfuse_host": self.langfuse_base_url,
                    },
                },
            )
        except RuntimeError as exc:
            message = str(exc).lower()
            if "callback_name" not in message or "already exists" not in message:
                raise

    async def _create_litellm_team_key(self, team_id: str, org: Organization) -> str:
        raw = await self._litellm_request(
            "POST",
            "/key/generate",
            json={
                "team_id": team_id,
                "models": ["*"],
                "key_alias": f"a2a-org-{org.id}-observability",
                "metadata": {
                    "a2a_org_id": org.id,
                    "a2a_org_slug": org.slug,
                    "purpose": "org_langfuse_routing",
                },
            },
        )
        key = _first_string(raw, "key", "token", "api_key")
        if not key:
            raise ProvisioningBlocked("LiteLLM key generate response had no key")
        return key

    async def _langfuse_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not settings.langfuse_org_public_key or not settings.langfuse_org_secret_key:
            raise ProvisioningBlocked(
                "A2A_CP_LANGFUSE_ORG_PUBLIC_KEY and "
                "A2A_CP_LANGFUSE_ORG_SECRET_KEY are required"
            )
        return await self._request(
            method,
            self.langfuse_base_url + path,
            json=json,
            auth=(settings.langfuse_org_public_key, settings.langfuse_org_secret_key),
        )

    async def _litellm_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        unavailable_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        if not self.litellm_key:
            raise ProvisioningBlocked("A2A_LITELLM_KEY is required")
        return await self._request(
            method,
            self.litellm_url + path,
            json=json,
            params=params,
            headers={"Authorization": f"Bearer {self.litellm_key}"},
            unavailable_statuses=unavailable_statuses,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
        headers: dict[str, str] | None = None,
        unavailable_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        close_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            response = await client.request(
                method,
                url,
                json=json,
                params=params,
                auth=auth,
                headers=headers,
            )
        finally:
            if close_client:
                await client.aclose()
        blocked_statuses = unavailable_statuses or {401, 403, 404, 405}
        if response.status_code in blocked_statuses:
            raise ProvisioningBlocked(
                f"{method} {url} is unavailable or unauthorized "
                f"({response.status_code})"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {url} failed with {response.status_code}: "
                f"{response.text[:500]}"
            )
        if not response.content:
            return {}
        raw = response.json()
        return raw if isinstance(raw, dict) else {"data": raw}

    async def _mark_blocked(
        self,
        session: AsyncSession,
        job: WorkJob,
        reason: str,
    ) -> None:
        if job.kind == "langfuse.workspace.provision":
            workspace = await self._workspace_from_job(session, job)
            workspace.status = "blocked"
            workspace.last_error = reason
        elif job.kind == "langfuse.account.provision":
            account = await self._account_from_job(session, job)
            account.status = "blocked"
            account.last_error = reason
        await fail_job(
            session,
            job,
            error=reason,
            summary="Langfuse provisioning blocked",
            status="blocked",
            event_type="langfuse_provisioning_blocked",
            commit=True,
        )


async def run_forever() -> None:
    await init_models()
    provisioner = LangfuseProvisioner()
    interval = max(1.0, float(settings.langfuse_provisioner_interval_seconds))
    shutdown = install_shutdown_event("langfuse-provisioner")
    while not shutdown.is_set():
        try:
            async with SessionLocal() as session:
                processed = await provisioner.run_once(session)
            if not processed:
                await sleep_or_shutdown(shutdown, interval)
        except Exception:  # noqa: BLE001
            log.exception("Langfuse provisioner loop failed")
            await sleep_or_shutdown(shutdown, interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
