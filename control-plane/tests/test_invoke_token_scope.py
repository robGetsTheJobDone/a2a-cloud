"""What a hosted agent is handed when someone pays to invoke it.

A marketplace invocation POSTs a body into a seller-controlled process. Until
this module existed that body carried ``issue_token(user.id)`` — the same
7-day, unrestricted credential the dashboard session cookie carries — so any
seller could log a request and keep acting as the buyer for a week.

These tests pin the replacement: a typed, agent-audience-bound, invocation-TTL
credential that resolves on the invoke callback surface and nowhere else.
"""
from __future__ import annotations

from dataclasses import replace
import json
from time import time as real_time
from types import SimpleNamespace
from typing import Any, AsyncIterator

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane import auth, k8s
from control_plane.auth import (
    AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS,
    AGENT_INVOKE_TOKEN_TYPE,
    _authorize_agent_invoke_request,
    current_user,
    decode_token,
    issue_agent_invoke_token,
    issue_invocation_cp_credential,
    issue_token,
    user_for_agent_audience,
)
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.models import Agent, User
from control_plane.routes import agents
from control_plane.routes.memory import router as memory_router


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_alg],
        options={"verify_aud": False},
    )


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("api.test", 443),
        }
    )


def _card(name: str = "invoice-helper", *, wants_cp_jwt: bool = False) -> dict[str, Any]:
    card: dict[str, Any] = {
        "name": name,
        "description": "Review invoices and identify payment problems.",
        "version": "1.2.3",
        "skills": [
            {
                "name": "review_invoice",
                "description": "Review invoice line items.",
                "tags": ["invoice"],
            }
        ],
    }
    if wants_cp_jwt:
        card["runtime"] = {"wants_cp_jwt": True}
    return card


def _agent(
    owner_id: int,
    name: str = "invoice-helper",
    *,
    public: bool = True,
    card: dict[str, Any] | None = None,
    **card_kwargs: Any,
) -> Agent:
    return Agent(
        owner_id=owner_id,
        name=name,
        description="Invoices",
        version="1.2.3",
        image="example/invoice",
        public=public,
        status="running",
        url="https://invoice.test",
        card=card if card is not None else _card(name, **card_kwargs),
    )


#: A decade, in seconds. Nothing validates these card fields, and ``k8s.py``
#: clamps the one it reads before it ever reaches Knative, so a card carrying
#: this still deploys and stays invokable.
_HOSTILE_SECONDS = 315_360_000


def _hostile_runtime_card(name: str = "invoice-helper") -> dict[str, Any]:
    card = _card(name)
    card["runtime"] = {"resources": {"max_runtime_seconds": _HOSTILE_SECONDS}}
    return card


def _hostile_skill_policy_card(name: str = "invoice-helper") -> dict[str, Any]:
    card = _card(name)
    card["skills"][0]["policy"] = {"timeout_seconds": _HOSTILE_SECONDS}
    return card


def test_invoke_token_is_typed_and_bound_to_one_agent() -> None:
    token = issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)
    claims = _claims(token)

    assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
    assert claims["target_agent"] == "invoice-helper"
    # The audience is what stops a seller replaying a buyer's credential at a
    # neighbouring agent on the same wildcard domain.
    assert claims["aud"] == "agent:invoice-helper"
    assert claims["sub"] == "7"
    assert set(claims["scopes"]) == {
        "agent:read",
        "agent:invoke",
        "memory:read",
        "memory:write",
        "run:read",
        "run:write",
        "simulation:read",
        "simulation:write",
    }


def test_invoke_token_ttl_is_one_invocation_not_one_week() -> None:
    token = issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)
    claims = _claims(token)

    assert claims["exp"] - claims["iat"] == 900
    assert claims["exp"] - claims["iat"] < settings.jwt_ttl_seconds


def test_invoke_token_ttl_tracks_the_workspace_grant() -> None:
    agent = _agent(owner_id=1)
    token = agents._hosted_invoke_cp_token(agent=agent, user=User(id=7, email="b@t", password_hash="x"))
    claims = _claims(token)

    # Same clock as the grant the agent gets for the same call: the credential
    # cannot outlive the work it was minted for.
    assert claims["exp"] - claims["iat"] == agents._agent_api_grant_ttl_seconds(agent)


