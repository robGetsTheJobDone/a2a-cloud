"""Direct-DB fallback for Langfuse provisioning.

Langfuse OSS gates ``/api/public/projects``, ``/api/public/projects/{id}/apiKeys``,
and ``/api/public/scim/Users`` behind the ``admin-api`` entitlement, which only
activates on ``self-hosted:enterprise`` / ``cloud:enterprise`` plans. On a
license-less self-hosted install those endpoints return 403/401 and the
``LangfuseProvisioner`` raises :class:`ProvisioningBlocked`.

This module connects directly to the Langfuse Postgres database and writes the
same rows the API would have written. The shapes match Langfuse OSS schema as
of 3.174.x: ``projects``, ``api_keys`` (PROJECT scope), ``users``,
``organization_memberships``, ``project_memberships``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass

import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LangfuseProjectRecord:
    id: str
    name: str


@dataclass(frozen=True)
class LangfuseApiKeyRecord:
    id: str
    public_key: str
    secret_key: str


def _new_id(prefix: str, *, nbytes: int = 16) -> str:
    return f"{prefix}{secrets.token_hex(nbytes)}"


def _gen_public_key() -> str:
    return "pk-lf-" + secrets.token_hex(24)


def _gen_secret_key() -> str:
    return "sk-lf-" + secrets.token_hex(24)


def _fast_hash(secret_key: str) -> str:
    return hashlib.sha256(secret_key.encode("utf-8")).hexdigest()


def _bcrypt_hash(secret_key: str) -> str:
    # Cost 11 to match Langfuse's API-generated keys.
    return bcrypt.hashpw(secret_key.encode("utf-8"), bcrypt.gensalt(11)).decode("utf-8")


def _display_secret(secret_key: str) -> str:
    return f"{secret_key[:6]}...{secret_key[-4:]}"


def _langfuse_role(role: str) -> str:
    mapping = {"owner": "OWNER", "admin": "ADMIN", "member": "MEMBER"}
    return mapping.get(role.lower(), "MEMBER")


class LangfuseDirectDb:
    """Idempotent writers for Langfuse's Postgres schema.

    All methods are safe to call when the row may already exist; INSERTs use
    ``ON CONFLICT`` to no-op and then look up the canonical row.
    """

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
        )

    async def close(self) -> None:
        await self._engine.dispose()

    async def find_project(
        self,
        organization_id: str,
        name: str,
    ) -> LangfuseProjectRecord | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, name FROM projects "
                        "WHERE org_id = :org_id AND name = :name AND deleted_at IS NULL "
                        "LIMIT 1"
                    ),
                    {"org_id": organization_id, "name": name},
                )
            ).first()
        if row is None:
            return None
        return LangfuseProjectRecord(id=row.id, name=row.name)

    async def ensure_project(
        self,
        organization_id: str,
        name: str,
    ) -> LangfuseProjectRecord:
        existing = await self.find_project(organization_id, name)
        if existing is not None:
            log.info(
                "langfuse_db_fallback: project already exists name=%s id=%s",
                name,
                existing.id,
            )
            return existing
        project_id = _new_id("cp-", nbytes=10)
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, org_id) "
                    "VALUES (:id, :name, :org_id) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": project_id, "name": name, "org_id": organization_id},
            )
        log.info(
            "langfuse_db_fallback: created project name=%s id=%s",
            name,
            project_id,
        )
        # Re-read to handle a unique-name collision under conflict.
        record = await self.find_project(organization_id, name)
        if record is None:
            # Highly unlikely, but the row should always be present here.
            return LangfuseProjectRecord(id=project_id, name=name)
        return record

    async def create_project_api_key(
        self,
        project_id: str,
        *,
        note: str | None = None,
    ) -> LangfuseApiKeyRecord:
        key_id = _new_id("cpk-")
        public_key = _gen_public_key()
        secret_key = _gen_secret_key()
        params = {
            "id": key_id,
            "public_key": public_key,
            "hashed_secret_key": _bcrypt_hash(secret_key),
            "display_secret_key": _display_secret(secret_key),
            "fast_hashed_secret_key": _fast_hash(secret_key),
            "project_id": project_id,
            "note": note,
        }
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_keys "
                    "(id, public_key, hashed_secret_key, display_secret_key, "
                    " fast_hashed_secret_key, scope, project_id, organization_id, note) "
                    "VALUES "
                    "(:id, :public_key, :hashed_secret_key, :display_secret_key, "
                    " :fast_hashed_secret_key, 'PROJECT', :project_id, NULL, :note) "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                params,
            )
        log.info(
            "langfuse_db_fallback: created PROJECT api_key project=%s public=%s",
            project_id,
            public_key,
        )
        return LangfuseApiKeyRecord(
            id=key_id,
            public_key=public_key,
            secret_key=secret_key,
        )

    async def ensure_user(
        self,
        email: str,
        name: str | None = None,
        password: str | None = None,
    ) -> str:
        password_hash = _bcrypt_hash(password) if password else None
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :email LIMIT 1"),
                    {"email": email},
                )
            ).first()
            if row is not None:
                if password_hash:
                    await conn.execute(
                        text(
                            "UPDATE users SET password = :password, updated_at = NOW() "
                            "WHERE id = :user_id OR email = :email"
                        ),
                        {"password": password_hash, "user_id": row.id, "email": email},
                    )
                return row.id
            user_id = _new_id("cpu")
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, name, email_verified, password) "
                    "VALUES (:id, :email, :name, NOW(), :password) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {
                    "id": user_id,
                    "email": email,
                    "name": name or email,
                    "password": password_hash,
                },
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :email LIMIT 1"),
                    {"email": email},
                )
            ).first()
        if row is None:
            raise RuntimeError(f"failed to ensure langfuse user for {email}")
        log.info("langfuse_db_fallback: ensured user email=%s id=%s", email, row.id)
        return row.id

    async def set_user_password(
        self,
        *,
        user_id: str,
        email: str,
        password: str | None = None,
        password_hash: str | None = None,
    ) -> None:
        stored = password_hash or (_bcrypt_hash(password) if password else None)
        if not stored:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE users SET password = :password, updated_at = NOW() "
                    "WHERE id = :user_id OR email = :email"
                ),
                {"password": stored, "user_id": user_id, "email": email},
            )


    async def ensure_project_membership(
        self,
        *,
        organization_id: str,
        project_id: str,
        user_id: str,
        role: str,
    ) -> None:
        mem_id = _new_id("cpm")
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO organization_memberships "
                        "(id, org_id, user_id, role) "
                        "VALUES (:id, :org_id, :user_id, CAST('NONE' AS \"Role\")) "
                        "ON CONFLICT (org_id, user_id) DO UPDATE "
                        "SET role = CAST('NONE' AS \"Role\"), updated_at = NOW() "
                        "RETURNING id"
                    ),
                    {
                        "id": mem_id,
                        "org_id": organization_id,
                        "user_id": user_id,
                    },
                )
            ).first()
            if row is None:
                row = (
                    await conn.execute(
                        text(
                            "SELECT id FROM organization_memberships "
                            "WHERE org_id = :org_id AND user_id = :user_id"
                        ),
                        {"org_id": organization_id, "user_id": user_id},
                    )
                ).first()
            if row is None:
                raise RuntimeError(
                    f"failed to ensure Langfuse org membership for {user_id}"
                )
            await conn.execute(
                text(
                    "INSERT INTO project_memberships "
                    "(project_id, user_id, org_membership_id, role) "
                    "VALUES (:project_id, :user_id, :org_membership_id, "
                    "CAST(:role AS \"Role\")) "
                    "ON CONFLICT (project_id, user_id) DO UPDATE "
                    "SET org_membership_id = EXCLUDED.org_membership_id, "
                    "role = EXCLUDED.role, updated_at = NOW()"
                ),
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "org_membership_id": row.id,
                    "role": _langfuse_role(role),
                },
            )
        log.info(
            "langfuse_db_fallback: ensured project membership project=%s user=%s role=%s",
            project_id,
            user_id,
            role,
        )
