from __future__ import annotations

import asyncio
import ipaddress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from control_plane import safe_http
from control_plane import imported_agent_auth, mail_ingress
from control_plane.imported_agent_auth import ExternalRequestAuth, ImportedAgentAuthError
from control_plane.openapi_agent import OpenAPIGenerationError, fetch_openapi_spec
from control_plane.models import Agent
from control_plane.routes import agents, llm_creds


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://10.20.30.40/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/secret",
        "http://[fe80::1]/secret",
        "http://[64:ff9b::7f00:1]/secret",
        "http://localhost/secret",
        "http://service.localhost/secret",
    ],
)
async def test_validate_public_url_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(safe_http.UnsafeURLError):
        await safe_http._validate_public_url(url)


async def test_validate_public_url_rejects_mixed_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mixed_answers(_hostname: str, _port: int):
        return (
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("10.0.0.7"),
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", mixed_answers)

    with pytest.raises(safe_http.UnsafeURLError, match="non-public"):
        await safe_http._validate_public_url("https://docs.example.test/openapi.json")


async def test_safe_fetch_validates_each_redirect_before_requesting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def answers(hostname: str, _port: int):
        if hostname == "public.example.test":
            return (ipaddress.ip_address("93.184.216.34"),)
        return (ipaddress.ip_address("169.254.169.254"),)

    async def fake_request(target, **_kwargs):
        requested.append(target.url)
        return safe_http._NetworkResponse(
            status_code=302,
            headers={"location": "http://metadata.example.test/latest"},
            content=b"",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    with pytest.raises(safe_http.UnsafeURLError, match="non-public"):
        await safe_http.safe_fetch_url(
            "https://public.example.test/spec",
            max_response_bytes=1024,
            timeout_seconds=1,
        )

    assert requested == ["https://public.example.test/spec"]


async def test_safe_fetch_connects_to_the_validated_numeric_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = ipaddress.ip_address("93.184.216.34")
    captured: dict[str, object] = {}

    async def answers(_hostname: str, _port: int):
        return (resolved,)

    async def fake_request_address(target, address, **_kwargs):
        captured["target"] = target
        captured["address"] = address
        return safe_http._NetworkResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"ok": true}',
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_address", fake_request_address)

    response = await safe_http.safe_fetch_url(
        "https://api.example.test/openapi.json",
        max_response_bytes=1024,
        timeout_seconds=1,
    )

    assert captured["address"] == resolved
    assert captured["target"].hostname == "api.example.test"
    assert response.url == "https://api.example.test/openapi.json"


async def test_safe_response_url_never_exposes_query_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(target, **_kwargs):
        requested_urls.append(target.url)
        return safe_http._NetworkResponse(
            status_code=200,
            headers={},
            content=b"{}",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    response = await safe_http.safe_fetch_url(
        "https://agent.example.test/card?visible=value",
        params={"api_key": "must-not-persist"},
        max_response_bytes=1024,
        timeout_seconds=1,
    )

    assert "must-not-persist" in requested_urls[0]
    assert response.url == "https://agent.example.test/card"


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"sensitive_headers": {"Authorization": "Bearer must-not-send"}},
        {"params": {"api_key": "must-not-send"}},
        {"client_options": {"cert": ("client.crt", "client.key")}},
    ],
)
async def test_sensitive_get_rejects_cleartext_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    request_kwargs: dict[str, object],
) -> None:
    resolved: list[str] = []

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext sensitive request must fail before DNS")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)

    with pytest.raises(safe_http.InsecureTransportError, match="HTTPS"):
        await safe_http.safe_request_url(
            "GET",
            "http://agent.example.test/card",
            max_response_bytes=1024,
            timeout_seconds=1,
            **request_kwargs,
        )

    assert resolved == []


async def test_post_payload_rejects_cleartext_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext POST must fail before DNS")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)

    with pytest.raises(safe_http.InsecureTransportError, match="HTTPS"):
        await safe_http.safe_request_url(
            "POST",
            "http://agent.example.test/invoke/echo",
            json_body={"arguments": {"secret_input": "must-not-send"}},
            max_response_bytes=1024,
            timeout_seconds=1,
        )

    assert resolved == []


async def test_embedded_query_rejects_cleartext_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext query must fail before DNS")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)

    with pytest.raises(safe_http.InsecureTransportError, match="HTTPS"):
        await safe_http.safe_fetch_url(
            "http://agent.example.test/card?api_key=must-not-send",
            max_response_bytes=1024,
            timeout_seconds=1,
        )

    assert resolved == []