def test_the_invocation_ttl_ceiling_is_the_one_the_platform_already_enforces() -> None:
    """The clamp is not an arbitrary number, and it must not drift.

    ``k8s._declared_runtime_timeout`` already refuses to configure a Knative
    request timeout above ``KNATIVE_MAX_TIMEOUT_SECONDS``, so no invocation can
    legitimately run longer than that. The credential ceiling is that same
    bound plus the grace the workspace grant uses.
    """
    assert AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS == (
        k8s.KNATIVE_MAX_TIMEOUT_SECONDS
        + agents._AGENT_API_INVOKE_TIMEOUT_GRACE_SECONDS
    )
    assert AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS < settings.jwt_ttl_seconds


@pytest.mark.parametrize(
    "card_factory",
    [_hostile_runtime_card, _hostile_skill_policy_card],
    ids=["runtime.resources.max_runtime_seconds", "skills[].policy.timeout_seconds"],
)
def test_a_hostile_card_cannot_stretch_the_credential(card_factory: Any) -> None:
    """The TTL is derived from the card, and the card belongs to the seller.

    ``_agent_api_grant_ttl_seconds`` reads ``runtime.resources`` and every
    ``skills[].policy.timeout_seconds`` off the agent card. Nothing validates
    those on register (``_reject_unsafe_user_card`` only looks at
    ``llm_provisioning``/``grant_signing``) and a hosted agent's card is
    re-persisted verbatim from the pod's own ``/.well-known/agent-card``. So a
    seller can ask for a decade — and must not get it.
    """
    agent = _agent(owner_id=1, card=card_factory())
    user = User(id=7, email="b@t", password_hash="x")

    # Precondition: the unclamped budget really is absurd, so this test would
    # fail loudly rather than vacuously if the card vector were ever ignored.
    assert agents._agent_api_grant_ttl_seconds(agent) > settings.jwt_ttl_seconds

    claims = _claims(agents._hosted_invoke_cp_token(agent=agent, user=user))
    ttl = claims["exp"] - claims["iat"]

    assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
    assert ttl == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
    assert ttl < settings.jwt_ttl_seconds


def test_the_clamp_is_in_the_mint_helper_not_only_at_the_call_site() -> None:
    """A future call site cannot opt out of the ceiling by passing a bigger TTL."""
    for token in (
        issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=_HOSTILE_SECONDS),
        issue_invocation_cp_credential(7, agent="invoice-helper", ttl_seconds=_HOSTILE_SECONDS),
        issue_invocation_cp_credential(7, agent="agent-builder", ttl_seconds=_HOSTILE_SECONDS),
    ):
        claims = _claims(token)
        assert claims["exp"] - claims["iat"] == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS


@pytest.mark.asyncio
async def test_expired_invoke_token_stops_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TTL is enforced, not merely recorded."""
    monkeypatch.setattr(auth.time, "time", lambda: real_time() - 86_400)
    token = issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)
    monkeypatch.undo()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            with pytest.raises(HTTPException) as excinfo:
                await user_for_agent_audience(session, token, "invoice-helper")
            assert excinfo.value.status_code == 401
    finally:
        await engine.dispose()


def test_invoke_token_scopes_are_validated_at_mint_time() -> None:
    with pytest.raises(ValueError):
        issue_agent_invoke_token(
            7, agent="invoice-helper", ttl_seconds=900, scopes=("source:write",)
        )
    with pytest.raises(ValueError):
        issue_agent_invoke_token(7, agent="   ", ttl_seconds=900)


def test_invoke_token_is_rejected_by_platform_token_decoding() -> None:
    token = issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)

    with pytest.raises(HTTPException) as excinfo:
        decode_token(token)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_leaked_invoke_token_cannot_be_replayed_as_a_session() -> None:
    """The whole point: a seller who logs the request body gains nothing.

    ``current_user`` is the dependency behind every ordinary authenticated
    route — workspace files, agent deletion, onboarding, credential minting. A
    credential handed to third-party code must not satisfy it.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            buyer = User(email="buyer@example.com", password_hash="x")
            session.add(buyer)
            await session.commit()

            harvested = issue_agent_invoke_token(
                buyer.id, agent="invoice-helper", ttl_seconds=900
            )
            with pytest.raises(HTTPException) as excinfo:
                await current_user(
                    authorization=f"Bearer {harvested}",
                    session_cookie=None,
                    session=session,
                )
            assert excinfo.value.status_code == 401

            # ...and it is rejected as a cookie too, not just as a header.
            with pytest.raises(HTTPException):
                await current_user(
                    authorization=None,
                    session_cookie=harvested,
                    session=session,
                )

            # Control: the credential this replaced *did* satisfy current_user,
            # which is exactly why leaking it was a full account takeover.
            resolved = await current_user(
                authorization=f"Bearer {issue_token(buyer.id)}",
                session_cookie=None,
                session=session,
            )
            assert resolved.id == buyer.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invoke_token_resolves_only_for_the_agent_it_names() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            buyer = User(email="buyer2@example.com", password_hash="x")
            session.add(buyer)
            await session.commit()

            token = issue_agent_invoke_token(
                buyer.id, agent="invoice-helper", ttl_seconds=900
            )
            # ``/v1/platform/llm-grant`` and the consumer-setup lookup both
            # resolve the caller this way, naming themselves as the audience.
            user = await user_for_agent_audience(session, token, "invoice-helper")
            assert user.id == buyer.id

            with pytest.raises(HTTPException) as excinfo:
                await user_for_agent_audience(session, token, "rival-agent")
            assert excinfo.value.status_code == 403
    finally:
        await engine.dispose()


