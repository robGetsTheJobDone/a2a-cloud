"""Pluggable auth principal models.

These describe *who* is invoking a skill. The runtime auth provider produces
an instance of the agent's declared ``auth_model`` and hands it to the
:class:`RunContext`.
"""
from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from http.cookies import SimpleCookie
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

AuthT = TypeVar("AuthT", bound=BaseModel)


class NoAuth(BaseModel):
    """Public agent: no caller identity required."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class APIKeyAuth(BaseModel):
    """Caller authenticated by a long-lived API key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key_id: str
    scopes: list[str] = Field(default_factory=list)


class JWTAuth(BaseModel):
    """Caller authenticated by a JWT (typically from a user-facing login)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub: str
    org_id: str | None = None
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)


class PlatformUserAuth(BaseModel):
    """Caller authenticated by the A2A platform session.

    Use this for packed frontend apps that should require a logged-in A2A
    Cloud user without wiring a custom OAuth/OIDC resolver.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub: str
    user_id: int | None = None
    email: str | None = None
    org_id: str | None = None
    org_slug: str | None = None
    scopes: list[str] = Field(default_factory=list)


class AuthError(Exception):
    """Raised by auth resolvers when a caller token is missing or invalid."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AuthResolver(Generic[AuthT]):
    """Resolve an inbound bearer token into an agent's typed auth principal.

    Agent authors set ``auth_model`` to the principal shape their skills want
    and optionally set ``auth_resolver`` to an instance of this class. The
    resolver may validate JWTs, call a customer API, hit an OIDC userinfo or
    introspection endpoint, or bridge any other identity system.
    """

    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> AuthT:
        raise NotImplementedError

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        """Stable principal identifier for receipts / replay sessions.

        Default returns ``""`` for resolvers that cannot derive a caller id.
        Subclasses override to surface the natural id of their auth model.
        """
        return ""


class NoAuthResolver(AuthResolver[NoAuth]):
    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> NoAuth:
        return NoAuth()

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        return ""


class APIKeyAuthResolver(AuthResolver[APIKeyAuth]):
    """Default resolver for ``auth_model = APIKeyAuth``.

    Requests must provide the configured bearer value. Public agents should use
    ``NoAuth`` rather than an API-key model without configured verification.
    """

    def __init__(
        self,
        *,
        accepted_key: str | None = None,
        api_key_id: str = "configured",
        scopes: list[str] | None = None,
    ) -> None:
        self.accepted_key = accepted_key
        self.api_key_id = api_key_id
        self.scopes = scopes or []

    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> APIKeyAuth:
        if self.accepted_key is None:
            raise AuthError("API key auth is not configured", status_code=500)
        if token is None:
            raise AuthError("missing bearer token")
        if token != self.accepted_key:
            raise AuthError("invalid bearer token")
        return APIKeyAuth(api_key_id=self.api_key_id, scopes=list(self.scopes))

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        """Return a short prefix of the api_key_id so receipts can pivot on it
        without leaking the full key material."""
        key_id = getattr(auth_principal, "api_key_id", "") or ""
        return f"apikey:{key_id[:12]}"


class StaticAuthResolver(AuthResolver[AuthT]):
    """Resolver for tests/local adapters that always returns one principal."""

    def __init__(self, principal: AuthT) -> None:
        self.principal = principal

    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> AuthT:
        return self.principal

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        return ""


class PlatformUserAuthResolver(AuthResolver[PlatformUserAuth]):
    """Resolve the user identity supplied by the A2A platform.

    Hosted deployments verify the HttpOnly platform session cookie against
    ``A2A_CP_URL/v1/me``. Trusted ``x-a2a-*`` identity headers are supported
    only when explicitly enabled for gateway deployments.
    """

    def __init__(
        self,
        *,
        allow_local_dev: bool = True,
        trust_headers: bool | None = None,
        cookie_name: str = "a2a_session",
    ) -> None:
        self.allow_local_dev = allow_local_dev
        self.trust_headers = trust_headers
        self.cookie_name = (
            os.environ.get("A2A_SESSION_COOKIE_NAME") or cookie_name
        )

    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> PlatformUserAuth:
        normalized = _lower_headers(headers)
        if self.allow_local_dev and os.environ.get("A2A_LOCAL_DEV") == "1":
            return PlatformUserAuth(
                sub="local-dev",
                email="dev@example.local",
                org_slug="local-dev",
                scopes=["agent:invoke", "files:read", "files:write"],
            )
        if self._trust_headers():
            header_auth = _platform_auth_from_headers(normalized)
            if header_auth is not None:
                return header_auth
        token = token or _token_from_cookie(normalized.get("cookie"), self.cookie_name)
        if token:
            cp_auth = await _platform_auth_from_cp_token(
                token, audience=_agent_audience(agent)
            )
            if cp_auth is not None:
                return cp_auth
        raise AuthError("platform session required")

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        user_id = getattr(auth_principal, "user_id", None)
        if user_id is not None:
            return str(user_id)
        return str(getattr(auth_principal, "sub", "") or "")

    def _trust_headers(self) -> bool:
        if self.trust_headers is not None:
            return self.trust_headers
        value = os.environ.get("A2A_TRUST_PLATFORM_HEADERS", "")
        return value.strip().lower() in {"1", "true", "yes"}


class RemoteBearerAuthResolver(AuthResolver[AuthT]):
    """Resolve bearer tokens by calling an external API.

    Works with OIDC ``userinfo`` endpoints and most homegrown
    ``GET /me``/``POST /introspect`` APIs. The response JSON is mapped into
    ``auth_model``. For SAML-backed apps, put your SAML/session exchange
    behind a bearer-token endpoint and use this same resolver.
    """

    def __init__(
        self,
        url: str,
        *,
        auth_model: type[AuthT],
        method: str = "GET",
        timeout_seconds: float = 5.0,
        token_header: str = "authorization",
        token_prefix: str = "bearer",
        active_field: str | None = "active",
        subject_field: str = "sub",
        email_field: str = "email",
        org_field: str = "org_id",
        scopes_field: str = "scope",
        extra_fields: Mapping[str, str] | None = None,
    ) -> None:
        self.url = url
        self.auth_model = auth_model
        self.method = method.upper()
        self.timeout_seconds = timeout_seconds
        self.token_header = token_header
        self.token_prefix = token_prefix
        self.active_field = active_field
        self.subject_field = subject_field
        self.email_field = email_field
        self.org_field = org_field
        self.scopes_field = scopes_field
        self.extra_fields = dict(extra_fields or {})

    async def resolve(
        self,
        token: str | None,
        *,
        headers: Mapping[str, str],
        agent: Any,
    ) -> AuthT:
        if token is None:
            raise AuthError("missing bearer token")
        import httpx

        request_headers = {
            self.token_header: f"{self.token_prefix} {token}".strip()
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            if self.method == "POST":
                resp = await client.post(self.url, headers=request_headers, data={"token": token})
            else:
                resp = await client.get(self.url, headers=request_headers)
        if resp.status_code in (401, 403):
            raise AuthError("invalid bearer token", status_code=resp.status_code)
        if resp.status_code >= 400:
            raise AuthError(
                f"auth resolver upstream returned HTTP {resp.status_code}",
                status_code=502,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise AuthError(
                "auth resolver returned invalid JSON",
                status_code=502,
            ) from exc
        if not isinstance(data, dict):
            raise AuthError("auth resolver returned non-object JSON", status_code=502)
        if self.active_field and data.get(self.active_field) is False:
            raise AuthError("inactive bearer token")
        try:
            return self.auth_model.model_validate(self._map_payload(data))
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"auth resolver payload did not match {self.auth_model.__name__}: {exc}",
                status_code=502,
            ) from exc

    @classmethod
    def principal_id(cls, auth_principal: Any) -> str:
        """Best-effort caller id: ``sub`` if present, else ``email``."""
        sub = getattr(auth_principal, "sub", "") or ""
        if sub:
            return str(sub)
        email = getattr(auth_principal, "email", "") or ""
        return str(email)

    def _map_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        sub = data.get(self.subject_field) or data.get("sub") or data.get("id")
        if sub is not None:
            payload["sub"] = str(sub)
        email = data.get(self.email_field)
        if email is not None:
            payload["email"] = str(email)
        org = data.get(self.org_field) or data.get("org") or data.get("organization_id")
        if org is not None:
            payload["org_id"] = str(org)
        scopes = data.get(self.scopes_field)
        if isinstance(scopes, str):
            payload["scopes"] = [s for s in scopes.split() if s]
        elif isinstance(scopes, list):
            payload["scopes"] = [str(s) for s in scopes]
        for target, source in self.extra_fields.items():
            if source in data:
                payload[target] = data[source]
        # Preserve exact matching fields for custom auth models.
        for key in self.auth_model.model_fields:
            if key in data and key not in payload:
                payload[key] = data[key]
        return payload


OIDCUserInfoAuthResolver = RemoteBearerAuthResolver


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _platform_auth_from_headers(headers: Mapping[str, str]) -> PlatformUserAuth | None:
    sub = _first_header(
        headers,
        "x-a2a-user-id",
        "x-a2a-user",
        "x-auth-request-user",
    )
    email = _first_header(
        headers,
        "x-a2a-user-email",
        "x-auth-request-email",
    )
    org_slug = _first_header(
        headers,
        "x-a2a-org",
        "x-auth-request-preferred-username",
    )
    org_id = _first_header(headers, "x-a2a-org-id")
    raw_scopes = _first_header(
        headers,
        "x-a2a-scopes",
        "x-a2a-scope",
        "x-auth-request-groups",
    )
    if not sub and not email:
        return None
    user_id = _parse_int(sub)
    return PlatformUserAuth(
        sub=str(sub or email),
        user_id=user_id,
        email=email,
        org_id=org_id,
        org_slug=org_slug,
        scopes=_split_scopes(raw_scopes),
    )


def _agent_audience(agent: Any) -> str | None:
    name = getattr(type(agent), "name", None) if agent is not None else None
    if not name and agent is not None:
        name = getattr(agent, "name", None)
    if not name:
        name = os.environ.get("A2A_AGENT_NAME")
    value = str(name or "").strip()
    return value or None


async def _platform_auth_from_cp_token(
    token: str, *, audience: str | None = None
) -> PlatformUserAuth | None:
    cp_url = os.environ.get("A2A_CP_URL")
    if not cp_url:
        return None
    import httpx

    # Origin-bound agent sessions are deliberately rejected by ``/v1/me`` — a
    # token scoped to one agent must not read the whole platform. Ask the
    # audience-checked endpoint instead, which also still accepts ordinary
    # bearer credentials from CLI/API callers.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if audience:
                resp = await client.post(
                    f"{cp_url.rstrip('/')}/v1/platform/agent-session/verify",
                    json={"token": token, "audience": audience},
                )
            else:
                resp = await client.get(
                    f"{cp_url.rstrip('/')}/v1/me",
                    headers={"authorization": f"bearer {token}"},
                )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    user_id = _parse_int(data.get("id"))
    email = data.get("email")
    if user_id is None and email is None:
        return None
    return PlatformUserAuth(
        sub=str(user_id if user_id is not None else email),
        user_id=user_id,
        email=str(email) if email is not None else None,
        scopes=["agent:invoke"],
    )


def _first_header(headers: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = headers.get(name)
        if value:
            return value.strip()
    return None


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _split_scopes(raw: str | None) -> list[str]:
    if not raw:
        return []
    normalized = raw.replace(",", " ")
    return [scope for scope in normalized.split() if scope]


def _token_from_cookie(raw_cookie: str | None, name: str) -> str | None:
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:  # noqa: BLE001
        return None
    morsel = cookie.get(name)
    if morsel is None:
        return None
    return morsel.value.strip() or None


async def resolve_auth(
    resolver: AuthResolver[AuthT],
    token: str | None,
    *,
    headers: Mapping[str, str],
    agent: Any,
) -> AuthT:
    """Call sync or async custom resolver implementations."""
    result = resolver.resolve(token, headers=headers, agent=agent)
    if inspect.isawaitable(result):
        return await result
    return result
