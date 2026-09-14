from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import (
    Organization,
    OrganizationAuditLog,
    OrganizationDomain,
    OrganizationMember,
    OrganizationScimToken,
    User,
)
from ..gitea_provisioning import ensure_gitea_workspace_row
from ..org_provisioning import (
    ensure_langfuse_user_account_row,
    ensure_langfuse_workspace_row,
)
from ..auth_utils import (
    domain_verification_record,
    normalize_domains,
    slugify_org,
    verify_domain_txt_record,
)

router = APIRouter(prefix="/v1/me/organizations", tags=["organizations"])
scim_router = APIRouter(prefix="/v1/scim", tags=["scim"])

ADMIN_ROLES = {"owner", "admin"}
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_SERVICE_PROVIDER_CONFIG_SCHEMA = (
    "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
)
SCIM_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCIM_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


class OrganizationCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str | None = Field(default=None, max_length=96)


class OrganizationOut(BaseModel):
    id: int
    slug: str
    name: str
    role: str
    created_at: datetime


class OrganizationDomainIn(BaseModel):
    domain: str = Field(min_length=3, max_length=255)


class OrganizationDomainOut(BaseModel):
    id: int
    domain: str
    verification_record_name: str
    verification_record_value: str
    verified_at: datetime | None
    created_at: datetime


class OrganizationMemberIn(BaseModel):
    email: EmailStr
    role: str = Field(default="member", pattern="^(owner|admin|member)$")


class OrganizationMemberUpdateIn(BaseModel):
    role: str = Field(pattern="^(owner|admin|member)$")


class OrganizationMemberOut(BaseModel):
    id: int
    user_id: int
    email: EmailStr
    role: str
    active: bool
    external_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class OrganizationAuditLogOut(BaseModel):
    id: int
    actor_user_id: int | None = None
    actor: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    target_email: str | None = None
    data: dict[str, Any]
    created_at: datetime


class OrganizationScimTokenIn(BaseModel):
    label: str = Field(default="SCIM provisioning", min_length=2, max_length=160)


class OrganizationScimTokenOut(BaseModel):
    id: int
    label: str
    token_last4: str
    enabled: bool
    created_by_email: EmailStr | None = None
    last_used_at: datetime | None = None
    created_at: datetime


class OrganizationScimTokenCreatedOut(OrganizationScimTokenOut):
    token: str


class OrganizationScimConfigOut(BaseModel):
    base_url: str
    service_provider_config_url: str
    users_url: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _membership(
    session: AsyncSession,
    org_id: int,
    user_id: int,
    *,
    include_inactive: bool = False,
) -> OrganizationMember | None:
    query = select(OrganizationMember).where(
        OrganizationMember.organization_id == org_id,
        OrganizationMember.user_id == user_id,
    )
    if not include_inactive:
        query = query.where(OrganizationMember.active == True)  # noqa: E712
    return (
        await session.execute(query)
    ).scalar_one_or_none()


async def _member_org(
    slug: str,
    user: User,
    session: AsyncSession,
    *,
    admin: bool = False,
) -> tuple[Organization, OrganizationMember]:
    row = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                Organization.slug == slug,
                OrganizationMember.user_id == user.id,
                OrganizationMember.active == True,  # noqa: E712
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "organization not found")
    org, member = row
    if admin and member.role not in ADMIN_ROLES:
        raise HTTPException(403, "organization admin required")
    return org, member


def _require_owner(member: OrganizationMember, detail: str) -> None:
    if member.role != "owner":
        raise HTTPException(403, detail)


def _hash_scim_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_scim_token() -> str:
    return "a2a_scim_" + secrets.token_urlsafe(32)


def _actor_for_user(user: User | None) -> str | None:
    return user.email if user is not None else None


def _scim_actor(token: OrganizationScimToken) -> str:
    return f"scim:{token.label}:{token.token_last4}"