def test_invoke_token_reaches_only_the_invoke_callback_surface() -> None:
    payload = _claims(
        issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)
    )

    allowed = [
        ("GET", "/v1/agents"),
        ("GET", "/v1/agents/invoice-helper"),
        # A2A hand-off has to resolve some *other* agent's card to find its URL.
        ("GET", "/v1/agents/some-other-agent"),
        ("GET", "/v1/agents/invoice-helper/memory"),
        ("PUT", "/v1/agents/invoice-helper/memory"),
        ("DELETE", "/v1/agents/invoice-helper/memory/notes/key"),
        ("POST", "/v1/agents/invoice-helper/meta-runs"),
        ("PATCH", "/v1/agents/invoice-helper/meta-runs/run-1"),
        ("GET", "/v1/agents/invoice-helper/protocol-simulations/scenarios"),
        ("POST", "/v1/agents/invoice-helper/protocol-simulations/runtime-readiness"),
        ("POST", "/v1/me/subagent-runs/track"),
        ("GET", "/v1/me/subagent-runs/grant-1"),
    ]
    for method, path in allowed:
        _authorize_agent_invoke_request(payload, _request(method, path))

    denied = [
        # The account surface the old credential opened up.
        ("GET", "/v1/me"),
        ("GET", "/v1/files"),
        ("GET", "/v1/me/onboarding"),
        ("POST", "/v1/llm-creds"),
        ("POST", "/v1/platform/gitea-token"),
        ("DELETE", "/v1/agents/invoice-helper"),
        # Build/deploy powers a marketplace listing has no business holding.
        ("POST", "/v1/agents/from-tarball"),
        ("POST", "/v1/agents/invoice-helper/source/deploy"),
        ("GET", "/v1/agents/invoice-helper/receipts"),
        # ...and another agent's stored state, even with the right scope.
        ("GET", "/v1/agents/rival-agent/memory"),
        ("POST", "/v1/agents/rival-agent/meta-runs"),
        # Route literals that sit in the ``{name}`` position. These are not
        # agent names, so they must not fall through the per-agent rules and
        # come out as an unbound ``agent:read``. Each is fail-closed at its own
        # dependency today; the scope table must not be what one route change
        # away from opening them.
        ("GET", "/v1/agents/mine"),
        ("GET", "/v1/agents/search"),
        ("GET", "/v1/agents/mine/summary"),
        ("POST", "/v1/agents/compose"),
        ("POST", "/v1/agents/import"),
        # The buyer's whole run history, and re-running one of their runs.
        ("GET", "/v1/me/subagent-runs"),
        ("POST", "/v1/me/subagent-runs/grant-1/rerun"),
        ("GET", "/v1/me/subagent-runs/grant-1/anything-added-later"),
    ]
    for method, path in denied:
        with pytest.raises(HTTPException) as excinfo:
            _authorize_agent_invoke_request(payload, _request(method, path))
        assert excinfo.value.status_code == 403, f"{method} {path} was not refused"


