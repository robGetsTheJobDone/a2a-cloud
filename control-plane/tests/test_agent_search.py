from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.agent_search import SemanticAgentMatch, agent_search_document
from control_plane.db import Base
from control_plane.models import Agent, User
from control_plane.routes import agents
from main_agent.tools.discovery import build_discovery_tools


def _card() -> dict[str, Any]:
    return {
        "name": "invoice-reviewer",
        "description": "Reviews invoices for payment issues.",
        "version": "1.0.0",
        "runtime": {"llm_provisioning": "platform"},
        "skills": [
            {
                "name": "review_invoice",
                "description": "Review invoice line items and totals.",
                "tags": ["finance", "invoice"],
                "input_schema": {
                    "type": "object",
                    "required": ["invoice_url"],
                    "properties": {
                        "invoice_url": {"type": "string"},
                        "strict": {"type": "boolean"},
                    },
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_agent_search_uses_semantic_index_and_filters_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            other = User(email="other@example.com", password_hash="x")
            session.add_all([owner, other])
            await session.flush()
            visible = Agent(
                owner_id=other.id,
                name="invoice-reviewer",
                description="Reviews invoices.",
                version="1.0.0",
                image="example/invoice",
                public=True,
                status="running",
                url="https://invoice.example.com",
                card=_card(),
            )
            owned = Agent(
                owner_id=owner.id,
                name="private-owner-helper",
                description="Owner private helper.",
                version="1.0.0",
                image="example/private",
                public=False,
                status="running",
                card=_card(),
            )
            hidden = Agent(
                owner_id=other.id,
                name="hidden-private-helper",
                description="Other user's private helper.",
                version="1.0.0",
                image="example/hidden",
                public=False,
                status="running",
                card=_card(),
            )
            session.add_all([visible, owned, hidden])
            await session.commit()

            class FakeSearch:
                configured = True
                indexed: list[str] = []

                async def index_agents(self, rows: list[Agent]) -> None:
                    self.indexed = [row.name for row in rows]

                async def query_agents(
                    self,
                    query: str,
                    *,
                    user_id: int,
                    limit: int,
                    score_threshold: float | None,
                ) -> list[SemanticAgentMatch]:
                    return [
                        SemanticAgentMatch(agent_id=hidden.id, score=0.99),
                        SemanticAgentMatch(agent_id=owned.id, score=0.91),
                        SemanticAgentMatch(agent_id=visible.id, score=0.82),
                    ]

            fake = FakeSearch()
            monkeypatch.setattr(agents, "semantic_agent_search", fake)
            monkeypatch.setattr(agents, "_agent_search_index_bootstrapped", False)

            result = await agents.search_agents(
                q="invoice review",
                tag=[],
                skill=None,
                limit=5,
                user=owner,
                session=session,
            )

            assert [item.name for item in result] == [
                "private-owner-helper",
                "invoice-reviewer",
            ]
            assert "hidden-private-helper" not in [item.name for item in result]
            assert set(fake.indexed) == {
                "invoice-reviewer",
                "private-owner-helper",
                "hidden-private-helper",
            }
            assert result[0].match_source == "semantic"
            assert result[0].skills[0].input_fields[0].name == "invoice_url"
            assert result[0].skills[0].input_fields[0].required is True
    finally:
        await engine.dispose()


def test_agent_search_document_includes_skill_schema_terms() -> None:
    agent = Agent(
        id=1,
        owner_id=1,
        name="invoice-reviewer",
        description="Reviews invoices.",
        version="1.0.0",
        image="example/invoice",
        public=True,
        status="running",
        card=_card(),
    )

    document = agent_search_document(agent)

    assert "review_invoice" in document
    assert "invoice_url" in document
    assert "finance" in document


@pytest.mark.asyncio
async def test_discover_agent_uses_compact_search_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        text = ""

        def json(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "invoice-reviewer",
                    "description": "Reviews invoices.",
                    "status": "running",
                    "score": 0.93,
                    "match_source": "semantic",
                    "llm_provisioning": "platform",
                    "setup_required": False,
                    "skills": [
                        {
                            "name": "review_invoice",
                            "description": "Review invoice line items.",
                            "tags": ["finance"],
                            "input_fields": [
                                {
                                    "name": "invoice_url",
                                    "type": "string",
                                    "required": True,
                                }
                            ],
                            "input_schema": {"large": "schema"},
                        }
                    ],
                }
            ]

    class Client:
        def __init__(self, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> Response:
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["params"] = kwargs.get("params")
            return Response()

    import main_agent.tools.discovery as discovery

    monkeypatch.setattr(discovery.httpx, "AsyncClient", Client)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(cp_url="http://cp.local"),
        jwt="token-123",
    )

    raw = await build_discovery_tools(ctx)[0].ainvoke(
        {"query": "invoice review", "tags": ["finance"], "limit": 4}
    )
    payload = json.loads(raw)

    assert captured["url"] == "http://cp.local/v1/agents/search"
    assert captured["headers"] == {"authorization": "bearer token-123"}
    assert ("q", "invoice review") in captured["params"]
    assert ("tag", "finance") in captured["params"]
    assert ("limit", 4) in captured["params"]
    assert payload["match_source"] == "semantic"
    assert payload["agents"][0]["skills"][0]["input_fields"][0]["name"] == "invoice_url"
    assert "input_schema" not in payload["agents"][0]["skills"][0]


@pytest.mark.asyncio
async def test_discover_agent_reports_blank_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> None:
            request = httpx.Request("GET", url)
            raise httpx.ReadTimeout("", request=request)

    import main_agent.tools.discovery as discovery

    monkeypatch.setattr(discovery.httpx, "AsyncClient", Client)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(cp_url="http://cp.local/"),
        jwt="token-123",
    )

    raw = await build_discovery_tools(ctx)[0].ainvoke({"query": "invoice"})
    payload = json.loads(raw)

    assert payload["error"] == "cp unreachable: ReadTimeout"
    assert "ReadTimeout" in payload["detail"]
    assert "request=http://cp.local/v1/agents/search" in payload["detail"]
    assert payload["cp_url"] == "http://cp.local"


@pytest.mark.asyncio
async def test_list_my_agents_uses_summary_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        text = ""

        def json(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "invoice-reviewer",
                    "description": "Reviews invoices.",
                    "version": "1.2.3",
                    "status": "running",
                    "url": "https://invoice-reviewer.example",
                    "skill_count": 2,
                }
            ]

    class Client:
        def __init__(self, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> Response:
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return Response()

    import main_agent.tools.discovery as discovery

    monkeypatch.setattr(discovery.httpx, "AsyncClient", Client)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(cp_url="http://cp.local/"),
        jwt="token-123",
    )

    raw = await build_discovery_tools(ctx)[1].ainvoke({})
    payload = json.loads(raw)

    assert captured["url"] == "http://cp.local/v1/agents/mine/summary"
    assert captured["headers"] == {"authorization": "bearer token-123"}
    assert payload == {
        "agents": [
            {
                "name": "invoice-reviewer",
                "url": "https://invoice-reviewer.example",
                "version": "1.2.3",
                "status": "running",
                "description": "Reviews invoices.",
                "skill_count": 2,
            }
        ]
    }