def _add_audit(
    session: AsyncSession,
    *,
    org_id: int,
    action: str,
    target_type: str,
    actor_user: User | None = None,
    actor: str | None = None,
    target_id: str | int | None = None,
    target_email: str | None = None,
    data: dict[str, Any] | None = None,
) -> OrganizationAuditLog:
    row = OrganizationAuditLog(
        organization_id=org_id,
        actor_user_id=actor_user.id if actor_user is not None else None,
        actor=actor or _actor_for_user(actor_user),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        target_email=target_email,
        data=data or {},
    )
    session.add(row)
    return row


def _audit_out(row: OrganizationAuditLog) -> OrganizationAuditLogOut:
    return OrganizationAuditLogOut(
        id=row.id,
        actor_user_id=row.actor_user_id,
        actor=row.actor,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        target_email=row.target_email,
        data=row.data or {},
        created_at=row.created_at,
    )


def _domain_out(domain: OrganizationDomain) -> OrganizationDomainOut:
    name, value = domain_verification_record(
        domain.domain,
        domain.verification_token,
    )
    return OrganizationDomainOut(
        id=domain.id,
        domain=domain.domain,
        verification_record_name=name,
        verification_record_value=value,
        verified_at=domain.verified_at,
        created_at=domain.created_at,
    )


def _member_out(member: OrganizationMember, user: User) -> OrganizationMemberOut:
    return OrganizationMemberOut(
        id=member.id,
        user_id=user.id,
        email=user.email,
        role=member.role,
        active=member.active,
        external_id=member.external_id,
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


def _scim_token_out(
    token: OrganizationScimToken,
    created_by: User | None = None,
) -> OrganizationScimTokenOut:
    return OrganizationScimTokenOut(
        id=token.id,
        label=token.label,
        token_last4=token.token_last4,
        enabled=token.enabled,
        created_by_email=created_by.email if created_by is not None else None,
        last_used_at=token.last_used_at,
        created_at=token.created_at,
    )


def _scim_config_out(slug: str, request: Request) -> OrganizationScimConfigOut:
    base = str(request.base_url).rstrip("/")
    scim_base = f"{base}/v1/scim/{slug}/v2"
    return OrganizationScimConfigOut(
        base_url=scim_base,
        service_provider_config_url=f"{scim_base}/ServiceProviderConfig",
        users_url=f"{scim_base}/Users",
    )


async def _verified_domains(
    org_id: int,
    session: AsyncSession,
) -> set[str]:
    rows = (
        await session.execute(
            select(OrganizationDomain).where(
                OrganizationDomain.organization_id == org_id,
                OrganizationDomain.verified_at.is_not(None),
            )
        )
    ).scalars().all()
    return {row.domain for row in rows}


async def _require_scim_email_domain(
    org: Organization,
    email: str,
    session: AsyncSession,
) -> None:
    domain = email.rsplit("@", 1)[-1].lower()
    verified_domains = await _verified_domains(org.id, session)
    if not verified_domains:
        raise HTTPException(
            400,
            "SCIM provisioning requires at least one verified organization domain",
        )
    if domain not in verified_domains:
        raise HTTPException(
            400,
            "SCIM user email domain must match a verified organization domain",
        )


async def _owner_count(org_id: int, session: AsyncSession) -> int:
    rows = (
        await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.role == "owner",
                OrganizationMember.active == True,  # noqa: E712
            )
        )
    ).scalars().all()
    return len(rows)


async def _member_outs(
    org_id: int,
    session: AsyncSession,
) -> list[OrganizationMemberOut]:
    rows = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == org_id)
            .order_by(OrganizationMember.active.desc(), OrganizationMember.role, User.email)
        )
    ).all()
    return [_member_out(member, user) for member, user in rows]


@router.get("", response_model=list[OrganizationOut])
async def list_organizations(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrganizationOut]:
    rows = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(OrganizationMember.user_id == user.id)
            .where(OrganizationMember.active == True)  # noqa: E712
            .order_by(Organization.name)
        )
    ).all()
    return [
        OrganizationOut(
            id=org.id,
            slug=org.slug,
            name=org.name,
            role=member.role,
            created_at=org.created_at,
        )
        for org, member in rows
    ]


