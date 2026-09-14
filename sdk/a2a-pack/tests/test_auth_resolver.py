from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

from a2a_pack import (
    A2AAgent,
    AuthError,
    AuthResolver,
    JWTAuth,
    PlatformUserAuth,
    PlatformUserAuthResolver,
    RunContext,
    skill,
)
from a2a_pack.auth import RemoteBearerAuthResolver
from a2a_pack.frontend import PackedFrontend
from a2a_pack.serve import build_app


class _ThirdPartyResolver(AuthResolver[JWTAuth]):
    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> JWTAuth:
        if token != "customer-token":
            raise AuthError("third-party token denied")
        return JWTAuth(
            sub="user-123",
            email=headers.get("x-user-email"),
            scopes=["profile:read"],
        )


class _ThirdPartyAgent(A2AAgent):
    name = "third-party-auth"
    description = "Uses app-owned bearer auth"
    auth_model = JWTAuth
    auth_resolver = _ThirdPartyResolver()

    @skill(scopes=["profile:read"])
    async def whoami(self, ctx: RunContext[JWTAuth]) -> dict[str, str | None]:
        return {"sub": ctx.auth.sub, "email": ctx.auth.email}


def test_invoke_uses_custom_auth_resolver_even_when_a2a_api_key_is_set(
    monkeypatch,
) -> None:
    monkeypatch.setenv("A2A_API_KEY", "platform-shared-key")
    client = TestClient(build_app(_ThirdPartyAgent()))

    r = client.post(
        "/invoke/whoami",
        headers={
            "Authorization": "Bearer customer-token",
            "X-User-Email": "dev@example.com",
        },
        json={"arguments": {}},
    )

    assert r.status_code == 200
    assert r.json()["result"] == {"sub": "user-123", "email": "dev@example.com"}


def test_invoke_rejects_custom_auth_resolver_failure(monkeypatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "platform-shared-key")
    client = TestClient(build_app(_ThirdPartyAgent()))

    r = client.post(
        "/invoke/whoami",
        headers={"Authorization": "Bearer wrong-token"},
        json={"arguments": {}},
    )

    assert r.status_code == 401
    assert r.json()["detail"] == "third-party token denied"


def test_mcp_tools_call_uses_custom_auth_resolver(monkeypatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "platform-shared-key")
    client = TestClient(build_app(_ThirdPartyAgent()))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "whoami", "arguments": {}},
    }

    r = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer customer-token",
            "X-User-Email": "mcp@example.com",
        },
        json=body,
    )

    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "sub": "user-123",
        "email": "mcp@example.com",
    }


def test_remote_bearer_auth_resolver_maps_oidc_userinfo_payload() -> None:
    resolver = RemoteBearerAuthResolver(
        "https://auth.example.test/userinfo",
        auth_model=JWTAuth,
    )

    principal = resolver._map_payload(
        {
            "sub": "oidc-user",
            "email": "oidc@example.com",
            "organization_id": "org-7",
            "scope": "profile:read files:write",
        }
    )

    assert JWTAuth.model_validate(principal) == JWTAuth(
        sub="oidc-user",
        email="oidc@example.com",
        org_id="org-7",
        scopes=["profile:read", "files:write"],
    )


class _PlatformAgent(A2AAgent):
    name = "platform-auth"
    description = "Uses A2A platform user auth"
    auth_model = PlatformUserAuth

    @skill(scopes=["agent:invoke"])
    async def whoami(self, ctx: RunContext[PlatformUserAuth]) -> dict[str, str | int | None]:
        return {
            "sub": ctx.auth.sub,
            "user_id": ctx.auth.user_id,
            "email": ctx.auth.email,
            "org_slug": ctx.auth.org_slug,
        }


def test_platform_user_auth_resolver_maps_trusted_headers() -> None:
    resolver = PlatformUserAuthResolver(trust_headers=True)

    principal = asyncio.run(
        resolver.resolve(
            None,
            headers={
                "X-A2A-User-Id": "42",
                "X-A2A-User-Email": "dev@example.com",
                "X-A2A-Org": "acme",
                "X-A2A-Scopes": "agent:invoke files:read",
            },
            agent=_PlatformAgent(),
        )
    )

    assert principal.sub == "42"
    assert principal.user_id == 42
    assert principal.email == "dev@example.com"
    assert principal.org_slug == "acme"
    assert principal.scopes == ["agent:invoke", "files:read"]


def test_agent_audience_accepts_instance_name(monkeypatch) -> None:
    from a2a_pack.auth import _agent_audience

    monkeypatch.delenv("A2A_AGENT_NAME", raising=False)

    class SidecarAgent:
        def __init__(self) -> None:
            self.name = "sidecar-agent"

    assert _agent_audience(SidecarAgent()) == "sidecar-agent"


def test_invoke_platform_user_auth_uses_forwarded_session_headers(monkeypatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "platform-shared-key")
    monkeypatch.setenv("A2A_TRUST_PLATFORM_HEADERS", "1")
    client = TestClient(build_app(_PlatformAgent()))

    response = client.post(
        "/invoke/whoami",
        headers={
            "X-A2A-User-Id": "42",
            "X-A2A-User-Email": "dev@example.com",
            "X-A2A-Org": "acme",
            "X-A2A-Scopes": "agent:invoke",
        },
        json={"arguments": {}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "sub": "42",
        "user_id": 42,
        "email": "dev@example.com",
        "org_slug": "acme",
    }


def test_invoke_platform_user_auth_rejects_missing_session(monkeypatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "platform-shared-key")
    client = TestClient(build_app(_PlatformAgent()))

    response = client.post("/invoke/whoami", json={"arguments": {}})

    assert response.status_code == 401
    assert response.json()["detail"] == "platform session required"


def test_platform_frontend_auth_gates_static_app(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("A2A_LOCAL_DEV", raising=False)
    monkeypatch.delenv("A2A_AGENT_PUBLIC", raising=False)
    monkeypatch.setenv("A2A_TRUST_PLATFORM_HEADERS", "1")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>Private app</h1>")
    frontend = PackedFrontend(dist_dir=dist, mount="/app", auth="platform")
    client = TestClient(build_app(_PlatformAgent(), frontend=frontend))

    denied = client.get("/app/")
    assert denied.status_code == 401

    allowed = client.get(
        "/app/config.json",
        headers={"X-A2A-User-Email": "dev@example.com"},
    )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["auth"]["mode"] == "platform"
    assert body["auth"]["flow"] == "platform-browser-session"
    assert body["auth"]["sessionTransport"] == "cookie"
    assert body["auth"]["requiresSession"] is True
    assert body["auth"]["invokeRequiresSession"] is True
    assert body["ui"]["auth"]["flow"] == "platform-browser-session"

    session = client.get(
        "/auth/session",
        headers={"X-A2A-User-Email": "dev@example.com"},
    )
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["user"]["email"] == "dev@example.com"


def test_platform_frontend_auth_gates_server_rendered_app(monkeypatch) -> None:
    monkeypatch.delenv("A2A_LOCAL_DEV", raising=False)
    monkeypatch.delenv("A2A_AGENT_PUBLIC", raising=False)
    monkeypatch.setenv("A2A_TRUST_PLATFORM_HEADERS", "1")
    frontend = PackedFrontend(
        mount="/app",
        auth="platform",
        kind="server-rendered",
        framework="nextjs",
        proxy_url="http://127.0.0.1:9",
    )
    client = TestClient(build_app(_PlatformAgent(), frontend=frontend))

    denied = client.get("/app/")
    assert denied.status_code == 401
    assert denied.json()["detail"] == "platform session required"

    config = client.get(
        "/app/config.json",
        headers={"X-A2A-User-Email": "dev@example.com"},
    )
    assert config.status_code == 200
    body = config.json()
    assert body["ui"]["type"] == "server-rendered"
    assert body["ui"]["framework"] == "nextjs"
    assert body["ui"]["requiresAuth"] is True
