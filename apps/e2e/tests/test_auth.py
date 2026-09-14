"""Auth surface: signup, login, whoami, 401 on missing bearer."""

from __future__ import annotations

import pytest

from .conftest import API_URL, ApiClient, E2EUser
import httpx


async def test_authenticated_user_returns_token_and_user(test_user: E2EUser) -> None:
    assert test_user.token
    assert test_user.user_id > 0
    assert "@" in test_user.email


async def test_login_with_same_creds(test_user: E2EUser) -> None:
    if test_user.password is None:
        pytest.skip("password login is not available for bearer-token E2E users")
    async with httpx.AsyncClient(base_url=API_URL, timeout=20.0, verify=False) as c:
        r = await c.post(
            "/v1/auth/login",
            json={"email": test_user.email, "password": test_user.password},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["user"]["id"] == test_user.user_id
    assert body["user"]["email"] == test_user.email


async def test_login_wrong_password_is_401(test_user: E2EUser) -> None:
    if test_user.password is None:
        pytest.skip("password login is not available for bearer-token E2E users")
    async with httpx.AsyncClient(base_url=API_URL, timeout=20.0, verify=False) as c:
        r = await c.post(
            "/v1/auth/login",
            json={"email": test_user.email, "password": "wrong-pw"},
        )
    assert r.status_code == 401


async def test_files_endpoint_requires_bearer(anon_client: ApiClient) -> None:
    r = await anon_client.get("/v1/me/files")
    assert r.status_code in (401, 403)


async def test_authed_files_endpoint_returns_list(client: ApiClient) -> None:
    r = await client.get("/v1/me/files")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