@router.post("", response_model=OrganizationOut, status_code=201)
async def create_organization(
    body: OrganizationCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "organization name is required")
    slug = slugify_org(body.slug or name)
    org = Organization(slug=slug, name=name, created_by_id=user.id)
    session.add(org)
    try:
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role="owner",
            )
        )
        _add_audit(
            session,
            org_id=org.id,
            actor_user=user,
            action="organization.create",
            target_type="organization",
            target_id=org.id,
            data={"slug": org.slug, "name": org.name},
        )
        await ensure_gitea_workspace_row(session, org, source="organization_create")
        await ensure_langfuse_workspace_row(session, org, source="organization_create")
        await ensure_langfuse_user_account_row(
            session,
            org,
            user,
            role="owner",
            source="organization_create",
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "organization slug already exists")
    await session.refresh(org)
    return OrganizationOut(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role="owner",
        created_at=org.created_at,
    )


@router.get("/{slug}/domains", response_model=list[OrganizationDomainOut])
async def list_organization_domains(
    slug: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrganizationDomainOut]:
    org, _member = await _member_org(slug, user, session, admin=True)
    rows = (
        await session.execute(
            select(OrganizationDomain)
            .where(OrganizationDomain.organization_id == org.id)
            .order_by(OrganizationDomain.domain)
        )
    ).scalars().all()
    return [_domain_out(row) for row in rows]


