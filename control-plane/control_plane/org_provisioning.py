from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .gitea_provisioning import ensure_gitea_workspace_row
from .models import (
    Organization,
    OrganizationAuditLog,
    OrganizationLangfuseWorkspace,
    OrganizationMember,
    User,
    UserControlPolicy,
    UserLangfuseAccount,
)
from .secret_crypto import decrypt_secret
from .work_ledger import create_job


@dataclass(frozen=True)
class LangfuseRoutingContext:
    organization_id: int | None = None
    organization_slug: str | None = None
    workspace_id: int | None = None
    langfuse_project_id: str | None = None
    litellm_api_key: str | None = None
    metadata: dict[str, str | int] | None = None


def personal_org_slug(user: User) -> str:
    if user.id is None:
        raise ValueError("user must be flushed before creating a personal org")
    return f"personal-{user.id}"


def langfuse_project_name(org: Organization) -> str:
    return f"{org.slug}-default"


def litellm_team_id(org: Organization) -> str:
    return f"a2a-org-{org.id}"


def workspace_secret_ref(workspace: OrganizationLangfuseWorkspace, key: str) -> str:
    return f"db:organization_langfuse_workspaces:{workspace.id}:{key}"


async def _enqueue_workspace_job(
    session: AsyncSession,
    org: Organization,
    workspace: OrganizationLangfuseWorkspace,
    *,
    source: str,
) -> None:
    if not settings.langfuse_provisioning_enabled:
        return
    await session.flush()
    await create_job(
        session,
        user_id=None,
        kind="langfuse.workspace.provision",
        payload={
            "organization_id": org.id,
            "organization_slug": org.slug,
            "workspace_id": workspace.id,
            "deployment_mode": workspace.deployment_mode,
            "langfuse_base_url": workspace.langfuse_base_url,
            "project_name": workspace.project_name,
        },
        title=f"Provision Langfuse workspace for {org.slug}",
        metadata={"source": source, "provider": "langfuse"},
        queue="observability",
        source_type="organization",
        source_id=str(org.id),
        subject_type="langfuse_workspace",
        subject_id=str(workspace.id),
        worker_type="langfuse",
        worker_name="langfuse_provisioner",
        idempotency_key=f"langfuse-workspace:{org.id}",
        idempotency_scope="langfuse_provisioning",
        commit=False,
    )


async def _enqueue_account_job(
    session: AsyncSession,
    org: Organization,
    account: UserLangfuseAccount,
    *,
    source: str,
) -> None:
    if not settings.langfuse_provisioning_enabled:
        return
    await session.flush()
    await create_job(
        session,
        user_id=account.user_id,
        kind="langfuse.account.provision",
        payload={
            "organization_id": org.id,
            "organization_slug": org.slug,
            "account_id": account.id,
            "user_id": account.user_id,
            "email": account.email,
            "role": account.role,
        },
        title=f"Provision Langfuse login for {account.email}",
        metadata={"source": source, "provider": "langfuse"},
        queue="observability",
        source_type="organization",
        source_id=str(org.id),
        subject_type="langfuse_account",
        subject_id=str(account.id),
        worker_type="langfuse",
        worker_name="langfuse_provisioner",
        idempotency_key=f"langfuse-account:{org.id}:{account.user_id}",
        idempotency_scope="langfuse_provisioning",
        commit=False,
    )


