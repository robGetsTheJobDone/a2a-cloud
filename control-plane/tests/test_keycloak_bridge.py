from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.keycloak_auth import provision_user_from_claims
from control_plane.models import KeycloakIdentity, User


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_provision_creates_user_and_identity_then_is_idempotent() -> None:
    engine, Session = await _session()
    try:
        async with Session() as session:
            claims = {
                "sub": "kc-abc-123",
                "email": "Connector@Example.com",
                "email_verified": True,
            }
            user = await provision_user_from_claims(session, claims)
            assert user.email == "connector@example.com"

            ident = (
                await session.execute(
                    select(KeycloakIdentity).where(
                        KeycloakIdentity.keycloak_sub == "kc-abc-123"
                    )
                )
            ).scalar_one()
            assert ident.user_id == user.id

            # Second login with the same sub returns the SAME user — no dup.
            again = await provision_user_from_claims(session, claims)
            assert again.id == user.id
            users = (await session.execute(select(User))).scalars().all()
            assert len(users) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_links_existing_user_by_email() -> None:
    engine, Session = await _session()
    try:
        async with Session() as session:
            existing = User(email="dev@example.com", password_hash="x")
            session.add(existing)
            await session.flush()
            existing_id = existing.id

            user = await provision_user_from_claims(
                session,
                {
                    "sub": "kc-link-1",
                    "email": "Dev@Example.com",
                    "email_verified": True,
                },
            )
            # Linked to the pre-existing account, not a new one.
            assert user.id == existing_id
            idents = (await session.execute(select(KeycloakIdentity))).scalars().all()
            assert len(idents) == 1 and idents[0].user_id == existing_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_rejects_unverified_email_claim() -> None:
    from fastapi import HTTPException

    engine, Session = await _session()
    try:
        async with Session() as session:
            existing = User(email="dev@example.com", password_hash="x")
            session.add(existing)
            await session.flush()

            with pytest.raises(HTTPException) as excinfo:
                await provision_user_from_claims(
                    session,
                    {
                        "sub": "kc-unverified",
                        "email": "Dev@Example.com",
                        "email_verified": False,
                    },
                )
            assert excinfo.value.status_code == 403
            users = (await session.execute(select(User))).scalars().all()
            assert len(users) == 1
            idents = (await session.execute(select(KeycloakIdentity))).scalars().all()
            assert idents == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_without_email_yields_serializable_user() -> None:
    # Email-less tokens (e.g. service accounts) get a placeholder email that
    # MUST pass UserOut's EmailStr — a reserved TLD like ".local" 500s /v1/me.
    from control_plane.schemas import UserOut

    engine, Session = await _session()
    try:
        async with Session() as session:
            user = await provision_user_from_claims(session, {"sub": "kc-no-email"})
            UserOut.model_validate(user)  # must not raise
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_rejects_token_without_sub() -> None:
    from fastapi import HTTPException

    engine, Session = await _session()
    try:
        async with Session() as session:
            with pytest.raises(HTTPException):
                await provision_user_from_claims(session, {"email": "x@y.z"})
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_adopts_verified_email_over_placeholder() -> None:
    # A user first seen via an email-less token gets a kc-<sub> placeholder.
    # The next login carrying a verified email must replace it so admin
    # pages show the real address.
    engine, Session = await _session()
    try:
        async with Session() as session:
            user = await provision_user_from_claims(session, {"sub": "kc-late-email"})
            assert user.email.endswith("@keycloak.a2acloud.io")

            user = await provision_user_from_claims(
                session,
                {"sub": "kc-late-email", "email": "Real@Example.com", "email_verified": True},
            )
            assert user.email == "real@example.com"
            ident = (
                await session.execute(
                    select(KeycloakIdentity).where(
                        KeycloakIdentity.keycloak_sub == "kc-late-email"
                    )
                )
            ).scalar_one()
            assert ident.email == "real@example.com"
            users = (await session.execute(select(User))).scalars().all()
            assert len(users) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_keeps_placeholder_when_email_already_taken() -> None:
    engine, Session = await _session()
    try:
        async with Session() as session:
            existing = await provision_user_from_claims(
                session,
                {"sub": "kc-owner", "email": "taken@example.com", "email_verified": True},
            )
            other = await provision_user_from_claims(session, {"sub": "kc-clash"})
            placeholder = other.email

            other = await provision_user_from_claims(
                session,
                {"sub": "kc-clash", "email": "taken@example.com", "email_verified": True},
            )
            assert other.email == placeholder
            assert other.id != existing.id
    finally:
        await engine.dispose()