@router.post("/{slug}/domains", response_model=OrganizationDomainOut, status_code=201)
async def add_organization_domain(
    slug: str,
    body: OrganizationDomainIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationDomainOut:
    org, _member = await _member_org(slug, user, session, admin=True)
    domains = normalize_domains([body.domain])
    if not domains:
        raise HTTPException(400, "valid domain is required")
    domain = domains[0]
    existing = (
        await session.execute(
            select(OrganizationDomain).where(OrganizationDomain.domain == domain)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.organization_id != org.id:
            raise HTTPException(409, "domain is already claimed")
        return _domain_out(existing)
    row = OrganizationDomain(
        organization_id=org.id,
        domain=domain,
        verification_token=secrets.token_urlsafe(24),
    )
    session.add(row)
    await session.flush()
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="domain.add",
        target_type="domain",
        target_id=row.id,
        data={"domain": row.domain},
    )
    await session.commit()
    await session.refresh(row)
    return _domain_out(row)


@router.post("/{slug}/domains/{domain}/verify", response_model=OrganizationDomainOut)
async def verify_organization_domain(
    slug: str,
    domain: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationDomainOut:
    org, _member = await _member_org(slug, user, session, admin=True)
    domains = normalize_domains([domain])
    if not domains:
        raise HTTPException(400, "valid domain is required")
    row = (
        await session.execute(
            select(OrganizationDomain).where(
                OrganizationDomain.organization_id == org.id,
                OrganizationDomain.domain == domains[0],
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "domain not found")
    if not verify_domain_txt_record(row.domain, row.verification_token):
        raise HTTPException(400, "DNS TXT verification record was not found")
    row.verified_at = _utcnow()
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="domain.verify",
        target_type="domain",
        target_id=row.id,
        data={"domain": row.domain},
    )
    await session.commit()
    await session.refresh(row)
    return _domain_out(row)


@router.delete("/{slug}/domains/{domain}", status_code=204)
async def delete_organization_domain(
    slug: str,
    domain: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    org, _member = await _member_org(slug, user, session, admin=True)
    domains = normalize_domains([domain])
    if not domains:
        raise HTTPException(400, "valid domain is required")
    row = (
        await session.execute(
            select(OrganizationDomain).where(
                OrganizationDomain.organization_id == org.id,
                OrganizationDomain.domain == domains[0],
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "domain not found")
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="domain.delete",
        target_type="domain",
        target_id=row.id,
        data={"domain": row.domain},
    )
    await session.delete(row)
    await session.commit()


@router.get("/{slug}/members", response_model=list[OrganizationMemberOut])
async def list_organization_members(
    slug: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrganizationMemberOut]:
    org, _member = await _member_org(slug, user, session, admin=True)
    return await _member_outs(org.id, session)


@router.post("/{slug}/members", response_model=OrganizationMemberOut, status_code=201)
async def add_organization_member(
    slug: str,
    body: OrganizationMemberIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationMemberOut:
    org, actor_member = await _member_org(slug, user, session, admin=True)
    if body.role == "owner":
        _require_owner(actor_member, "organization owner required to add another owner")
    email = str(body.email).strip().lower()
    target = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if target is None:
        # Invite without email infrastructure: create a disabled-password user
        # that can later enter through Keycloak with a verified email.
        target = User(email=email, password_hash="")
        session.add(target)
        await session.flush()
    existing = await _membership(session, org.id, target.id, include_inactive=True)
    if existing is None:
        existing = OrganizationMember(
            organization_id=org.id,
            user_id=target.id,
            role=body.role,
            active=True,
        )
        session.add(existing)
    else:
        existing.role = body.role
        existing.active = True
    await session.flush()
    await ensure_gitea_workspace_row(session, org, source="member_add")
    await ensure_langfuse_workspace_row(session, org, source="member_add")
    await ensure_langfuse_user_account_row(
        session,
        org,
        target,
        role=existing.role,
        source="member_add",
    )
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="member.add",
        target_type="member",
        target_id=existing.id,
        target_email=target.email,
        data={"role": existing.role},
    )
    await session.commit()
    await session.refresh(existing)
    return _member_out(existing, target)


@router.patch("/{slug}/members/{member_id}", response_model=OrganizationMemberOut)
async def update_organization_member(
    slug: str,
    member_id: int,
    body: OrganizationMemberUpdateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationMemberOut:
    org, actor_member = await _member_org(slug, user, session, admin=True)
    row = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.id == member_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "member not found")
    member, target = row
    if member.role != body.role and (member.role == "owner" or body.role == "owner"):
        _require_owner(actor_member, "organization owner required to manage owner role")
    if member.role == "owner" and body.role != "owner":
        if await _owner_count(org.id, session) <= 1:
            raise HTTPException(400, "cannot demote the last owner")
    old_role = member.role
    member.role = body.role
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="member.update",
        target_type="member",
        target_id=member.id,
        target_email=target.email,
        data={"old_role": old_role, "new_role": member.role},
    )
    await session.commit()
    await session.refresh(member)
    return _member_out(member, target)


@router.delete("/{slug}/members/{member_id}", status_code=204)
async def delete_organization_member(
    slug: str,
    member_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    org, actor_member = await _member_org(slug, user, session, admin=True)
    row = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.id == member_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "member not found")
    member, target = row
    if member.role == "owner":
        _require_owner(actor_member, "organization owner required to remove an owner")
        if await _owner_count(org.id, session) <= 1:
            raise HTTPException(400, "cannot remove the last owner")
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="member.delete",
        target_type="member",
        target_id=member.id,
        target_email=target.email,
        data={"role": member.role},
    )
    await session.delete(member)
    await session.commit()


@router.get("/{slug}/audit-log", response_model=list[OrganizationAuditLogOut])
async def list_organization_audit_log(
    slug: str,
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrganizationAuditLogOut]:
    org, _member = await _member_org(slug, user, session, admin=True)
    rows = (
        await session.execute(
            select(OrganizationAuditLog)
            .where(OrganizationAuditLog.organization_id == org.id)
            .order_by(OrganizationAuditLog.created_at.desc(), OrganizationAuditLog.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_audit_out(row) for row in rows]


@router.get("/{slug}/scim", response_model=OrganizationScimConfigOut)
async def get_scim_config(
    slug: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationScimConfigOut:
    org, _member = await _member_org(slug, user, session, admin=True)
    return _scim_config_out(org.slug, request)


@router.get("/{slug}/scim/tokens", response_model=list[OrganizationScimTokenOut])
async def list_scim_tokens(
    slug: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[OrganizationScimTokenOut]:
    org, _member = await _member_org(slug, user, session, admin=True)
    rows = (
        await session.execute(
            select(OrganizationScimToken, User)
            .join(User, User.id == OrganizationScimToken.created_by_id, isouter=True)
            .where(OrganizationScimToken.organization_id == org.id)
            .order_by(OrganizationScimToken.enabled.desc(), OrganizationScimToken.created_at.desc())
        )
    ).all()
    return [_scim_token_out(token, creator) for token, creator in rows]


@router.post(
    "/{slug}/scim/tokens",
    response_model=OrganizationScimTokenCreatedOut,
    status_code=201,
)
async def create_scim_token(
    slug: str,
    body: OrganizationScimTokenIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OrganizationScimTokenCreatedOut:
    org, _member = await _member_org(slug, user, session, admin=True)
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "SCIM token label is required")
    raw_token = _new_scim_token()
    token = OrganizationScimToken(
        organization_id=org.id,
        label=label,
        token_hash=_hash_scim_token(raw_token),
        token_last4=raw_token[-4:],
        enabled=True,
        created_by_id=user.id,
    )
    session.add(token)
    await session.flush()
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="scim.token.create",
        target_type="scim_token",
        target_id=token.id,
        data={"label": token.label, "token_last4": token.token_last4},
    )
    await session.commit()
    await session.refresh(token)
    out = _scim_token_out(token, user)
    return OrganizationScimTokenCreatedOut(**out.model_dump(), token=raw_token)


@router.delete("/{slug}/scim/tokens/{token_id}", status_code=204)
async def revoke_scim_token(
    slug: str,
    token_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    org, _member = await _member_org(slug, user, session, admin=True)
    token = (
        await session.execute(
            select(OrganizationScimToken).where(
                OrganizationScimToken.organization_id == org.id,
                OrganizationScimToken.id == token_id,
            )
        )
    ).scalar_one_or_none()
    if token is None:
        raise HTTPException(404, "SCIM token not found")
    token.enabled = False
    _add_audit(
        session,
        org_id=org.id,
        actor_user=user,
        action="scim.token.revoke",
        target_type="scim_token",
        target_id=token.id,
        data={"label": token.label, "token_last4": token.token_last4},
    )
    await session.commit()


async def _scim_context(
    slug: str,
    authorization: str | None,
    session: AsyncSession,
) -> tuple[Organization, OrganizationScimToken]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            401,
            "SCIM bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raw_token = authorization.split(" ", 1)[1].strip()
    if not raw_token:
        raise HTTPException(
            401,
            "SCIM bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    row = (
        await session.execute(
            select(Organization, OrganizationScimToken)
            .join(
                OrganizationScimToken,
                OrganizationScimToken.organization_id == Organization.id,
            )
            .where(
                Organization.slug == slug,
                OrganizationScimToken.token_hash == _hash_scim_token(raw_token),
                OrganizationScimToken.enabled == True,  # noqa: E712
            )
        )
    ).first()
    if row is None:
        raise HTTPException(
            401,
            "invalid SCIM bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    org, token = row
    token.last_used_at = _utcnow()
    await session.commit()
    return org, token


def _scim_user_resource(
    slug: str,
    member: OrganizationMember,
    user: User,
    request: Request | None = None,
) -> dict[str, Any]:
    created = member.created_at.isoformat()
    updated = (member.updated_at or member.created_at).isoformat()
    location = None
    if request is not None:
        base = str(request.base_url).rstrip("/")
        location = f"{base}/v1/scim/{slug}/v2/Users/{member.id}"
    meta: dict[str, Any] = {
        "resourceType": "User",
        "created": created,
        "lastModified": updated,
    }
    if location:
        meta["location"] = location
    resource: dict[str, Any] = {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(member.id),
        "userName": user.email,
        "active": member.active,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "roles": [{"value": member.role, "primary": True}],
        "meta": meta,
    }
    if member.external_id:
        resource["externalId"] = member.external_id
    return resource


async def _scim_user_rows(
    org_id: int,
    session: AsyncSession,
) -> list[tuple[OrganizationMember, User]]:
    rows = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == org_id)
            .order_by(User.email)
        )
    ).all()
    return [(member, user) for member, user in rows]


async def _scim_user_row(
    org_id: int,
    member_id: str,
    session: AsyncSession,
) -> tuple[OrganizationMember, User]:
    try:
        parsed_member_id = int(member_id)
    except ValueError:
        raise HTTPException(404, "SCIM user not found")
    row = (
        await session.execute(
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.id == parsed_member_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "SCIM user not found")
    member, user = row
    return member, user


def _scim_email_from_payload(payload: dict[str, Any]) -> str:
    candidate = payload.get("userName")
    if not candidate and isinstance(payload.get("emails"), list):
        emails = payload["emails"]
        primary = next(
            (
                email
                for email in emails
                if isinstance(email, dict) and email.get("primary") is True
            ),
            None,
        )
        email_obj = primary or next(
            (email for email in emails if isinstance(email, dict)),
            None,
        )
        if email_obj is not None:
            candidate = email_obj.get("value")
    if not isinstance(candidate, str):
        raise HTTPException(400, "SCIM userName email is required")
    email = candidate.strip().lower()
    local_part, separator, domain = email.rpartition("@")
    if not separator or not local_part or not domain:
        raise HTTPException(400, "SCIM userName email is required")
    return email


def _scim_external_id_from_payload(payload: dict[str, Any]) -> str | None:
    value = payload.get("externalId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _scim_active_from_payload(payload: dict[str, Any], default: bool = True) -> bool:
    value = payload.get("active", default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() not in {"false", "0", "no"}
    return bool(value)


async def _ensure_scim_user(
    org: Organization,
    payload: dict[str, Any],
    token: OrganizationScimToken,
    session: AsyncSession,
) -> tuple[OrganizationMember, User, bool]:
    email = _scim_email_from_payload(payload)
    await _require_scim_email_domain(org, email, session)
    external_id = _scim_external_id_from_payload(payload)
    active = _scim_active_from_payload(payload)
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(email=email, password_hash="")
        session.add(user)
        await session.flush()
    member = await _membership(session, org.id, user.id, include_inactive=True)
    created = member is None
    if member is None:
        member = OrganizationMember(
            organization_id=org.id,
            user_id=user.id,
            role="member",
            active=active,
            external_id=external_id,
        )
        session.add(member)
    else:
        if member.role == "owner" and member.active and not active:
            if await _owner_count(org.id, session) <= 1:
                raise HTTPException(400, "cannot deactivate the last owner")
        member.active = active
        member.external_id = external_id or member.external_id
    await session.flush()
    if member.active:
        await ensure_gitea_workspace_row(session, org, source="scim")
        await ensure_langfuse_workspace_row(session, org, source="scim")
        await ensure_langfuse_user_account_row(
            session,
            org,
            user,
            role=member.role,
            source="scim",
        )
    _add_audit(
        session,
        org_id=org.id,
        actor=_scim_actor(token),
        action="scim.user.create" if created else "scim.user.update",
        target_type="member",
        target_id=member.id,
        target_email=user.email,
        data={"active": member.active, "external_id": member.external_id},
    )
    return member, user, created


def _filter_scim_users(
    rows: list[tuple[OrganizationMember, User]],
    filter_value: str | None,
) -> list[tuple[OrganizationMember, User]]:
    if not filter_value:
        return rows
    match = re.fullmatch(
        r"\s*(userName|externalId)\s+eq\s+\"([^\"]+)\"\s*",
        filter_value,
        flags=re.IGNORECASE,
    )
    if not match:
        raise HTTPException(400, "unsupported SCIM filter")
    field, value = match.group(1).lower(), match.group(2).lower()
    if field == "username":
        return [(member, user) for member, user in rows if user.email.lower() == value]
    return [
        (member, user)
        for member, user in rows
        if (member.external_id or "").lower() == value
    ]


async def _apply_scim_operation(
    org: Organization,
    member: OrganizationMember,
    user: User,
    op: dict[str, Any],
    session: AsyncSession,
) -> None:
    operation = str(op.get("op") or "replace").lower()
    if operation not in {"replace", "add"}:
        raise HTTPException(400, f"unsupported SCIM patch operation: {operation}")
    path = str(op.get("path") or "").lower()
    value = op.get("value")
    updates = value if isinstance(value, dict) and not path else {path: value}
    for raw_key, raw_value in updates.items():
        key = str(raw_key).lower()
        if key == "active":
            active = _scim_active_from_payload({"active": raw_value})
            if member.role == "owner" and member.active and not active:
                if await _owner_count(org.id, session) <= 1:
                    raise HTTPException(400, "cannot deactivate the last owner")
            member.active = active
        elif key in {"username", "userName".lower(), "emails[type eq \"work\"].value"}:
            if not isinstance(raw_value, str):
                raise HTTPException(400, "valid email is required")
            email = raw_value.strip().lower()
            local_part, separator, domain = email.rpartition("@")
            if not separator or not local_part or not domain:
                raise HTTPException(400, "valid email is required")
            await _require_scim_email_domain(org, email, session)
            user.email = email
        elif key == "externalid":
            member.external_id = str(raw_value).strip() if raw_value is not None else None
        elif key.startswith("name"):
            continue
        else:
            continue


@scim_router.get("/{slug}/v2/ServiceProviderConfig")
async def scim_service_provider_config(
    slug: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _scim_context(slug, authorization, session)
    return {
        "schemas": [SCIM_SERVICE_PROVIDER_CONFIG_SCHEMA],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 500},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer Token",
                "description": "Use the organization SCIM bearer token.",
                "primary": True,
            }
        ],
    }


@scim_router.get("/{slug}/v2/ResourceTypes")
async def scim_resource_types(
    slug: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _scim_context(slug, authorization, session)
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "schemas": [SCIM_RESOURCE_TYPE_SCHEMA],
                "id": "User",
                "name": "User",
                "endpoint": "/Users",
                "schema": SCIM_USER_SCHEMA,
            }
        ],
    }


@scim_router.get("/{slug}/v2/Schemas")
async def scim_schemas(
    slug: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _scim_context(slug, authorization, session)
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "schemas": [SCIM_SCHEMA_SCHEMA],
                "id": SCIM_USER_SCHEMA,
                "name": "User",
                "attributes": [
                    {"name": "userName", "type": "string", "required": True},
                    {"name": "active", "type": "boolean"},
                    {"name": "emails", "type": "complex", "multiValued": True},
                    {"name": "externalId", "type": "string"},
                ],
            }
        ],
    }


@scim_router.get("/{slug}/v2/Groups")
async def scim_groups(
    slug: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _scim_context(slug, authorization, session)
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 0,
        "startIndex": 1,
        "itemsPerPage": 0,
        "Resources": [],
    }


@scim_router.get("/{slug}/v2/Users")
async def list_scim_users(
    slug: str,
    request: Request,
    authorization: str | None = Header(default=None),
    filter_: str | None = Query(default=None, alias="filter"),
    start_index: int = Query(default=1, alias="startIndex", ge=1),
    count: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    org, _token = await _scim_context(slug, authorization, session)
    rows = _filter_scim_users(await _scim_user_rows(org.id, session), filter_)
    total = len(rows)
    offset = start_index - 1
    page = rows[offset : offset + count]
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(page),
        "Resources": [
            _scim_user_resource(org.slug, member, user, request) for member, user in page
        ],
    }


@scim_router.post("/{slug}/v2/Users", status_code=201)
async def create_scim_user(
    slug: str,
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    org, token = await _scim_context(slug, authorization, session)
    try:
        member, user, _created = await _ensure_scim_user(org, payload, token, session)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "SCIM user email already exists")
    await session.refresh(member)
    await session.refresh(user)
    return _scim_user_resource(org.slug, member, user, request)


@scim_router.get("/{slug}/v2/Users/{user_id}")
async def get_scim_user(
    slug: str,
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    org, _token = await _scim_context(slug, authorization, session)
    member, user = await _scim_user_row(org.id, user_id, session)
    return _scim_user_resource(org.slug, member, user, request)


@scim_router.put("/{slug}/v2/Users/{user_id}")
async def replace_scim_user(
    slug: str,
    user_id: str,
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    org, token = await _scim_context(slug, authorization, session)
    member, user = await _scim_user_row(org.id, user_id, session)
    old_email = user.email
    active = _scim_active_from_payload(payload, default=member.active)
    if member.role == "owner" and member.active and not active:
        if await _owner_count(org.id, session) <= 1:
            raise HTTPException(400, "cannot deactivate the last owner")
    email = _scim_email_from_payload(payload)
    await _require_scim_email_domain(org, email, session)
    user.email = email
    member.active = active
    member.external_id = _scim_external_id_from_payload(payload) or member.external_id
    _add_audit(
        session,
        org_id=org.id,
        actor=_scim_actor(token),
        action="scim.user.update",
        target_type="member",
        target_id=member.id,
        target_email=user.email,
        data={
            "old_email": old_email,
            "active": member.active,
            "external_id": member.external_id,
        },
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "SCIM user email already exists")
    await session.refresh(member)
    await session.refresh(user)
    return _scim_user_resource(org.slug, member, user, request)


@scim_router.patch("/{slug}/v2/Users/{user_id}")
async def patch_scim_user(
    slug: str,
    user_id: str,
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    org, token = await _scim_context(slug, authorization, session)
    member, user = await _scim_user_row(org.id, user_id, session)
    old_state = {
        "email": user.email,
        "active": member.active,
        "external_id": member.external_id,
    }
    operations = payload.get("Operations") or payload.get("operations")
    if not isinstance(operations, list):
        raise HTTPException(400, "SCIM patch Operations list is required")
    for op in operations:
        if not isinstance(op, dict):
            raise HTTPException(400, "SCIM patch operation must be an object")
        await _apply_scim_operation(org, member, user, op, session)
    action = "scim.user.deactivate" if old_state["active"] and not member.active else "scim.user.update"
    _add_audit(
        session,
        org_id=org.id,
        actor=_scim_actor(token),
        action=action,
        target_type="member",
        target_id=member.id,
        target_email=user.email,
        data={
            "old": old_state,
            "new": {
                "email": user.email,
                "active": member.active,
                "external_id": member.external_id,
            },
        },
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, "SCIM user email already exists")
    await session.refresh(member)
    await session.refresh(user)
    return _scim_user_resource(org.slug, member, user, request)


@scim_router.delete("/{slug}/v2/Users/{user_id}", status_code=204)
async def delete_scim_user(
    slug: str,
    user_id: str,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    org, token = await _scim_context(slug, authorization, session)
    member, user = await _scim_user_row(org.id, user_id, session)
    if member.role == "owner" and member.active:
        if await _owner_count(org.id, session) <= 1:
            raise HTTPException(400, "cannot deactivate the last owner")
    member.active = False
    _add_audit(
        session,
        org_id=org.id,
        actor=_scim_actor(token),
        action="scim.user.deactivate",
        target_type="member",
        target_id=member.id,
        target_email=user.email,
        data={"external_id": member.external_id},
    )
    await session.commit()
