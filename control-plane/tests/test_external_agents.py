from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import Agent
from control_plane.imported_agent_auth import (
    ExternalRequestAuth,
    detect_auth_requirements,
    list_connections,
    select_requirement_for_setup,
    upsert_connection,
)
from control_plane.models import User
from control_plane.routes import agent_auth
from control_plane.routes import agents
from control_plane.schemas import AgentImportAuthIn


def test_external_card_import_normalizes_protocol_card(monkeypatch) -> None:
    monkeypatch.setattr(agents.settings, "public_cp_url", "https://api.example.com")
    card = agents._normalize_external_card(
        {
            "name": "Invoice Agent",
            "description": "Reviews invoices.",
            "version": "1.2.3",
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {
                    "id": "review",
                    "name": "review_invoice",
                    "description": "Review an invoice.",
                    "tags": ["finance"],
                    "inputSchema": {
                        "type": "object",
                        "properties": {"invoice_url": {"type": "string"}},
                    },
                }
            ],
        },
        registry_name="invoice-reviewer",
        base_url="https://invoice.example.com",
        card_url="https://invoice.example.com/.well-known/agent-card.json",
    )

    assert card["name"] == "invoice-reviewer"
    assert card["skills"][0]["name"] == "review_invoice"
    assert card["skills"][0]["input_schema"]["properties"]["invoice_url"]["type"] == "string"
    assert card["capabilities"]["a2a_cloud_import"] == {
        "source": "external_a2a",
        "url": "https://invoice.example.com",
        "card_url": "https://invoice.example.com/.well-known/agent-card.json",
        "mcp": {
            "mode": "generated",
            "base_url": "https://api.example.com/v1/agents/invoice-reviewer",
            "path": "/mcp",
        },
    }


def _hosted_agent(name: str) -> Agent:
    return Agent(
        name=name,
        image=f"registry.a2acloud.io/agents/{name}:latest",
        public=True,
        url=agents._canonical_url(name),
        card={},
    )


class _FakeSession:
    async def commit(self) -> None:  # pragma: no cover - trivial
        return None

    async def rollback(self) -> None:  # pragma: no cover - trivial
        return None


class _JsonRequest:
    def __init__(self, body: dict):
        self._body = body
        self.headers = {
            "host": "internal.svc.cluster.local",
            "x-forwarded-host": "app.a2acloud.io",
            "x-forwarded-proto": "https",
        }

    async def json(self) -> dict:
        return self._body


def test_refresh_cards_inplace_does_not_wake_hosted_agents_by_default(monkeypatch) -> None:
    fetched: list[str] = []

    async def fake_fetch(agent, *, request_auth=None):
        fetched.append(agent.name)
        return None

    monkeypatch.setattr(agents, "_fetch_card_with_resolved_auth", fake_fetch)

    asyncio.run(agents._refresh_cards_inplace([_hosted_agent("hosted")], _FakeSession()))

    # Passive read: a scaled-to-zero hosted agent must never be cold-started.
    assert fetched == []


def test_refresh_cards_inplace_wakes_hosted_agents_when_forced(monkeypatch) -> None:
    fetched: list[str] = []

    async def fake_fetch(agent, *, request_auth=None):
        fetched.append(agent.name)
        return None

    monkeypatch.setattr(agents, "_fetch_card_with_resolved_auth", fake_fetch)

    asyncio.run(
        agents._refresh_cards_inplace(
            [_hosted_agent("hosted")], _FakeSession(), wake_hosted=True
        )
    )

    # Explicit owner/operator refresh may intentionally wake the agent.
    assert fetched == ["hosted"]


def test_external_card_import_preserves_native_mcp_endpoint() -> None:
    card = agents._normalize_external_card(
        {
            "name": "Native MCP Agent",
            "version": "1.0.0",
            "skills": [{"name": "echo", "description": "Echo input."}],
            "mcp_endpoint": "/custom-mcp",
            "connector_mcp_endpoint": "/connector-mcp",
        },
        registry_name="native-mcp",
        base_url="https://native.example.com",
        card_url="https://native.example.com/.well-known/agent-card",
    )

    assert card["mcp_endpoint"] == "/custom-mcp"
    assert card["capabilities"]["a2a_cloud_import"]["mcp"] == {
        "mode": "native",
        "base_url": "https://native.example.com",
        "path": "/custom-mcp",
        "connector_path": "/connector-mcp",
    }
    assert card["connector_mcp_endpoint"] == "/connector-mcp"
    assert card["mcp_endpoints"]["connector"]["poll_tool"] == "job_result"