def _all_v1_routes() -> list[tuple[str, str, tuple[str, ...]]]:
    """(method, concrete path, dependency names) for every registered route."""
    import importlib
    import pkgutil

    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    from control_plane import routes as routes_pkg

    seen: set[int] = set()
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for module in pkgutil.iter_modules(routes_pkg.__path__):
        imported = importlib.import_module(f"control_plane.routes.{module.name}")
        for router in vars(imported).values():
            if not isinstance(router, APIRouter):
                continue
            for route in router.routes:
                if not isinstance(route, APIRoute) or id(route) in seen:
                    continue
                seen.add(id(route))
                # ``APIRouter`` already prepends its own prefix to ``path``.
                path = route.path.replace("{name}", "invoice-helper")
                path = path.replace("{agent_name}", "invoice-helper")
                while "{" in path and "}" in path:
                    start, end = path.index("{"), path.index("}")
                    path = f"{path[:start]}placeholder{path[end + 1:]}"
                deps = tuple(
                    sorted(
                        {
                            dep.call.__name__
                            for dep in route.dependant.dependencies
                            if dep.call is not None
                        }
                    )
                )
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    out.append((method, path, deps))
    return out


def test_the_scope_table_never_permits_more_than_the_dependencies_accept() -> None:
    """Deny-by-default, enforced against the real router table.

    The scope table and the per-route dependency are two independent gates, and
    the table is the one that is easy to widen by accident — a prefix rule like
    ``startswith("/v1/me/subagent-runs")`` or a bare ``{name}`` match silently
    adopts every route added under it later. Anything the table lets through
    but ``current_user_or_agent_invoke`` does not back is a route that is one
    dependency change away from being reachable by third-party agent code.
    """
    payload = _claims(
        issue_agent_invoke_token(7, agent="invoice-helper", ttl_seconds=900)
    )

    all_routes = _all_v1_routes()
    assert len(all_routes) > 250, "route enumeration did not find the real app"

    permitted: list[tuple[str, str, tuple[str, ...]]] = []
    for method, path, deps in all_routes:
        try:
            _authorize_agent_invoke_request(payload, _request(method, path))
        except HTTPException:
            continue
        permitted.append((method, path, deps))

    assert permitted, "the scope table permits nothing at all"
    unbacked = [
        (method, path, deps)
        for method, path, deps in permitted
        if "current_user_or_agent_invoke" not in deps
    ]
    assert not unbacked, (
        "scope table permits routes whose dependency does not accept an invoke "
        f"token: {unbacked}"
    )


def test_narrower_scopes_shrink_the_callback_surface() -> None:
    payload = _claims(
        issue_agent_invoke_token(
            7, agent="invoice-helper", ttl_seconds=900, scopes=("agent:read",)
        )
    )

    _authorize_agent_invoke_request(payload, _request("GET", "/v1/agents/invoice-helper"))
    with pytest.raises(HTTPException):
        _authorize_agent_invoke_request(
            payload, _request("PUT", "/v1/agents/invoice-helper/memory")
        )


