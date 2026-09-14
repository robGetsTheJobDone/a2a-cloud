from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ["A2A_CP_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.receipts import verify_receipt
from control_plane.auth import issue_token
from control_plane.consumer_setup import upsert_consumer_setup_values
from control_plane.db import Base
from control_plane.models import Agent, AgentApiToken, AgentReceipt, GrantAudit, User
from control_plane.routes import agents


class _Request:
    def __init__(self, body: dict):
        self._body = body
        self.headers = {
            "host": "internal.svc.cluster.local",
            "x-forwarded-host": "app.example.com",
            "x-forwarded-proto": "https",
        }

    async def json(self) -> dict:
        return self._body


class _Upload:
    def __init__(self, filename: str, data: bytes, content_type: str) -> None:
        self.filename = filename
        self._data = data
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._data


class _Form:
    def __init__(self, items: list[tuple[str, object]]) -> None:
        self._items = items

    def multi_items(self) -> list[tuple[str, object]]:
        return list(self._items)


class _MultipartRequest:
    headers = {"content-type": "multipart/form-data; boundary=x"}

    def __init__(self, items: list[tuple[str, object]]) -> None:
        self._items = items

    async def form(self) -> _Form:
        return _Form(self._items)


class _EndpointRequest:
    def __init__(
        self,
        body: dict,
        *,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self._body = body
        self.method = method
        self.headers = {
            "host": "internal.svc.cluster.local",
            "x-forwarded-host": "app.example.com",
            "x-forwarded-proto": "https",
            "content-type": "application/json",
            **(headers or {}),
        }
        self.query_params = query_params or {}

    async def body(self) -> bytes:
        return json.dumps(self._body).encode("utf-8")


def test_runtime_cp_jwt_projection_requires_agent_opt_in(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        agents.secret_store,
        "upsert_agent_secret_value",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        agents,
        "issue_runtime_cp_credential",
        lambda user_id, *, agent, ttl_seconds=None: f"token-{user_id}-{agent}-{ttl_seconds}",
    )

    agent = SimpleNamespace(name="growth-signal-agent")
    user = SimpleNamespace(id=42)
    agents._provision_runtime_cp_jwt_if_requested(
        dsl=SimpleNamespace(runtime=SimpleNamespace(wants_cp_jwt=False)),
        agent=agent,
        user=user,
    )
    assert calls == []

    agents._provision_runtime_cp_jwt_if_requested(
        dsl=SimpleNamespace(runtime=SimpleNamespace(wants_cp_jwt=True)),
        agent=agent,
        user=user,
    )

    assert calls == [
        {
            "agent_name": "growth-signal-agent",
            "key": "A2A_CP_JWT",
            "value": (
                f"token-42-growth-signal-agent-{agents._RUNTIME_CP_JWT_TTL_SECONDS}"
            ),
            "owner_id": 42,
        }
    ]


def _receipt_keys() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii"), base64.b64encode(public_raw).decode("ascii")


def _set_receipt_keys(monkeypatch) -> None:
    signing_key, verifying_key = _receipt_keys()
    monkeypatch.setenv("A2A_RECEIPT_SIGNING_KEY", signing_key)
    monkeypatch.setenv("A2A_RECEIPT_VERIFYING_KEY", verifying_key)


async def _seed_agent(session, *, public: bool = True) -> tuple[User, Agent]:
    user = User(email="owner@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name="reporter",
        description="Build reports",
        version="1.2.3",
        image="registry.example/reporter:latest",
        public=public,
        status="running",
        url="https://reporter.example.com",
        card={
            "description": "Build reports",
            "runtime": {"llm_provisioning": "caller_provided"},
            "skills": [
                {
                    "name": "build_report",
                    "description": "Build a report.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"topic": {"type": "string"}},
                        "required": ["topic"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                    },
                }
            ],
        },
    )
    session.add(agent)
    await session.commit()
    await session.refresh(user)
    await session.refresh(agent)
    return user, agent


@pytest.mark.asyncio
async def test_agent_api_token_create_list_revoke() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)

            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="CI token"),
                user=user,
                session=session,
            )

            assert created.token.startswith("a2a_app_")
            assert created.token_last4 == created.token[-4:]
            listed = await agents.list_agent_api_tokens(
                "reporter",
                user=user,
                session=session,
            )
            assert [(item.name, item.enabled) for item in listed] == [("CI token", True)]

            await agents.revoke_agent_api_token(
                "reporter",
                created.id,
                user=user,
                session=session,
            )
            row = (
                await session.execute(select(AgentApiToken).where(AgentApiToken.id == created.id))
            ).scalar_one()
            assert row.enabled is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_integration_link_create_list_revoke_for_installed_agent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner, _agent = await _seed_agent(session)
            caller = User(email="caller@example.com", password_hash="x")
            session.add(caller)
            await session.commit()
            await session.refresh(caller)

            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=caller,
                session=session,
            )

            assert created.token.startswith("a2a_app_")
            assert created.scopes == ["invoke", "mcp"]
            # The secret must never be baked into the URLs by default.
            assert created.token_placement == "header"
            assert created.security_notice is None
            for url in (
                created.openapi_url,
                created.invoke_base_url,
                created.sample_invoke_url,
                created.mcp_url,
            ):
                assert "integration_token=" not in url
                assert created.token not in url
            # The documented way to authenticate is the header form.
            assert f"Authorization: Bearer {created.token}" in created.curl_example
            assert "integration_token=" not in created.curl_example
            # A credential the platform hands out must expire.
            assert created.expires_at is not None

            listed = await agents.list_agent_integration_links(
                "reporter",
                _Request({}),
                user=caller,
                session=session,
            )
            assert [(item.id, item.enabled) for item in listed] == [(created.id, True)]

            owner_links = await agents.list_agent_integration_links(
                "reporter",
                _Request({}),
                user=owner,
                session=session,
            )
            assert owner_links == []

            await agents.revoke_agent_integration_link(
                "reporter",
                created.id,
                user=caller,
                session=session,
            )
            row = (
                await session.execute(select(AgentApiToken).where(AgentApiToken.id == created.id))
            ).scalar_one()
            assert row.enabled is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_integration_link_url_token_is_opt_in_and_labelled() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)

            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                agents.AgentIntegrationLinkCreateIn(url_token=True),
                user=user,
                session=session,
            )

            assert created.token_placement == "url_query"
            assert "lower security" in (created.security_notice or "").lower()
            assert created.mcp_url.endswith(f"integration_token={created.token}")
            assert f"integration_token={created.token}" in created.openapi_url
            # Even the opt-in variant documents the header form.
            assert f"Authorization: Bearer {created.token}" in created.curl_example
            # ... and it is shorter lived than the header-only default.
            assert created.expires_at is not None
            url_ttl = agents._as_utc(created.expires_at) - datetime.now(timezone.utc)
            assert (
                url_ttl.days < agents._AGENT_INTEGRATION_LINK_TTL_DAYS
            ), url_ttl
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_integration_link_expired_token_is_rejected() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )

            row = (
                await session.execute(
                    select(AgentApiToken).where(AgentApiToken.id == created.id)
                )
            ).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()

            with pytest.raises(HTTPException) as excinfo:
                await agents._agent_api_token_agent(
                    name="reporter",
                    authorization=f"Bearer {created.token}",
                    session=session,
                )
            assert excinfo.value.status_code == 401
            assert "expired" in str(excinfo.value.detail)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_integration_link_revoked_token_is_rejected() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )

            await agents.revoke_agent_integration_link(
                "reporter",
                created.id,
                user=user,
                session=session,
            )

            with pytest.raises(HTTPException) as excinfo:
                await agents._agent_api_token_agent(
                    name="reporter",
                    authorization=f"Bearer {created.token}",
                    session=session,
                )
            assert excinfo.value.status_code == 401
            assert "disabled" in str(excinfo.value.detail)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_integration_link_without_expiry_gets_a_migration_window() -> None:
    """Pre-existing never-expiring links keep working but stop being immortal."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            raw_token = agents._new_agent_api_token()
            legacy = AgentApiToken(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                name=f"{agent.name} integration link",
                token_hash=agents._hash_agent_api_token(raw_token),
                token_last4=raw_token[-4:],
                scopes=["invoke", "mcp"],
                enabled=True,
                expires_at=None,
            )
            session.add(legacy)
            await session.commit()

            listed = await agents.list_agent_integration_links(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )
            assert len(listed) == 1
            assert listed[0].expires_at is not None

            # Still usable right now - the migration is a window, not a revoke.
            api_token, _agent, _user = await agents._agent_api_token_agent(
                name="reporter",
                authorization=f"Bearer {raw_token}",
                session=session,
            )
            assert api_token.id == legacy.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_openapi_accepts_header_bearer_for_private_agent() -> None:
    """The header form has to work, or users are pushed back to URL tokens."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session, public=False)
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )

            spec = await agents.agent_api_openapi(
                "reporter",
                _Request({}),
                authorization=f"Bearer {created.token}",
                integration_token=None,
                user=None,
                session=session,
            )
            operation = spec["paths"]["/invoke/build_report"]["post"]
            # Header auth: no token echoed into the document as a query default.
            assert operation["security"] == [{"AgentApiToken": []}]
            assert operation["parameters"] == []
            assert created.token not in json.dumps(spec)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_scoped_api_token_keeps_its_deliberate_null_expiry() -> None:
    """The legacy backfill must only reach links this route minted.

    A plain API token created through ``POST /{name}/api-tokens`` with an
    ``mcp`` scope also has "mcp" in ``scopes``; giving it a lifetime its owner
    explicitly declined would 401 it 90 days later.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)

            plain = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(
                    name="CI token, no expiry on purpose",
                    scopes=["invoke", "mcp"],
                    expires_at=None,
                ),
                user=user,
                session=session,
            )
            assert plain.expires_at is None

            api_token, _agent2, _user = await agents._agent_api_token_agent(
                name="reporter",
                authorization=f"Bearer {plain.token}",
                required_scope="mcp",
                session=session,
            )
            await session.commit()
            assert api_token.id == plain.id
            row = (
                await session.execute(
                    select(AgentApiToken).where(AgentApiToken.id == plain.id)
                )
            ).scalar_one()
            assert row.expires_at is None

            # ... and it is not silently backfilled by a list either.
            listed = await agents.list_agent_integration_links(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )
            assert [item.expires_at for item in listed if item.id == plain.id] == [None]

            # The predicate that gates the backfill says so directly.
            assert agents._is_integration_link(row) is True
            assert agents._is_minted_integration_link(row) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_openapi_public_agent_survives_a_foreign_app_token() -> None:
    """Accepting an app bearer here must not make a public read stricter.

    An MCP host or OpenAPI tool runner configured with one agent's integration
    token as a default ``Authorization`` header, pointed at another, *public*
    agent's spec, read it fine before the header form was accepted here.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session, public=False)
            public = Agent(
                owner_id=user.id,
                name="publicbot",
                description="Public",
                version="1.0.0",
                image="registry.example/publicbot:latest",
                public=True,
                status="running",
                url="https://publicbot.example.com",
                card=dict(agent.card),
            )
            session.add(public)
            await session.commit()

            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )

            async def read(name: str, authorization: str | None) -> dict:
                return await agents.agent_api_openapi(
                    name,
                    _Request({}),
                    authorization=authorization,
                    integration_token=None,
                    user=None,
                    session=session,
                )

            anonymous = await read("publicbot", None)
            foreign = await read("publicbot", f"Bearer {created.token}")
            bogus = await read("publicbot", "Bearer a2a_app_totallybogus")
            assert anonymous == foreign == bogus
            assert created.token not in json.dumps(foreign)

            # The token's own agent still resolves through the header.
            assert (await read("reporter", f"Bearer {created.token}"))["info"][
                "title"
            ] == "reporter API"

            # ... and where the anonymous path would deny anyway, the caller
            # still gets the specific token reason rather than a bare 404/403.
            public_link = await agents.create_agent_integration_link(
                "publicbot",
                _Request({}),
                user=user,
                session=session,
            )
            with pytest.raises(HTTPException) as excinfo:
                await read("reporter", f"Bearer {public_link.token}")
            assert excinfo.value.status_code == 403
            assert "not scoped" in str(excinfo.value.detail)

            row = (
                await session.execute(
                    select(AgentApiToken).where(AgentApiToken.id == created.id)
                )
            ).scalar_one()
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await session.commit()
            with pytest.raises(HTTPException) as expired:
                await read("reporter", f"Bearer {created.token}")
            assert expired.value.status_code == 401
            assert "expired" in str(expired.value.detail)
            # The same expired token is still just ignored on a public agent.
            assert await read("publicbot", f"Bearer {created.token}") == anonymous
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_integration_link_list_makes_no_token_placement_claim() -> None:
    """Placement is not persisted, so only the create response may report it."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                agents.AgentIntegrationLinkCreateIn(url_token=True),
                user=user,
                session=session,
            )
            assert created.token_placement == "url_query"

            listed = await agents.list_agent_integration_links(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )
            assert len(listed) == 1
            payload = listed[0].model_dump()
            for field in ("token_placement", "auth_header", "security_notice"):
                assert field not in payload, field
            # A listed link has no token to embed, so its URLs are the clean ones.
            for url in (
                payload["openapi_url"],
                payload["invoke_base_url"],
                payload["mcp_url"],
            ):
                assert "integration_token=" not in url
                assert created.token not in url
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_endpoint_proxy_adapts_raw_endpoint_and_forwards_cp_jwt(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            agent.card = {
                **agent.card,
                "runtime": {
                    **(agent.card.get("runtime") or {}),
                    "wants_cp_jwt": True,
                    "endpoints": [
                        {
                            "name": "telegram",
                            "path": "/telegram/webhook",
                            "methods": ["POST"],
                            "skill": "telegram_webhook",
                            "body_arg": "update",
                            "headers_arg": "headers",
                            "query_arg": "query",
                        }
                    ],
                },
                "skills": [
                    *(agent.card.get("skills") or []),
                    {
                        "name": "telegram_webhook",
                        "description": "Handle Telegram webhook.",
                        "input_schema": {"type": "object"},
                    },
                ],
            }
            await session.commit()
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )
            seen: dict[str, object] = {}

            async def fake_post_hosted_agent_invoke(
                *,
                agent,
                skill_name,
                body,
                authorization,
                grant_id,
            ):
                seen["agent"] = agent.name
                seen["skill_name"] = skill_name
                seen["body"] = body
                seen["authorization"] = authorization
                seen["grant_id"] = grant_id
                return {
                    "result": {"ok": True, "reply": "pong"},
                    "events": [],
                    "artifacts": [],
                    "grant_id": "grant-1",
                }

            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )
            monkeypatch.setattr(agents.settings, "public_cp_url", "https://api.example.test")

            response = await agents.invoke_agent_api_endpoint(
                "reporter",
                "telegram",
                _EndpointRequest(
                    {"message": {"text": "/probe"}},
                    headers={
                        "authorization": "Bearer should-not-forward",
                        "x-telegram-bot-api-secret-token": "secret",
                    },
                    query_params={
                        "delivery": "test",
                        "integration_token": created.token,
                    },
                ),
                integration_token=created.token,
                session=session,
            )

            body = seen["body"]
            assert response == {"ok": True, "reply": "pong"}
            assert isinstance(body, dict)
            assert body["arguments"] == {
                "update": {"message": {"text": "/probe"}},
                "headers": {
                    "host": "internal.svc.cluster.local",
                    "x-forwarded-host": "app.example.com",
                    "x-forwarded-proto": "https",
                    "content-type": "application/json",
                    "x-telegram-bot-api-secret-token": "secret",
                },
                "query": {"delivery": "test"},
            }
            assert body["cp_jwt"]
            assert body["cp_url"] == "https://api.example.test"
            assert seen["skill_name"] == "telegram_webhook"
            assert seen["authorization"] != "Bearer should-not-forward"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_openapi_uses_skill_schemas() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            _user, _agent = await _seed_agent(session)

            spec = await agents.agent_api_openapi(
                "reporter",
                _Request({}),
                user=None,
                session=session,
            )

            assert spec["servers"] == [
                {"url": "https://app.example.com/v1/agents/reporter/api"}
            ]
            path = spec["paths"]["/invoke/build_report"]["post"]
            assert path["security"] == [{"AgentApiToken": []}]
            assert path["requestBody"]["content"]["application/json"]["schema"]["required"] == [
                "topic"
            ]
            assert "file_outputs" in path["responses"]["200"]["content"]["application/json"][
                "schema"
            ]["properties"]
            async_path = spec["paths"]["/runs/build_report/start"]["post"]
            assert async_path["security"] == [{"AgentApiToken": []}]
            assert async_path["responses"]["202"]["description"] == "Async agent run accepted."
            assert "/runs/{run_id}" in spec["paths"]
            file_path = spec["paths"]["/files/{path}"]["get"]
            assert file_path["security"] == [
                {"AgentApiToken": []},
                {"ControlPlaneBearer": []},
                {"ControlPlaneSession": []},
            ]
            assert file_path["responses"]["200"]["content"][
                "application/octet-stream"
            ]["schema"] == {"type": "string", "format": "binary"}
            assert "ControlPlaneBearer" in spec["components"]["securitySchemes"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_openapi_renders_file_upload_as_multipart() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            _user, agent = await _seed_agent(session)
            agent.card["skills"][0]["input_schema"] = {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "object",
                        "x-a2a-file-upload": {
                            "required_upload": True,
                            "accept": ["application/pdf"],
                            "max_bytes": 1000,
                        },
                    },
                    "question": {"type": "string"},
                },
                "required": ["document", "question"],
                "additionalProperties": False,
            }
            await session.commit()

            spec = await agents.agent_api_openapi(
                "reporter",
                _Request({}),
                user=None,
                session=session,
            )

            content = spec["paths"]["/invoke/build_report"]["post"]["requestBody"]["content"]
            assert "application/json" not in content
            multipart = content["multipart/form-data"]
            props = multipart["schema"]["properties"]
            assert props["document"] == {"type": "string", "format": "binary"}
            assert props["question"] == {"type": "string"}
            assert multipart["schema"]["required"] == ["document", "question"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_multipart_agent_api_request_stages_upload(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    uploads: list[tuple[str, str, bytes, str | None]] = []

    def fake_upload_file(bucket: str, path: str, data: bytes, content_type: str | None):
        uploads.append((bucket, path, data, content_type))
        return {"path": path, "size": len(data), "content_type": content_type}

    monkeypatch.setattr(agents, "upload_file", fake_upload_file)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            agent.card["skills"][0]["input_schema"] = {
                "type": "object",
                "properties": {
                    "document": {
                        "type": "object",
                        "x-a2a-file-upload": {
                            "required_upload": True,
                            "accept": ["application/pdf"],
                            "max_bytes": 1000,
                        },
                    },
                    "question": {"type": "string"},
                },
                "required": ["document", "question"],
            }

            request = _MultipartRequest(
                [
                    ("document", _Upload("../report.pdf", b"%PDF", "application/pdf")),
                    ("question", "summarize"),
                ]
            )
            args = await agents._invoke_arguments_from_request(
                request,
                agent=agent,
                skill_name="build_report",
                user=user,
            )

            assert args["question"] == "summarize"
            assert args["document"]["filename"] == "report.pdf"
            assert args["document"]["media_type"] == "application/pdf"
            assert args["document"]["size_bytes"] == 4
            assert uploads == [
                (
                    f"user-{user.id}-files",
                    args["document"]["path"],
                    b"%PDF",
                    "application/pdf",
                )
            ]
            assert args["document"]["path"].startswith("inputs/api/reporter/")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_invoke_accepts_integration_token_query(monkeypatch) -> None:
    _set_receipt_keys(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_integration_link(
                "reporter",
                _Request({}),
                user=user,
                session=session,
            )

            async def fake_invoke(**kwargs):
                assert kwargs["arguments"] == {"topic": "sales"}
                return (
                    {
                        "result": {"summary": "ok"},
                        "events": [],
                        "artifacts": [{"name": "report.csv"}],
                        "grant_id": "grant-link",
                    },
                    "outputs/api/reporter/test/",
                )

            monkeypatch.setattr(agents, "_invoke_agent_api_skill", fake_invoke)

            out = await agents.invoke_agent_api(
                "reporter",
                "build_report",
                _Request({"topic": "sales"}),
                authorization=None,
                integration_token=created.token,
                session=session,
            )

            assert out["result"] == {"summary": "ok"}
            assert out["grant_id"] == "grant-link"
            assert out["file_outputs"][0]["download_url"].endswith(
                f"report.csv?integration_token={created.token}"
            )
            receipt_row = (await session.execute(select(AgentReceipt))).scalar_one()
            receipt = verify_receipt(receipt_row.signed_token)
            assert receipt.agent_name == "reporter"
            assert receipt.skill_name == "build_report"
            assert receipt.status == "ok"
            assert receipt.caller == f"user:{user.id}"
            assert receipt.input_hash
            assert receipt_row.payload["result_preview"]

            spec = await agents.agent_api_openapi(
                "reporter",
                _Request({}),
                authorization=None,
                integration_token=created.token,
                user=None,
                session=session,
            )
            operation = spec["paths"]["/invoke/build_report"]["post"]
            assert operation["security"] == []
            assert operation["parameters"] == [
                {
                    "name": "integration_token",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "default": created.token},
                    "description": "Pre-authorized integration link token.",
                }
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_invoke_mints_output_grant_and_normalizes_files(monkeypatch) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            agent.card = {
                **agent.card,
                "consumer_setup": {
                    "fields": [
                        {
                            "name": "APIFY_API_KEY",
                            "kind": "secret",
                            "label": "Apify API key",
                            "required": True,
                            "input_type": "password",
                        },
                        {
                            "name": "APIFY_REGION",
                            "kind": "config",
                            "label": "Apify region",
                            "required": False,
                            "input_type": "text",
                        },
                    ]
                },
            }
            await session.commit()
            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"APIFY_API_KEY": "apify-token", "APIFY_REGION": "us-east"},
                scope="user",
            )
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="call token"),
                user=user,
                session=session,
            )
            seen: dict[str, object] = {}

            async def fake_post_hosted_agent_invoke(
                *,
                agent,
                skill_name,
                body,
                authorization,
                grant_id,
            ):
                seen["agent"] = agent.name
                seen["skill_name"] = skill_name
                seen["body"] = body
                seen["authorization"] = authorization
                seen["grant_id"] = grant_id
                return {
                    "result": {"summary": "done"},
                    "events": [],
                    "artifacts": [{"name": "report.csv", "size_bytes": 12}],
                    "grant_id": "grant-1",
                }

            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )
            monkeypatch.setattr(agents.settings, "public_cp_url", "https://api.example.test")

            response = await agents.invoke_agent_api(
                "reporter",
                "build_report",
                _Request({"topic": "sales"}),
                authorization=f"Bearer {created.token}",
                session=session,
            )

            body = seen["body"]
            assert isinstance(body, dict)
            assert body["arguments"] == {"topic": "sales"}
            assert body["grant"].count(".") == 1
            assert body["consumer_secrets"] == {"APIFY_API_KEY": "apify-token"}
            assert body["consumer_config"] == {"APIFY_REGION": "us-east"}
            audit = (await session.execute(select(GrantAudit))).scalar_one()
            assert audit.decision == "auto_approve"
            assert audit.decided_by == "api_token"
            assert audit.audience == "reporter"
            assert response["result"] == {"summary": "done"}
            assert response["file_outputs"] == [
                {
                    "name": "report.csv",
                    "path": response["file_outputs"][0]["path"],
                    "download_url": response["file_outputs"][0]["download_url"],
                    "source": "artifact",
                    "size_bytes": 12,
                }
            ]
            assert response["file_outputs"][0]["path"].endswith(
                "/http-build_report/report.csv"
            )
            assert response["file_outputs"][0]["download_url"].startswith(
                "https://app.example.com/v1/agents/reporter/api/files/outputs/api/reporter/"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_builder_api_grant_includes_target_workspace_prefix(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=7, email="builder@example.com", password_hash="x")
            agent = Agent(
                owner_id=user.id,
                name="agent-builder",
                description="",
                version="0.1.0",
                image="registry.example/agent-builder:latest",
                public=True,
                status="running",
                url="https://agent-builder.example.com",
                card={"skills": [{"name": "build"}]},
            )
            session.add_all([user, agent])
            await session.commit()
            seen: dict[str, object] = {}

            def fake_mint_grant_token(**kwargs):
                seen["write_prefixes"] = kwargs["write_prefixes"]
                seen["source_grants"] = kwargs["source_grants"]
                payload = {
                    "grant_id": "grant-builder",
                    "issuer": kwargs["issuer"],
                    "audience": kwargs["audience"],
                    "bucket": kwargs["bucket"],
                    "mode": kwargs["mode"],
                    "allow_patterns": list(kwargs["allow_patterns"]),
                    "deny_patterns": [],
                    "outputs_prefix": kwargs["outputs_prefix"],
                    "write_prefixes": list(kwargs["write_prefixes"]),
                    "source_grants": list(kwargs["source_grants"]),
                    "expires_at": 9999999999,
                }
                return "grant.body", payload

            async def fake_post_hosted_agent_invoke(**kwargs):
                seen["body"] = kwargs["body"]
                return {"result": {"ok": True}, "events": [], "artifacts": []}

            monkeypatch.setattr(agents, "mint_grant_token", fake_mint_grant_token)
            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )

            await agents._call_hosted_agent_api_skill(
                agent=agent,
                user=user,
                session=session,
                skill_name="build",
                arguments={"name": "new-echo-agent", "prompt": "make an echo agent"},
            )

            assert "agents/new-echo-agent/" in seen["write_prefixes"]
            assert seen["source_grants"] == (
                {"agent": "new-echo-agent", "scope": "write"},
            )
            assert "outputs/api/agent-builder/" in seen["write_prefixes"][0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_grant_includes_skill_declared_workspace_prefixes(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=8, email="studio@example.com", password_hash="x")
            agent = Agent(
                owner_id=user.id,
                name="agent-studio",
                description="",
                version="0.1.0",
                image="registry.example/agent-studio:latest",
                public=False,
                status="running",
                url="https://agent-studio.example.com",
                card={
                    "skills": [
                        {
                            "name": "create_agent",
                            "policy": {
                                "grant_outputs_prefix": "agents/{name}/.agent-studio/",
                                "grant_write_prefixes": ["agents/{name}/"],
                            },
                        }
                    ]
                },
            )
            session.add_all([user, agent])
            await session.commit()
            seen: dict[str, object] = {}

            def fake_mint_grant_token(**kwargs):
                seen["write_prefixes"] = kwargs["write_prefixes"]
                seen["source_grants"] = kwargs["source_grants"]
                payload = {
                    "grant_id": "grant-studio",
                    "issuer": kwargs["issuer"],
                    "audience": kwargs["audience"],
                    "bucket": kwargs["bucket"],
                    "mode": kwargs["mode"],
                    "allow_patterns": list(kwargs["allow_patterns"]),
                    "deny_patterns": [],
                    "outputs_prefix": kwargs["outputs_prefix"],
                    "write_prefixes": list(kwargs["write_prefixes"]),
                    "source_grants": list(kwargs["source_grants"]),
                    "expires_at": 9999999999,
                }
                return "grant.body", payload

            async def fake_post_hosted_agent_invoke(**kwargs):
                seen["body"] = kwargs["body"]
                return {"result": {"ok": True}, "events": [], "artifacts": []}

            monkeypatch.setattr(agents, "mint_grant_token", fake_mint_grant_token)
            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )

            await agents._call_hosted_agent_api_skill(
                agent=agent,
                user=user,
                session=session,
                skill_name="create_agent",
                arguments={"name": "release-readiness-smoke", "goal": "ship safely"},
            )

            assert "outputs/api/agent-studio/" in seen["write_prefixes"][0]
            assert "agents/release-readiness-smoke/.agent-studio/" in seen["write_prefixes"]
            assert "agents/release-readiness-smoke/" in seen["write_prefixes"]
            assert seen["source_grants"] == (
                {"agent": "release-readiness-smoke", "scope": "write"},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_platform_llm_agent_receives_llm_creds(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=9, email="platform@example.com", password_hash="x")
            agent = Agent(
                owner_id=user.id,
                name="code-editor-agent",
                description="",
                version="0.1.0",
                image="registry.example/code-editor-agent:latest",
                public=False,
                status="running",
                url="https://code-editor-agent.example.com",
                card={
                    "runtime": {"llm_provisioning": "platform_or_caller_provided"},
                    "skills": [{"name": "turn"}],
                },
            )
            session.add_all([user, agent])
            await session.commit()

            seen: dict[str, object] = {}

            def fake_mint_grant_token(**kwargs):
                seen["grant_kwargs"] = kwargs
                payload = {
                    "grant_id": "grant-platform",
                    "issuer": kwargs["issuer"],
                    "audience": kwargs["audience"],
                    "bucket": kwargs["bucket"],
                    "mode": kwargs["mode"],
                    "allow_patterns": list(kwargs["allow_patterns"]),
                    "deny_patterns": [],
                    "outputs_prefix": kwargs["outputs_prefix"],
                    "write_prefixes": list(kwargs["write_prefixes"]),
                    "source_grants": list(kwargs["source_grants"]),
                    "expires_at": 9999999999,
                }
                return "grant.token", payload

            async def fake_post_hosted_agent_invoke(**kwargs):
                seen["body"] = kwargs["body"]
                return {"result": {"ok": True}, "events": [], "artifacts": []}

            monkeypatch.setattr(agents, "mint_grant_token", fake_mint_grant_token)
            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )
            monkeypatch.setattr(
                agents,
                "_load_platform_llm_settings",
                lambda: SimpleNamespace(
                    litellm_url="http://litellm.test",
                    platform_llm_models=("gpt-platform",),
                    platform_llm_max_budget_usd=2.5,
                    platform_llm_rpm_limit=30,
                    platform_llm_tpm_limit=50000,
                ),
            )

            await agents._call_hosted_agent_api_skill(
                agent=agent,
                user=user,
                session=session,
                skill_name="turn",
                arguments={"repo": "control-plane", "goal": "make it faster"},
            )

            grant_kwargs = seen["grant_kwargs"]
            assert isinstance(grant_kwargs, dict)
            assert grant_kwargs["llm_models"] == ("gpt-platform",)
            assert grant_kwargs["llm_max_budget_usd"] == 2.5
            assert grant_kwargs["llm_rpm_limit"] == 30
            assert grant_kwargs["llm_tpm_limit"] == 50000

            body = seen["body"]
            assert isinstance(body, dict)
            assert body["llm_creds"] == {
                "base_url": "http://litellm.test/v1",
                "api_key": "grant.token",
                "model": "gpt-platform",
                "temperature_mode": "omit",
                "extra_body": {},
                "metadata": {
                    "a2a_user_id": 9,
                    "a2a_user_email": "platform@example.com",
                    "a2a_grant_id": "grant-platform",
                    "a2a_agent_name": "code-editor-agent",
                    "a2a_skill_name": "turn",
                    "a2a_llm_source": "agent_api",
                },
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_exhausted_account_trial_returns_byok_action(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            agent.card = {
                **agent.card,
                "runtime": {
                    "llm_provisioning": "platform",
                    "account_access": {
                        "required": True,
                        "platform_skill_calls": 0,
                        "after_trial": "byok",
                    },
                },
            }
            await session.commit()
            monkeypatch.setattr(
                agents,
                "_load_platform_llm_settings",
                lambda: SimpleNamespace(
                    litellm_url="http://litellm.test",
                    platform_llm_models=("gpt-platform",),
                    platform_llm_max_budget_usd=1.0,
                    platform_llm_rpm_limit=60,
                    platform_llm_tpm_limit=200000,
                ),
            )

            with pytest.raises(Exception) as exc:
                await agents._invoke_agent_api_skill(
                    agent=agent,
                    user=user,
                    skill_name="build_report",
                    arguments={"topic": "sales"},
                    session=session,
                )

            assert getattr(exc.value, "status_code", None) == 402
            detail = getattr(exc.value, "detail", {})
            assert detail["error"] == "llm_credentials_required"
            assert detail["reason"] == "platform_trial_exhausted"
            assert detail["platform_skill_calls_remaining"] == 0
            assert detail["setup_url"].endswith("/llm-keys")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_async_run_creates_job_and_polls_result(monkeypatch) -> None:
    _set_receipt_keys(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="async token"),
                user=user,
                session=session,
            )

            async def fake_post_hosted_agent_invoke(
                *,
                agent,
                skill_name,
                body,
                authorization,
                grant_id,
            ):
                return {
                    "result": {"summary": f"{skill_name}: {body['arguments']['topic']}"},
                    "events": [],
                    "artifacts": [{"name": "async-report.md", "size_bytes": 42}],
                    "grant_id": "grant-async",
                }

            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fake_post_hosted_agent_invoke,
            )
            monkeypatch.setattr(agents.settings, "public_cp_url", "https://api.example.test")

            run = await agents.start_agent_api_run(
                "reporter",
                "build_report",
                _Request({"topic": "sales"}),
                authorization=f"Bearer {created.token}",
                session=session,
            )

            assert run["status"] == "queued"
            assert run["run_id"].startswith("api-")
            assert run["poll_url"] == (
                f"https://app.example.com/v1/agents/reporter/api/runs/{run['run_id']}"
            )

            await agents._execute_agent_api_run_job(
                session=session,
                job_id=run["run_id"],
            )

            polled = await agents.get_agent_api_run(
                "reporter",
                run["run_id"],
                _Request({}),
                authorization=f"Bearer {created.token}",
                session=session,
            )

            assert polled["status"] == "complete"
            assert polled["result"]["result"] == {"summary": "build_report: sales"}
            assert polled["result"]["grant_id"] == "grant-async"
            assert polled["result"]["file_outputs"][0]["download_url"].startswith(
                "https://app.example.com/v1/agents/reporter/api/files/outputs/api/reporter/"
            )
            receipt_row = (await session.execute(select(AgentReceipt))).scalar_one()
            receipt = verify_receipt(receipt_row.signed_token)
            assert receipt.agent_name == "reporter"
            assert receipt.skill_name == "build_report"
            assert receipt.status == "ok"
            assert receipt.task_id == run["run_id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_async_run_fails_on_failure_payload(monkeypatch) -> None:
    _set_receipt_keys(monkeypatch)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="async token"),
                user=user,
                session=session,
            )

            async def fake_invoke_agent_api_skill(**_kwargs):
                return (
                    {
                        "result": {
                            "structured": {
                                "ok": False,
                                "status": "failed",
                                "stop_reason": "builder_failed",
                                "error": "workspace write failed: 403",
                            },
                        },
                        "events": [],
                        "artifacts": [],
                    },
                    None,
                )

            monkeypatch.setattr(
                agents,
                "_invoke_agent_api_skill",
                fake_invoke_agent_api_skill,
            )

            run = await agents.start_agent_api_run(
                "reporter",
                "build_report",
                _Request({"topic": "sales"}),
                authorization=f"Bearer {created.token}",
                session=session,
            )

            await agents._execute_agent_api_run_job(
                session=session,
                job_id=run["run_id"],
            )

            polled = await agents.get_agent_api_run(
                "reporter",
                run["run_id"],
                _Request({}),
                authorization=f"Bearer {created.token}",
                session=session,
            )

            assert polled["status"] == "error"
            assert polled["error"] == "workspace write failed: 403"
            assert polled["result"] is None
            receipt_row = (await session.execute(select(AgentReceipt))).scalar_one()
            receipt = verify_receipt(receipt_row.signed_token)
            assert receipt.status == "error"
            assert receipt.error_type == "agent_api_failure_payload"
            assert receipt.task_id == run["run_id"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_file_download_streams_bytes_for_agent_token(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="download token"),
                user=user,
                session=session,
            )
            seen: dict[str, object] = {}

            def fake_stat_file(bucket: str, path: str) -> dict:
                seen["stat_bucket"] = bucket
                seen["stat_path"] = path
                return {
                    "modified_at": "2026-06-06T12:00:00+00:00",
                    "content_type": "text/plain",
                    "size": 11,
                }

            def fake_iter_file(bucket: str, path: str):
                seen["stream_bucket"] = bucket
                seen["stream_path"] = path
                return iter([b"hello ", b"world"]), "text/plain"

            monkeypatch.setattr(agents, "stat_file", fake_stat_file)
            monkeypatch.setattr(agents, "iter_file", fake_iter_file)

            response = await agents.download_agent_api_file(
                "reporter",
                "outputs/api/reporter/call/report.txt",
                authorization=f"Bearer {created.token}",
                session_cookie=None,
                session=session,
            )

            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
            assert body == b"hello world"
            assert response.media_type == "text/plain"
            assert response.headers["content-length"] == "11"
            assert "report.txt" in response.headers["content-disposition"]
            assert seen == {
                "stat_bucket": f"user-{user.id}-files",
                "stat_path": "outputs/api/reporter/call/report.txt",
                "stream_bucket": f"user-{user.id}-files",
                "stream_path": "outputs/api/reporter/call/report.txt",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_file_download_accepts_control_plane_bearer(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)

            monkeypatch.setattr(
                agents,
                "stat_file",
                lambda bucket, path: {"content_type": "text/csv", "size": 18},
            )
            monkeypatch.setattr(
                agents,
                "iter_file",
                lambda bucket, path: (iter([b"a,b\n1,2\n"]), "text/csv"),
            )

            response = await agents.download_agent_api_file(
                "reporter",
                "outputs/api/reporter/call/report.csv",
                authorization=f"Bearer {issue_token(user.id)}",
                session_cookie=None,
                session=session,
            )

            body = b""
            async for chunk in response.body_iterator:
                body += chunk if isinstance(chunk, bytes) else chunk.encode()
            assert body == b"a,b\n1,2\n"
            assert response.media_type == "text/csv"
            assert response.headers["content-length"] == "18"
    finally:
        await engine.dispose()


def test_hosted_agent_invoke_timeout_uses_agent_runtime() -> None:
    agent = Agent(
        owner_id=1,
        name="slow-agent",
        description="",
        version="0.1.0",
        image="registry.example/slow-agent:latest",
        public=True,
        status="running",
        card={"runtime": {"resources": {"max_runtime_seconds": 900}}},
    )

    assert agents._hosted_agent_invoke_timeout(agent) == 930.0
    assert agents._agent_api_grant_ttl_seconds(agent) == 930


def test_hosted_agent_invoke_timeout_uses_skill_policy_timeout() -> None:
    agent = Agent(
        owner_id=1,
        name="slow-skill-agent",
        description="",
        version="0.1.0",
        image="registry.example/slow-skill-agent:latest",
        public=True,
        status="running",
        card={
            "skills": [
                {
                    "name": "run_slow",
                    "policy": {"timeout_seconds": 1800.0},
                }
            ],
        },
    )

    assert agents._hosted_agent_invoke_timeout(agent) == 1830.0
    assert agents._agent_api_grant_ttl_seconds(agent) == 1830


def test_hosted_agent_invoke_timeout_defaults_to_api_ttl() -> None:
    agent = Agent(
        owner_id=1,
        name="default-timeout-agent",
        description="",
        version="0.1.0",
        image="registry.example/default-timeout-agent:latest",
        public=True,
        status="running",
        card={},
    )

    assert agents._hosted_agent_invoke_timeout(agent) == 900.0


@pytest.mark.asyncio
async def test_agent_api_invoke_requires_saved_consumer_setup(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, agent = await _seed_agent(session)
            agent.card = {
                **agent.card,
                "consumer_setup": {
                    "fields": [
                        {
                            "name": "APIFY_API_KEY",
                            "kind": "secret",
                            "label": "Apify API key",
                            "required": True,
                            "input_type": "password",
                        }
                    ]
                },
            }
            await session.commit()
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="call token"),
                user=user,
                session=session,
            )

            async def fail_post_hosted_agent_invoke(**_kwargs):
                raise AssertionError("hosted agent should not be invoked")

            monkeypatch.setattr(
                agents,
                "_post_hosted_agent_invoke",
                fail_post_hosted_agent_invoke,
            )

            with pytest.raises(Exception) as exc:
                await agents.invoke_agent_api(
                    "reporter",
                    "build_report",
                    _Request({"topic": "sales"}),
                    authorization=f"Bearer {created.token}",
                    session=session,
                )

            assert getattr(exc.value, "status_code", None) == 409
            detail = getattr(exc.value, "detail", {})
            assert detail["error"] == "agent_setup_required"
            assert detail["missing_required"] == ["APIFY_API_KEY"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_api_token_is_scoped_to_agent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user, _agent = await _seed_agent(session)
            created = await agents.create_agent_api_token(
                "reporter",
                agents.AgentApiTokenCreateIn(name="scoped"),
                user=user,
                session=session,
            )
            other = Agent(
                owner_id=user.id,
                name="other",
                description="",
                version="0.1.0",
                image="registry.example/other:latest",
                public=True,
                status="running",
                url="https://other.example.com",
                card={"skills": [{"name": "build_report"}]},
            )
            session.add(other)
            await session.commit()

            with pytest.raises(Exception) as exc:
                await agents.invoke_agent_api(
                    "other",
                    "build_report",
                    _Request({"topic": "sales"}),
                    authorization=f"Bearer {created.token}",
                    session=session,
                )
            assert getattr(exc.value, "status_code", None) == 403
    finally:
        await engine.dispose()
