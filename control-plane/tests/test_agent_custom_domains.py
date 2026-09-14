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
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from control_plane.db import Base
from control_plane.k8s import (
    render_custom_domain_http_redirect_ingress,
    render_custom_domain_ingress,
    render_custom_domain_redirect_middlewares,
)
from control_plane.models import Agent, User
from control_plane.routes import agents as agent_routes
from control_plane.schemas import AgentCustomDomainIn


def test_custom_domain_records_match_dns_onboarding_shape() -> None:
    with pytest.raises(HTTPException, match="reserved for platform infrastructure"):
        agent_routes._validate_agent_name("agent-ingress-gateway")
    assert (
        agent_routes._normalize_custom_hostname(" Agent.Customer.test. ")
        == "agent.customer.test"
    )
    assert agent_routes._custom_domain_verification_record(
        "agent.customer.test",
        "tok",
    ) == (
        "_a2a-agent.agent.customer.test",
        "a2a-agent-verification=tok",
    )
    assert agent_routes._custom_domain_routing_record(
        "research-agent",
        "agent.customer.test",
    ) == (
        "CNAME",
        "agent.customer.test",
        "research-agent.example.com",
    )
    assert agent_routes._is_apex_hostname("customer.test")
    assert not agent_routes._is_apex_hostname("agent.customer.test")
    with pytest.raises(HTTPException):
        agent_routes._normalize_custom_hostname("https://agent.customer.test")
    with pytest.raises(HTTPException):
        agent_routes._normalize_custom_hostname("agent.example.com")


def test_render_custom_domain_ingress_routes_hosts_to_trusted_gateway() -> None:
    doc = render_custom_domain_ingress(
        "research-agent",
        ["agent.customer.test", "demo.customer.test", "agent.customer.test"],
    )

    assert doc is not None
    assert doc["metadata"]["name"] == "research-agent-custom-domains"
    assert doc["spec"]["tls"] == [
        {
            "hosts": ["agent.customer.test", "demo.customer.test"],
            "secretName": "research-agent-custom-domains-tls",
        }
    ]
    assert [rule["host"] for rule in doc["spec"]["rules"]] == [
        "agent.customer.test",
        "demo.customer.test",
    ]
    for rule in doc["spec"]["rules"]:
        backend = rule["http"]["paths"][0]["backend"]["service"]
        assert backend == {
            "name": "agent-ingress-gateway",
            "port": {"number": 80},
        }
    # The gateway resolves active custom domains itself. Rewriting Host to the
    # platform hostname would hide the verified domain and break application
    # redirects/cookie scope.
    assert (
        "traefik.ingress.kubernetes.io/router.middlewares"
        not in doc["metadata"]["annotations"]
    )


def test_render_custom_domain_redirect_middleware_for_www_pair() -> None:
    routes = [
        {
            "hostname": "www.customer.test",
            "canonical_hostname": "customer.test",
            "redirect_enabled": True,
        },
        {
            "hostname": "customer.test",
            "canonical_hostname": "customer.test",
            "redirect_enabled": False,
        },
    ]
    doc = render_custom_domain_ingress("research-agent", routes)
    middlewares = render_custom_domain_redirect_middlewares("research-agent", routes)

    assert doc is not None
    assert (
        "traefik.ingress.kubernetes.io/router.middlewares"
        in doc["metadata"]["annotations"]
    )
    ingress_middlewares = doc["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"
    ].split(",")
    assert ingress_middlewares[0].startswith(
        "agents-research-agent-redir-www-customer-test-"
    )
    assert ingress_middlewares[0].endswith("@kubernetescrd")
    assert len(ingress_middlewares) == 1
    assert len(middlewares) == 1
    middleware = middlewares[0]
    assert middleware["kind"] == "Middleware"
    assert middleware["spec"]["redirectRegex"] == {
        "regex": r"^https?://www\.customer\.test(/.*)?$",
        "replacement": "https://customer.test${1}",
        "permanent": True,
    }


def test_render_custom_domain_http_redirect_uses_canonical_redirect_first() -> None:
    routes = [
        {
            "hostname": "www.customer.test",
            "canonical_hostname": "customer.test",
            "redirect_enabled": True,
        },
        {
            "hostname": "customer.test",
            "canonical_hostname": "customer.test",
            "redirect_enabled": False,
        },
    ]
    doc = render_custom_domain_http_redirect_ingress("research-agent", routes)

    assert doc is not None
    middlewares = doc["metadata"]["annotations"][
        "traefik.ingress.kubernetes.io/router.middlewares"
    ].split(",")
    assert middlewares[0].startswith("agents-research-agent-redir-www-customer-test-")
    assert middlewares[0].endswith("@kubernetescrd")
    assert middlewares[1] == "agents-research-agent-custom-domains-https@kubernetescrd"