async def test_sensitive_request_rejects_cleartext_redirect_before_second_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []
    requested: list[str] = []

    async def answers(hostname: str, _port: int):
        resolved.append(hostname)
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(target, **_kwargs):
        requested.append(target.url)
        return safe_http._NetworkResponse(
            status_code=302,
            headers={"location": "http://other.example.test/card"},
            content=b"",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    with pytest.raises(safe_http.InsecureTransportError, match="HTTPS"):
        await safe_http.safe_fetch_url(
            "https://agent.example.test/card",
            sensitive_headers={"Authorization": "Bearer must-not-send"},
            max_response_bytes=1024,
            timeout_seconds=1,
        )

    assert resolved == ["agent.example.test"]
    assert requested == ["https://agent.example.test/card"]


async def test_agent_card_url_does_not_persist_query_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(_target, **_kwargs):
        return safe_http._NetworkResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"name":"remote","skills":[{"name":"echo"}]}',
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    _card, card_url = await agents._fetch_card_from_base_url(
        "https://agent.example.test",
        request_auth=({}, {"api_key": "must-not-persist"}),
    )

    assert card_url == "https://agent.example.test/.well-known/agent-card"
    assert "must-not-persist" not in card_url


async def test_request_uses_numeric_url_with_original_host_and_tls_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        headers = httpx.Headers({"content-type": "application/json"})
        encoding = "utf-8"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_bytes(self):
            yield b"{}"

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(safe_http.httpx, "AsyncClient", FakeClient)
    target = safe_http._ValidatedURL(
        url="https://api.example.test/openapi.json?version=3",
        scheme="https",
        hostname="api.example.test",
        port=443,
        authority="api.example.test",
        addresses=(ipaddress.ip_address("93.184.216.34"),),
    )

    await safe_http._request_address(
        target,
        ipaddress.ip_address("93.184.216.34"),
        headers={"accept": "application/json"},
        max_response_bytes=1024,
        timeout_seconds=1,
    )

    assert captured["url"] == "https://93.184.216.34:443/openapi.json?version=3"
    request_kwargs = captured["request_kwargs"]
    assert request_kwargs["headers"]["host"] == "api.example.test"
    assert request_kwargs["extensions"] == {"sni_hostname": "api.example.test"}
    assert captured["client_kwargs"]["trust_env"] is False


