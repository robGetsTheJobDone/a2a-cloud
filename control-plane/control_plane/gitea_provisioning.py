from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .gitea import (
    GITEA_PUBLIC,
    ensure_organization,
    ensure_user_account,
    ensure_user_oauth_link,
    gitea_org_url,
    gitea_username_for_user_email,
)
from .models import (
    KeycloakIdentity,
    Organization,
    OrganizationGiteaWorkspace,
    OrganizationMember,
    User,
)
from .work_ledger import create_job


_GITEA_ORG_RE = re.compile(r"[^a-z0-9-]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gitea_org_name(org: Organization) -> str:
    raw = _GITEA_ORG_RE.sub("-", org.slug.lower()).strip("-") or f"org-{org.id}"
    candidate = f"a2a-{raw}"
    if len(candidate) <= 39:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
    return f"{candidate[:30].rstrip('-')}-{digest}"


def _personal_org_slug(user: User) -> str:
    if user.id is None:
        raise ValueError("user must be flushed before resolving a personal org")
    return f"personal-{user.id}"


def _is_personal_org_for_user(org: Organization, user: User | None) -> bool:
    return user is not None and org.slug == _personal_org_slug(user)


def _workspace_owner_name(org: Organization, user: User | None = None) -> str:
    if _is_personal_org_for_user(org, user):
        return gitea_username_for_user_email(user.email, user_id=user.id)
    return gitea_org_name(org)


async def _enqueue_gitea_workspace_job(
    session: AsyncSession,
    org: Organization,
    workspace: OrganizationGiteaWorkspace,
    *,
    source: str,
) -> None:
    if not settings.gitea_provisioning_enabled:
        return
    await session.flush()
    request_hash = hashlib.sha256(
        "|".join(
            [
                "gitea.workspace.provision",
                str(org.id),
                str(workspace.id),
                workspace.gitea_org_name,
            ]
        ).encode("utf-8")
    ).hexdigest()
    idempotency_key = (
        f"gitea-workspace:{org.id}:{workspace.id}:{workspace.gitea_org_name}"
    )
    await create_job(
        session,
        user_id=None,
        kind="gitea.workspace.provision",
        payload={
            "organization_id": org.id,
            "organization_slug": org.slug,
            "workspace_id": workspace.id,
            "gitea_org_name": workspace.gitea_org_name,
        },
        title=f"Provision Gitea organization for {org.slug}",
        metadata={"source": source, "provider": "gitea"},
        queue="source-control",
        source_type="organization",
        source_id=str(org.id),
        subject_type="gitea_workspace",
        subject_id=str(workspace.id),
        worker_type="gitea",
        worker_name="gitea_provisioner",
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        idempotency_scope="gitea_provisioning",
        commit=False,
    )


async def ensure_gitea_workspace_row(
    session: AsyncSession,
    org: Organization,
    *,
    user: User | None = None,
    source: str,
) -> OrganizationGiteaWorkspace:
    owner_name = _workspace_owner_name(org, user)
    existing = (
        await session.execute(
            select(OrganizationGiteaWorkspace).where(
                OrganizationGiteaWorkspace.organization_id == org.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            _is_personal_org_for_user(org, user)
            and existing.gitea_org_name != owner_name
        ):
            if existing.status == "provisioned":
                metadata = dict(existing.metadata_json or {})
                metadata.update({
                    "source": source,
                    "interface": "gitea_api",
                })
                existing.metadata_json = metadata
                return existing
            existing.gitea_org_name = owner_name
            existing.org_url = gitea_org_url(owner_name)
            existing.status = (
                "pending" if settings.gitea_provisioning_enabled else "disabled"
            )
            metadata = dict(existing.metadata_json or {})
            metadata.update(
                {
                    "source": source,
                    "scope": "personal_user",
                    "interface": "gitea_api",
                    "user_id": user.id,
                    "user_email": user.email,
                }
            )
            existing.metadata_json = metadata
            await _enqueue_gitea_workspace_job(session, org, existing, source=source)
        return existing

    personal = _is_personal_org_for_user(org, user)
    workspace = OrganizationGiteaWorkspace(
        organization_id=org.id,
        status="pending" if settings.gitea_provisioning_enabled else "disabled",
        gitea_base_url=GITEA_PUBLIC.rstrip("/"),
        gitea_org_name=owner_name,
        org_url=gitea_org_url(owner_name),
        metadata_json={
            "source": source,
            "scope": "personal_user" if personal else "organization",
            "interface": "gitea_api",
            **({"user_id": user.id, "user_email": user.email} if personal else {}),
        },
    )
    session.add(workspace)
    await _enqueue_gitea_workspace_job(session, org, workspace, source=source)
    return workspace


async def provision_gitea_workspace(
    session: AsyncSession,
    workspace: OrganizationGiteaWorkspace,
    org: Organization,
) -> OrganizationGiteaWorkspace:
    metadata = dict(workspace.metadata_json or {})
    if metadata.get("scope") == "personal_user":
        email = str(metadata.get("user_email") or "").strip()
        if not email:
            raise RuntimeError("personal Gitea workspace is missing user_email metadata")
        raw = ensure_user_account(
            workspace.gitea_org_name,
            email=email,
            full_name=email,
        )
        keycloak_sub = await _keycloak_sub_for_personal_workspace(session, metadata)
        if keycloak_sub:
            try:
                if ensure_user_oauth_link(
                    workspace.gitea_org_name,
                    email=email,
                    keycloak_sub=keycloak_sub,
                ):
                    metadata["gitea_oauth_linked"] = True
                    metadata["gitea_oauth_auth_source"] = settings.gitea_oauth_auth_source_name
            except Exception as exc:  # noqa: BLE001
                metadata["gitea_oauth_linked"] = False
                metadata["gitea_oauth_link_error"] = str(exc)
    else:
        raw = ensure_organization(
            workspace.gitea_org_name,
            full_name=org.name,
            description=f"A2A organization {org.slug}",
        )
    workspace.status = "provisioned"
    workspace.gitea_base_url = GITEA_PUBLIC.rstrip("/")
    workspace.gitea_org_id = _first_string(raw, "id", "ID")
    workspace.org_url = gitea_org_url(workspace.gitea_org_name)
    workspace.last_error = None
    workspace.provisioned_at = _utcnow()
    metadata.update({
        "gitea_org_name": workspace.gitea_org_name,
        "gitea_org_url": workspace.org_url,
    })
    workspace.metadata_json = metadata
    await session.flush()
    return workspace


async def _keycloak_sub_for_personal_workspace(
    session: AsyncSession,
    metadata: dict,
) -> str | None:
    user_id = metadata.get("user_id")
    if user_id is None:
        return None
    try:
        parsed_user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    ident = (
        await session.execute(
            select(KeycloakIdentity)
            .where(KeycloakIdentity.user_id == parsed_user_id)
            .order_by(KeycloakIdentity.created_at.asc(), KeycloakIdentity.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return ident.keycloak_sub if ident is not None else None


async def resolve_agent_gitea_workspace(
    session: AsyncSession,
    user: User,
    *,
    organization_slug: str | None = None,
    source: str,
) -> tuple[Organization, OrganizationGiteaWorkspace]:
    query = (
        select(Organization, OrganizationMember)
        .join(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(OrganizationMember.user_id == user.id)
        .where(OrganizationMember.active == True)  # noqa: E712
    )
    if organization_slug:
        query = query.where(Organization.slug == organization_slug)
    query = query.order_by(
        (Organization.slug != _personal_org_slug(user)).asc(),
        Organization.created_at.asc(),
    )
    row = (await session.execute(query)).first()
    if row is None:
        raise ValueError("user has no active organization for source repository")
    org, _member = row
    workspace = await ensure_gitea_workspace_row(
        session,
        org,
        user=user,
        source=source,
    )
    if not settings.gitea_provisioning_enabled:
        raise ValueError("Gitea org provisioning is disabled")
    if workspace.status != "provisioned":
        await provision_gitea_workspace(session, workspace, org)
    return org, workspace


async def gitea_owner_for_agent(
    session: AsyncSession,
    agent_owner: User,
    organization_id: int | None,
) -> str | None:
    if organization_id is None:
        return None
    workspace = (
        await session.execute(
            select(OrganizationGiteaWorkspace).where(
                OrganizationGiteaWorkspace.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    org = await session.get(Organization, organization_id)
    if org is None:
        return workspace.gitea_org_name if workspace is not None else None
    expected_owner = _workspace_owner_name(org, agent_owner)
    if (
        workspace is not None
        and workspace.status == "provisioned"
        and workspace.gitea_org_name == expected_owner
    ):
        return workspace.gitea_org_name
    workspace = await ensure_gitea_workspace_row(
        session,
        org,
        user=agent_owner,
        source="agent_lookup",
    )
    await provision_gitea_workspace(session, workspace, org)
    return workspace.gitea_org_name


def _first_string(raw: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    return None
