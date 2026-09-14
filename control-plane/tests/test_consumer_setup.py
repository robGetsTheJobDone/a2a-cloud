from __future__ import annotations

import json
import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.consumer_setup import (
    ConsumerSetupRequired,
    install_agent,
    list_installed_agents,
    require_consumer_setup,
    resolve_consumer_setup,
    upsert_consumer_setup_values,
)
from control_plane.db import Base
from control_plane.imported_agent_auth import upsert_connection
from control_plane.models import Agent, Organization, OrganizationMember, User
from control_plane.routes.consumer_setup import get_consumer_setup_invocation


def _card() -> dict[str, object]:
    return {
        "name": "github-agent",
        "description": "GitHub helper",
        "version": "0.1.0",
        "skills": [],
        "consumer_setup": {
            "fields": [
                {
                    "name": "GITHUB_TOKEN",
                    "kind": "secret",
                    "label": "GitHub token",
                    "required": True,
                    "input_type": "password",
                },
                {
                    "name": "DEFAULT_REPO",
                    "kind": "config",
                    "label": "Default repo",
                    "required": False,
                    "input_type": "text",
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_consumer_setup_resolves_user_over_org_and_decrypts(
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
            org = Organization(slug="acme", name="Acme", created_by_id=user.id)
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
            agent = Agent(
                owner_id=user.id,
                name="github-agent",
                description="",
                version="0.1.0",
                image="example/agent",
                public=True,
                status="running",
                card=_card(),
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

            with pytest.raises(ConsumerSetupRequired):
                await require_consumer_setup(agent=agent, user=user, session=session)

            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"GITHUB_TOKEN": "org-token", "DEFAULT_REPO": "acme/api"},
                scope="org",
                organization_slug="acme",
            )
            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"GITHUB_TOKEN": "user-token"},
                scope="user",
            )

            resolved = await resolve_consumer_setup(
                agent=agent,
                user=user,
                session=session,
                organization_slug="acme",
            )

            assert resolved.complete is True
            assert resolved.consumer_secrets["GITHUB_TOKEN"] == "user-token"
            assert resolved.consumer_config["DEFAULT_REPO"] == "acme/api"
            assert {value["name"]: value["source"] for value in resolved.values} == {
                "GITHUB_TOKEN": "user",
                "DEFAULT_REPO": "org",
            }
            assert resolved.payload()["values"][0]["value_redacted"] != "user-token"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_installed_agents_lists_redacted_setup_and_auth(
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
            org = Organization(slug="acme", name="Acme", created_by_id=user.id)
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
            setup_agent = Agent(
                owner_id=user.id,
                name="github-agent",
                description="",
                version="0.1.0",
                image="example/agent",
                public=True,
                status="running",
                card=_card(),
            )
            imported_agent = Agent(
                owner_id=user.id,
                name="imported-api",
                description="",
                version="0.1.0",
                image="external/a2a",
                public=False,
                status="running",
                url="https://api.example.test/a2a",
                card={
                    "name": "imported-api",
                    "description": "",
                    "version": "0.1.0",
                    "skills": [],
                    "securitySchemes": {
                        "apiKey": {
                            "type": "apiKey",
                            "in": "header",
                            "name": "X-API-Key",
                        }
                    },
                },
            )
            session.add_all([setup_agent, imported_agent])
            await session.commit()
            await session.refresh(setup_agent)
            await session.refresh(imported_agent)

            await upsert_consumer_setup_values(
                agent=setup_agent,
                user=user,
                session=session,
                values={"GITHUB_TOKEN": "org-token", "DEFAULT_REPO": "acme/api"},
                scope="org",
                organization_slug="acme",
            )
            await upsert_consumer_setup_values(
                agent=setup_agent,
                user=user,
                session=session,
                values={"GITHUB_TOKEN": "user-token"},
                scope="user",
            )
            await upsert_connection(
                session,
                agent=imported_agent,
                user=user,
                scheme_name="apiKey",
                scheme_type="api_key",
                credential_scope="agent",
                secret_payload={
                    "value": "imported-secret-key",
                    "location": "header",
                    "name": "X-API-Key",
                },
                metadata={
                    "scheme_name": "apiKey",
                    "scheme_type": "api_key",
                    "location": "header",
                    "name": "X-API-Key",
                    "value": "imported-secret-key",
                },
            )
            await session.commit()

            installed = await list_installed_agents(user=user, session=session)

            by_name = {item["agent"]["name"]: item for item in installed}
            assert set(by_name) == {"github-agent", "imported-api"}
            setup_values = {
                (value["scope"], value["name"]): value
                for value in by_name["github-agent"]["setup_values"]
            }
            assert setup_values[("user", "GITHUB_TOKEN")]["value_redacted"] != "user-token"
            assert setup_values[("org", "DEFAULT_REPO")]["organization"]["slug"] == "acme"
            assert by_name["github-agent"]["missing_required"] == []
            assert by_name["imported-api"]["auth_connections"][0]["scheme_name"] == "apiKey"
            assert by_name["imported-api"]["auth_connections"][0]["metadata"] == {
                "scheme_name": "apiKey",
                "scheme_type": "api_key",
                "location": "header",
                "name": "X-API-Key",
            }
            serialized = repr(installed)
            assert "user-token" not in serialized
            assert "org-token" not in serialized
            assert "imported-secret-key" not in serialized
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_install_agent_lists_agent_without_setup_values() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            agent = Agent(
                owner_id=user.id,
                name="plain-agent",
                description="No setup required",
                version="0.1.0",
                image="example/plain-agent",
                public=True,
                status="running",
                card={
                    "name": "plain-agent",
                    "description": "No setup required",
                    "version": "0.1.0",
                    "skills": [],
                    "consumer_setup": {"fields": []},
                },
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

            installed = await install_agent(agent=agent, user=user, session=session)

            assert installed["agent"]["name"] == "plain-agent"
            assert installed["setup_values"] == []
            assert installed["auth_connections"] == []
            assert installed["missing_required"] == []
            by_name = {
                item["agent"]["name"]: item
                for item in await list_installed_agents(user=user, session=session)
            }
            assert set(by_name) == {"plain-agent"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consumer_setup_invocation_endpoint_returns_decrypted_payload(
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
            agent = Agent(
                owner_id=user.id,
                name="github-agent",
                description="",
                version="0.1.0",
                image="example/agent",
                public=True,
                status="running",
                card=_card(),
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"GITHUB_TOKEN": "user-token", "DEFAULT_REPO": "acme/api"},
                scope="user",
            )

            async def fail_full_resolve(**_kwargs: object) -> object:
                raise AssertionError("invocation success should not build setup status")

            monkeypatch.setattr(
                "control_plane.consumer_setup.resolve_consumer_setup",
                fail_full_resolve,
            )

            payload = await get_consumer_setup_invocation(
                "github-agent",
                organization_slug=None,
                user=user,
                session=session,
            )

            assert payload.consumer_config == {"DEFAULT_REPO": "acme/api"}
            assert payload.consumer_secrets == {"GITHUB_TOKEN": "user-token"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consumer_setup_invocation_setup_required_detail_is_json_serializable(
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
            agent = Agent(
                owner_id=user.id,
                name="github-agent",
                description="",
                version="0.1.0",
                image="example/agent",
                public=True,
                status="running",
                card=_card(),
            )
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"DEFAULT_REPO": "acme/api"},
                scope="user",
            )

            with pytest.raises(HTTPException) as exc:
                await get_consumer_setup_invocation(
                    "github-agent",
                    organization_slug=None,
                    user=user,
                    session=session,
                )

            assert exc.value.status_code == 409
            assert exc.value.detail["error"] == "agent_setup_required"
            assert exc.value.detail["missing_required"] == ["GITHUB_TOKEN"]
            json.dumps(exc.value.detail)
    finally:
        await engine.dispose()
