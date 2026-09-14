from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import (
    Organization,
    OrganizationAuditLog,
    OrganizationDomain,
    OrganizationMember,
    OrganizationScimToken,
    User,
)
from control_plane.routes.organizations import (
    OrganizationMemberIn,
    OrganizationMemberUpdateIn,
    OrganizationScimTokenIn,
    _apply_scim_operation,
    _filter_scim_users,
    _hash_scim_token,
    _ensure_scim_user,
    _scim_email_from_payload,
    _scim_user_resource,
    add_organization_member,
    create_scim_token,
    delete_organization_member,
    update_organization_member,
)
from control_plane.auth_utils import (
    domain_verification_record,
    normalize_domains,
    sanitize_redirect,
    slugify_org,
)


def test_slugify_org_keeps_enterprise_slugs_url_safe() -> None:
    assert slugify_org("Acme, Inc.") == "acme-inc"
    assert slugify_org("  $$$  ") == "org"


def test_normalize_domains_dedupes_and_rejects_invalid_values() -> None:
    assert normalize_domains(["@Acme.com", "acme.com", "bad", "ENG.example"]) == [
        "acme.com",
        "eng.example",
    ]


def test_domain_verification_record_uses_dedicated_txt_name() -> None:
    assert domain_verification_record("acme.com", "tok") == (
        "_a2a-sso.acme.com",
        "a2a-sso-verification=tok",
    )


def test_sanitize_redirect_rejects_open_redirects() -> None:
    assert sanitize_redirect("/workspace") == "/workspace"
    assert sanitize_redirect("https://evil.example") == "/"
    assert sanitize_redirect("//evil.example") == "/"
    assert sanitize_redirect("/\\evil.example") == "/"
    assert sanitize_redirect("/%2fevil.example") == "/"
    assert sanitize_redirect("/%255cevil.example") == "/"
    assert sanitize_redirect("/workspace%0d%0aLocation:%20https://evil.example") == "/"


def test_hash_scim_token_is_stable_sha256_without_storing_raw_secret() -> None:
    assert _hash_scim_token("a2a_scim_secret") == _hash_scim_token("a2a_scim_secret")
    assert _hash_scim_token("a2a_scim_secret") != "a2a_scim_secret"
    assert len(_hash_scim_token("a2a_scim_secret")) == 64


def test_scim_user_resource_maps_member_state() -> None:
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    member = OrganizationMember(
        id=9,
        organization_id=1,
        user_id=5,
        role="member",
        active=False,
        external_id="okta-123",
        created_at=now,
        updated_at=now,
    )
    user = User(id=5, email="person@acme.com", password_hash="")

    resource = _scim_user_resource("acme", member, user)

    assert resource["id"] == "9"
    assert resource["userName"] == "person@acme.com"
    assert resource["active"] is False
    assert resource["externalId"] == "okta-123"
    assert resource["emails"][0]["value"] == "person@acme.com"


def test_filter_scim_users_supports_username_and_external_id() -> None:
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    member = OrganizationMember(
        id=9,
        organization_id=1,
        user_id=5,
        role="member",
        active=True,
        external_id="Okta-123",
        created_at=now,
        updated_at=now,
    )
    user = User(id=5, email="person@acme.com", password_hash="")

    rows = [(member, user)]

    assert _filter_scim_users(rows, 'userName eq "PERSON@acme.com"') == rows
    assert _filter_scim_users(rows, 'externalId eq "okta-123"') == rows
    with pytest.raises(HTTPException):
        _filter_scim_users(rows, 'emails.value co "acme.com"')


def test_scim_email_payload_requires_local_part_and_domain() -> None:
    assert (
        _scim_email_from_payload({"userName": " Person@Acme.com "})
        == "person@acme.com"
    )
    with pytest.raises(HTTPException):
        _scim_email_from_payload({"userName": "@acme.com"})
    with pytest.raises(HTTPException):
        _scim_email_from_payload({"emails": [{"value": "person@"}]})


