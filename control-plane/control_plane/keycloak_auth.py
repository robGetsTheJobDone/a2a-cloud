"""Keycloak OAuth identity bridge (E1-P2).

Validates Keycloak-issued RS256 access tokens against the realm JWKS and maps
the token ``sub`` to a control-plane :class:`User`, provisioning one on first
login. This is what makes ``token -> user_id -> bucket -> policy`` work for
external MCP clients (ChatGPT/Claude) once the leaf agents and orchestrator
become OAuth resource servers (P3/P4).

The CP's own HS256 login JWT is untouched — :func:`current_user` only routes a
token here when its header ``alg`` is RS* and Keycloak is enabled.
"""
from __future__ import annotations

import secrets

import bcrypt
import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .analytics import identify_profile, track_event
from .config import settings
from .models import KeycloakIdentity, User
from .org_provisioning import create_personal_organization

_jwks_client: jwt.PyJWKClient | None = None


def _client() -> jwt.PyJWKClient:
    """Lazily build a cached JWKS client. PyJWKClient caches signing keys, so
    repeated validations don't re-fetch the JWKS on every request."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(settings.keycloak_jwks_url, cache_keys=True)
    return _jwks_client


def verify_keycloak_token(token: str) -> dict:
    """Verify signature (RS256 via JWKS), issuer, and expiry. Audience is NOT
    verified here — per-resource ``aud`` binding (RFC 8707) is enforced at each
    MCP resource server in P3, not at the control-plane identity bridge."""
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.keycloak_issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid keycloak token: {exc}") from exc
    return claims


def verify_keycloak_id_token(token: str, *, audience: str) -> dict:
    """Verify a browser-login ID token for the dashboard OIDC client."""
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.keycloak_issuer,
            audience=audience,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid keycloak id token: {exc}") from exc
    return claims


async def provision_user_from_claims(
    session: AsyncSession, claims: dict, request: Request | None = None
) -> User:
    """Resolve (or create) the CP user for a validated Keycloak token.

    1. Known ``sub`` -> return the linked user.
    2. Else link by verified email if a user with that email exists.
    3. Else create a fresh user (random password — auth is Keycloak-managed)
       with a personal organization for the first Keycloak login.

    ``request`` is the inbound browser request when available (OIDC callback);
    it enriches the signup analytics event with geo/device/fingerprint.
    """
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(401, "keycloak token missing sub")
    email = (claims.get("email") or "").strip().lower() or None
    email_verified = claims.get("email_verified") is True

    ident = (
        await session.execute(
            select(KeycloakIdentity).where(KeycloakIdentity.keycloak_sub == sub)
        )
    ).scalar_one_or_none()
    if ident is not None:
        user = await session.get(User, ident.user_id)
        if user is not None:
            # A token from an email-less client may have created this user with
            # a kc-<sub>@keycloak.a2acloud.io placeholder. Adopt the real email
            # the first time a verified one shows up, unless another user
            # already owns it.
            if (
                email
                and email_verified
                and user.email != email
                and user.email.endswith("@keycloak.a2acloud.io")
            ):
                taken = (
                    await session.execute(select(User).where(User.email == email))
                ).scalar_one_or_none()
                if taken is None:
                    user.email = email
                    ident.email = email
                    await session.commit()
                    await session.refresh(user)
            return user

    user = None
    created = False
    if email and not email_verified:
        raise HTTPException(403, "keycloak email is not verified")
    if email:
        user = (
            await session.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
    if user is None:
        created = True
        placeholder = bcrypt.hashpw(
            secrets.token_urlsafe(32).encode(), bcrypt.gensalt()
        ).decode()
        # Placeholder must be a syntactically valid, non-reserved domain or
        # UserOut's EmailStr rejects it (e.g. ".local" is a reserved TLD). Real
        # connector users arrive via auth-code flow and carry a real email; this
        # fallback only covers email-less tokens (e.g. service accounts).
        user = User(
            email=email or f"kc-{sub}@keycloak.a2acloud.io", password_hash=placeholder
        )
        session.add(user)
        await session.flush()
        await create_personal_organization(session, user)

    if ident is None:
        session.add(KeycloakIdentity(keycloak_sub=sub, user_id=user.id, email=email))
    await session.commit()
    await session.refresh(user)
    if created:
        profile_id = f"user:{user.id}"
        identify_profile(
            profile_id,
            email=email,
            properties={"userId": str(user.id), "signupMethod": "keycloak"},
            request=request,
        )
        track_event(
            "user_signed_up",
            profile_id=profile_id,
            properties={"signupMethod": "keycloak"},
            request=request,
        )
    return user
