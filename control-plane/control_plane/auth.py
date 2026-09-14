from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any
from urllib.parse import urlsplit

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_frontend_session import (
    AGENT_SESSION_TOKEN_TYPE,
    agent_token_audience,
    decode_agent_session_token,
)
from .config import settings
from .db import get_session
from .models import AgentStudioRun, User

_LEGACY_SHARED_COOKIE_NAME = "a2a_session"
_LEGACY_SHARED_COOKIE_DOMAIN = ".a2acloud.io"
_UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
STUDIO_JOB_TOKEN_TYPE = "agent_studio_job"
_STUDIO_JOB_SCOPES = frozenset({
    "agent:read",
    "agent:invoke",
    "source:write",
    "visibility:write",
    "deployment:read",
    "receipt:read",
    "run:read",
    "run:write",
})
_STUDIO_TOOLCHAIN_AGENTS = frozenset({
    "agent-builder",
    "agent-reviewer",
    "code-editor-agent",
})
AGENT_INVOKE_TOKEN_TYPE = "agent_invoke"
_AGENT_INVOKE_SCOPES = frozenset({
    "agent:read",
    "agent:invoke",
    "memory:read",
    "memory:write",
    "run:read",
    "run:write",
    "simulation:read",
    "simulation:write",
})
#: Ceiling on how long any credential handed to an invoked agent stays alive.
#: The runtime budget it is derived from comes out of the agent *card*, which
#: is seller-supplied (``runtime.resources.max_runtime_seconds`` and every
#: ``skills[].policy.timeout_seconds``), so it is clamped here rather than
#: trusted. The value is the Knative request ceiling the platform already
#: enforces on the call itself (``k8s.KNATIVE_MAX_TIMEOUT_SECONDS``) plus the
#: same 30s grace the workspace grant uses; a call that cannot run longer than
#: that cannot need a credential that outlives it.
AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS = 1800 + 30
AGENT_RUNTIME_TOKEN_TYPE = "agent_runtime"
#: The longest life any *session-shaped* platform credential may have.
#:
#: This is not a new number: ``routes/admin.py`` already bounds the one
#: deliberately long-lived session the platform mints — the operator-issued
#: user token — at exactly 30 days, and a dashboard cookie is
#: ``settings.jwt_ttl_seconds`` (7 days by default). Nothing legitimate is
#: longer, which is what makes it usable as a decode-time rule: see
#: :func:`_reject_overlong_platform_session`.
PLATFORM_TOKEN_MAX_TTL_SECONDS = 60 * 60 * 24 * 30
#: Ceiling on the credential that lives in an agent's *pod* as ``A2A_CP_JWT``.
#: Unlike an invoke token this one is not attached to a single call: it is
#: written into the agent's runtime secret at deploy time and only re-minted by
#: the next deploy, so it has to outlive the call it was created during. It is
#: the same scoped, audience-bound class as the invoke token — ``decode_token``
#: rejects it, ``_authorize_agent_invoke_request`` gates it — with a
#: deploy-to-deploy lifetime instead of a call-length one. Until this existed
#: the value here was a 365-day *session*: a leaked pod environment handed over
#: the deployer's whole account.
#:
#: The number is deliberate, not incidental, so it is anchored rather than
#: invented: the pod credential does not get a longer life than the longest
#: credential the platform already issues anywhere
#: (``PLATFORM_TOKEN_MAX_TTL_SECONDS``). What that buys, stated plainly: a copy
#: scraped out of a pod can act on that one agent's invoke surface — including
#: minting an LLM grant at that audience, i.e. spending the deployer's LLM
#: budget — for up to 30 days, and nothing revokes it before then. What it
#: costs: an agent that is never redeployed loses its environment fallback
#: after 30 days, which only affects invocations that carry no per-call
#: ``cp_jwt`` (see :func:`issue_runtime_cp_credential`). A redeploy re-mints it.
AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS = PLATFORM_TOKEN_MAX_TTL_SECONDS
#: What an agent may do while acting for the user who invoked (or deployed) it.
_AGENT_SCOPES_DEFAULT = (
    "agent:read",
    "agent:invoke",
    "memory:read",
    "memory:write",
    "run:read",
    "run:write",
    "simulation:read",
    "simulation:write",
)
#: Path segments under ``/v1/agents`` that name a route rather than an agent.
#: An invoke token addresses agents by name, so it must never fall through the
#: ``/v1/agents/{name}`` rules with one of these in the name position.
_RESERVED_AGENT_PATH_SEGMENTS = frozenset({
    "compose",
    "from-openapi",
    "from-source",
    "from-tarball",
    "import",
    "mine",
    "openapi",
    "search",
})
#: Build specialists shipped from this repo. They are not marketplace listings,
#: and their tools legitimately drive the whole source/deploy surface on the
#: caller's behalf, which a scoped invoke token deliberately withholds.
PLATFORM_TOOLCHAIN_AGENTS = _STUDIO_TOOLCHAIN_AGENTS | {"agent-studio"}


