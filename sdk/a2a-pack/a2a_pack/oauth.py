"""OAuth 2.1 resource-server helpers for the MCP transport (E1-P3).

Makes a deployed agent an OAuth *resource server* for the self-hosted Keycloak
realm: it serves Protected Resource Metadata (RFC 9728), answers unauthenticated
``/mcp`` calls with a ``WWW-Authenticate`` challenge that bootstraps discovery,
and validates Keycloak RS256 access tokens against the realm JWKS — including
per-resource ``aud`` binding (RFC 8707) so a token minted for agent A cannot be
replayed at agent B.

Identity (sub -> control-plane user) still resolves through the control plane's
``/v1/me`` (the P2 identity bridge); this module only adds token *validation* and
audience enforcement at the edge. All behaviour is env-driven so self-hosted
deployments without Keycloak are unaffected.
"""
from __future__ import annotations

import os
from typing import Any

# PyJWT is imported lazily inside the validation helpers so importing this
# module (for the PRM document, challenge header, or config) never hard-requires
# the dependency — only actual Keycloak token validation does.

_DEFAULT_ISSUER = "https://auth.a2acloud.io/realms/a2acloud"


def oauth_enabled() -> bool:
    return os.environ.get("A2A_OAUTH_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "",
    }


def oauth_issuer() -> str:
    return os.environ.get("A2A_OAUTH_ISSUER", _DEFAULT_ISSUER).rstrip("/")


def oauth_jwks_url() -> str:
    return (
        os.environ.get("A2A_OAUTH_JWKS_URL")
        or f"{oauth_issuer()}/protocol/openid-connect/certs"
    )


def oauth_scopes() -> list[str]:
    raw = os.environ.get("A2A_OAUTH_SCOPES", "mcp:invoke agent:read")
    return [s for s in raw.replace(",", " ").split() if s]


def require_audience() -> bool:
    """Whether a Keycloak token's ``aud`` MUST include this resource. Off by
    default until the connector's resource-indicator minting is confirmed in
    the P6 E2E; when off, an audience mismatch is logged but not rejected."""
    return os.environ.get("A2A_OAUTH_REQUIRE_AUDIENCE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def require_scopes() -> bool:
    """Whether MCP resource servers enforce method-level OAuth scopes."""
    return os.environ.get("A2A_OAUTH_REQUIRE_SCOPES", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def resource_id(base_url: str) -> str:
    """Canonical resource identifier for this agent (RFC 8707). Defaults to the
    agent's own public base URL; override with ``A2A_OAUTH_RESOURCE``."""
    return (os.environ.get("A2A_OAUTH_RESOURCE") or base_url).rstrip("/")


def protected_resource_metadata(resource: str) -> dict:
    """RFC 9728 Protected Resource Metadata document."""
    return {
        "resource": resource,
        "authorization_servers": [oauth_issuer()],
        "scopes_supported": oauth_scopes(),
        "bearer_methods_supported": ["header"],
    }


def www_authenticate(prm_url: str, *, error: str | None = None) -> str:
    parts = [f'Bearer resource_metadata="{prm_url}"']
    if error:
        parts.append(f'error="{error}"')
    return ", ".join(parts)


class OAuthError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


_jwks_client: Any = None


def _client() -> Any:
    global _jwks_client
    if _jwks_client is None:
        import jwt

        _jwks_client = jwt.PyJWKClient(oauth_jwks_url(), cache_keys=True)
    return _jwks_client


def is_keycloak_token(token: str) -> bool:
    """A Keycloak access token is RS256-signed; the CP login JWT is HS256."""
    import jwt

    try:
        return (jwt.get_unverified_header(token).get("alg") or "").upper().startswith(
            "RS"
        )
    except jwt.PyJWTError:
        return False


def token_scopes(claims: dict[str, Any]) -> set[str]:
    """Return normalized OAuth scopes from common JWT claim shapes."""
    raw = claims.get("scope")
    scopes: set[str] = set()
    if isinstance(raw, str):
        scopes.update(part for part in raw.replace(",", " ").split() if part)
    elif isinstance(raw, list):
        scopes.update(str(part) for part in raw if str(part))

    scp = claims.get("scp")
    if isinstance(scp, str):
        scopes.update(part for part in scp.replace(",", " ").split() if part)
    elif isinstance(scp, list):
        scopes.update(str(part) for part in scp if str(part))
    return scopes


def has_scope(claims: dict[str, Any], scope: str) -> bool:
    return scope in token_scopes(claims)


def _audience_values(claims: dict[str, Any]) -> list[str]:
    aud = claims.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list):
        return [str(item) for item in aud if str(item)]
    return []


def _looks_like_resource_audience(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def validate_keycloak_token(token: str, *, audience: str | None = None) -> dict:
    """Verify RS256 signature, issuer, expiry, subject, and audience binding.

    URL resource-audience replay is rejected by default. Tokens that only carry
    generic client audiences are tolerated unless
    ``A2A_OAUTH_REQUIRE_AUDIENCE=1`` is set.
    """
    import jwt

    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=oauth_issuer(),
            options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OAuthError(f"invalid token: {exc}") from exc

    if audience:
        aud_list = _audience_values(claims)
        # Always reject replay when the token already names a different URL
        # resource audience. Tokens with only generic client audiences remain
        # tolerated unless strict audience enforcement is enabled.
        has_resource_audience = any(_looks_like_resource_audience(item) for item in aud_list)
        if audience not in aud_list and (require_audience() or has_resource_audience):
            raise OAuthError(f"token audience does not include {audience!r}")
    return claims