@pytest.mark.asyncio
async def test_every_invocation_carries_the_scoped_credential_in_body_and_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cp_jwt`` ships to every agent, and it is never a session.

    Gating the body field on ``runtime.wants_cp_jwt`` bought nothing — the same
    token is in the ``Authorization`` header of the same POST — while breaking
    subagent-run tracking for every agent that did not set the flag (the SDK's
    ``_post_tracking`` reads ``body.cp_jwt`` with no header fallback, and
    ``A2A_CP_JWT`` is only provisioned for ``wants_cp_jwt=True`` agents). That
    would blank the work ledger and LLM cost attribution for essentially every
    marketplace listing. The boundary is the token's shape, so send the field.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            buyer = User(email="buyer3@example.com", password_hash="x")
            session.add(buyer)
            await session.flush()
            quiet = _agent(buyer.id, "invoice-helper")
            asking = _agent(buyer.id, "memory-helper", wants_cp_jwt=True)
            session.add_all([quiet, asking])
            await session.commit()

            posted: list[dict[str, Any]] = []

            async def _capture(**kwargs: Any) -> dict[str, Any]:
                posted.append(kwargs)
                return {"result": "ok"}

            monkeypatch.setattr(agents, "_post_hosted_agent_invoke", _capture)

            await agents._call_hosted_agent_api_skill(
                agent=quiet,
                user=buyer,
                session=session,
                skill_name="review_invoice",
                arguments={},
            )
            await agents._call_hosted_agent_api_skill(
                agent=asking,
                user=buyer,
                session=session,
                skill_name="review_invoice",
                arguments={},
            )

    finally:
        await engine.dispose()

    quiet_call, asking_call = posted
    for call, name in ((quiet_call, "invoice-helper"), (asking_call, "memory-helper")):
        header = call["authorization"]
        assert header.startswith("Bearer ")
        header_token = header.split(None, 1)[1]
        # The body copy is the same scoped credential, not a second one — the
        # agent gains nothing by reading one rather than the other.
        assert call["body"]["cp_jwt"] == header_token
        claims = _claims(header_token)
        assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
        assert claims["aud"] == f"agent:{name}"
        assert claims["exp"] - claims["iat"] <= AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
        with pytest.raises(HTTPException):
            decode_token(header_token)
        # ...and it is scoped for the one thing the field exists to enable.
        _authorize_agent_invoke_request(
            claims, _request("POST", "/v1/me/subagent-runs/track")
        )


@pytest.mark.asyncio
async def test_over_http_the_token_opens_memory_but_not_an_ordinary_route() -> None:
    """The same assertion as above, but through real routing.

    ``/v1/agents/{name}/memory`` is on the invoke callback surface, so the
    forwarded credential must work there. ``/v1/settings`` stands in for every
    ordinary ``Depends(current_user)`` route the old 7-day token unlocked.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as setup:
            owner = User(email="owner-http@example.com", password_hash="x")
            setup.add(owner)
            await setup.flush()
            setup.add(_agent(owner.id, "memory-helper", wants_cp_jwt=True))
            await setup.commit()
            owner_id = owner.id

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as request_session:
                yield request_session

        app = FastAPI()
        app.include_router(memory_router)

        @app.get("/v1/settings")
        async def _ordinary_route(user: User = Depends(current_user)) -> dict[str, int]:
            return {"id": user.id}

        app.dependency_overrides[get_session] = _override_session

        token = issue_agent_invoke_token(
            owner_id, agent="memory-helper", ttl_seconds=900
        )
        session_token = issue_token(owner_id)
        with TestClient(app) as client:
            auth = {"authorization": f"Bearer {token}"}
            assert client.get("/v1/agents/memory-helper/memory", headers=auth).status_code == 200
            # Its own agent only — a token minted for one listing must not read
            # what the buyer stored against another.
            assert client.get("/v1/agents/other-agent/memory", headers=auth).status_code == 403
            assert client.get("/v1/settings", headers=auth).status_code == 401
            # A real session still works everywhere, so the 401 above is about
            # the credential class and not a broken route.
            ok = client.get(
                "/v1/settings", headers={"authorization": f"Bearer {session_token}"}
            )
            assert ok.status_code == 200 and ok.json() == {"id": owner_id}
    finally:
        await engine.dispose()


def test_platform_build_specialists_keep_a_broader_credential() -> None:
    """Documents the deliberate carve-out, and pins its blast radius.

    ``agent-builder`` and friends are first-party code in this repo, not
    marketplace listings, and their tools drive source/deploy routes a scoped
    invoke token withholds. They keep an ordinary credential — but capped to
    the invocation, not the default week.
    """
    builder = _agent(owner_id=1, name="agent-builder")
    token = agents._hosted_invoke_cp_token(
        agent=builder, user=User(id=7, email="b@t", password_hash="x")
    )
    claims = _claims(token)

    assert "typ" not in claims
    ttl = claims["exp"] - claims["iat"]
    assert ttl == agents._agent_api_grant_ttl_seconds(builder)
    assert ttl < settings.jwt_ttl_seconds


def test_the_carve_out_credential_is_clamped_too() -> None:
    """The widest credential must not also be the longest-lived one.

    The carve-out hands out an ordinary session (no ``typ``, ``current_user``
    resolves it), so an unclamped card here is worse than anywhere else: it is
    a full platform session for as long as the card asks for.
    """
    builder = _agent(
        owner_id=1, name="agent-builder", card=_hostile_runtime_card("agent-builder")
    )
    claims = _claims(
        agents._hosted_invoke_cp_token(
            agent=builder, user=User(id=7, email="b@t", password_hash="x")
        )
    )

    assert "typ" not in claims
    assert claims["exp"] - claims["iat"] == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
    assert claims["exp"] - claims["iat"] < settings.jwt_ttl_seconds


@pytest.mark.asyncio
async def test_bulk_agent_listing_serves_an_invocation_the_public_marketplace_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent:read`` is unbound, so ``GET /v1/agents`` must not be an inventory.

    A hand-off has to resolve a named target, which is why a card read is not
    pinned to the token's own agent. Bulk enumeration is a different thing:
    ``visible_agents_clause`` matches the buyer's private and org-only agents,
    each with its ``url`` and full card. A paid invocation gets the public
    marketplace instead.
    """
    monkeypatch.setattr(agents, "_refresh_cards_inplace", _noop_refresh)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            buyer = User(email="buyer-list@example.com", password_hash="x")
            session.add(buyer)
            await session.flush()
            session.add_all([
                _agent(buyer.id, "invoice-helper"),
                _agent(buyer.id, "buyer-secret-agent", public=False),
            ])
            await session.commit()

            browser = _request("GET", "/v1/agents")
            listed = await agents.list_agents(browser, user=buyer, session=session)
            # Control: the buyer's own session still sees their private agent,
            # so the assertion below is about the credential, not the query.
            assert {row.name for row in listed} == {
                "invoice-helper",
                "buyer-secret-agent",
            }

            invocation = _request("GET", "/v1/agents")
            invocation.state.agent_invoke_claims = _claims(
                issue_agent_invoke_token(
                    buyer.id, agent="invoice-helper", ttl_seconds=900
                )
            )
            seen = await agents.list_agents(invocation, user=buyer, session=session)
            assert {row.name for row in seen} == {"invoice-helper"}
    finally:
        await engine.dispose()