async def ensure_user_control_policy(session: AsyncSession, user: User) -> UserControlPolicy:
    existing = (
        await session.execute(
            select(UserControlPolicy).where(UserControlPolicy.user_id == user.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    policy = UserControlPolicy(user_id=user.id)
    session.add(policy)
    return policy


async def ensure_langfuse_workspace_row(
    session: AsyncSession,
    org: Organization,
    *,
    source: str,
) -> OrganizationLangfuseWorkspace:
    existing = (
        await session.execute(
            select(OrganizationLangfuseWorkspace).where(
                OrganizationLangfuseWorkspace.organization_id == org.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    workspace = OrganizationLangfuseWorkspace(
        organization_id=org.id,
        status="pending" if settings.langfuse_provisioning_enabled else "disabled",
        deployment_mode=settings.langfuse_provisioning_mode,
        langfuse_base_url=settings.langfuse_base_url,
        project_name=langfuse_project_name(org),
        litellm_team_id=litellm_team_id(org),
        metadata_json={
            "source": source,
            "scope": "organization",
            "interface": "public_langfuse_adapter",
        },
    )
    session.add(workspace)
    await _enqueue_workspace_job(session, org, workspace, source=source)
    return workspace


async def ensure_langfuse_user_account_row(
    session: AsyncSession,
    org: Organization,
    user: User,
    *,
    role: str,
    source: str,
) -> UserLangfuseAccount:
    existing = (
        await session.execute(
            select(UserLangfuseAccount).where(
                UserLangfuseAccount.organization_id == org.id,
                UserLangfuseAccount.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.email = user.email
        existing.role = role
        return existing

    account = UserLangfuseAccount(
        organization_id=org.id,
        user_id=user.id,
        email=user.email,
        role=role,
        status="pending" if settings.langfuse_provisioning_enabled else "disabled",
        metadata_json={
            "source": source,
            "scope": "organization_user",
            "interface": "public_langfuse_adapter",
        },
    )
    session.add(account)
    await _enqueue_account_job(session, org, account, source=source)
    return account


async def user_langfuse_routing_context(
    session: AsyncSession,
    user: User,
    *,
    organization_slug: str | None = None,
) -> LangfuseRoutingContext:
    org_member_query = (
        select(Organization, OrganizationMember)
        .join(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(OrganizationMember.user_id == user.id)
        .where(OrganizationMember.active == True)  # noqa: E712
    )
    if organization_slug:
        org_member_query = org_member_query.where(Organization.slug == organization_slug)
    org_member_query = org_member_query.order_by(
        (Organization.slug != personal_org_slug(user)).asc(),
        Organization.created_at.asc(),
    )
    row = (await session.execute(org_member_query)).first()
    if row is None:
        return LangfuseRoutingContext(
            metadata={"a2a_user_id": user.id, "a2a_user_email": user.email}
        )
    org, _member = row
    workspace = (
        await session.execute(
            select(OrganizationLangfuseWorkspace).where(
                OrganizationLangfuseWorkspace.organization_id == org.id
            )
        )
    ).scalar_one_or_none()
    metadata: dict[str, str | int] = {
        "a2a_user_id": user.id,
        "a2a_user_email": user.email,
        "a2a_org_id": org.id,
        "a2a_org_slug": org.slug,
    }
    if workspace is None:
        return LangfuseRoutingContext(
            organization_id=org.id,
            organization_slug=org.slug,
            metadata=metadata,
        )
    if workspace.langfuse_project_id:
        metadata["langfuse_project_id"] = workspace.langfuse_project_id
    if workspace.id is not None:
        metadata["langfuse_workspace_id"] = workspace.id
    litellm_api_key = (
        decrypt_secret(workspace.litellm_key_ciphertext)
        if workspace.litellm_key_ciphertext
        else None
    )
    return LangfuseRoutingContext(
        organization_id=org.id,
        organization_slug=org.slug,
        workspace_id=workspace.id,
        langfuse_project_id=workspace.langfuse_project_id,
        litellm_api_key=litellm_api_key,
        metadata=metadata,
    )


async def create_personal_organization(
    session: AsyncSession,
    user: User,
) -> Organization:
    if user.id is None:
        await session.flush()

    slug = personal_org_slug(user)
    org = (
        await session.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    org_created = False
    if org is None:
        org = Organization(
            slug=slug,
            name="Personal workspace",
            created_by_id=user.id,
        )
        session.add(org)
        await session.flush()
        org_created = True

    member = (
        await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        session.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role="owner",
                active=True,
            )
        )
    else:
        member.role = "owner"
        member.active = True

    await ensure_user_control_policy(session, user)
    await ensure_gitea_workspace_row(session, org, user=user, source="keycloak_login")
    await ensure_langfuse_workspace_row(session, org, source="keycloak_login")
    await ensure_langfuse_user_account_row(
        session,
        org,
        user,
        role="owner",
        source="keycloak_login",
    )

    if org_created:
        session.add(
            OrganizationAuditLog(
                organization_id=org.id,
                actor_user_id=user.id,
                actor=user.email,
                action="organization.create",
                target_type="organization",
                target_id=str(org.id),
                data={
                    "slug": org.slug,
                    "name": org.name,
                    "source": "keycloak_login",
                    "langfuse_workspace": "pending",
                },
            )
        )
    return org
