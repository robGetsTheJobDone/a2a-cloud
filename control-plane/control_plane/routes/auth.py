from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    UserIdentity,
    UserSessionIdentity,
    clear_legacy_session_cookie,
    clear_session_cookie,
    current_user_identity,
    issue_token,
    optional_current_user_session,
    set_session_cookie,
    user_from_token,
)
from ..agent_frontend_session import (
    AgentSessionExchangeStore,
    AgentSessionExchangeUnavailable,
    get_agent_session_exchange_store,
    mint_agent_session_token,
)
from ..auth_exchange import (
    AuthExchangeUnavailable,
    CliSessionExchangeStore,
    get_cli_session_exchange_store,
)
from ..auth_utils import dashboard_error_url, sanitize_redirect
from ..config import settings
from ..db import get_session
from ..keycloak_auth import provision_user_from_claims, verify_keycloak_id_token
from ..models import Agent, User
from ..schemas import (
    AgentSessionExchangeIn,
    AgentSessionExchangeOut,
    AuthCliSessionCreateIn,
    AuthCliSessionCreateOut,
    AuthLogoutOut,
    AuthSessionOut,
    UserOut,
    UserSessionOut,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _openid_endpoint(name: str, *, backend: bool = False) -> str:
    base = (
        settings.keycloak_backend_url
        if backend and settings.keycloak_backend_url
        else settings.keycloak_issuer
    )
    return f"{base.rstrip('/')}/protocol/openid-connect/{name}"


def _browser_auth_base_url() -> str:
    # Browser auth must stay on the dashboard origin so host-only cookies are
    # never exposed to user-controlled ``*.<platform_domain>`` agent origins.
    return settings.dashboard_url.rstrip("/")


def _oidc_callback_url() -> str:
    return f"{_browser_auth_base_url()}/v1/auth/oidc/callback"


def _oidc_logout_url(redirect_to: str | None = "/") -> str | None:
    if not settings.keycloak_enabled:
        return None
    params = {
        "client_id": settings.keycloak_browser_client_id,
        "post_logout_redirect_uri": _dashboard_redirect_url(redirect_to),
    }
    return f"{_openid_endpoint('logout')}?{urlencode(params)}"


def _dashboard_redirect_url(redirect_to: str | None) -> str:
    return f"{settings.dashboard_url.rstrip('/')}{sanitize_redirect(redirect_to)}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _encode_oidc_state(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def _decode_oidc_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise HTTPException(400, "missing oidc state cookie")
    try:
        payload = jwt.decode(raw, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except jwt.PyJWTError as exc:
        raise HTTPException(400, "invalid oidc state cookie") from exc
    if payload.get("typ") != "oidc_state":
        raise HTTPException(400, "invalid oidc state cookie")
    return payload


def _set_oidc_state_cookie(response: Response, value: str) -> None:
    _clear_legacy_oidc_state_cookie(response)
    response.set_cookie(
        settings.oidc_state_cookie_name,
        value,
        max_age=settings.oidc_state_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/v1/auth/oidc",
    )


def _clear_oidc_state_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.oidc_state_cookie_name,
        path="/v1/auth/oidc",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    _clear_legacy_oidc_state_cookie(response)


def _clear_legacy_oidc_state_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.oidc_state_cookie_name,
        domain=settings.shared_cookie_domain,
        path="/v1/auth/oidc",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _assert_safe_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    value = origin or referer
    if not value:
        return
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(403, "invalid request origin")
    source = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    allowed = {
        str(request.base_url).rstrip("/"),
        settings.dashboard_url.rstrip("/"),
        _browser_auth_base_url(),
    }
    if source not in allowed:
        raise HTTPException(403, "invalid request origin")


async def _exchange_oidc_code(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": settings.keycloak_browser_client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if settings.keycloak_browser_client_secret:
        data["client_secret"] = settings.keycloak_browser_client_secret
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(_openid_endpoint("token", backend=True), data=data)
    if response.status_code >= 400:
        raise HTTPException(502, "keycloak token exchange failed")
    return response.json()


def _oidc_error_redirect(message: str) -> RedirectResponse:
    response = RedirectResponse(dashboard_error_url(message), status_code=303)
    _clear_oidc_state_cookie(response)
    return response


@router.get("/oidc/start")
async def oidc_start(
    redirect_to: str | None = Query(default="/"),
    login_hint: str | None = Query(default=None),
) -> RedirectResponse:
    if not settings.keycloak_enabled:
        raise HTTPException(404, "keycloak auth is not enabled")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    redirect_uri = _oidc_callback_url()
    now = int(time.time())
    payload = {
        "typ": "oidc_state",
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "redirect_to": sanitize_redirect(redirect_to),
        "redirect_uri": redirect_uri,
        "iat": now,
        "exp": now + settings.oidc_state_ttl_seconds,
    }
    params = {
        "client_id": settings.keycloak_browser_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint.strip()[:320]
    response = RedirectResponse(
        f"{_openid_endpoint('auth')}?{urlencode(params)}",
        status_code=303,
    )
    _set_oidc_state_cookie(response, _encode_oidc_state(payload))
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    if error:
        return _oidc_error_redirect(error_description or error)
    try:
        stored = _decode_oidc_state(request.cookies.get(settings.oidc_state_cookie_name))
        expected_state = str(stored.get("state") or "")
        if not state or not secrets.compare_digest(expected_state, state):
            raise HTTPException(400, "invalid oidc state")
        if not code:
            raise HTTPException(400, "missing oidc code")
        token_response = await _exchange_oidc_code(
            code=code,
            code_verifier=str(stored.get("code_verifier") or ""),
            redirect_uri=str(stored.get("redirect_uri") or _oidc_callback_url()),
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise HTTPException(502, "keycloak token response missing id token")
        claims = verify_keycloak_id_token(
            id_token,
            audience=settings.keycloak_browser_client_id,
        )
        expected_nonce = str(stored.get("nonce") or "")
        token_nonce = str(claims.get("nonce") or "")
        if not expected_nonce or not secrets.compare_digest(expected_nonce, token_nonce):
            raise HTTPException(401, "invalid oidc nonce")
        user = await provision_user_from_claims(session, claims, request=request)
    except HTTPException as exc:
        return _oidc_error_redirect(str(exc.detail))
    redirect = RedirectResponse(
        _dashboard_redirect_url(str(stored.get("redirect_to") or "/")),
        status_code=303,
    )
    _clear_oidc_state_cookie(redirect)
    set_session_cookie(redirect, issue_token(user.id))
    return redirect


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _set_cli_confirmation_cookie(response: Response, code: str) -> None:
    response.set_cookie(
        settings.cli_session_confirmation_cookie_name,
        code,
        max_age=settings.cli_session_exchange_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )


def _clear_cli_confirmation_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.cli_session_confirmation_cookie_name,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _cli_confirmation_page(email: str) -> HTMLResponse:
    safe_email = html.escape(email, quote=True)
    response = HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Confirm a2a Cloud sign-in</title>"
        "<style>body{margin:0;background:#090909;color:#f5f5f5;font:16px system-ui;"
        "display:grid;min-height:100vh;place-items:center}main{width:min(30rem,calc(100% - 2rem));"
        "border:1px solid #333;padding:2rem}p{color:#bbb;line-height:1.6}button{background:#fff;"
        "border:0;color:#090909;cursor:pointer;font:inherit;font-weight:650;padding:.75rem 1rem}"
        "</style></head><body><main><h1>Confirm sign-in</h1>"
        f"<p>Continue to a2a Cloud as <strong>{safe_email}</strong>.</p>"
        "<form method='post' action='/v1/auth/cli-session/confirm'>"
        "<button type='submit'>Continue</button></form></main></body></html>"
    )
    _no_store(response)
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post(
    "/cli-session",
    response_model=AuthCliSessionCreateOut,
    status_code=201,
)
async def create_cli_session(
    body: AuthCliSessionCreateIn,
    response: Response,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    exchange_store: CliSessionExchangeStore = Depends(get_cli_session_exchange_store),
) -> AuthCliSessionCreateOut:
    """Mint a short-lived browser exchange code from a bearer credential."""
    parts = authorization.split(None, 1) if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(401, "platform bearer token required")
    token = parts[1].strip()
    user = await user_from_token(session, token)  # raises 401 on invalid/expired
    ttl_seconds = settings.cli_session_exchange_ttl_seconds
    try:
        code = await exchange_store.issue(
            user_id=user.id,
            redirect_to=sanitize_redirect(body.redirect_to),
            ttl_seconds=ttl_seconds,
        )
    except AuthExchangeUnavailable as exc:
        raise HTTPException(
            503,
            "browser session exchange is unavailable",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc
    _no_store(response)
    redeem_url = (
        f"{_browser_auth_base_url()}/v1/auth/cli-session/redeem?"
        + urlencode({"code": code})
    )
    return AuthCliSessionCreateOut(redeem_url=redeem_url, expires_in=ttl_seconds)


@router.get("/cli-session/redeem")
async def redeem_cli_session(
    request: Request,
    code: str = Query(
        ...,
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    existing_user: UserSessionIdentity | None = Depends(optional_current_user_session),
    exchange_store: CliSessionExchangeStore = Depends(get_cli_session_exchange_store),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Consume a one-time code and establish the browser session cookie."""
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site in {"same-site", "cross-site"}:
        raise HTTPException(
            403,
            "browser session exchange must be opened directly",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    try:
        exchange = await exchange_store.consume(code)
    except AuthExchangeUnavailable as exc:
        raise HTTPException(
            503,
            "browser session exchange is unavailable",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc
    if exchange is None:
        raise HTTPException(
            410,
            "browser session exchange code is invalid or expired",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
            },
        )
    if existing_user is not None and existing_user.id != exchange.user_id:
        raise HTTPException(
            409,
            "browser is already signed in as a different account",
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
            },
        )
    if existing_user is None:
        user = await session.get(User, exchange.user_id)
        if user is None:
            raise HTTPException(410, "browser session exchange user no longer exists")
        try:
            confirmation_code = await exchange_store.issue(
                user_id=exchange.user_id,
                redirect_to=exchange.redirect_to,
                ttl_seconds=settings.cli_session_exchange_ttl_seconds,
            )
        except AuthExchangeUnavailable as exc:
            raise HTTPException(
                503,
                "browser session confirmation is unavailable",
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            ) from exc
        confirmation = _cli_confirmation_page(user.email)
        _set_cli_confirmation_cookie(confirmation, confirmation_code)
        return confirmation
    redirect = RedirectResponse(
        _dashboard_redirect_url(exchange.redirect_to),
        status_code=303,
    )
    set_session_cookie(redirect, issue_token(exchange.user_id))
    _no_store(redirect)
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


@router.post("/cli-session/confirm")
async def confirm_cli_session(
    request: Request,
    confirmation_code: str | None = Cookie(
        default=None,
        alias=settings.cli_session_confirmation_cookie_name,
    ),
    exchange_store: CliSessionExchangeStore = Depends(get_cli_session_exchange_store),
) -> RedirectResponse:
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != _browser_auth_base_url():
        raise HTTPException(403, "browser session confirmation origin is not allowed")
    if request.headers.get("sec-fetch-site", "").strip().lower() not in {
        "",
        "same-origin",
    }:
        raise HTTPException(403, "browser session confirmation must be same-origin")
    if not confirmation_code:
        raise HTTPException(410, "browser session confirmation is missing or expired")
    try:
        exchange = await exchange_store.consume(confirmation_code)
    except AuthExchangeUnavailable as exc:
        raise HTTPException(
            503,
            "browser session confirmation is unavailable",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc
    if exchange is None:
        raise HTTPException(
            410,
            "browser session confirmation is invalid or expired",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    redirect = RedirectResponse(
        _dashboard_redirect_url(exchange.redirect_to),
        status_code=303,
    )
    _clear_cli_confirmation_cookie(redirect)
    set_session_cookie(redirect, issue_token(exchange.user_id))
    _no_store(redirect)
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


def _agent_origin(name: str) -> str:
    # Derived from the ingress template and never from caller input, so this
    # redirect target cannot be steered at an attacker's host.
    return f"https://{settings.ingress_host_template.format(name=name)}"


@router.get("/agent-session/authorize")
async def authorize_agent_session(
    agent: str = Query(..., min_length=1, max_length=128, pattern=r"^[a-z0-9-]+$"),
    next: str | None = Query(default="/"),
    existing_user: UserSessionIdentity | None = Depends(optional_current_user_session),
    session: AsyncSession = Depends(get_session),
    exchange_store: AgentSessionExchangeStore = Depends(get_agent_session_exchange_store),
) -> Response:
    """Hand a signed-in dashboard visitor off to an agent's own origin.

    Runs on the dashboard origin, where the ``__Host-`` platform session cookie
    is readable. It never forwards that cookie: it mints a single-use code the
    agent redeems for a token scoped to itself.
    """
    redirect_to = sanitize_redirect(next)
    self_url = f"/v1/auth/agent-session/authorize?" + urlencode(
        {"agent": agent, "next": redirect_to}
    )
    if existing_user is None:
        # Bounce through login, then land back here to mint the code.
        login = RedirectResponse(
            f"{_browser_auth_base_url()}/v1/auth/oidc/start?"
            + urlencode({"redirect_to": self_url}),
            status_code=303,
        )
        _no_store(login)
        return login

    row = (
        await session.execute(select(Agent).where(Agent.name == agent))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "agent not found")

    try:
        code = await exchange_store.issue(
            user_id=existing_user.id,
            agent=agent,
            redirect_to=redirect_to,
            ttl_seconds=settings.agent_session_exchange_ttl_seconds,
        )
    except AgentSessionExchangeUnavailable as exc:
        raise HTTPException(
            503,
            "agent session exchange is unavailable",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc

    redirect = RedirectResponse(
        f"{_agent_origin(agent)}/auth/callback?"
        + urlencode({"code": code, "next": redirect_to}),
        status_code=303,
    )
    _no_store(redirect)
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


@router.post("/agent-session/exchange", response_model=AgentSessionExchangeOut)
async def exchange_agent_session(
    body: AgentSessionExchangeIn,
    response: Response,
    session: AsyncSession = Depends(get_session),
    exchange_store: AgentSessionExchangeStore = Depends(get_agent_session_exchange_store),
) -> AgentSessionExchangeOut:
    """Redeem a hand-off code for a token scoped to the calling agent.

    The code is the bearer secret here: it is single-use, short-lived, and only
    ever delivered to the one agent origin the user was redirected to.
    """
    try:
        exchange = await exchange_store.consume(body.code)
    except AgentSessionExchangeUnavailable as exc:
        raise HTTPException(
            503,
            "agent session exchange is unavailable",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        ) from exc
    if exchange is None:
        raise HTTPException(410, "agent session code is invalid or expired")
    # A code issued for one agent must never mint a token for another, even if
    # some other agent somehow obtains it.
    if exchange.agent != body.audience.strip():
        raise HTTPException(403, "agent session code was issued for a different agent")

    user = await session.get(User, exchange.user_id)
    if user is None:
        raise HTTPException(410, "agent session user no longer exists")

    token, expires_at = mint_agent_session_token(
        user_id=user.id,
        agent=exchange.agent,
        ttl_seconds=settings.agent_session_ttl_seconds,
    )
    _no_store(response)
    return AgentSessionExchangeOut(
        token=token,
        expires_at=expires_at,
        user_id=user.id,
        email=user.email,
    )


@router.post("/logout", response_model=AuthLogoutOut)
async def logout(
    request: Request,
    response: Response,
    redirect_to: str | None = Query(default="/"),
) -> AuthLogoutOut:
    _assert_safe_origin(request)
    clear_session_cookie(response)
    return AuthLogoutOut(
        authenticated=False,
        user=None,
        logout_url=_oidc_logout_url(redirect_to),
    )


@router.get("/session", response_model=AuthSessionOut)
async def auth_session(
    response: Response,
    user: UserSessionIdentity | None = Depends(optional_current_user_session),
) -> AuthSessionOut:
    clear_legacy_session_cookie(response)
    if user is None:
        return AuthSessionOut(authenticated=False, user=None)
    return AuthSessionOut(
        authenticated=True,
        user=UserSessionOut(id=user.id, email=user.email),
    )


me_router = APIRouter(prefix="/v1", tags=["auth"])


@me_router.get("/me", response_model=UserOut)
async def me(user: UserIdentity = Depends(current_user_identity)) -> UserOut:
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)