async def _noop_refresh(*args: Any, **kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# The orchestrator hand-off path (chat, schedules, template updates).
#
# ``/v1/chat`` reaches the same third-party processes as the paid agent-API
# route, and it used to write the caller's *live session* straight into the
# hand-off body. Scoping only the agent-API path would have left the primary
# product surface handing out week-long account takeovers.
# ---------------------------------------------------------------------------


async def _silent_emit(event: dict[str, Any]) -> None:
    return None


def _handoff_ctx(hooks: Any) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            agents_namespace_dns="{name}.agents.svc.cluster.local",
            litellm_url="http://litellm:4000",
            litellm_model="platform-model",
            platform_llm_models=("platform-model",),
            platform_llm_max_budget_usd=1.0,
            platform_llm_rpm_limit=60,
            platform_llm_tpm_limit=200000,
        ),
        hooks=hooks,
        bucket="user-7-files",
        user_id=7,
        policy_controls={},
    )


class _FakeStream:
    def __init__(self) -> None:
        self.status_code = 200

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_text(self) -> Any:
        yield 'data: {"type":"result","result":{"ok":true}}\n\n'

    async def aread(self) -> bytes:
        return b""


async def _handoff_body(
    monkeypatch: pytest.MonkeyPatch, hooks: Any, *, callee: str
) -> dict[str, Any]:
    import main_agent.tools.handoff as handoff
    from main_agent.tools.handoff import build_handoff_tools

    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def stream(
            self, method: str, url: str, *, json: dict[str, Any], headers: dict[str, str]
        ) -> _FakeStream:
            captured["url"] = url
            captured["body"] = json
            return _FakeStream()

    monkeypatch.setattr(handoff.httpx, "AsyncClient", FakeClient)

    async def get_agent_card(_name: str) -> dict[str, Any]:
        return {"skills": [{"name": "review_invoice", "stream": True}]}

    call_agent = build_handoff_tools(
        _handoff_ctx(replace(hooks, get_agent_card=get_agent_card))
    )[0]
    raw = await call_agent.ainvoke(
        {"name": callee, "skill": "review_invoice", "args_json": "{}"}
    )
    assert json.loads(raw).get("ok") is True, raw
    return captured["body"]


@pytest.mark.asyncio
async def test_chat_handoff_never_forwards_the_callers_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.routes import chat as chat_routes

    hooks = chat_routes._build_hooks(
        emit=_silent_emit,
        approval_mode=False,
        session=object(),
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=_silent_emit),
    )
    body = await _handoff_body(monkeypatch, hooks, callee="invoice-helper")

    forwarded = body["cp_jwt"]
    claims = _claims(forwarded)
    assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
    # Bound to the callee the orchestrator was asked to hand off to — the hook
    # is handed that name precisely so the token can name it.
    assert claims["aud"] == "agent:invoice-helper"
    assert claims["sub"] == "7"
    assert claims["exp"] - claims["iat"] <= AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
    with pytest.raises(HTTPException):
        decode_token(forwarded)


@pytest.mark.asyncio
async def test_chat_handoff_credential_is_clamped_for_the_toolchain_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.routes import chat as chat_routes

    hooks = chat_routes._build_hooks(
        emit=_silent_emit,
        approval_mode=False,
        session=object(),
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=_silent_emit),
    )
    body = await _handoff_body(monkeypatch, hooks, callee="agent-builder")

    claims = _claims(body["cp_jwt"])
    assert "typ" not in claims  # the deliberate carve-out
    assert claims["exp"] - claims["iat"] == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
    assert claims["exp"] - claims["iat"] < settings.jwt_ttl_seconds


@pytest.mark.asyncio
async def test_no_invoke_body_anywhere_carries_a_platform_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other three helpers that POST straight into an agent's ``/invoke``.

    Agent proofs, trial rooms and hybrid kernel simulations all built their
    body's ``cp_jwt`` from ``_credential_token(authorization, session_cookie)``
    — the caller's live session, forwarded verbatim into a process the platform
    does not control, for any registered agent the caller names.
    """
    from control_plane import live_kernel_simulations
    from control_plane.routes import agent_proofs

    posted: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        @property
        def text(self) -> str:
            return "{}"

        def json(self) -> dict[str, Any]:
            return {"result": {"ok": True}}

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
            posted.append(json)
            return FakeResponse()

    async def _no_creds(*args: Any, **kwargs: Any) -> None:
        return None

    for module in (agent_proofs, live_kernel_simulations):
        monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(module, "get_creds_for_user", _no_creds)

    agent = _agent(owner_id=1)
    user = User(id=7, email="b@t", password_hash="x")

    await agent_proofs._invoke_agent(
        agent=agent,
        skill_name="review_invoice",
        args={},
        grant="grant-token",
        session=object(),
        user=user,
    )
    await live_kernel_simulations._call_internal_agent(
        agent=agent,
        skill_name="review_invoice",
        arguments={},
        user=user,
        session=object(),
    )

    assert len(posted) == 2
    for body in posted:
        claims = _claims(body["cp_jwt"])
        assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
        assert claims["aud"] == "agent:invoice-helper"
        assert claims["exp"] - claims["iat"] <= AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS
        with pytest.raises(HTTPException):
            decode_token(body["cp_jwt"])


@pytest.mark.asyncio
async def test_unattended_run_paths_mint_the_same_scoped_credential() -> None:
    """Schedules and template updates feed the same hand-off tool.

    Both built their hook from ``issue_token(user.id)`` — a full 7-day session,
    with no user watching the run that hands it out.
    """
    from control_plane import agent_schedules, template_updates

    user = SimpleNamespace(id=7)
    builders = {
        "agent_schedules": agent_schedules._schedule_hooks(
            object(),
            user=user,
            schedule=SimpleNamespace(schedule_id="sched-1"),
            job_id="job-1",
            thread_id=None,
        ),
        "template_updates": template_updates._template_update_hooks(
            object(),
            user=user,
            agent=SimpleNamespace(name="invoice-helper"),
            job=SimpleNamespace(id=1),
        ),
    }

    for source, hooks in builders.items():
        assert hooks.get_cp_jwt is not None, source
        pair = await hooks.get_cp_jwt("invoice-helper")
        claims = _claims(pair["jwt"])
        assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE, source
        assert claims["aud"] == "agent:invoice-helper", source
        assert claims["exp"] - claims["iat"] <= AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS, source
        with pytest.raises(HTTPException):
            decode_token(pair["jwt"])