def test_external_card_import_preserves_consumer_setup() -> None:
    card = agents._normalize_external_card(
        {
            "name": "GitHub Agent",
            "version": "1.0.0",
            "skills": [{"name": "create_issue", "description": "Create an issue."}],
            "consumer_setup": {
                "fields": [
                    {
                        "name": "GITHUB_TOKEN",
                        "kind": "secret",
                        "label": "GitHub token",
                        "required": True,
                    }
                ]
            },
        },
        registry_name="github-agent",
        base_url="https://github-agent.example.com",
        card_url="https://github-agent.example.com/.well-known/agent-card",
    )

    assert card["consumer_setup"]["fields"][0]["name"] == "GITHUB_TOKEN"


def test_external_card_import_with_auth_uses_generated_mcp_proxy() -> None:
    auth = {"type": "api_key", "location": "header", "name": "X-Agent-Key", "stored": True}
    card = agents._normalize_external_card(
        {
            "name": "Native MCP Agent",
            "version": "1.0.0",
            "skills": [{"name": "echo", "description": "Echo input."}],
            "mcp_endpoint": "/custom-mcp",
        },
        registry_name="native-mcp",
        base_url="https://native.example.com",
        card_url="https://native.example.com/.well-known/agent-card",
        auth=auth,
    )

    assert card["mcp_endpoint"] == "/mcp"
    assert card["capabilities"]["a2a_cloud_import"]["mcp"]["mode"] == "generated"
    assert card["capabilities"]["a2a_cloud_import"]["auth"] == auth


def test_import_without_auth_allows_needs_auth_setup_later() -> None:
    auth, secret = agents._normalize_import_auth(None, card_declares_auth=True)

    assert auth == {"type": "none"}
    assert secret is None


def test_import_auth_builds_http_and_api_key_request_auth() -> None:
    bearer, bearer_secret = agents._normalize_import_auth(
        AgentImportAuthIn(type="bearer", value="token-123"),
        card_declares_auth=True,
    )
    assert agents._external_request_auth(bearer, bearer_secret) == (
        {"authorization": "Bearer token-123"},
        {},
    )

    api_key, api_key_secret = agents._normalize_import_auth(
        AgentImportAuthIn(
            type="api_key",
            value="secret-456",
            location="query",
            name="api_key",
        ),
        card_declares_auth=True,
    )
    assert agents._external_request_auth(api_key, api_key_secret) == (
        {},
        {"api_key": "secret-456"},
    )


def test_detect_auth_requirements_supports_a2a_security_schemes() -> None:
    requirements = detect_auth_requirements(
        {
            "securitySchemes": {
                "remoteKey": {
                    "apiKeySecurityScheme": {
                        "location": "header",
                        "name": "X-Remote-Key",
                    }
                },
                "remoteOAuth": {
                    "oauth2SecurityScheme": {
                        "authorizationCode": {
                            "authorizationUrl": "https://auth.example.com/authorize",
                            "tokenUrl": "https://auth.example.com/token",
                            "scopes": {"agent.invoke": "Invoke the agent"},
                        }
                    }
                },
                "remoteMTLS": {"mtlsSecurityScheme": {}},
            },
            "securityRequirements": [{"remoteOAuth": ["agent.invoke"]}],
        }
    )

    by_name = {item["scheme_name"]: item for item in requirements}
    assert by_name["remoteKey"]["scheme_type"] == "api_key"
    assert by_name["remoteKey"]["location"] == "header"
    assert by_name["remoteKey"]["name"] == "X-Remote-Key"
    assert by_name["remoteOAuth"]["scheme_type"] == "oauth2"
    assert by_name["remoteOAuth"]["required"] is True
    assert by_name["remoteOAuth"]["token_url"] == "https://auth.example.com/token"
    assert by_name["remoteMTLS"]["scheme_type"] == "mtls"


def test_scheme_matching_rejects_wrong_api_key_shape() -> None:
    requirements = detect_auth_requirements(
        {
            "securitySchemes": {
                "remoteKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Remote-Key",
                },
            },
            "security": [{"remoteKey": []}],
        }
    )

    assert (
        select_requirement_for_setup(
            requirements,
            scheme_type="api_key",
            scheme_name=None,
            api_key_location="query",
            api_key_name="api_key",
        )
        is None
    )
    assert (
        select_requirement_for_setup(
            requirements,
            scheme_type="api_key",
            scheme_name=None,
            api_key_location="header",
            api_key_name="X-Remote-Key",
        )["scheme_name"]
        == "remoteKey"
    )


def test_external_mcp_out_uses_generated_control_plane_url(monkeypatch) -> None:
    monkeypatch.setattr(agents.settings, "public_cp_url", "https://api.example.com")
    card = agents._normalize_external_card(
        {
            "name": "Remote",
            "version": "1.0.0",
            "skills": [{"name": "summarize", "description": "Summarize text."}],
        },
        registry_name="remote",
        base_url="https://remote.example.com",
        card_url="https://remote.example.com/.well-known/agent-card",
    )
    agent = Agent(
        owner_id=1,
        name="remote",
        description="",
        version="1.0.0",
        image=agents._external_image("https://remote.example.com"),
        public=False,
        status="running",
        url="https://remote.example.com",
        card=card,
    )

    out = agents._mcp_out_for_agent(agent)

    assert out.mode == "generated"
    assert out.url == "https://api.example.com/v1/agents/remote/mcp"