@dataclass(frozen=True)
class UserSessionIdentity:
    id: int
    email: str


@dataclass(frozen=True)
class UserIdentity(UserSessionIdentity):
    created_at: datetime


def issue_token(user_id: int, *, ttl_seconds: int | None = None) -> str:
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else settings.jwt_ttl_seconds
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def issue_studio_job_token(
    user_id: int,
    *,
    run_id: str,
    target_agent: str,
    ttl_seconds: int,
    scopes: tuple[str, ...] = (
        "agent:read",
        "agent:invoke",
        "source:write",
        "visibility:write",
        "deployment:read",
        "receipt:read",
        "run:read",
        "run:write",
    ),
) -> str:
    """Mint a short-lived credential that cannot act as a browser session."""
    clean_scopes = tuple(dict.fromkeys(str(scope) for scope in scopes))
    invalid = set(clean_scopes) - _STUDIO_JOB_SCOPES
    if invalid:
        raise ValueError(f"unsupported Studio job scope(s): {', '.join(sorted(invalid))}")
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + max(1, int(ttl_seconds)),
        "typ": STUDIO_JOB_TOKEN_TYPE,
        "studio_run_id": str(run_id),
        "target_agent": str(target_agent),
        "scopes": list(clean_scopes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def _issue_agent_scoped_token(
    user_id: int,
    *,
    agent: str,
    token_type: str,
    ttl_seconds: int,
    scopes: tuple[str, ...],
) -> str:
    """Encode one agent-bound, scope-checked, non-session credential."""
    clean_scopes = tuple(dict.fromkeys(str(scope) for scope in scopes))
    invalid = set(clean_scopes) - _AGENT_INVOKE_SCOPES
    if invalid:
        raise ValueError(f"unsupported agent invoke scope(s): {', '.join(sorted(invalid))}")
    target = str(agent).strip()
    if not target:
        raise ValueError("agent invoke tokens must name a target agent")
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + max(1, int(ttl_seconds)),
        "typ": token_type,
        "aud": agent_token_audience(target),
        "target_agent": target,
        "scopes": list(clean_scopes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def issue_agent_invoke_token(
    user_id: int,
    *,
    agent: str,
    ttl_seconds: int,
    scopes: tuple[str, ...] = _AGENT_SCOPES_DEFAULT,
) -> str:
    """Mint the credential one hosted invocation may call back with.

    A hosted invocation runs third-party code, so the caller credential it
    receives must not be a session: ``decode_token`` rejects this ``typ``, the
    audience names the one agent being invoked, and the lifetime is clamped to
    ``AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS`` — the longest a call is allowed to
    run — rather than to whatever runtime budget the seller's card declares.
    An agent that logs its request keeps a credential that can only address
    that agent, and only for the outer bound of the call it was serving.
    """
    return _issue_agent_scoped_token(
        user_id,
        agent=agent,
        token_type=AGENT_INVOKE_TOKEN_TYPE,
        ttl_seconds=clamp_invocation_ttl_seconds(ttl_seconds),
        scopes=scopes,
    )


def issue_agent_runtime_token(
    user_id: int,
    *,
    agent: str,
    ttl_seconds: int = AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS,
    scopes: tuple[str, ...] = _AGENT_SCOPES_DEFAULT,
) -> str:
    """Mint the credential an agent's own pod holds as ``A2A_CP_JWT``.

    Same boundary as an invoke token — rejected by ``decode_token``, bound to
    the one agent by audience, gated by ``_authorize_agent_invoke_request`` —
    but it is persisted in the agent's runtime secret rather than handed out
    per call, so it is allowed a deploy-to-deploy lifetime instead of a
    call-length one. Nothing refreshes it: it is re-minted by the next deploy,
    and the per-invocation ``cp_jwt`` remains the credential a control-plane
    driven call actually uses. A copy scraped out of the pod environment
    therefore stays usable until it expires or the agent is redeployed, which
    is exactly why it may not be a session.
    """
    return _issue_agent_scoped_token(
        user_id,
        agent=agent,
        token_type=AGENT_RUNTIME_TOKEN_TYPE,
        ttl_seconds=max(1, min(int(ttl_seconds), AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS)),
        scopes=scopes,
    )


def clamp_invocation_ttl_seconds(ttl_seconds: int) -> int:
    """Bound a card-derived runtime budget to what an invocation may use."""
    try:
        requested = int(ttl_seconds)
    except (TypeError, ValueError):
        requested = 0
    return max(1, min(requested, AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS))


def issue_invocation_cp_credential(
    user_id: int,
    *,
    agent: str,
    ttl_seconds: int = AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS,
) -> str:
    """The caller credential forwarded into one invocation of ``agent``.

    Every path that hands a control-plane credential to an agent process — the
    hosted agent-API route and the orchestrator's hand-off tool — goes through
    here, so the rule lives in one place: a marketplace listing gets a scoped,
    audience-bound invoke token, the platform's own build specialists keep an
    ordinary credential because their tools drive the source/deploy surface on
    the caller's behalf, and neither may outlive the call.

    Callers must never forward their own session instead: a raw ``a2a_session``
    cookie handed to a seller's process is a week-long account takeover.
    """
    ttl = clamp_invocation_ttl_seconds(ttl_seconds)
    target = str(agent).strip()
    if target in PLATFORM_TOOLCHAIN_AGENTS:
        return issue_token(user_id, ttl_seconds=ttl)
    return issue_agent_invoke_token(user_id, agent=target, ttl_seconds=ttl)


def issue_runtime_cp_credential(
    user_id: int,
    *,
    agent: str,
    ttl_seconds: int = AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS,
) -> str:
    """The credential written into ``agent``'s pod as ``A2A_CP_JWT``.

    The twin of :func:`issue_invocation_cp_credential`, and deliberately not
    the same rule. That one carves the platform's own build specialists out and
    hands them an ordinary credential; **this one carves nobody out**, because
    the two credentials differ in the property the carve-out is paid for with:
    an invoke token dies with the call that created it, this one lives until
    the next deploy. A platform session that outlives its call by weeks, in a
    process environment, is exactly the thing this function exists to stop —
    and for the four ``PLATFORM_TOOLCHAIN_AGENTS`` it would be the *operator's*
    session, since they are deployed from this repo by the operator account.

    So the pod credential is never stronger than the per-call one, and for the
    toolchain agents it is strictly weaker. The cost is real and is not
    silent: with only the pod credential those agents cannot reach
    ``/v1/platform/gitea-token``, ``/v1/me/files`` or ``/v1/agents/from-tarball``
    — the surface ``wants_cp_jwt`` is documented for. That is affordable
    because every control-plane path that invokes them puts a per-call
    ``cp_jwt`` in the invoke body (``agent_review._call_reviewer``,
    ``agent_studio_autopilot._call_studio_upgrade``, ``agent_studio_runs``,
    ``routes/agents._call_hosted_agent_api_skill``, ``mail_ingress``, the
    orchestrator hand-off tools), and the SDK prefers the body value over the
    environment. The environment copy is a fallback for calls nothing on this
    side made.
    """
    return issue_agent_runtime_token(
        user_id,
        agent=agent,
        ttl_seconds=ttl_seconds,
    )


def set_session_cookie(response: Response, token: str) -> None:
    clear_legacy_session_cookie(response)
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.jwt_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    clear_legacy_session_cookie(response)


def clear_legacy_session_cookie(response: Response) -> None:
    """Expire the former parent-domain cookie during the host-only migration."""
    response.delete_cookie(
        _LEGACY_SHARED_COOKIE_NAME,
        domain=_LEGACY_SHARED_COOKIE_DOMAIN,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def enforce_browser_session_csrf(request: Request) -> None:
    """Require the dashboard origin for unsafe cookie-authenticated requests."""
    if request.method.upper() not in _UNSAFE_HTTP_METHODS:
        return
    authorization = request.headers.get("authorization", "")
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return
    if not (
        request.cookies.get(settings.session_cookie_name)
        or request.cookies.get(_LEGACY_SHARED_COOKIE_NAME)
    ):
        return

    source = request.headers.get("origin") or request.headers.get("referer")
    if _http_origin(source) != _http_origin(settings.dashboard_url):
        raise HTTPException(403, "browser session request origin is not allowed")


def _http_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{scheme}://{authority}"


def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    # Agent-scoped frontend sessions are signed with the same secret but are
    # only ever valid at the one agent origin named in their audience. Refuse
    # them here so a hosted agent cannot replay a visitor's cookie against the
    # platform API. (PyJWT already rejects the ``aud`` claim above when no
    # audience is expected; this keeps the guarantee explicit.)
    if payload.get("typ") == AGENT_SESSION_TOKEN_TYPE:
        raise HTTPException(401, "agent session token is not valid for platform access")
    if payload.get("typ") == STUDIO_JOB_TOKEN_TYPE:
        raise HTTPException(401, "Studio job token is not valid for general platform access")
    if payload.get("typ") == AGENT_INVOKE_TOKEN_TYPE:
        raise HTTPException(401, "agent invoke token is not valid for platform access")
    if payload.get("typ") == AGENT_RUNTIME_TOKEN_TYPE:
        raise HTTPException(401, "agent runtime token is not valid for platform access")
    _reject_overlong_platform_session(payload)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(401, "token missing subject")
    return int(sub)


def platform_session_max_lifetime_seconds() -> int:
    """The longest lifetime a session-shaped credential may claim."""
    return max(int(settings.jwt_ttl_seconds), PLATFORM_TOKEN_MAX_TTL_SECONDS)


def _reject_overlong_platform_session(payload: dict[str, Any]) -> None:
    """Retire a credential shape no current mint site can produce.

    Minting ``A2A_CP_JWT`` as a scoped runtime token fixes the *next* deploy
    and nothing else. Every agent already running keeps the value it was given:
    an untyped 365-day platform session, in the secret store and in the pod
    environment, indistinguishable at decode from a dashboard cookie, with no
    per-token revocation anywhere in the platform. The only server-side kill
    switch would be rotating ``jwt_secret``, which logs out every dashboard
    user.

    Bounding the *lifetime* is a kill switch that costs nothing legitimate.
    Every session the platform issues today is at most
    ``settings.jwt_ttl_seconds`` (browser login, 7 days by default) or the
    30-day ceiling ``routes/admin.py`` puts on an operator-minted user token;
    the machine-triggered ones are minutes to hours. A session claiming more
    than that is the legacy runtime secret, so it stops working on its next
    request rather than in 2027.

    A token with no ``iat`` cannot be bounded, so it is refused rather than
    exempted: no mint site in this codebase omits it (:func:`issue_token` and
    every typed variant set it), so the only thing that shape can be is
    something this platform did not issue in its current form.
    """
    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(exp, (int, float)) or not isinstance(iat, (int, float)):
        raise HTTPException(401, "platform session is missing its lifetime claims")
    if int(exp) - int(iat) > platform_session_max_lifetime_seconds():
        raise HTTPException(401, "platform session lifetime exceeds the maximum")


async def user_from_token(session: AsyncSession, token: str) -> User:
    # Route by signing alg: browser sessions are short-lived CP HS256 cookies;
    # Keycloak access tokens are RS256 and go through the identity bridge.
    if _is_keycloak_token(token):
        return await _keycloak_user_from_token(session, token)

    user_id = decode_token(token)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "user not found")
    return user


def _is_keycloak_token(token: str) -> bool:
    if not settings.keycloak_enabled:
        return False
    try:
        alg = (jwt.get_unverified_header(token).get("alg") or "").upper()
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    return alg.startswith("RS")


async def _keycloak_user_from_token(session: AsyncSession, token: str) -> User:
    from .keycloak_auth import (
        provision_user_from_claims,
        verify_keycloak_token,
    )

    claims = verify_keycloak_token(token)
    return await provision_user_from_claims(session, claims)


async def user_session_identity_from_token(
    session: AsyncSession,
    token: str,
) -> UserSessionIdentity:
    if _is_keycloak_token(token):
        user = await _keycloak_user_from_token(session, token)
        return UserSessionIdentity(id=user.id, email=user.email)

    user_id = decode_token(token)
    row = (
        await session.execute(
            select(User.id, User.email).where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(401, "user not found")
    return UserSessionIdentity(id=row.id, email=row.email)


async def user_identity_from_token(session: AsyncSession, token: str) -> UserIdentity:
    if _is_keycloak_token(token):
        user = await _keycloak_user_from_token(session, token)
        return UserIdentity(id=user.id, email=user.email, created_at=user.created_at)

    user_id = decode_token(token)
    row = (
        await session.execute(
            select(User.id, User.email, User.created_at).where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(401, "user not found")
    return UserIdentity(id=row.id, email=row.email, created_at=row.created_at)


def _credential_token(
    authorization: str | None,
    session_cookie: str | None,
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        # Slice rather than split: ``Authorization: Bearer`` followed by only
        # whitespace still matches the prefix, and ``split(None, 1)`` returns a
        # single element for it — indexing [1] there raised an unhandled
        # IndexError (HTTP 500) on every credential-accepting route.
        token = authorization[len("bearer ") :].strip()
        return token or None
    return session_cookie.strip() if session_cookie else None


async def current_user(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User:
    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(401, "missing bearer token or session cookie")
    return await user_from_token(session, token)


async def current_user_or_studio_job(
    request: Request,
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Accept a normal user or an explicitly scoped Studio worker.

    General ``current_user`` endpoints reject Studio credentials.  Routes that
    opt into this dependency are checked against method, target agent, and the
    scopes embedded in the short-lived job token.
    """
    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(401, "missing bearer token or session cookie")
    if _is_keycloak_token(token):
        return await user_from_token(session, token)
    payload = _decode_platform_payload(token)
    if payload.get("typ") != STUDIO_JOB_TOKEN_TYPE:
        return await user_from_token(session, token)
    _authorize_studio_job_request(payload, request)
    request.state.studio_job_claims = payload
    return await _active_studio_job_user(session, payload)


async def current_user_or_agent_invoke(
    request: Request,
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Accept a normal user, a Studio worker, or a live hosted invocation.

    This is the callback surface a hosted agent reaches with the ``cp_jwt`` it
    was handed for the current call. The credential resolves only after the
    request is checked against method, path, the agent it was minted for, and
    the scopes it carries; everywhere else it is simply an invalid token.
    """
    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(401, "missing bearer token or session cookie")
    if not _is_keycloak_token(token):
        payload = _decode_agent_invoke_payload(token)
        if payload is not None:
            _authorize_agent_invoke_request(payload, request)
            request.state.agent_invoke_claims = payload
            return await _agent_invoke_user(session, payload)
    return await current_user_or_studio_job(
        request, authorization, session_cookie, session
    )


def _decode_agent_invoke_payload(token: str) -> dict[str, Any] | None:
    """Return the claims iff ``token`` is a structurally sound invoke token.

    ``aud`` is verified against the signed ``target_agent`` rather than against
    a caller-supplied audience, so the two claims can never disagree — an
    invoke token is usable at exactly the agent it was minted for.

    The pod-resident ``A2A_CP_JWT`` (``AGENT_RUNTIME_TOKEN_TYPE``) is the same
    shape and gets the same treatment: it differs only in how long it lives, so
    it must not reach a wider surface than the per-call credential does.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_alg],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        return None
    if not isinstance(payload, dict) or payload.get("typ") not in {
        AGENT_INVOKE_TOKEN_TYPE,
        AGENT_RUNTIME_TOKEN_TYPE,
    }:
        return None
    target = str(payload.get("target_agent") or "").strip()
    scopes = {str(item) for item in payload.get("scopes") or []}
    if (
        not target
        or payload.get("aud") != agent_token_audience(target)
        or not scopes
        or not scopes <= _AGENT_INVOKE_SCOPES
    ):
        raise HTTPException(403, "agent invoke token is missing a valid boundary")
    return payload


def _authorize_agent_invoke_request(payload: dict[str, Any], request: Request) -> None:
    target = str(payload.get("target_agent") or "").strip()
    scopes = {str(item) for item in payload.get("scopes") or []}
    method = request.method.upper()
    path = request.url.path.rstrip("/") or "/"
    parts = [part for part in path.split("/") if part]
    required: str | None = None
    path_target: str | None = None

    # Deny by default: every rule below matches an exact path shape, so a route
    # added later under one of these prefixes is out of scope until someone
    # puts it in scope on purpose.
    if method == "POST" and parts == ["v1", "me", "subagent-runs", "track"]:
        required = "run:write"
    elif (
        method == "GET"
        and len(parts) == 4
        and parts[:3] == ["v1", "me", "subagent-runs"]
        and parts[3] != "track"
    ):
        # One run by grant id. The unfiltered list route is *not* in scope: an
        # invocation has no business reading the buyer's whole run history.
        required = "run:read"
    elif method == "GET" and parts == ["v1", "agents"]:
        required = "agent:read"
    elif (
        len(parts) >= 3
        and parts[:2] == ["v1", "agents"]
        and parts[2] not in _RESERVED_AGENT_PATH_SEGMENTS
    ):
        path_target = parts[2]
        suffix = parts[3:]
        section = suffix[0] if suffix else ""
        if not section and method == "GET":
            required = "agent:read"
        elif section == "memory":
            required = "memory:read" if method == "GET" else "memory:write"
        elif section == "meta-runs":
            required = "run:read" if method == "GET" else "run:write"
        elif section == "protocol-simulations":
            # Only the four an agent's own runtime calls. The rest of this
            # router is dashboard surface (custom suites, job stop, event
            # ingest) that an invocation has no business reaching, so it is not
            # enough to leave those fail-closed at their dependency.
            if method == "GET" and suffix[1:] in (["scenarios"], ["protocol-registry"]):
                required = "simulation:read"
            elif method == "POST" and (
                suffix[1:] == ["runtime-readiness"]
                or (len(suffix) == 3 and suffix[2] == "scenario-runs")
            ):
                required = "simulation:write"
    if required is None or required not in scopes:
        raise HTTPException(403, "agent invoke token is not scoped for this operation")
    # Resolving an A2A hand-off target needs to read *some other* agent's card,
    # so a plain card read stays unbound. Everything that touches an agent's
    # own state is pinned to the agent this token was minted for.
    #
    # Said exactly, because "reads its own card" would understate it: with
    # ``agent:read`` this credential can name ANY agent ``require_agent_access``
    # would show its subject — public listings and that subject's own private
    # and org-visible agents alike — and receive the full card, URL included.
    # The unfiltered ``GET /v1/agents`` list is separately narrowed to public
    # agents for this credential class (see ``routes/agents.list_agents``), so
    # the exposure needs the name up front rather than yielding an inventory.
    #
    # It stays unbound for both classes. For ``AGENT_RUNTIME_TOKEN_TYPE`` the
    # subject is the agent's own deployer, so a named private read reaches the
    # deployer's own data from the deployer's own pod; for the per-call
    # ``AGENT_INVOKE_TOKEN_TYPE`` the subject is the buyer and this is the one
    # place a seller's process can learn a buyer's private agent exists, which
    # is the price of A2A hand-off working at all.
    if path_target is not None and path_target != target and required != "agent:read":
        raise HTTPException(403, "agent invoke token is bound to a different agent")


async def _agent_invoke_user(
    session: AsyncSession,
    payload: dict[str, Any],
) -> User:
    try:
        user_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "agent invoke token has an invalid subject") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "user not found")
    return user


async def _active_studio_job_user(
    session: AsyncSession,
    payload: dict[str, Any],
) -> User:
    """Resolve a Studio credential only while its exact build is active."""
    run_id = str(payload.get("studio_run_id") or "").strip()
    target = str(payload.get("target_agent") or "").strip()
    if not run_id or not target:
        raise HTTPException(403, "Studio job token is missing a valid boundary")
    try:
        user_id = int(payload.get("sub") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(401, "Studio job token has an invalid subject") from exc
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "user not found")
    active_run = (
        await session.execute(
            select(AgentStudioRun.id).where(
                AgentStudioRun.run_id == run_id,
                AgentStudioRun.agent_name == target,
                AgentStudioRun.status.in_(
                    {"queued", "building", "evaluating", "reviewing", "improving", "deploying"}
                ),
            )
        )
    ).scalar_one_or_none()
    if active_run is None:
        raise HTTPException(403, "Studio job token is no longer attached to an active run")
    return user


def studio_job_claims(request: Request) -> dict[str, Any] | None:
    value = getattr(request.state, "studio_job_claims", None)
    return dict(value) if isinstance(value, dict) else None


def agent_invoke_claims(request: Request) -> dict[str, Any] | None:
    """The invoke-token claims for this request, if that is what authenticated it.

    Routes that a hosted invocation may reach use this to serve it less than
    they serve the buyer's own browser session.
    """
    value = getattr(request.state, "agent_invoke_claims", None)
    return dict(value) if isinstance(value, dict) else None


def _decode_platform_payload(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"invalid token: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(401, "invalid token payload")
    return payload


def _authorize_studio_job_request(payload: dict[str, Any], request: Request) -> None:
    target = str(payload.get("target_agent") or "").strip()
    run_id = str(payload.get("studio_run_id") or "").strip()
    scopes = {str(item) for item in payload.get("scopes") or []}
    if not target or not run_id or not scopes <= _STUDIO_JOB_SCOPES:
        raise HTTPException(403, "Studio job token is missing a valid boundary")

    method = request.method.upper()
    path = request.url.path.rstrip("/") or "/"
    required: str | None = None
    path_target: str | None = None
    if method == "POST" and path == "/v1/agents/from-tarball":
        required = "source:write"
    elif method == "POST" and path == "/v1/platform/gitea-token":
        required = "source:write"
    elif method == "DELETE" and path.startswith("/v1/platform/gitea-token/"):
        required = "source:write"
    elif method == "POST" and path == "/v1/me/subagent-runs/track":
        required = "run:write"
    elif method == "GET" and path.startswith("/v1/me/subagent-runs"):
        required = "run:read"
    else:
        parts = [part for part in path.split("/") if part]
        if (
            method == "PATCH"
            and len(parts) == 5
            and parts[:3] == ["v1", "agents", "mine"]
            and parts[4] == "visibility"
        ):
            path_target = parts[3]
            required = "visibility:write"
        elif len(parts) >= 3 and parts[:2] == ["v1", "agents"]:
            path_target = parts[2]
            suffix = parts[3:]
            if method == "GET" and not suffix:
                required = "agent:read"
            elif suffix and suffix[0] == "deployments" and method == "GET":
                required = "deployment:read"
            elif suffix == ["code-editor"] and method in {"GET", "POST"}:
                required = "source:write"
            elif suffix == ["source"] and method == "GET":
                required = "source:write"
            elif suffix == ["source", "deploy"] and method == "POST":
                required = "source:write"
            elif suffix == ["receipts"] and method == "GET":
                required = "receipt:read"
    if required is None or required not in scopes:
        raise HTTPException(403, "Studio job token is not scoped for this operation")
    # Studio resolves the fixed platform toolchain through the same read-only
    # agent-card route used by normal A2A handoffs. Keep every mutable and
    # deployment route bound to the generated target; only an exact card read
    # may address one of these platform-owned specialists.
    toolchain_card_read = (
        required == "agent:read"
        and method == "GET"
        and path_target in _STUDIO_TOOLCHAIN_AGENTS
        and len([part for part in path.split("/") if part]) == 3
    )
    if path_target is not None and path_target != target and not toolchain_card_read:
        raise HTTPException(403, "Studio job token is bound to a different agent")


async def current_credential_token(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> str:
    """The raw caller credential, for endpoints that must inspect it.

    Used where the accepted credential depends on the request body — an
    agent-scoped session token is only valid for the agent it names.
    """
    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(401, "missing bearer token or session cookie")
    return token


async def user_for_agent_audience(
    session: AsyncSession,
    token: str,
    audience: str,
) -> User:
    """Resolve a user from a platform credential *or* an agent-scoped session.

    An agent-scoped token only resolves for the agent named in ``audience``,
    so a hosted agent can act for its visitor at itself and nowhere else.
    """
    user_id = decode_agent_session_token(token, agent=audience)
    if user_id is not None:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(401, "user not found")
        return user
    if not _is_keycloak_token(token):
        invoke_payload = _decode_agent_invoke_payload(token)
        if invoke_payload is not None:
            scopes = {str(item) for item in invoke_payload.get("scopes") or []}
            target = str(invoke_payload.get("target_agent") or "").strip()
            if "agent:invoke" not in scopes or audience != target:
                raise HTTPException(
                    403,
                    "agent invoke token is not scoped for this agent audience",
                )
            return await _agent_invoke_user(session, invoke_payload)
        payload = _decode_platform_payload(token)
        if payload.get("typ") == STUDIO_JOB_TOKEN_TYPE:
            scopes = {str(item) for item in payload.get("scopes") or []}
            target = str(payload.get("target_agent") or "").strip()
            if (
                "agent:invoke" not in scopes
                or not scopes <= _STUDIO_JOB_SCOPES
                or (audience != target and audience not in _STUDIO_TOOLCHAIN_AGENTS)
            ):
                raise HTTPException(
                    403,
                    "Studio job token is not scoped for this agent audience",
                )
            return await _active_studio_job_user(session, payload)
    return await user_from_token(session, token)


async def current_user_identity(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> UserIdentity:
    token = _credential_token(authorization, session_cookie)
    if not token:
        raise HTTPException(401, "missing bearer token or session cookie")
    return await user_identity_from_token(session, token)


async def optional_current_user_session(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> UserSessionIdentity | None:
    token = _credential_token(authorization, session_cookie)
    if not token:
        return None
    try:
        return await user_session_identity_from_token(session, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def optional_current_user(
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    token = _credential_token(authorization, session_cookie)
    if not token:
        return None
    try:
        return await user_from_token(session, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


async def optional_current_user_or_studio_job(
    request: Request,
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    """``optional_current_user`` that also resolves a scoped Studio worker.

    ``decode_token`` deliberately refuses a Studio job credential, so a route
    guarded by plain ``optional_current_user`` silently downgrades the Studio
    worker to *anonymous* rather than rejecting it — which reads as "this agent
    does not exist" instead of "this token may not do that". Routes that Studio
    legitimately reads (see ``_authorize_studio_job_request``) use this instead,
    so the job token is checked against method, path, target agent and scope
    like everywhere else.

    Denials keep the optional shape: a missing/expired/foreign credential is
    anonymous (``None``), while a Studio token that is real but out of bounds
    still raises 403 rather than being quietly ignored.
    """
    token = _credential_token(authorization, session_cookie)
    if not token:
        return None
    try:
        return await current_user_or_studio_job(
            request, authorization, session_cookie, session
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise
