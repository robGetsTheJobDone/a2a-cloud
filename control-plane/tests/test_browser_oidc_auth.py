from __future__ import annotations

import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from control_plane.auth import issue_token, set_session_cookie
from control_plane.auth_exchange import InMemoryCliSessionExchangeStore
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.models import User
from control_plane.routes import auth as auth_routes


def test_production_session_cookie_is_host_prefixed_and_host_only(monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    response = Response()

    set_session_cookie(response, "signed-session")

    cookies = response.headers.getlist("set-cookie")
    current = next(
        value
        for value in cookies
        if value.startswith(f"{settings.session_cookie_name}=")
        and "max-age=0" not in value.lower()
    )
    assert settings.session_cookie_name.startswith("__Host-")
    assert "secure" in current.lower()
    assert "path=/" in current.lower()
    assert "domain=" not in current.lower()
    assert any(
        value.startswith("a2a_session=")
        and "domain=.example.com" in value.lower()
        and "max-age=0" in value.lower()
        for value in cookies
    )


async def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchange_store: InMemoryCliSessionExchangeStore | None = None,
) -> tuple[AsyncClient, Any]:
    monkeypatch.setattr(settings, "dashboard_url", "http://test")
    monkeypatch.setattr(settings, "keycloak_issuer", "https://auth.example/realms/a2a")
    monkeypatch.setattr(settings, "keycloak_browser_client_id", "a2a-dashboard")
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "session_cookie_secure", False)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(auth_routes.me_router)

    async def override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    if exchange_store is not None:
        app.dependency_overrides[auth_routes.get_cli_session_exchange_store] = lambda: (
            exchange_store
        )
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )
    return client, engine


@pytest.mark.asyncio
async def test_cli_session_uses_single_use_exchange_code(monkeypatch) -> None:
    exchange_store = InMemoryCliSessionExchangeStore()
    client, engine = await _client(monkeypatch, exchange_store=exchange_store)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="cli@example.com", password_hash="unused")
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        platform_token = issue_token(user_id)
        created = await client.post(
            "/v1/auth/cli-session",
            headers={"authorization": f"Bearer {platform_token}"},
            json={"redirect_to": "/my-agents?view=active"},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        assert created.headers["pragma"] == "no-cache"
        assert platform_token not in created.text
        assert platform_token not in str(created.url)
        assert created.json()["expires_in"] == 60
        redeem_url = created.json()["redeem_url"]
        assert redeem_url.startswith("http://test/v1/auth/cli-session/redeem?code=")
        exchange_code = parse_qs(urlsplit(redeem_url).query)["code"][0]
        assert exchange_code != platform_token

        redeemed = await client.get(
            "/v1/auth/cli-session/redeem",
            params={"code": exchange_code},
            headers={"sec-fetch-site": "none"},
        )
        assert redeemed.status_code == 200
        assert redeemed.headers["cache-control"] == "no-store"
        assert redeemed.headers["referrer-policy"] == "no-referrer"
        assert redeemed.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in redeemed.headers["content-security-policy"]
        assert "cli@example.com" in redeemed.text
        assert exchange_code not in redeemed.text
        redeem_cookies = redeemed.headers.get_list("set-cookie")
        assert any(
            settings.cli_session_confirmation_cookie_name in value
            and "max-age=0" not in value.lower()
            and "httponly" in value.lower()
            and "samesite=strict" in value.lower()
            and "path=/" in value.lower()
            and "domain=" not in value.lower()
            for value in redeem_cookies
        )
        assert settings.session_cookie_name not in client.cookies
        confirmation_code = client.cookies.get(
            settings.cli_session_confirmation_cookie_name
        )
        assert confirmation_code

        confirmed = await client.post(
            "/v1/auth/cli-session/confirm",
            headers={"origin": "http://test", "sec-fetch-site": "same-origin"},
        )
        assert confirmed.status_code == 303
        assert confirmed.headers["location"] == "http://test/my-agents?view=active"
        assert confirmed.headers["cache-control"] == "no-store"
        assert confirmed.headers["referrer-policy"] == "no-referrer"
        confirmed_cookies = confirmed.headers.get_list("set-cookie")
        assert any("domain=.example.com" in value.lower() for value in confirmed_cookies)
        assert any(
            settings.session_cookie_name in value
            and "max-age=0" not in value.lower()
            and "domain=" not in value.lower()
            for value in confirmed_cookies
        )
        assert client.cookies.get(settings.session_cookie_name)

        replayed = await client.get(
            "/v1/auth/cli-session/redeem",
            params={"code": exchange_code},
            headers={"sec-fetch-site": "same-origin"},
        )
        assert replayed.status_code == 410
        assert replayed.headers["cache-control"] == "no-store"

        client.cookies.set(
            settings.cli_session_confirmation_cookie_name,
            confirmation_code,
        )
        confirmation_replay = await client.post(
            "/v1/auth/cli-session/confirm",
            headers={"origin": "http://test", "sec-fetch-site": "same-origin"},
        )
        assert confirmation_replay.status_code == 410
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site"])
async def test_cli_session_redeem_rejects_navigation_from_untrusted_site(
    monkeypatch,
    fetch_site: str,
) -> None:
    exchange_store = InMemoryCliSessionExchangeStore()
    client, engine = await _client(monkeypatch, exchange_store=exchange_store)
    try:
        code = await exchange_store.issue(
            user_id=7,
            redirect_to="/my-agents",
            ttl_seconds=60,
        )
        response = await client.get(
            "/v1/auth/cli-session/redeem",
            params={"code": code},
            headers={"sec-fetch-site": fetch_site},
        )
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert settings.session_cookie_name not in response.cookies
        assert (await exchange_store.consume(code)) is not None
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_cli_session_redeem_does_not_replace_a_different_account(
    monkeypatch,
) -> None:
    exchange_store = InMemoryCliSessionExchangeStore()
    client, engine = await _client(monkeypatch, exchange_store=exchange_store)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            victim = User(email="victim@example.com", password_hash="unused")
            attacker = User(email="attacker@example.com", password_hash="unused")
            session.add_all([victim, attacker])
            await session.commit()
            await session.refresh(victim)
            await session.refresh(attacker)

        client.cookies.set(settings.session_cookie_name, issue_token(victim.id))
        code = await exchange_store.issue(
            user_id=attacker.id,
            redirect_to="/my-agents",
            ttl_seconds=60,
        )
        response = await client.get(
            "/v1/auth/cli-session/redeem",
            params={"code": code},
            headers={"sec-fetch-site": "none"},
        )
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store"

        session_response = await client.get("/v1/auth/session")
        assert session_response.json()["user"]["id"] == victim.id
        assert await exchange_store.consume(code) is None
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_cli_session_rejects_legacy_query_token_flow(monkeypatch) -> None:
    exchange_store = InMemoryCliSessionExchangeStore()
    client, engine = await _client(monkeypatch, exchange_store=exchange_store)
    try:
        legacy = await client.get(
            "/v1/auth/cli-session",
            params={"token": "legacy-platform-token", "redirect_to": "/my-agents"},
        )
        assert legacy.status_code == 405
        assert settings.session_cookie_name not in legacy.cookies

        query_token_cannot_authenticate = await client.post(
            "/v1/auth/cli-session?token=legacy-platform-token",
            json={"redirect_to": "/my-agents"},
        )
        assert query_token_cannot_authenticate.status_code == 401
        assert (
            settings.session_cookie_name not in query_token_cannot_authenticate.cookies
        )
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_oidc_callback_sets_cookie_session_and_me_uses_it(monkeypatch) -> None:
    client, engine = await _client(monkeypatch)
    try:
        start = await client.get(
            "/v1/auth/oidc/start?redirect_to=/workspace&login_hint=Dev%40Example.com"
        )
        assert start.status_code == 303
        location = start.headers["location"]
        query = parse_qs(urlsplit(location).query)
        assert query["client_id"] == ["a2a-dashboard"]
        assert query["redirect_uri"] == ["http://test/v1/auth/oidc/callback"]
        assert query["response_type"] == ["code"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["login_hint"] == ["Dev@Example.com"]
        start_cookies = start.headers.get_list("set-cookie")
        assert any("domain=.example.com" in value.lower() for value in start_cookies)
        assert any(
            settings.oidc_state_cookie_name in value
            and "max-age=0" not in value.lower()
            and "domain=" not in value.lower()
            for value in start_cookies
        )

        state_cookie = client.cookies.get(settings.oidc_state_cookie_name)
        state_payload = auth_routes._decode_oidc_state(state_cookie)

        async def fake_exchange_oidc_code(
            *,
            code: str,
            code_verifier: str,
            redirect_uri: str,
        ) -> dict:
            assert code == "code-123"
            assert code_verifier == state_payload["code_verifier"]
            assert redirect_uri == "http://test/v1/auth/oidc/callback"
            return {"id_token": "id-token-123"}

        def fake_verify_keycloak_id_token(token: str, *, audience: str) -> dict:
            assert token == "id-token-123"
            assert audience == "a2a-dashboard"
            return {
                "sub": "kc-user-123",
                "email": "Dev@Example.com",
                "email_verified": True,
                "nonce": state_payload["nonce"],
            }

        monkeypatch.setattr(auth_routes, "_exchange_oidc_code", fake_exchange_oidc_code)
        monkeypatch.setattr(
            auth_routes,
            "verify_keycloak_id_token",
            fake_verify_keycloak_id_token,
        )

        callback = await client.get(
            f"/v1/auth/oidc/callback?code=code-123&state={state_payload['state']}"
        )
        assert callback.status_code == 303
        assert callback.headers["location"] == "http://test/workspace"
        assert any(
            settings.session_cookie_name in value
            and "max-age=0" not in value.lower()
            and "domain=" not in value.lower()
            for value in callback.headers.get_list("set-cookie")
        )
        assert client.cookies.get(settings.session_cookie_name)

        auth_session = await client.get("/v1/auth/session")
        assert auth_session.json()["authenticated"] is True
        assert isinstance(auth_session.json()["user"]["id"], int)
        assert auth_session.json()["user"]["email"] == "dev@example.com"
        assert "created_at" not in auth_session.json()["user"]

        me = await client.get("/v1/me")
        assert me.status_code == 200
        assert me.json()["email"] == "dev@example.com"

        logout = await client.post(
            "/v1/auth/logout?redirect_to=/workspace",
            headers={"origin": "http://test"},
        )
        assert logout.status_code == 200
        assert logout.json() == {
            "authenticated": False,
            "user": None,
            "logout_url": (
                "https://auth.example/realms/a2a/protocol/openid-connect/logout"
                "?client_id=a2a-dashboard"
                "&post_logout_redirect_uri=http%3A%2F%2Ftest%2Fworkspace"
            ),
        }
        assert (await client.get("/v1/auth/session")).json()["authenticated"] is False
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_and_me_use_user_column_projection(monkeypatch) -> None:
    client, engine = await _client(monkeypatch)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(
                email="speed@example.com",
                password_hash="expensive-secret",
                is_admin=True,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        client.cookies.set(settings.session_cookie_name, issue_token(user_id))
        user_selects: list[str] = []

        def capture_user_select(
            _conn,
            _cursor,
            statement: str,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if "select " in normalized and " from users " in f" {normalized} ":
                user_selects.append(normalized)

        event.listen(engine.sync_engine, "before_cursor_execute", capture_user_select)
        try:
            auth_session = await client.get("/v1/auth/session")
            me = await client.get("/v1/me")
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                capture_user_select,
            )

        assert auth_session.status_code == 200
        assert auth_session.json() == {
            "authenticated": True,
            "user": {"id": user_id, "email": "speed@example.com"},
        }
        assert me.status_code == 200
        assert me.json()["id"] == user_id
        assert me.json()["email"] == "speed@example.com"
        assert me.json()["created_at"]

        assert len(user_selects) == 2
        assert "users.password_hash" not in user_selects[0]
        assert "users.is_admin" not in user_selects[0]
        assert "users.created_at" not in user_selects[0]
        assert "users.password_hash" not in user_selects[1]
        assert "users.is_admin" not in user_selects[1]
        assert "users.created_at" in user_selects[1]
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_oidc_callback_rejects_state_mismatch(monkeypatch) -> None:
    client, engine = await _client(monkeypatch)
    try:
        await client.get("/v1/auth/oidc/start?redirect_to=//evil.example")
        callback = await client.get("/v1/auth/oidc/callback?code=code-123&state=wrong")
        assert callback.status_code == 303
        assert "auth_error=invalid+oidc+state" in callback.headers["location"]
        assert not client.cookies.get(settings.session_cookie_name)
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_logout_rejects_cross_site_origin(monkeypatch) -> None:
    client, engine = await _client(monkeypatch)
    try:
        response = await client.post(
            "/v1/auth/logout",
            headers={"origin": "https://evil.example"},
        )
        assert response.status_code == 403
    finally:
        await client.aclose()
        await engine.dispose()
