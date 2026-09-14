"""E1-P3: agent as OAuth resource server — PRM, token validation, aud binding."""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from a2a_pack import oauth

ISSUER = "https://auth.a2acloud.io/realms/a2acloud"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _token(priv, *, aud, iss=ISSUER, exp_delta=300, sub="user-1", scope="mcp:invoke agent:read"):
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub,
            "iss": iss,
            "iat": now,
            "exp": now + exp_delta,
            "aud": aud,
            "scope": scope,
        },
        priv,
        algorithm="RS256",
    )


@pytest.fixture
def signing(monkeypatch):
    priv, pub = _keypair()

    class _Key:
        key = pub

    class _Client:
        def get_signing_key_from_jwt(self, _token):
            return _Key()

    monkeypatch.setattr(oauth, "_jwks_client", _Client())
    monkeypatch.setenv("A2A_OAUTH_ISSUER", ISSUER)
    return priv


def test_prm_document_points_at_realm():
    prm = oauth.protected_resource_metadata("https://tasks-api.a2acloud.io")
    assert prm["resource"] == "https://tasks-api.a2acloud.io"
    assert prm["authorization_servers"] == [ISSUER]
    assert "mcp:invoke" in prm["scopes_supported"]


def test_is_keycloak_token_detects_rs256(signing):
    rs = _token(signing, aud="x")
    hs = jwt.encode({"sub": "1"}, "secret", algorithm="HS256")
    assert oauth.is_keycloak_token(rs) is True
    assert oauth.is_keycloak_token(hs) is False


def test_valid_token_passes(signing):
    claims = oauth.validate_keycloak_token(
        _token(signing, aud="https://tasks-api.a2acloud.io"),
        audience="https://tasks-api.a2acloud.io",
    )
    assert claims["sub"] == "user-1"


def test_expired_token_rejected(signing):
    with pytest.raises(oauth.OAuthError):
        oauth.validate_keycloak_token(_token(signing, aud="x", exp_delta=-10))


def test_wrong_issuer_rejected(signing):
    with pytest.raises(oauth.OAuthError):
        oauth.validate_keycloak_token(_token(signing, aud="x", iss="https://evil/realms/x"))


def test_resource_audience_replay_rejected_by_default(signing):
    # Token minted for agent A; we are agent B.
    tok = _token(signing, aud="https://agent-a.a2acloud.io")
    with pytest.raises(oauth.OAuthError):
        oauth.validate_keycloak_token(tok, audience="https://agent-b.a2acloud.io")


def test_generic_audience_enforced_only_when_required(signing, monkeypatch):
    tok = _token(signing, aud="account")
    oauth.validate_keycloak_token(tok, audience="https://agent-b.a2acloud.io")

    monkeypatch.setenv("A2A_OAUTH_REQUIRE_AUDIENCE", "1")
    with pytest.raises(oauth.OAuthError):
        oauth.validate_keycloak_token(tok, audience="https://agent-b.a2acloud.io")


def test_token_scopes_parse_scope_and_scp_claims():
    assert oauth.token_scopes({"scope": "mcp:invoke agent:read"}) == {
        "mcp:invoke",
        "agent:read",
    }
    assert oauth.token_scopes({"scp": ["orchestrator:run", "agent:read"]}) == {
        "orchestrator:run",
        "agent:read",
    }


def test_www_authenticate_header_format():
    h = oauth.www_authenticate(
        "https://tasks-api.a2acloud.io/.well-known/oauth-protected-resource",
        error="invalid_token",
    )
    assert h.startswith("Bearer resource_metadata=")
    assert 'error="invalid_token"' in h
