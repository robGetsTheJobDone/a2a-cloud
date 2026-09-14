"""What an agent's *pod* holds, and what a copy of it is worth.

``runtime.wants_cp_jwt`` writes ``A2A_CP_JWT`` into the agent's runtime secret
at deploy time. Until this module existed that value was
``issue_token(user.id, ttl_seconds=60 * 60 * 24 * 365)`` — a 365-day platform
session for the deploying user, the same credential class as the dashboard
cookie, accepted by every ``Depends(current_user)`` route, persisted in the
secret store and present in the environment of every process in that pod.

Unlike the per-invocation ``cp_jwt``, nothing refreshes this one: only the next
deploy re-mints it, so it cannot be call-length. What it can be is the same
*class* as the invoke credential — typed, audience-bound, scope-checked, and
rejected by ``decode_token`` — which is what these tests pin.

The mail-ingress bearer is here too: it is the other credential the platform
hands to seller-controlled code outside the invoke path.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, AsyncIterator

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane import agent_review, mail_ingress as mail_ingress_module
from control_plane.auth import (
    AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS,
    AGENT_INVOKE_TOKEN_TYPE,
    AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS,
    AGENT_RUNTIME_TOKEN_TYPE,
    PLATFORM_TOKEN_MAX_TTL_SECONDS,
    PLATFORM_TOOLCHAIN_AGENTS,
    _AGENT_INVOKE_SCOPES,
    _authorize_agent_invoke_request,
    current_user,
    decode_token,
    issue_agent_runtime_token,
    issue_invocation_cp_credential,
    issue_runtime_cp_credential,
    issue_token,
)
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.k8s import KNATIVE_MAX_TIMEOUT_SECONDS
from control_plane.mail_ingress import EMAIL_HANDLER_TAG, parse_inbound_email
from control_plane.models import Agent, User
from control_plane.routes import agents
from control_plane.routes.memory import router as memory_router


_ONE_YEAR_SECONDS = 60 * 60 * 24 * 365
#: The ceiling ``routes/admin.py`` puts on an operator-minted user token.
_ADMIN_PLATFORM_TOKEN_MAX_TTL_SECONDS = 2_592_000


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


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_alg],
        options={"verify_aud": False},
    )


def _provisioned_secret(agent_name: str, user_id: int, monkeypatch) -> dict[str, Any]:
    """Run the real deploy-time provisioning and return the secret written."""
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        agents.secret_store,
        "upsert_agent_secret_value",
        lambda **kwargs: written.append(kwargs),
    )
    agents._provision_runtime_cp_jwt_if_requested(
        dsl=SimpleNamespace(runtime=SimpleNamespace(wants_cp_jwt=True)),
        agent=SimpleNamespace(name=agent_name),
        user=SimpleNamespace(id=user_id),
    )
    assert len(written) == 1
    return written[0]


def test_provisioned_runtime_secret_is_a_scoped_credential_not_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = _provisioned_secret("growth-signal-agent", 42, monkeypatch)
    assert secret["key"] == "A2A_CP_JWT"
    claims = _claims(secret["value"])

    assert claims["typ"] == AGENT_RUNTIME_TOKEN_TYPE
    assert claims["aud"] == "agent:growth-signal-agent"
    assert claims["target_agent"] == "growth-signal-agent"
    assert set(claims["scopes"]) <= _AGENT_INVOKE_SCOPES
    # The whole point: this is not a platform session.
    with pytest.raises(HTTPException) as excinfo:
        decode_token(secret["value"])
    assert excinfo.value.status_code == 401


def test_provisioned_runtime_secret_is_ttl_bounded_well_under_a_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = _claims(_provisioned_secret("growth-signal-agent", 42, monkeypatch)["value"])
    lifetime = claims["exp"] - claims["iat"]

    assert lifetime == AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS
    assert lifetime < _ONE_YEAR_SECONDS
    assert agents._RUNTIME_CP_JWT_TTL_SECONDS < _ONE_YEAR_SECONDS


def test_runtime_token_ttl_cannot_be_argued_past_the_ceiling() -> None:
    claims = _claims(
        issue_agent_runtime_token(7, agent="invoice-helper", ttl_seconds=_ONE_YEAR_SECONDS)
    )
    assert claims["exp"] - claims["iat"] == AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS


def test_the_pod_credential_ceiling_is_anchored_not_invented() -> None:
    """The one number that sets the residual blast radius, pinned to a peer.

    A credential that sits in a pod environment and is never refreshed should
    not get a longer life than the longest credential the platform already
    issues anywhere — which is the operator-minted user token that
    ``routes/admin.py`` bounds at 30 days. Pinning them together is what stops
    the ceiling drifting upward in a later edit without anyone deciding to.
    """
    assert PLATFORM_TOKEN_MAX_TTL_SECONDS == _ADMIN_PLATFORM_TOKEN_MAX_TTL_SECONDS
    assert AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS <= PLATFORM_TOKEN_MAX_TTL_SECONDS
    # And it is still far longer than a call, which is why it may not be a
    # session in the first place.
    assert AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS > AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS


@pytest.mark.parametrize("toolchain_agent", sorted(PLATFORM_TOOLCHAIN_AGENTS))
def test_the_pod_credential_is_never_stronger_than_the_per_call_one(
    toolchain_agent: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant, checked where it is most likely to be violated.

    ``issue_invocation_cp_credential`` deliberately hands the four platform
    build specialists an ordinary session for the length of one call. Those
    same four are the only agents in this repo that declare
    ``runtime.wants_cp_jwt``, so they are exactly the agents whose *pod* would
    inherit that carve-out if the carve-out were copied here — and the pod
    credential is the one that survives the call by weeks.

    So the carve-out is not copied, and this pins both halves: the pod value is
    the scoped class for a toolchain agent just as it is for a marketplace
    listing, and the capability that costs is asserted rather than assumed.
    """
    secret = _provisioned_secret(toolchain_agent, 42, monkeypatch)
    claims = _claims(secret["value"])

    assert claims["typ"] == AGENT_RUNTIME_TOKEN_TYPE
    assert claims["aud"] == f"agent:{toolchain_agent}"
    with pytest.raises(HTTPException) as excinfo:
        decode_token(secret["value"])
    assert excinfo.value.status_code == 401

    # The per-call credential for the same agent *is* a session — that is the
    # carve-out — so the two genuinely differ, and this test is not vacuous.
    per_call = issue_invocation_cp_credential(42, agent=toolchain_agent)
    assert "typ" not in _claims(per_call)
    assert decode_token(per_call) == 42

    # What the pod copy therefore cannot do. This is the documented purpose of
    # ``wants_cp_jwt`` ("/v1/me/files, /v1/agents/from-tarball, etc."), and it
    # is reachable only with the per-call credential from here on.
    for method, path in (
        ("POST", "/v1/platform/gitea-token"),
        ("GET", "/v1/me/files"),
        ("POST", "/v1/agents/from-tarball"),
        ("POST", f"/v1/agents/{toolchain_agent}/source/deploy"),
    ):
        with pytest.raises(HTTPException) as denied:
            _authorize_agent_invoke_request(claims, _request(method, path))
        assert denied.value.status_code == 403, (method, path)


def test_the_runtime_class_is_chosen_in_one_place() -> None:
    """``issue_runtime_cp_credential`` is the policy, not the mint helper.

    Deploy-time provisioning routes through it for every agent, so the decision
    about which class a pod holds cannot be re-made at the call site.
    """
    for name in (*sorted(PLATFORM_TOOLCHAIN_AGENTS), "invoice-helper"):
        claims = _claims(issue_runtime_cp_credential(42, agent=name))
        assert claims["typ"] == AGENT_RUNTIME_TOKEN_TYPE
        assert claims["target_agent"] == name
        assert claims["exp"] - claims["iat"] == AGENT_RUNTIME_TOKEN_MAX_TTL_SECONDS


def test_runtime_token_scopes_are_validated_at_mint_time() -> None:
    with pytest.raises(ValueError):
        issue_agent_runtime_token(7, agent="invoice-helper", scopes=("source:write",))
    with pytest.raises(ValueError):
        issue_agent_runtime_token(7, agent="   ")


def test_runtime_token_reaches_the_invoke_surface_and_nothing_wider() -> None:
    """It is gated by the same table as the per-call credential."""
    claims = _claims(issue_agent_runtime_token(7, agent="invoice-helper"))

    _authorize_agent_invoke_request(claims, _request("GET", "/v1/agents/invoice-helper/memory"))
    _authorize_agent_invoke_request(claims, _request("POST", "/v1/me/subagent-runs/track"))
    for method, path in (
        ("DELETE", "/v1/agents/invoice-helper"),
        ("GET", "/v1/me/onboarding"),
        ("GET", "/v1/agents/invoice-helper/receipts"),
        ("GET", "/v1/me/llm-credentials"),
        ("GET", "/v1/agents/someone-elses-agent/memory"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            _authorize_agent_invoke_request(claims, _request(method, path))
        assert excinfo.value.status_code == 403, (method, path)


@pytest.mark.asyncio
async def test_over_http_a_leaked_runtime_secret_is_not_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing case: scrape ``A2A_CP_JWT`` out of the pod, then try it.

    ``/v1/settings`` stands in for every ordinary ``Depends(current_user)``
    route the old 365-day token unlocked. The control assertion at the bottom
    runs a plain ``issue_token`` against the same route, so this test fails
    loudly if someone reverts the mint site instead of passing vacuously.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as setup:
            owner = User(email="runtime-owner@example.com", password_hash="x")
            setup.add(owner)
            await setup.flush()
            setup.add(
                Agent(
                    owner_id=owner.id,
                    name="memory-helper",
                    description="Remembers things",
                    version="1.0.0",
                    image="example/memory",
                    public=True,
                    status="running",
                    url="https://memory.test",
                    card={"runtime": {"wants_cp_jwt": True}},
                )
            )
            await setup.commit()
            owner_id = owner.id

        leaked = _provisioned_secret("memory-helper", owner_id, monkeypatch)["value"]

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as request_session:
                yield request_session

        app = FastAPI()
        app.include_router(memory_router)

        @app.get("/v1/settings")
        async def _ordinary_route(user: User = Depends(current_user)) -> dict[str, int]:
            return {"id": user.id}

        app.dependency_overrides[get_session] = _override_session

        with TestClient(app) as client:
            headers = {"authorization": f"Bearer {leaked}"}
            # Still does the job the runtime secret exists for.
            assert client.get("/v1/agents/memory-helper/memory", headers=headers).status_code == 200
            # ...its own agent only.
            assert client.get("/v1/agents/other-agent/memory", headers=headers).status_code == 403
            # ...and it is not an account.
            assert client.get("/v1/settings", headers=headers).status_code == 401
            assert (
                client.get(
                    "/v1/settings", cookies={settings.session_cookie_name: leaked}
                ).status_code
                == 401
            )
            # Control: a real session still works on the same route, so the
            # 401s above are about the credential class, not a broken route.
            ok = client.get(
                "/v1/settings",
                headers={"authorization": f"Bearer {issue_token(owner_id)}"},
            )
            assert ok.status_code == 200 and ok.json() == {"id": owner_id}
    finally:
        await engine.dispose()


def test_a_legacy_365_day_pod_session_is_refused_at_decode() -> None:
    """The installed base, not just the next deploy.

    Every agent already running holds the value it was provisioned with: an
    untyped 365-day session, which the new ``typ`` checks cannot see because it
    has no ``typ``. Nothing revokes individual tokens, so without a rule on the
    *shape* this change protects no agent that exists today.
    """
    legacy = issue_token(42, ttl_seconds=_ONE_YEAR_SECONDS)

    with pytest.raises(HTTPException) as excinfo:
        decode_token(legacy)
    assert excinfo.value.status_code == 401

    # Controls: everything the platform legitimately mints still decodes, so
    # the rule kills the legacy shape and not sessions in general.
    assert decode_token(issue_token(42)) == 42
    assert (
        decode_token(
            issue_token(42, ttl_seconds=_ADMIN_PLATFORM_TOKEN_MAX_TTL_SECONDS)
        )
        == 42
    )


def test_the_lifetime_rule_follows_a_longer_configured_session() -> None:
    """An operator who lengthens browser sessions does not lock themselves out."""
    long_session = int(settings.jwt_ttl_seconds) * 2 + PLATFORM_TOKEN_MAX_TTL_SECONDS
    token = issue_token(42, ttl_seconds=long_session)
    with pytest.raises(HTTPException):
        decode_token(token)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "jwt_ttl_seconds", long_session)
        assert decode_token(token) == 42
    finally:
        monkey.undo()


@pytest.mark.asyncio
async def test_over_http_a_legacy_pod_session_stops_being_an_account() -> None:
    """The same scrape-the-pod path, for a token minted before this change."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as setup:
            owner = User(email="legacy-owner@example.com", password_hash="x")
            setup.add(owner)
            await setup.commit()
            owner_id = owner.id

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as request_session:
                yield request_session

        app = FastAPI()

        @app.get("/v1/settings")
        async def _ordinary_route(user: User = Depends(current_user)) -> dict[str, int]:
            return {"id": user.id}

        app.dependency_overrides[get_session] = _override_session

        with TestClient(app) as client:
            legacy = issue_token(owner_id, ttl_seconds=_ONE_YEAR_SECONDS)
            assert (
                client.get(
                    "/v1/settings", headers={"authorization": f"Bearer {legacy}"}
                ).status_code
                == 401
            )
            assert (
                client.get(
                    "/v1/settings", cookies={settings.session_cookie_name: legacy}
                ).status_code
                == 401
            )
            # Control: the dashboard still works.
            ok = client.get(
                "/v1/settings",
                headers={"authorization": f"Bearer {issue_token(owner_id)}"},
            )
            assert ok.status_code == 200 and ok.json() == {"id": owner_id}
    finally:
        await engine.dispose()


def test_the_reviewer_identity_outlives_a_heartbeating_review() -> None:
    """The reviewer streams, so its httpx timeout is not the call's bound.

    ``_call_reviewer`` passes ``REVIEWER_TIMEOUT_S + 30`` to
    ``httpx.AsyncClient`` and then streams SSE, where that value bounds the gap
    between reads; the reviewer emits a heartbeat every 20s, so it never trips.
    A credential cut to that number expires mid-review and 401s the reviewer's
    own ``release_gitea_token`` in its ``finally``. The bound that is real is
    the reviewer's Knative revision timeout.
    """
    assert agent_review.REVIEWER_CALL_TTL_SECONDS > int(
        agent_review.REVIEWER_TIMEOUT_S
    ) + 30
    assert agent_review.REVIEWER_CALL_TTL_SECONDS > KNATIVE_MAX_TIMEOUT_SECONDS
    assert agent_review.REVIEWER_CALL_TTL_SECONDS == AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS


_RAW_EMAIL = (
    b"From: client@corp.io\r\n"
    b"To: mailer@example.com\r\n"
    b"Subject: Need pricing\r\n"
    b"Message-ID: <pricing@corp.io>\r\n"
    b"\r\n"
    b"How much for 100 seats?\r\n"
)


@pytest.mark.asyncio
async def test_mailbox_invocation_bearer_is_scoped_not_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inbound mail invokes seller code, so the bearer it carries is scoped.

    It used to be ``issue_token(owner_id, ttl_seconds=3600)``: an hour of the
    mailbox owner's whole account, in the ``Authorization`` header of a request
    to a process the platform does not control.

    The same credential also has to be in the *body*. The SDK builds
    ``ctx.cp_jwt`` from ``body.cp_jwt``, then the cookie, then the pod's own
    ``A2A_CP_JWT`` — never from this header — so a header-only call was the one
    control-plane-driven path that silently ran on the agent's deploy-time
    credential instead of the one minted for this email.
    """
    captured: list[tuple[dict[str, str] | None, str | None]] = []

    async def _fake_invoke(
        base_url, agent_name, skill, arguments, headers, cp_jwt=None
    ):
        captured.append((headers, cp_jwt))
        return {"body": "handled"}

    monkeypatch.setattr(mail_ingress_module, "_post_invoke", _fake_invoke)
    agent = Agent(
        owner_id=11,
        name="mailer",
        description="",
        version="1.0.0",
        image="x",
        public=True,
        status="running",
        url="https://mailer.example.com",
        card={"skills": [{"id": "handle_email", "tags": [EMAIL_HANDLER_TAG]}]},
    )
    await mail_ingress_module._invoke_agent_with_email(
        agent, parse_inbound_email(7, _RAW_EMAIL)
    )

    headers, body_cp_jwt = captured[0]
    assert headers is not None
    token = headers["Authorization"].split(None, 1)[1]
    # Body and header carry the same credential, so nothing here falls through
    # to the agent's long-lived ``A2A_CP_JWT``.
    assert body_cp_jwt == token
    claims = _claims(token)
    assert claims["typ"] == AGENT_INVOKE_TOKEN_TYPE
    assert claims["aud"] == "agent:mailer"
    assert claims["exp"] - claims["iat"] <= 3600
    with pytest.raises(HTTPException):
        decode_token(token)
    # Control: an ordinary session of the same owner still decodes, so the
    # rejection above is a property of this credential, not of the secret.
    assert decode_token(issue_token(11)) == 11


@pytest.mark.asyncio
async def test_mail_invocation_puts_the_credential_in_the_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real ``_post_invoke``, so the wire shape is what is checked.

    The credential class test above stubs ``_post_invoke`` and therefore cannot
    see whether the value reaches the request body — and the body is the only
    place the SDK looks for it. This one asserts the serialized POST.
    """
    posted: dict[str, Any] = {}

    class _Response:
        status_code = 200
        text = '{"result": {"body": "handled"}}'

    async def _fake_request(method, url, **kwargs):
        posted["url"] = url
        posted["headers"] = kwargs.get("sensitive_headers")
        posted["body"] = kwargs.get("json_body")
        return _Response()

    async def _fake_grant(agent_name, skill, agent=None):
        return "grant-token"

    monkeypatch.setattr(mail_ingress_module, "safe_request_url", _fake_request)
    monkeypatch.setattr(mail_ingress_module, "_mint_mail_grant", _fake_grant)
    agent = Agent(
        owner_id=11,
        name="mailer",
        description="",
        version="1.0.0",
        image="x",
        public=True,
        status="running",
        url="https://mailer.example.com",
        card={"skills": [{"id": "handle_email", "tags": [EMAIL_HANDLER_TAG]}]},
    )
    await mail_ingress_module._invoke_agent_with_email(
        agent, parse_inbound_email(7, _RAW_EMAIL)
    )

    body = posted["body"]
    assert body["grant"] == "grant-token"
    header_token = posted["headers"]["Authorization"].split(None, 1)[1]
    assert body["cp_jwt"] == header_token
    assert _claims(body["cp_jwt"])["typ"] == AGENT_INVOKE_TOKEN_TYPE


@pytest.mark.asyncio
async def test_deploy_review_identity_expires_with_the_review_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent-reviewer`` is first-party, so it keeps an ordinary credential.

    It does not keep a week of one. The reviewer runs the caller's source
    surface on their behalf — see ``PLATFORM_TOOLCHAIN_AGENTS`` — so the carve
    out is deliberate, and the TTL is the reviewer call's own outer bound.
    """
    captured: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(agent_review, "run_deploy_review", _fake_run)
    app = SimpleNamespace(state=SimpleNamespace())

    review_id = agent_review.enqueue_deploy_review(
        app,
        agent_id=1,
        agent_name="invoice-helper",
        ref="deadbeef",
        owner="owner-repos",
        user_id=5,
    )
    assert review_id
    for task in list(getattr(app.state, "agent_review_tasks", set())):
        await task

    claims = _claims(captured["user_jwt"])
    assert "typ" not in claims  # deliberate first-party carve-out
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == agent_review.REVIEWER_CALL_TTL_SECONDS
    assert lifetime < settings.jwt_ttl_seconds


@pytest.mark.asyncio
async def test_deploy_review_identity_is_scoped_for_a_non_platform_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point ``A2A_CP_REVIEWER_AGENT`` elsewhere and the carve-out disappears."""
    captured: dict[str, Any] = {}

    async def _fake_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(agent_review, "run_deploy_review", _fake_run)
    monkeypatch.setattr(agent_review, "REVIEWER_AGENT_NAME", "someones-own-reviewer")
    app = SimpleNamespace(state=SimpleNamespace())

    agent_review.enqueue_deploy_review(
        app,
        agent_id=1,
        agent_name="invoice-helper",
        ref="deadbeef",
        owner="owner-repos",
        user_id=5,
    )
    for task in list(getattr(app.state, "agent_review_tasks", set())):
        await task

    token = captured["user_jwt"]
    assert _claims(token)["typ"] == AGENT_INVOKE_TOKEN_TYPE
    with pytest.raises(HTTPException):
        decode_token(token)