def test_apex_domain_response_includes_a_record_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_routes,
        "_dns_host_addresses",
        lambda *_args, **_kwargs: {"203.0.113.10"},
    )
    row = agent_routes.AgentCustomDomain(
        id=1,
        agent_id=1,
        user_id=1,
        agent_name="research-agent",
        hostname="customer.test",
        verification_token="tok",
        status="pending",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    out = agent_routes._agent_custom_domain_out(row)

    assert out.routing_record_type == "CNAME"
    assert out.routing_record_value == "research-agent.example.com"
    assert out.routing_fallback_record_type == "A"
    assert out.routing_fallback_record_name == "customer.test"
    assert out.routing_fallback_record_value == "203.0.113.10"


@pytest.mark.asyncio
async def test_agent_custom_domain_verification_syncs_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    synced: list[tuple[str, list[dict[str, str | bool]]]] = []
    monkeypatch.setattr(agent_routes, "_dns_txt_record_matches", lambda *_args: True)
    monkeypatch.setattr(agent_routes, "_dns_cname_points_to", lambda *_args: True)
    monkeypatch.setattr(
        agent_routes,
        "sync_custom_domain_ingress",
        lambda name, hostnames: synced.append((name, hostnames)),
    )

    async with Session() as session:
        user = User(email="owner@customer.test", password_hash="hash")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="research-agent",
            description="",
            version="1.0.0",
            image="registry.example.com/agents/research-agent:latest",
            public=True,
            status="running",
            url="https://research-agent.example.com",
            card={"skills": [{"name": "search"}]},
        )
        session.add(agent)
        await session.commit()

        pending = await agent_routes.add_agent_custom_domain(
            "research-agent",
            AgentCustomDomainIn(hostname="Agent.Customer.test"),
            user,
            session,
        )
        assert pending.hostname == "agent.customer.test"
        assert pending.status == "pending"
        assert pending.url is None

        active = await agent_routes.verify_agent_custom_domain(
            "research-agent",
            "agent.customer.test",
            user,
            session,
        )

        assert active.status == "active"
        assert active.url == "https://agent.customer.test"
        assert synced == [
            (
                "research-agent",
                [
                    {
                        "hostname": "agent.customer.test",
                        "canonical_hostname": "agent.customer.test",
                        "redirect_enabled": False,
                    }
                ],
            )
        ]

    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_custom_domain_verification_activates_www_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    synced: list[tuple[str, list[dict[str, str | bool]]]] = []
    monkeypatch.setattr(agent_routes, "_dns_txt_record_matches", lambda *_args: True)
    monkeypatch.setattr(agent_routes, "_dns_cname_points_to", lambda *_args: True)
    monkeypatch.setattr(
        agent_routes,
        "sync_custom_domain_ingress",
        lambda name, hostnames: synced.append((name, hostnames)),
    )

    async with Session() as session:
        user = User(email="owner@customer.test", password_hash="hash")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="research-agent",
            description="",
            version="1.0.0",
            image="registry.example.com/agents/research-agent:latest",
            public=True,
            status="running",
            url="https://research-agent.example.com",
            card={"skills": [{"name": "search"}]},
        )
        session.add(agent)
        await session.commit()

        pending = await agent_routes.add_agent_custom_domain(
            "research-agent",
            AgentCustomDomainIn(hostname="customer.test", include_www=True),
            user,
            session,
        )
        assert pending.hostname == "customer.test"

        active = await agent_routes.verify_agent_custom_domain(
            "research-agent",
            "customer.test",
            user,
            session,
        )

        assert active.status == "active"
        assert synced == [
            (
                "research-agent",
                [
                    {
                        "hostname": "customer.test",
                        "canonical_hostname": "customer.test",
                        "redirect_enabled": False,
                    },
                    {
                        "hostname": "www.customer.test",
                        "canonical_hostname": "customer.test",
                        "redirect_enabled": True,
                    },
                ],
            )
        ]
        rows = (
            await session.execute(
                select(agent_routes.AgentCustomDomain).order_by(
                    agent_routes.AgentCustomDomain.hostname
                )
            )
        ).scalars().all()
        assert [(row.hostname, row.status) for row in rows] == [
            ("customer.test", "active"),
            ("www.customer.test", "active"),
        ]

    await engine.dispose()
