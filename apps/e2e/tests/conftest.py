"""Shared fixtures for the e2e suite.

The default target is the production a2a cluster (https://api.a2acloud.io).
Override with ``A2A_E2E_API_URL`` for the local docker-desktop cluster
(``http://api.127-0-0-1.nip.io``) or any other deployment.

Production runs should provide pre-minted bearer tokens through
``A2A_E2E_BEARER_TOKEN`` and ``A2A_E2E_OTHER_BEARER_TOKEN``. Legacy signup is
kept as a local/dev fallback. Tests share each user's MinIO bucket; sequence
carefully if a test depends on state another test produced. Most tests use
unique paths.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio


API_URL = os.environ.get("A2A_E2E_API_URL", "https://api.a2acloud.io").rstrip("/")
DEFAULT_TIMEOUT = 30.0


@dataclass
class E2EUser:
    email: str
    password: str | None
    token: str
    user_id: int
    source: str


class ApiClient:
    """Thin httpx wrapper carrying the test user's bearer token."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=DEFAULT_TIMEOUT,
            verify=False,  # local nip.io clusters use self-signed certs
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.get(path, headers=self._headers(), **kwargs)

    async def post_json(
        self, path: str, body: Any | None = None, **kwargs: Any
    ) -> httpx.Response:
        return await self._client.post(
            path,
            headers=self._headers({"Content-Type": "application/json"}),
            content=json.dumps(body) if body is not None else None,
            **kwargs,
        )

    async def post_form(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        return await self._client.post(
            path,
            headers=self._headers(),
            data=data,
            files=files,
            **kwargs,
        )

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.delete(
            path,
            headers=self._headers(),
            **kwargs,
        )

    async def stream_sse(
        self,
        path: str,
        body: Any,
        *,
        timeout: float = 300.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """POST JSON to ``path`` with ``Accept: text/event-stream`` and yield
        parsed ``data: {...}`` events as dicts. ``[DONE]`` sentinel ends."""
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            verify=False,
        ) as c:
            async with c.stream(
                "POST",
                path,
                headers=self._headers(
                    {
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    }
                ),
                content=json.dumps(body),
            ) as resp:
                resp.raise_for_status()
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        raw, buf = buf.split("\n\n", 1)
                        data_lines = [
                            ln[5:].lstrip()
                            for ln in raw.split("\n")
                            if ln.startswith("data:")
                        ]
                        if not data_lines:
                            continue
                        payload = "\n".join(data_lines)
                        if payload == "[DONE]":
                            return
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                raise RuntimeError("SSE stream ended before [DONE]")


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


@pytest_asyncio.fixture(scope="session")
async def test_user() -> AsyncIterator[E2EUser]:
    """One authenticated user per session, shared across tests in the run."""
    user = await _session_user(
        token_env="A2A_E2E_BEARER_TOKEN",
        signup_prefix="e2e",
        source="bearer",
    )
    yield user


@pytest_asyncio.fixture(scope="session")
async def other_user() -> AsyncIterator[E2EUser]:
    """Second authenticated user for isolation checks."""
    user = await _session_user(
        token_env="A2A_E2E_OTHER_BEARER_TOKEN",
        signup_prefix="e2e-iso",
        source="other_bearer",
    )
    yield user


async def _session_user(
    *,
    token_env: str,
    signup_prefix: str,
    source: str,
) -> E2EUser:
    token = os.environ.get(token_env, "").strip()
    if token:
        return await _user_from_token(token, source=source)
    return await _signup_user(signup_prefix)


async def _user_from_token(token: str, *, source: str) -> E2EUser:
    async with httpx.AsyncClient(
        base_url=API_URL, timeout=DEFAULT_TIMEOUT, verify=False
    ) as c:
        r = await c.get("/v1/me", headers={"Authorization": f"bearer {token}"})
        r.raise_for_status()
        body = r.json()
    return E2EUser(
        email=str(body.get("email") or f"user-{body['id']}@example.invalid"),
        password=None,
        token=token,
        user_id=int(body["id"]),
        source=source,
    )


async def _signup_user(prefix: str) -> E2EUser:
    """Legacy local/dev fallback for stacks that still expose password signup."""
    suffix = f"{int(time.time())}-{secrets.token_hex(3)}"
    email = f"{prefix}-{suffix}@example.com"
    password = f"e2e-pw-{suffix}"
    async with httpx.AsyncClient(
        base_url=API_URL, timeout=DEFAULT_TIMEOUT, verify=False
    ) as c:
        r = await c.post(
            "/v1/auth/signup",
            json={"email": email, "password": password},
        )
        if r.status_code in {404, 405}:
            raise RuntimeError(
                f"{API_URL} does not expose legacy /v1/auth/signup; "
                "set A2A_E2E_BEARER_TOKEN and A2A_E2E_OTHER_BEARER_TOKEN."
            )
        r.raise_for_status()
        body = r.json()
    return E2EUser(
        email=email,
        password=password,
        token=body["access_token"],
        user_id=body["user"]["id"],
        source="legacy_signup",
    )


@pytest_asyncio.fixture
async def client(test_user: E2EUser) -> AsyncIterator[ApiClient]:
    api = ApiClient(API_URL, token=test_user.token)
    try:
        yield api
    finally:
        await api.aclose()


@pytest_asyncio.fixture
async def anon_client() -> AsyncIterator[ApiClient]:
    """No bearer token — for testing 401 paths."""
    api = ApiClient(API_URL, token=None)
    try:
        yield api
    finally:
        await api.aclose()