async def test_cross_origin_redirect_drops_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(_target, *, headers, **_kwargs):
        seen_headers.append(dict(headers))
        if len(seen_headers) == 1:
            return safe_http._NetworkResponse(
                status_code=302,
                headers={
                    "location": "https://other.example.test/card?echo=Bearer-secret"
                },
                content=b"",
                encoding="utf-8",
            )
        return safe_http._NetworkResponse(
            status_code=200,
            headers={},
            content=b"{}",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    response = await safe_http.safe_fetch_url(
        "https://agent.example.test/card",
        headers={"Accept": "application/json"},
        sensitive_headers={"Authorization": "Bearer secret"},
        max_response_bytes=1024,
        timeout_seconds=1,
    )

    assert seen_headers == [
        {"Authorization": "Bearer secret", "Accept": "application/json"},
        {"Accept": "application/json"},
    ]
    assert response.url == "https://other.example.test/card"


async def test_cross_origin_redirect_rejects_query_credentials_before_second_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(target, **_kwargs):
        requested.append(target.url)
        return safe_http._NetworkResponse(
            status_code=302,
            headers={
                "location": "https://other.example.test/card?api_key=must-not-send"
            },
            content=b"",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    with pytest.raises(safe_http.UnsafeURLError, match="query credentials"):
        await safe_http.safe_fetch_url(
            "https://agent.example.test/card",
            params={"api_key": "must-not-send"},
            max_response_bytes=1024,
            timeout_seconds=1,
        )

    assert requested == ["https://agent.example.test/card?api_key=must-not-send"]


async def test_cross_origin_redirect_drops_sensitive_safe_named_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(_target, *, headers, **_kwargs):
        seen_headers.append(dict(headers))
        if len(seen_headers) == 1:
            return safe_http._NetworkResponse(
                status_code=302,
                headers={"location": "https://other.example.test/card"},
                content=b"",
                encoding="utf-8",
            )
        return safe_http._NetworkResponse(
            status_code=200,
            headers={},
            content=b"{}",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    await safe_http.safe_fetch_url(
        "https://agent.example.test/card",
        sensitive_headers={"Accept": "secret-api-key"},
        max_response_bytes=1024,
        timeout_seconds=1,
    )

    assert seen_headers == [{"Accept": "secret-api-key"}, {}]


async def test_credential_post_does_not_follow_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(_target, **kwargs):
        requests.append(kwargs)
        return safe_http._NetworkResponse(
            status_code=307,
            headers={"location": "https://redirect.example.test/token"},
            content=b"",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    with pytest.raises(safe_http.TooManyRedirectsError):
        await safe_http.safe_request_url(
            "POST",
            "https://auth.example.test/token",
            sensitive_headers={"authorization": "Basic secret"},
            form_data={"refresh_token": "refresh-secret"},
            max_response_bytes=1024,
            timeout_seconds=1,
            max_redirects=0,
        )

    assert len(requests) == 1
    assert requests[0]["form_data"] == {"refresh_token": "refresh-secret"}


class _StreamingResponse:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None) -> None:
        self.headers = httpx.Headers()
        if content_length is not None:
            self.headers["content-length"] = content_length
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


async def test_response_limit_checks_declared_and_streamed_size() -> None:
    with pytest.raises(safe_http.ResponseTooLargeError):
        await safe_http._read_limited_body(
            _StreamingResponse([], content_length="101"),  # type: ignore[arg-type]
            100,
        )

    with pytest.raises(safe_http.ResponseTooLargeError):
        await safe_http._read_limited_body(
            _StreamingResponse([b"a" * 60, b"b" * 41]),  # type: ignore[arg-type]
            100,
        )


async def test_safe_fetch_enforces_total_wall_clock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def slow_request(_target, **_kwargs):
        await asyncio.sleep(1)
        raise AssertionError("request should have timed out")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", slow_request)

    with pytest.raises(safe_http.SafeHTTPTimeoutError):
        await safe_http.safe_fetch_url(
            "https://api.example.test/spec",
            max_response_bytes=1024,
            timeout_seconds=0.01,
        )


async def test_openapi_fetch_rejects_private_url() -> None:
    with pytest.raises(OpenAPIGenerationError, match="non-public"):
        await fetch_openapi_spec("http://169.254.169.254/openapi.json")


async def test_agent_card_fetch_rejects_private_url() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await agents._fetch_card_from_base_url("http://127.0.0.1:8000")

    assert exc_info.value.status_code == 400
    assert "non-public" in str(exc_info.value.detail)


async def test_external_invocation_rebinding_is_blocked_before_secrets_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    async def rebound_to_private(_hostname: str, _port: int):
        return (ipaddress.ip_address("127.0.0.1"),)

    async def request_address(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("private target must not be requested")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", rebound_to_private)
    monkeypatch.setattr(safe_http, "_request_address", request_address)

    with pytest.raises(safe_http.UnsafeURLError):
        await agents._call_external_invoke(
            base_url="https://imported.example.test",
            skill_name="echo",
            arguments={"secret_input": "must-not-send"},
            request_auth=ExternalRequestAuth(
                headers={"authorization": "Bearer must-not-send"},
                params={"api_key": "must-not-send"},
            ),
        )

    assert sent == []


async def test_external_invocation_rejects_cleartext_before_payload_or_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext external invoke must fail before DNS")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)

    with pytest.raises(safe_http.InsecureTransportError, match="HTTPS"):
        await agents._call_external_invoke(
            base_url="http://imported.example.test",
            skill_name="echo",
            arguments={"secret_input": "must-not-send"},
            request_auth=ExternalRequestAuth(
                headers={"authorization": "Bearer must-not-send"},
                params={"api_key": "must-not-send"},
            ),
        )

    assert resolved == []


async def test_authenticated_card_fetch_rejects_cleartext_before_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext authenticated fetch must fail before DNS")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)

    with pytest.raises(HTTPException) as exc_info:
        await agents._fetch_card_from_base_url(
            "http://agent.example.test",
            request_auth=({}, {"api_key": "must-not-send"}),
        )

    assert exc_info.value.status_code == 400
    assert "HTTPS" in str(exc_info.value.detail)
    assert resolved == []


async def test_mail_ingress_rebinding_is_blocked_before_owner_token_or_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []

    async def rebound_to_private(_hostname: str, _port: int):
        return (ipaddress.ip_address("10.0.0.9"),)

    async def request_address(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("private mail target must not be requested")

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", rebound_to_private)
    monkeypatch.setattr(safe_http, "_request_address", request_address)
    agent = Agent(
        owner_id=1,
        name="mail-import",
        image="external-a2a:https://mail-import.example.test",
        url="https://mail-import.example.test",
        card={
            "skills": [
                {"id": "handle_email", "tags": [mail_ingress.EMAIL_HANDLER_TAG]}
            ]
        },
    )
    email = mail_ingress.InboundEmail(
        uid=1,
        sender="sender@example.test",
        subject="Private target test",
        message_id="message-1",
        references=(),
        in_reply_to=None,
        auto_submitted=False,
        body="must not send",
    )

    with pytest.raises(safe_http.UnsafeURLError):
        await mail_ingress._invoke_agent_with_email(agent, email)

    assert sent == []


async def test_oauth_refresh_rejects_private_token_url_before_sending_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []
    connection = SimpleNamespace(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        metadata_json={"token_url": "https://169.254.169.254/token"},
        status="connected",
        secret_ciphertext="unused",
    )

    class Session:
        flushed = False

        async def flush(self):
            self.flushed = True

    async def request_address(*args, **kwargs):
        sent.append((args, kwargs))
        raise AssertionError("private token URL must not be requested")

    monkeypatch.setattr(
        imported_agent_auth,
        "_decrypt_payload",
        lambda _connection: {
            "refresh_token": "refresh-must-not-send",
            "client_id": "client-id",
            "client_secret": "client-must-not-send",
        },
    )
    monkeypatch.setattr(safe_http, "_request_address", request_address)
    session = Session()

    with pytest.raises(ImportedAgentAuthError, match="not public"):
        await imported_agent_auth.refresh_oauth_if_needed(session, connection)

    assert sent == []
    assert connection.status == "failed"
    assert session.flushed is True


async def test_oauth_refresh_rejects_public_cleartext_before_sending_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved: list[str] = []
    connection = SimpleNamespace(
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        metadata_json={"token_url": "http://auth.example.test/token"},
        status="connected",
        secret_ciphertext="unused",
    )

    class Session:
        flushed = False

        async def flush(self):
            self.flushed = True

    async def unexpected_resolution(hostname: str, _port: int):
        resolved.append(hostname)
        raise AssertionError("cleartext OAuth refresh must fail before DNS")

    monkeypatch.setattr(
        imported_agent_auth,
        "_decrypt_payload",
        lambda _connection: {
            "refresh_token": "refresh-must-not-send",
            "client_id": "client-id",
            "client_secret": "client-must-not-send",
        },
    )
    monkeypatch.setattr(safe_http, "_resolve_host_addresses", unexpected_resolution)
    session = Session()

    with pytest.raises(ImportedAgentAuthError, match="must use HTTPS"):
        await imported_agent_auth.refresh_oauth_if_needed(session, connection)

    assert resolved == []
    assert connection.status == "failed"
    assert connection.metadata_json["last_error"] == "OAuth token URL must use HTTPS"
    assert session.flushed is True


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:11434/v1", "http://169.254.169.254/latest/v1"],
)
async def test_llm_model_verification_rejects_private_urls(base_url: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await llm_creds._verify_model_available(base_url, "secret-key", "model")

    assert exc_info.value.status_code == 400
    assert "public IP" in str(exc_info.value.detail)


async def test_llm_model_verification_rejects_mixed_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def mixed_answers(_hostname: str, _port: int):
        return (
            ipaddress.ip_address("93.184.216.34"),
            ipaddress.ip_address("10.0.0.5"),
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", mixed_answers)

    with pytest.raises(HTTPException) as exc_info:
        await llm_creds._verify_model_available(
            "https://models.example.test/v1", "secret-key", "model"
        )

    assert exc_info.value.status_code == 400


async def test_llm_model_redirect_does_not_forward_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_headers: list[dict[str, str]] = []

    async def answers(_hostname: str, _port: int):
        return (ipaddress.ip_address("93.184.216.34"),)

    async def fake_request(_target, *, headers, **_kwargs):
        seen_headers.append(dict(headers))
        if len(seen_headers) == 1:
            return safe_http._NetworkResponse(
                status_code=302,
                headers={"location": "https://cdn.example.test/models"},
                content=b"",
                encoding="utf-8",
            )
        return safe_http._NetworkResponse(
            status_code=404,
            headers={},
            content=b"",
            encoding="utf-8",
        )

    monkeypatch.setattr(safe_http, "_resolve_host_addresses", answers)
    monkeypatch.setattr(safe_http, "_request_validated_url", fake_request)

    await llm_creds._verify_model_available(
        "https://models.example.test/v1", "secret-key", "model"
    )

    assert seen_headers[0]["Authorization"] == "Bearer secret-key"
    assert "Authorization" not in seen_headers[1]


async def test_legacy_stored_private_llm_url_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        base_url="http://127.0.0.1:11434/v1",
        api_key="legacy-secret",
        model="local-model",
        temperature_mode="omit",
        temperature=None,
        extra_body={},
    )

    class Result:
        def scalar_one_or_none(self):
            return row

    class Session:
        async def execute(self, _query):
            return Result()

    with pytest.raises(HTTPException) as exc_info:
        await llm_creds.get_creds_for_user(1, Session())

    assert exc_info.value.status_code == 400
    assert "curated HTTPS provider" in str(exc_info.value.detail)