async def _seed_org(session: AsyncSession) -> tuple[User, User]:
    owner = User(id=1, email="owner@acme.com", password_hash="x")
    admin = User(id=2, email="admin@acme.com", password_hash="x")
    org = Organization(id=1, slug="acme", name="Acme", created_by_id=owner.id)
    session.add_all(
        [
            owner,
            admin,
            org,
            OrganizationMember(
                id=1,
                organization_id=org.id,
                user_id=owner.id,
                role="owner",
            ),
            OrganizationMember(
                id=2,
                organization_id=org.id,
                user_id=admin.id,
                role="admin",
            ),
        ]
    )
    await session.commit()
    return owner, admin


async def _seed_verified_domain_and_scim_token(
    session: AsyncSession,
    org_id: int,
) -> OrganizationScimToken:
    session.add(
        OrganizationDomain(
            organization_id=org_id,
            domain="acme.com",
            verification_token="tok",
            verified_at=datetime.now(timezone.utc),
        )
    )
    token = OrganizationScimToken(
        organization_id=org_id,
        label="Okta",
        token_hash=_hash_scim_token("a2a_scim_secret"),
        token_last4="cret",
        enabled=True,
        created_by_id=1,
    )
    session.add(token)
    await session.commit()
    return token


@pytest.mark.asyncio
async def test_admins_cannot_manage_owner_role() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            _owner, admin = await _seed_org(session)

            with pytest.raises(HTTPException) as exc_info:
                await add_organization_member(
                    "acme",
                    OrganizationMemberIn(email="new-owner@acme.com", role="owner"),
                    user=admin,
                    session=session,
                )
            assert exc_info.value.status_code == 403

            with pytest.raises(HTTPException) as exc_info:
                await update_organization_member(
                    "acme",
                    1,
                    OrganizationMemberUpdateIn(role="admin"),
                    user=admin,
                    session=session,
                )
            assert exc_info.value.status_code == 403

            with pytest.raises(HTTPException) as exc_info:
                await delete_organization_member(
                    "acme",
                    1,
                    user=admin,
                    session=session,
                )
            assert exc_info.value.status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_scim_token_rejects_blank_label_after_trimming() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _admin = await _seed_org(session)

            with pytest.raises(HTTPException) as exc_info:
                await create_scim_token(
                    "acme",
                    OrganizationScimTokenIn(label="  "),
                    user=owner,
                    session=session,
                )
            assert exc_info.value.status_code == 400
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scim_user_create_requires_verified_email_domain() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_org(session)
            org = (await session.execute(select(Organization))).scalar_one()
            token = await _seed_verified_domain_and_scim_token(session, org.id)

            with pytest.raises(HTTPException) as exc_info:
                await _ensure_scim_user(
                    org,
                    {"userName": "person@evil.example", "externalId": "okta-1"},
                    token,
                    session,
                )
            assert exc_info.value.status_code == 400
            assert "verified organization domain" in str(exc_info.value.detail)
            missing = (
                await session.execute(
                    select(User).where(User.email == "person@evil.example")
                )
            ).scalar_one_or_none()
            assert missing is None

            member, user, created = await _ensure_scim_user(
                org,
                {"userName": "person@acme.com", "externalId": "okta-2"},
                token,
                session,
            )
            await session.flush()
            audit = (
                await session.execute(
                    select(OrganizationAuditLog)
                    .where(OrganizationAuditLog.target_email == "person@acme.com")
                    .order_by(OrganizationAuditLog.id.desc())
                )
            ).scalar_one()
            assert created is True
            assert user.email == "person@acme.com"
            assert member.external_id == "okta-2"
            assert audit.action == "scim.user.create"
            assert audit.actor == "scim:Okta:cret"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scim_patch_email_requires_verified_domain() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_org(session)
            org = (await session.execute(select(Organization))).scalar_one()
            await _seed_verified_domain_and_scim_token(session, org.id)
            user = User(email="person@acme.com", password_hash="")
            session.add(user)
            await session.flush()
            member = OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role="member",
                active=True,
            )
            session.add(member)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await _apply_scim_operation(
                    org,
                    member,
                    user,
                    {"op": "replace", "path": "userName", "value": "person@evil.example"},
                    session,
                )
            assert exc_info.value.status_code == 400
            assert user.email == "person@acme.com"

            await _apply_scim_operation(
                org,
                member,
                user,
                {"op": "replace", "path": "userName", "value": "new@acme.com"},
                session,
            )
            assert user.email == "new@acme.com"
    finally:
        await engine.dispose()