def test_external_mcp_tools_derive_from_card_skills() -> None:
    tools = agents._external_mcp_tools(
        {
            "skills": [
                {
                    "name": "extract",
                    "description": "Extract fields.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"fields": {"type": "object"}},
                    },
                }
            ]
        }
    )

    assert tools == [
        {
            "name": "extract",
            "description": "Extract fields.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
            "outputSchema": {
                "type": "object",
                "properties": {"fields": {"type": "object"}},
            },
        }
    ]


@pytest.mark.asyncio
async def test_refresh_cards_resolves_external_auth_without_session_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_use = False
    overlaps: list[str] = []
    auth_calls: list[str] = []

    async def fake_request_auth(
        agent: Agent,
        session: object,
    ) -> ExternalRequestAuth:
        nonlocal in_use
        if in_use:
            overlaps.append(agent.name)
        in_use = True
        await asyncio.sleep(0)
        auth_calls.append(agent.name)
        in_use = False
        return ExternalRequestAuth(headers={}, params={})

    async def fake_fetch_card(
        base_url: str,
        *,
        request_auth: tuple[dict[str, str], dict[str, str]] | None = None,
    ) -> tuple[dict[str, object], str]:
        return (
            {
                "name": base_url.rsplit("/", 1)[-1],
                "version": "1.0.0",
                "skills": [{"name": "echo", "description": "Echo input."}],
            },
            f"{base_url}/.well-known/agent-card",
        )

    class FakeSession:
        commits = 0

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            pass

    monkeypatch.setattr(agents, "_external_request_auth_for_agent", fake_request_auth)
    monkeypatch.setattr(agents, "_fetch_card_from_base_url", fake_fetch_card)

    rows = [
        Agent(
            owner_id=1,
            name="remote-a",
            description="",
            version="1.0.0",
            image=agents._external_image("https://example.com/remote-a"),
            public=True,
            status="running",
            url="https://example.com/remote-a",
            card={"skills": []},
        ),
        Agent(
            owner_id=1,
            name="remote-b",
            description="",
            version="1.0.0",
            image=agents._external_image("https://example.com/remote-b"),
            public=True,
            status="running",
            url="https://example.com/remote-b",
            card={"skills": []},
        ),
    ]

    await agents._refresh_cards_inplace(rows, FakeSession())  # type: ignore[arg-type]

    assert overlaps == []
    assert auth_calls == ["remote-a", "remote-b"]
    assert all(row.card.get("skills") for row in rows)


