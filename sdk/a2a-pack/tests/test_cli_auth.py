from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from typer.testing import CliRunner

from a2a_pack.cli import credentials, main
from a2a_pack.cli import oauth_login
from a2a_pack.cli.oauth_login import build_authorization_url, create_pkce_pair


runner = CliRunner()


def test_credentials_preserve_keycloak_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    credentials.save(
        "https://api.example.test",
        "access-token",
        "dev@example.test",
        user_id=7,
        bucket="user-7-files",
        refresh_token="refresh-token",
        expires_at=12345,
        oauth_issuer="https://auth.example.test/realms/a2a",
        client_id="a2acloud-cli",
        scope="openid email mcp:invoke",
    )

    loaded = credentials.load()
    assert loaded is not None
    assert loaded.api_url == "https://api.example.test"
    assert loaded.token == "access-token"
    assert loaded.email == "dev@example.test"
    assert loaded.user_id == 7
    assert loaded.bucket == "user-7-files"
    assert loaded.refresh_token == "refresh-token"
    assert loaded.expires_at == 12345
    assert loaded.oauth_issuer == "https://auth.example.test/realms/a2a"
    assert loaded.client_id == "a2acloud-cli"
    assert loaded.scope == "openid email mcp:invoke"


def test_pkce_authorization_url_requests_keycloak_code_flow():
    verifier, challenge = create_pkce_pair()
    assert len(verifier) > 40
    assert len(challenge) > 40

    url = build_authorization_url(
        authorization_endpoint="https://auth.example.test/auth",
        client_id="a2acloud-cli",
        redirect_uri="http://127.0.0.1:41873/callback",
        scope="openid email mcp:invoke",
        state="state-123",
        code_challenge=challenge,
    )
    params = parse_qs(urlparse(url).query)

    assert params["client_id"] == ["a2acloud-cli"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid email mcp:invoke"]


def test_login_token_path_uses_keycloak_access_token(monkeypatch):
    def fake_login_with_access_token(token: str, **kwargs):
        assert token == "access-token"
        assert kwargs["api_url"] == "https://api.example.test"
        assert kwargs["issuer"] == "https://auth.example.test/realms/a2a"
        assert kwargs["client_id"] == "a2acloud-cli"
        assert kwargs["scope"] == "openid email"
        return credentials.Credentials(
            api_url=kwargs["api_url"],
            token=token,
            email="dev@example.test",
        )

    monkeypatch.setattr(main, "login_with_access_token", fake_login_with_access_token)

    result = runner.invoke(
        main.app,
        [
            "login",
            "--token",
            "access-token",
            "--api",
            "https://api.example.test",
            "--issuer",
            "https://auth.example.test/realms/a2a",
            "--client-id",
            "a2acloud-cli",
            "--scope",
            "openid email",
            "--no-open",
        ],
    )

    assert result.exit_code == 0
    assert "logged in" in result.output
    assert "dev@example.test" in result.output


def test_refresh_credentials_if_needed_refreshes_expired_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    credentials.save(
        "https://api.example.test",
        "old-token",
        "dev@example.test",
        refresh_token="refresh-token",
        expires_at=1,
        oauth_issuer="https://auth.example.test/realms/a2a",
        client_id="a2acloud-cli",
        scope="openid email",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            assert url == "https://auth.example.test/realms/a2a/.well-known/openid-configuration"
            return oauth_login.httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.example.test/auth",
                    "token_endpoint": "https://auth.example.test/token",
                },
            )

        def post(self, url, data=None, **kwargs):
            assert url == "https://auth.example.test/token"
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "refresh-token"
            return oauth_login.httpx.Response(
                200,
                json={
                    "access_token": "fresh-token",
                    "refresh_token": "fresh-refresh-token",
                    "expires_in": 3600,
                    "scope": "openid email",
                },
            )

    monkeypatch.setattr(oauth_login.httpx, "Client", FakeClient)

    refreshed = oauth_login.refresh_credentials_if_needed(credentials.load())

    assert refreshed.token == "fresh-token"
    assert refreshed.refresh_token == "fresh-refresh-token"
    assert credentials.load().token == "fresh-token"


def test_refresh_credentials_if_needed_reports_expired_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    credentials.save(
        "https://api.example.test",
        "old-token",
        "dev@example.test",
        refresh_token="refresh-token",
        expires_at=1,
        oauth_issuer="https://auth.example.test/realms/a2a",
        client_id="a2acloud-cli",
    )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            return oauth_login.httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://auth.example.test/auth",
                    "token_endpoint": "https://auth.example.test/token",
                },
            )

        def post(self, url, data=None, **kwargs):
            return oauth_login.httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Session doesn't have required client",
                },
            )

    monkeypatch.setattr(oauth_login.httpx, "Client", FakeClient)

    try:
        oauth_login.refresh_credentials_if_needed(credentials.load())
    except RuntimeError as exc:
        assert "Login expired or revoked" in str(exc)
        assert "a2a login" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
