from __future__ import annotations

import pytest
from fastapi import HTTPException, Request

from control_plane.auth import enforce_browser_session_csrf
from control_plane.config import settings


def _request(
    *,
    method: str = "POST",
    origin: str | None = None,
    referer: str | None = None,
    cookie_name: str | None = None,
    authorization: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    if cookie_name is not None:
        headers.append((b"cookie", f"{cookie_name}=signed-session".encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/v1/me/files",
            "raw_path": b"/v1/me/files",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("app.a2acloud.io", 443),
        }
    )


def test_cookie_authenticated_mutation_requires_dashboard_origin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashboard_url", "https://app.a2acloud.io")
    request = _request(
        origin="https://evil-agent.a2acloud.io",
        cookie_name=settings.session_cookie_name,
    )

    with pytest.raises(HTTPException) as raised:
        enforce_browser_session_csrf(request)

    assert raised.value.status_code == 403


@pytest.mark.parametrize("origin", [None, "null", "https://app.a2acloud.io.evil.test"])
def test_cookie_authenticated_mutation_rejects_missing_or_lookalike_origin(
    monkeypatch,
    origin,
) -> None:
    monkeypatch.setattr(settings, "dashboard_url", "https://app.a2acloud.io")

    with pytest.raises(HTTPException):
        enforce_browser_session_csrf(
            _request(origin=origin, cookie_name=settings.session_cookie_name)
        )


def test_dashboard_origin_and_referer_are_accepted(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashboard_url", "https://app.a2acloud.io")
    enforce_browser_session_csrf(
        _request(
            origin="https://app.a2acloud.io",
            cookie_name=settings.session_cookie_name,
        )
    )
    enforce_browser_session_csrf(
        _request(
            referer="https://app.a2acloud.io/workspace",
            cookie_name=settings.session_cookie_name,
        )
    )


def test_bearer_and_safe_method_requests_are_exempt(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashboard_url", "https://app.a2acloud.io")
    enforce_browser_session_csrf(
        _request(
            origin="https://evil-agent.a2acloud.io",
            cookie_name=settings.session_cookie_name,
            authorization="Bearer platform-token",
        )
    )
    enforce_browser_session_csrf(
        _request(
            method="GET",
            origin="https://evil-agent.a2acloud.io",
            cookie_name=settings.session_cookie_name,
        )
    )


def test_legacy_parent_domain_cookie_is_also_protected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "dashboard_url", "https://app.a2acloud.io")

    with pytest.raises(HTTPException):
        enforce_browser_session_csrf(
            _request(origin="https://evil-agent.a2acloud.io", cookie_name="a2a_session")
        )