@pytest.mark.asyncio
async def test_external_agent_call_uses_calling_user_imported_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            caller = User(email="caller@example.com", password_hash="x")
            session.add_all([owner, caller])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="remote-writer",
                description="",
                version="1.0.0",
                image=agents._external_image("https://writer.example.test"),
                public=True,
                status="running",
                url="https://writer.example.test",
                card={
                    "skills": [{"name": "draft", "description": "Draft text."}],
                    "securitySchemes": {
                        "apiKeyAuth": {
                            "type": "apiKey",
                            "in": "header",
                            "name": "X-API-Key",
                        }
                    },
                    "security": [{"apiKeyAuth": []}],
                },
            )
            session.add(agent)
            await session.flush()
            for user, value in ((owner, "owner-key"), (caller, "caller-key")):
                await upsert_connection(
                    session,
                    agent=agent,
                    user=user,
                    scheme_name="apiKeyAuth",
                    scheme_type="api_key",
                    credential_scope="user",
                    secret_payload={
                        "value": value,
                        "location": "header",
                        "name": "X-API-Key",
                    },
                    metadata={
                        "scheme_name": "apiKeyAuth",
                        "scheme_type": "api_key",
                        "location": "header",
                        "name": "X-API-Key",
                    },
                )
            await session.commit()

            seen_headers: list[dict[str, str]] = []

            async def fake_invoke(
                *,
                base_url: str,
                skill_name: str,
                arguments: dict[str, object],
                request_auth: ExternalRequestAuth | None = None,
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                seen_headers.append(dict((request_auth or ExternalRequestAuth({}, {})).headers))
                return {"base_url": base_url, "skill_name": skill_name, "arguments": arguments}

            monkeypatch.setattr(agents, "_call_external_invoke", fake_invoke)

            out = await agents._call_external_agent_skill(
                agent=agent,
                session=session,
                user=caller,
                skill_name="draft",
                arguments={"topic": "launch"},
            )

            assert out["skill_name"] == "draft"
            assert seen_headers == [{"X-API-Key": "caller-key"}]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_external_mcp_accepts_integration_token_and_uses_token_user_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            caller = User(email="caller@example.com", password_hash="x")
            session.add_all([owner, caller])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="remote-mcp",
                description="",
                version="1.0.0",
                image=agents._external_image("https://remote.example.test"),
                public=True,
                status="running",
                url="https://remote.example.test",
                card={
                    "skills": [{"name": "draft", "description": "Draft text."}],
                    "securitySchemes": {
                        "apiKeyAuth": {
                            "type": "apiKey",
                            "in": "header",
                            "name": "X-API-Key",
                        }
                    },
                    "security": [{"apiKeyAuth": []}],
                },
            )
            session.add(agent)
            await session.flush()
            for user, value in ((owner, "owner-key"), (caller, "caller-key")):
                await upsert_connection(
                    session,
                    agent=agent,
                    user=user,
                    scheme_name="apiKeyAuth",
                    scheme_type="api_key",
                    credential_scope="user",
                    secret_payload={
                        "value": value,
                        "location": "header",
                        "name": "X-API-Key",
                    },
                    metadata={
                        "scheme_name": "apiKeyAuth",
                        "scheme_type": "api_key",
                        "location": "header",
                        "name": "X-API-Key",
                    },
                )
            await session.commit()

            created = await agents.create_agent_integration_link(
                "remote-mcp",
                _JsonRequest({}),
                user=caller,
                session=session,
            )
            seen_headers: list[dict[str, str]] = []

            async def fake_invoke(
                *,
                base_url: str,
                skill_name: str,
                arguments: dict[str, object],
                request_auth: ExternalRequestAuth | None = None,
                timeout_seconds: float | None = None,
            ) -> dict[str, object]:
                seen_headers.append(dict((request_auth or ExternalRequestAuth({}, {})).headers))
                return {"skill_name": skill_name, "arguments": arguments}

            monkeypatch.setattr(agents, "_call_external_invoke", fake_invoke)

            out = await agents.external_agent_mcp(
                "remote-mcp",
                _JsonRequest(
                    {
                        "jsonrpc": "2.0",
                        "id": "call-1",
                        "method": "tools/call",
                        "params": {
                            "name": "draft",
                            "arguments": {"topic": "launch"},
                        },
                    }
                ),
                authorization=None,
                integration_token=created.token,
                user=None,
                session=session,
            )

            assert out["result"]["structuredContent"]["skill_name"] == "draft"
            assert seen_headers == [{"X-API-Key": "caller-key"}]

            # Same credential in an Authorization header - the default form,
            # since the minted mcp_url no longer carries the token.
            assert "integration_token=" not in created.mcp_url
            header_out = await agents.external_agent_mcp(
                "remote-mcp",
                _JsonRequest(
                    {
                        "jsonrpc": "2.0",
                        "id": "call-2",
                        "method": "tools/call",
                        "params": {
                            "name": "draft",
                            "arguments": {"topic": "launch"},
                        },
                    }
                ),
                authorization=f"Bearer {created.token}",
                integration_token=None,
                user=None,
                session=session,
            )
            assert header_out["result"]["structuredContent"]["skill_name"] == "draft"
            assert seen_headers == [
                {"X-API-Key": "caller-key"},
                {"X-API-Key": "caller-key"},
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_imported_agent_auth_setup_is_per_calling_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            caller = User(email="caller@example.com", password_hash="x")
            session.add_all([owner, caller])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="public-remote",
                description="",
                version="1.0.0",
                image=agents._external_image("https://remote.example.test"),
                public=True,
                status="needs_auth",
                url="https://remote.example.test",
                card={
                    "skills": [{"name": "draft", "description": "Draft text."}],
                    "securitySchemes": {
                        "apiKeyAuth": {
                            "type": "apiKey",
                            "in": "header",
                            "name": "X-API-Key",
                        }
                    },
                    "security": [{"apiKeyAuth": []}],
                },
            )
            session.add(agent)
            await session.commit()

            out = await agent_auth.connect_agent_auth(
                "public-remote",
                agent_auth.AgentAuthConnectIn(
                    scheme_type="api_key",
                    value="caller-key",
                    location="header",
                    name="X-API-Key",
                ),
                user=caller,
                session=session,
            )

            assert out.scheme_name == "apiKeyAuth"
            await session.refresh(agent)
            assert agent.status == "needs_auth"
            caller_rows = await list_connections(session, agent, user=caller)
            owner_rows = await list_connections(session, agent, user=owner)
            assert [row.scheme_name for row in caller_rows] == ["apiKeyAuth"]
            assert owner_rows == []
    finally:
        await engine.dispose()
