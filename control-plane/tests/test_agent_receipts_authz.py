"""Authorization matrix for receipt / replay-session reads.

Regression guard for the disclosure bug where ``GET /v1/agents/{name}/receipts``,
``GET /v1/agents/{name}/receipts/{receipt_id}`` and
``GET /v1/agents/{name}/sessions`` served signed tokens (whose payload carries
the *caller's* prompt text, input hash, grant ids and result preview) to
completely anonymous callers for any agent, public or not.

Policy asserted here:

* an **evidence reader** of the agent reads every row. One definition, in
  ``agent_authorization.may_read_agent_evidence``: a platform admin, or anyone
  ``decide_agent_access`` would let ``edit_existing`` the agent — its owner and
  the organization maintainers/admins/owners who can push its source. Whoever
  may ship the code may read its evidence; a plain org member, who can only
  discover and invoke, may not;
* anyone else who can **discover** the agent reads only **their own calls** —
  the rows whose ``caller`` is ``user:<their id>``. Refusing a caller the
  receipt for their own run was a regression, and a second "my receipts" route
  would have drifted from this one;
* everyone else — anonymous, or logged in without discovery — gets **404**
  carrying the body of a genuinely missing agent, so the route is not an
  oracle for private agent names;
* ``GET /v1/sessions/{session_id}`` is **not** a capability URL, and has **no
  anonymous branch at all**. Its event stream carries ``skill_start`` with the
  caller's complete validated arguments (``a2a_pack.agent``). The proof-run
  "publication" grant an earlier round added is gone: it keyed on
  ``AgentProofRun.events``, a blob stored verbatim from owner-supplied agent
  code, so an owner could republish a *third party's* session by naming its id.
  Readers are the agent's evidence readers and the caller who produced it;
* ``routes/compliance.py`` serves byte-identical receipt payloads at
  ``/decision-records/skill_execution/{id}`` and is held to the same rule, via
  the same functions — this file asserts the two routes agree, because a
  divergence there is a policy the platform does not enforce;
* both POSTs stay unauthenticated at the HTTP layer — they are authenticated
  by Ed25519 signature verification, which is the runtime's self-reporting
  path.

Unlike an earlier version of this file, **nothing here overrides
``optional_current_user``**. Every request carries a real credential through
the real auth stack, because the interesting failures are token-type failures:
a Studio job token that ``decode_token`` rejects silently downgrades to
anonymous, and a malformed ``Authorization`` header used to raise an unhandled
``IndexError``. A dependency-override suite cannot see either.
"""
from __future__ import annotations

import os

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import time
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.receipts import seal_receipt
from a2a_pack.replay import EventRecorder, seal_replay_session

from control_plane import object_store
from control_plane.agent_frontend_session import mint_agent_session_token
from control_plane.auth import (
    issue_agent_invoke_token,
    issue_studio_job_token,
    issue_token,
)
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.models import (
    Agent,
    AgentProofRun,
    AgentSession,
    AgentStudioRun,
    Organization,
    OrganizationMember,
    User,
)
from control_plane.object_store import InMemoryReplayObjectStore
from control_plane.routes.agent_proofs import (
    public_agent_router as proofs_public_agent_router,
    public_router as proofs_public_router,
)
from control_plane.routes.agent_receipts import router as receipts_router
from control_plane.routes.agent_sessions import agent_router as sessions_agent_router
from control_plane.routes.agent_sessions import session_router as sessions_router
from control_plane.routes.compliance import router as compliance_router

PUBLIC_AGENT = "public-agent"
PRIVATE_AGENT = "private-agent"
ORG_SLUG = "acme"
STUDIO_RUN_ID = "studio-run-1"

# The caller's prompt text lives inside the signed token's base64 payload —
# this is the string that must never reach a caller who is not a reader.
SECRET_PROMPT = "I need a demo agent for investors that shows off what a2a cloud can do"

# What the SDK and the gateway actually mint: secrets.token_hex(16).
CAP_SESSION = {
    PUBLIC_AGENT: "9b1f0c2a4d6e8f01a3b5c7d9e1f30245",
    PRIVATE_AGENT: "1a2b3c4d5e6f708192a3b4c5d6e7f809",
}
# What a hand-rolled runtime can seal instead: build_session() honours a
# caller-supplied session_id and nothing validates its entropy.
GUESSABLE_SESSION = "session-1"

# What ``agent_ingress._external_caller`` actually writes: the gateway observes
# that a credential was presented but cannot assert *whose* it was, so these
# rows belong to nobody as far as authorization is concerned.
UNATTRIBUTED_CALLER = "credential-present"

# Session ids for the rows a real buyer produced (caller == "user:<buyer id>").
BUYER_SESSION = "5c7e9a1b3d5f70819a2b4c6d8e0f1234"
BUYER_PRIVATE_SESSION = "77ee55cc33aa1188ff66dd44bb22990a"


def _agent_row(
    owner_id: int, name: str, *, public: bool, org_id: int | None = None
) -> Agent:
    return Agent(
        owner_id=owner_id,
        name=name,
        description="",
        version="1.0.0",
        image=f"registry.a2acloud.io/agents/{name}:latest",
        public=public,
        status="running",
        url=f"https://{name}.a2acloud.io",
        card={"skills": [{"name": "search"}]},
        organization_id=org_id,
    )


def _receipt_token(agent_name: str, *, caller: str = UNATTRIBUTED_CALLER) -> str:
    started = int(time.time()) - 5
    _, token = seal_receipt(
        agent_name=agent_name,
        skill_name="search",
        started_at=started,
        ended_at=started + 2,
        caller=caller,
        inputs={"prompt": SECRET_PROMPT},
        result={"answer": "a private answer"},
    )
    return token


def _session_token(
    agent_name: str,
    *,
    session_id: str | None = None,
    caller: str = UNATTRIBUTED_CALLER,
) -> str:
    recorder = EventRecorder(
        agent_name=agent_name,
        skill_name="search",
        caller=caller,
        receipt_id="",
    )
    # a2a_pack.agent records the caller's validated arguments in this event.
    recorder.record("skill_start", {"args": {"prompt": SECRET_PROMPT}})
    recorder.record("skill_end", {"ok": True})
    built = recorder.build_session(
        session_id=session_id,
        ended_at=int(time.time()) + 2,
    )
    _, token = seal_replay_session(built)
    return token


async def proof_run_naming_session(
    Session: Any,
    *,
    agent_name: str,
    session_id: str,
    status: str = "passed",
    count: int = 1,
    padding: int = 0,
) -> int:
    """Store proof runs whose event stream names ``session_id``.

    This is what ``routes/agent_proofs.run_agent_proof`` persists: ``run.events``
    verbatim from whatever the agent process returned, and the agent process is
    **owner-supplied code**. An earlier round joined the anonymous session grant
    onto exactly this blob, so an owner could publish any third party's replay
    session — including a buyer's ``skill_start`` arguments — by emitting a
    ``replay_sealed`` event naming its id. Nothing here may open a session.
    """
    async with Session() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one()
        last = 0
        for _ in range(count):
            run = AgentProofRun(
                agent_id=agent.id,
                agent_name=agent_name,
                user_id=agent.owner_id,
                skill_name="search",
                status=status,
                events=[
                    {"kind": "receipt_sealed", "payload": {"receipt_id": "r-1"}},
                    {
                        "kind": "replay_sealed",
                        "payload": {
                            "token": "t",
                            "session_id": session_id,
                            "receipt_id": "r-1",
                            "pad": "x" * padding,
                        },
                    },
                ],
            )
            session.add(run)
            await session.flush()
            last = int(run.id)
        await session.commit()
        return last


class Env:
    client: AsyncClient
    Session: Any
    engine: Any
    owner: User
    intruder: User
    admin: User
    org_admin: User
    org_maintainer: User
    org_member: User
    buyer: User
    receipts: dict[str, str]
    sessions: dict[str, str]
    buyer_receipt: str
    member_receipt: str


@pytest.fixture
async def env():
    """App + DB + in-memory object store. No auth dependency is overridden."""
    store = InMemoryReplayObjectStore()
    object_store.set_default_store(store)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(receipts_router)
    app.include_router(sessions_agent_router)
    app.include_router(sessions_router)
    # Mounted so the sibling route that serves the same receipt bytes is held
    # to the same policy by the tests below.
    app.include_router(compliance_router)
    # Proof runs are the third surface carrying this material — same arguments,
    # same result, same sealed event stream, published under /v1/public. Held
    # here so the three cannot drift apart again.
    app.include_router(proofs_public_router)
    app.include_router(proofs_public_agent_router)

    async def _override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    async with Session() as session:
        owner = User(email="owner@example.com", password_hash="x")
        intruder = User(email="intruder@example.com", password_hash="x")
        admin = User(email="admin@example.com", password_hash="x", is_admin=True)
        org_admin = User(email="orgadmin@example.com", password_hash="x")
        org_maintainer = User(email="orgmaintainer@example.com", password_hash="x")
        org_member = User(email="orgmember@example.com", password_hash="x")
        # Someone who *called* the public agent through the platform API. They
        # own nothing, are in no organization, and are billed off the caller
        # string ``routes/agents._persist_agent_api_receipt`` writes.
        buyer = User(email="buyer@example.com", password_hash="x")
        session.add_all(
            [owner, intruder, admin, org_admin, org_maintainer, org_member, buyer]
        )
        await session.flush()
        org = Organization(name="Acme", slug=ORG_SLUG, created_by_id=owner.id)
        session.add(org)
        await session.flush()
        session.add_all(
            [
                OrganizationMember(
                    organization_id=org.id, user_id=owner.id, role="owner", active=True
                ),
                OrganizationMember(
                    organization_id=org.id,
                    user_id=org_admin.id,
                    role="admin",
                    active=True,
                ),
                # A maintainer may push new source to this agent
                # (``decide_agent_access(action="edit_existing")``), so denying
                # them the evidence produced by the code they ship is a policy
                # nobody chose. A plain member can only discover and invoke, and
                # must not read the callers' prompts.
                OrganizationMember(
                    organization_id=org.id,
                    user_id=org_maintainer.id,
                    role="maintainer",
                    active=True,
                ),
                OrganizationMember(
                    organization_id=org.id,
                    user_id=org_member.id,
                    role="member",
                    active=True,
                ),
            ]
        )
        session.add_all(
            [
                _agent_row(owner.id, PUBLIC_AGENT, public=True, org_id=org.id),
                _agent_row(owner.id, PRIVATE_AGENT, public=False, org_id=org.id),
            ]
        )
        session.add(
            AgentStudioRun(
                run_id=STUDIO_RUN_ID,
                user_id=owner.id,
                agent_name=PUBLIC_AGENT,
                status="building",
            )
        )
        await session.commit()

    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    env = Env()
    env.client = client
    env.Session = Session
    env.engine = engine
    env.owner = owner
    env.intruder = intruder
    env.admin = admin
    env.org_admin = org_admin
    env.org_maintainer = org_maintainer
    env.org_member = org_member
    env.buyer = buyer
    env.receipts = {}
    env.sessions = {}

    # Seed one receipt + one replay session per agent through the real POST
    # paths, anonymously — proving the self-reporting path needs no user.
    for name in (PUBLIC_AGENT, PRIVATE_AGENT):
        r = await client.post(
            f"/v1/agents/{name}/receipts",
            json={"signed_token": _receipt_token(name)},
        )
        assert r.status_code == 201, r.text
        env.receipts[name] = r.json()["receipt_id"]

        s = await client.post(
            f"/v1/agents/{name}/sessions",
            json={"signed_token": _session_token(name, session_id=CAP_SESSION[name])},
        )
        assert s.status_code == 201, s.text
        env.sessions[name] = s.json()["session_id"]

    # And one attributable call per agent: what the platform's own agent API
    # records (``caller = f"user:{user.id}"``) when ``buyer`` invokes.
    buyer_ref = f"user:{buyer.id}"
    r = await client.post(
        f"/v1/agents/{PUBLIC_AGENT}/receipts",
        json={"signed_token": _receipt_token(PUBLIC_AGENT, caller=buyer_ref)},
    )
    assert r.status_code == 201, r.text
    env.buyer_receipt = r.json()["receipt_id"]
    for name, sid in (
        (PUBLIC_AGENT, BUYER_SESSION),
        (PRIVATE_AGENT, BUYER_PRIVATE_SESSION),
    ):
        s = await client.post(
            f"/v1/agents/{name}/sessions",
            json={
                "signed_token": _session_token(
                    name, session_id=sid, caller=buyer_ref
                )
            },
        )
        assert s.status_code == 201, s.text

    # A plain org member's own call on the org's private agent. They are not an
    # evidence reader, but they are in the org — so this row is the one place
    # the receipts route and the org compliance route must agree on "mine".
    m = await env.client.post(
        f"/v1/agents/{PRIVATE_AGENT}/receipts",
        json={
            "signed_token": _receipt_token(
                PRIVATE_AGENT, caller=f"user:{org_member.id}"
            )
        },
    )
    assert m.status_code == 201, m.text
    env.member_receipt = m.json()["receipt_id"]

    try:
        yield env
    finally:
        await client.aclose()
        await engine.dispose()
        object_store.set_default_store(None)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _read_urls(env: Env, name: str) -> list[str]:
    return [
        f"/v1/agents/{name}/receipts?limit=2",
        f"/v1/agents/{name}/receipts/{env.receipts[name]}",
        f"/v1/agents/{name}/sessions?limit=2",
    ]


def _assert_no_leak(resp) -> None:
    """A denied response must not carry receipt/replay material."""
    body = resp.text
    assert "signed_token" not in body
    assert SECRET_PROMPT not in body
    assert "a private answer" not in body
    assert "X-A2A-Replay-Token" not in resp.headers


# --------------------------------------------------------------------------
# POST paths are unchanged: Ed25519 signature is the credential.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_post_receipt_still_works_without_any_user(env, agent_name: str) -> None:
    resp = await env.client.post(
        f"/v1/agents/{agent_name}/receipts",
        json={"signed_token": _receipt_token(agent_name, caller="user-9")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["signed_token"]


@pytest.mark.asyncio
async def test_post_session_still_works_without_any_user(env) -> None:
    resp = await env.client.post(
        f"/v1/agents/{PRIVATE_AGENT}/sessions",
        json={"signed_token": _session_token(PRIVATE_AGENT)},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_post_receipt_signature_still_enforced(env) -> None:
    token = _receipt_token(PUBLIC_AGENT)
    payload, signature = token.rsplit(".", 1)
    first = "A" if payload[0] != "A" else "B"
    resp = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/receipts",
        json={"signed_token": first + payload[1:] + "." + signature},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_post_receipt_duplicate_is_conflict(env) -> None:
    token = _receipt_token(PUBLIC_AGENT)
    first = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/receipts", json={"signed_token": token}
    )
    assert first.status_code == 201, first.text
    dup = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/receipts", json={"signed_token": token}
    )
    assert dup.status_code == 409, dup.text


@pytest.mark.asyncio
async def test_minted_session_ids_stay_high_entropy_but_are_not_a_grant(env) -> None:
    """Unguessability is a floor, never the grant.

    The SDK default must stay >= 64 bits, but holding a freshly minted id for a
    public agent opens nothing: nothing has published that session.
    """
    resp = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/sessions",
        json={"signed_token": _session_token(PUBLIC_AGENT)},
    )
    assert resp.status_code == 201, resp.text
    minted = resp.json()["session_id"]
    assert len(minted) >= 16
    int(minted, 16)  # raises if the default stops being hex

    anon = await env.client.get(f"/v1/sessions/{minted}")
    assert anon.status_code == 404, anon.text
    _assert_no_leak(anon)


# --------------------------------------------------------------------------
# GET /v1/agents/{name}/receipts        (list)
# GET /v1/agents/{name}/receipts/{id}   (detail, carries signed_token)
# GET /v1/agents/{name}/sessions        (replay index)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_anonymous_gets_404_and_no_payload(env, agent_name: str) -> None:
    for url in _read_urls(env, agent_name):
        resp = await env.client.get(url)
        assert resp.status_code == 404, f"{url} -> {resp.status_code} {resp.text}"
        _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_logged_in_stranger_sees_no_row_of_a_public_agent(env) -> None:
    """A stranger who can *call* a public agent reads only what they called.

    They made no call, so both collections are empty and the one receipt that
    exists is not addressable. Not a 403: the collection is scoped to them, and
    the day they do make a call it appears here rather than at a second
    ``/v1/me/receipts`` surface that would drift from this policy.
    """
    headers = _bearer(issue_token(env.intruder.id))

    for url in (
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=200",
        f"/v1/agents/{PUBLIC_AGENT}/sessions?limit=200",
    ):
        resp = await env.client.get(url, headers=headers)
        assert resp.status_code == 200, f"{url} -> {resp.status_code} {resp.text}"
        assert resp.json() == [], resp.text
        _assert_no_leak(resp)

    for receipt_id in (env.receipts[PUBLIC_AGENT], env.buyer_receipt):
        detail = await env.client.get(
            f"/v1/agents/{PUBLIC_AGENT}/receipts/{receipt_id}", headers=headers
        )
        assert detail.status_code == 404, detail.text
        _assert_no_leak(detail)


@pytest.mark.asyncio
async def test_logged_in_stranger_cannot_see_a_private_agent_at_all(env) -> None:
    """No existence oracle for a caller who cannot discover the agent.

    Status *and* body match a genuinely missing agent, so a logged-in account
    cannot enumerate private agent names through the evidence routes.
    """
    headers = _bearer(issue_token(env.intruder.id))
    missing = await env.client.get("/v1/agents/no-such-agent/receipts", headers=headers)
    for url in _read_urls(env, PRIVATE_AGENT):
        resp = await env.client.get(url, headers=headers)
        assert resp.status_code == 404, f"{url} -> {resp.status_code} {resp.text}"
        _assert_no_leak(resp)
    private = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts", headers=headers
    )
    assert private.status_code == missing.status_code == 404
    assert private.json() == missing.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_owner_still_reads_everything(env, agent_name: str) -> None:
    headers = _bearer(issue_token(env.owner.id))

    listed = await env.client.get(
        f"/v1/agents/{agent_name}/receipts?limit=50", headers=headers
    )
    assert listed.status_code == 200, listed.text
    # Everything, including calls made by other people.
    assert env.receipts[agent_name] in {r["receipt_id"] for r in listed.json()}
    if agent_name == PUBLIC_AGENT:
        assert env.buyer_receipt in {r["receipt_id"] for r in listed.json()}
    # The list model is headers-only even for a reader.
    assert "signed_token" not in listed.text

    detail = await env.client.get(
        f"/v1/agents/{agent_name}/receipts/{env.receipts[agent_name]}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["signed_token"]
    assert body["payload"]["receipt_id"] == env.receipts[agent_name]

    index = await env.client.get(
        f"/v1/agents/{agent_name}/sessions?limit=50", headers=headers
    )
    assert index.status_code == 200, index.text
    seen = {s["session_id"] for s in index.json()}
    assert env.sessions[agent_name] in seen
    assert (
        BUYER_SESSION if agent_name == PUBLIC_AGENT else BUYER_PRIVATE_SESSION
    ) in seen


@pytest.mark.asyncio
async def test_owner_reads_with_a_session_cookie(env) -> None:
    """The dashboard SPA authenticates with a cookie, not a bearer."""
    env.client.cookies.set(settings.session_cookie_name, issue_token(env.owner.id))
    try:
        for url in _read_urls(env, PRIVATE_AGENT):
            resp = await env.client.get(url)
            assert resp.status_code == 200, f"{url} -> {resp.status_code} {resp.text}"
    finally:
        env.client.cookies.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_platform_admin_still_reads_everything(env, agent_name: str) -> None:
    headers = _bearer(issue_token(env.admin.id))
    for url in _read_urls(env, agent_name):
        resp = await env.client.get(url, headers=headers)
        assert resp.status_code == 200, f"{url} -> {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_owner_gets_404_for_unknown_receipt_id(env) -> None:
    resp = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts/nope",
        headers=_bearer(issue_token(env.owner.id)),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_receipt_of_another_agent_is_not_readable_cross_agent(env) -> None:
    """Owner of both agents still cannot pull agent A's receipt via agent B."""
    resp = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts/{env.receipts[PRIVATE_AGENT]}",
        headers=_bearer(issue_token(env.owner.id)),
    )
    assert resp.status_code == 404
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_unknown_agent_is_404_for_everyone(env) -> None:
    for principal in (None, "intruder", "owner", "admin"):
        headers = (
            {}
            if principal is None
            else _bearer(issue_token(getattr(env, principal).id))
        )
        for suffix in ("receipts", "sessions"):
            resp = await env.client.get(
                f"/v1/agents/ghost-agent/{suffix}", headers=headers
            )
            assert resp.status_code == 404, (
                f"{principal} {suffix} -> {resp.status_code}"
            )


@pytest.mark.asyncio
async def test_anonymous_404_body_is_identical_for_missing_and_private(env) -> None:
    """Anonymous callers get no existence oracle: same status *and* same body."""
    private = await env.client.get(f"/v1/agents/{PRIVATE_AGENT}/receipts")
    missing = await env.client.get("/v1/agents/does-not-exist-at-all/receipts")
    assert private.status_code == missing.status_code == 404
    assert private.json() == missing.json()


# --------------------------------------------------------------------------
# The caller's own calls. ``a2a receipt list <agent>`` and every caller of a
# public agent live here: the platform owes the user named by ``caller`` the
# evidence for their own run.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buyer_reads_their_own_calls_on_an_agent_they_do_not_own(env) -> None:
    headers = _bearer(issue_token(env.buyer.id))

    listed = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=200", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert [r["receipt_id"] for r in listed.json()] == [env.buyer_receipt]

    detail = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts/{env.buyer_receipt}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["signed_token"]
    assert SECRET_PROMPT in detail.text

    index = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/sessions?limit=200", headers=headers
    )
    assert index.status_code == 200, index.text
    assert [s["session_id"] for s in index.json()] == [BUYER_SESSION]

    replay = await env.client.get(f"/v1/sessions/{BUYER_SESSION}", headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.headers["X-A2A-Replay-Token"]


@pytest.mark.asyncio
async def test_buyer_reads_nothing_of_anyone_elses(env) -> None:
    """Own-call access is an equality on one column, not a foot in the door."""
    headers = _bearer(issue_token(env.buyer.id))

    detail = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts/{env.receipts[PUBLIC_AGENT]}",
        headers=headers,
    )
    assert detail.status_code == 404, detail.text
    _assert_no_leak(detail)

    replay = await env.client.get(
        f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}", headers=headers
    )
    assert replay.status_code == 403, replay.text
    _assert_no_leak(replay)


@pytest.mark.asyncio
async def test_gateway_rows_belong_to_nobody(env) -> None:
    """``agent_ingress`` cannot attribute a call, so its rows match no user.

    ``_external_caller`` writes ``"credential-present"`` / ``"anonymous"``: the
    gateway observes that a credential was sent but cannot assert whose it was.
    Every seeded non-buyer row uses that value, so no signed-in account may
    claim one — asserted across every principal that is not a reader.
    """
    for principal in ("intruder", "buyer", "org_member"):
        resp = await env.client.get(
            f"/v1/agents/{PUBLIC_AGENT}/receipts/{env.receipts[PUBLIC_AGENT]}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert resp.status_code == 404, f"{principal} -> {resp.status_code}"
        _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_own_call_survives_the_agent_going_private(env) -> None:
    """Their own replay stays readable; the agent's collections do not.

    Once the buyer cannot discover the agent, ``/v1/agents/{name}/...`` answers
    exactly as it does for a missing agent — but the session they produced is
    still their own data, addressed directly.
    """
    headers = _bearer(issue_token(env.buyer.id))
    async with env.Session() as session:
        await session.execute(
            update(Agent).where(Agent.name == PUBLIC_AGENT).values(public=False)
        )
        await session.commit()

    listed = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts", headers=headers
    )
    assert listed.status_code == 404, listed.text
    _assert_no_leak(listed)

    replay = await env.client.get(f"/v1/sessions/{BUYER_SESSION}", headers=headers)
    assert replay.status_code == 200, replay.text


@pytest.mark.asyncio
async def test_buyer_of_a_private_agent_still_reads_their_own_session(env) -> None:
    """Private is about discovery, not about disowning the caller's own run."""
    resp = await env.client.get(
        f"/v1/sessions/{BUYER_PRIVATE_SESSION}",
        headers=_bearer(issue_token(env.buyer.id)),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_pagination_cannot_widen_the_caller_scope(env) -> None:
    """``limit`` / ``before`` are page controls, not the authorization boundary."""
    headers = _bearer(issue_token(env.buyer.id))
    for query in (
        "?limit=200",
        "?limit=1",
        "?before=2999-01-01T00:00:00Z",
        "?limit=200&before=2999-01-01T00:00:00Z",
    ):
        resp = await env.client.get(
            f"/v1/agents/{PUBLIC_AGENT}/receipts{query}", headers=headers
        )
        assert resp.status_code == 200, f"{query} -> {resp.text}"
        assert {r["caller"] for r in resp.json()} <= {f"user:{env.buyer.id}"}, resp.text

    for query in ("?limit=200", "?limit=1", "?before=9999999999"):
        resp = await env.client.get(
            f"/v1/agents/{PUBLIC_AGENT}/sessions{query}", headers=headers
        )
        assert resp.status_code == 200, f"{query} -> {resp.text}"
        assert {s["caller"] for s in resp.json()} <= {f"user:{env.buyer.id}"}, resp.text


@pytest.mark.asyncio
async def test_a_forged_caller_opens_exactly_one_row_and_no_more(env) -> None:
    """``caller`` is agent-chosen, so the grant it carries must be a single row.

    Whoever can mint a signed receipt for an agent can already write arbitrary
    rows into that agent's evidence — that is what the Ed25519 signature buys.
    Naming a victim in ``caller`` must therefore hand the victim that one
    (agent-authored) row and nothing that was already there.
    """
    planted = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/receipts",
        json={
            "signed_token": _receipt_token(
                PUBLIC_AGENT, caller=f"user:{env.intruder.id}"
            )
        },
    )
    assert planted.status_code == 201, planted.text
    planted_id = planted.json()["receipt_id"]

    headers = _bearer(issue_token(env.intruder.id))
    listed = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=200", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert [r["receipt_id"] for r in listed.json()] == [planted_id]

    for other in (env.receipts[PUBLIC_AGENT], env.buyer_receipt):
        resp = await env.client.get(
            f"/v1/agents/{PUBLIC_AGENT}/receipts/{other}", headers=headers
        )
        assert resp.status_code == 404, resp.text
        _assert_no_leak(resp)

    denied = await env.client.get(
        f"/v1/sessions/{BUYER_SESSION}", headers=headers
    )
    assert denied.status_code == 403, denied.text
    _assert_no_leak(denied)


@pytest.mark.parametrize(
    "caller, matches",
    [
        ("user:11", True),
        ("user:011", False),  # not a string the platform mints
        ("user-11", False),  # the SDK's own example format, not an identity
        ("11", False),  # a looser parser might accept it; this must not
        ("user:1", False),
        ("user:110", False),
        ("credential-present", False),
        ("anonymous", False),
        ("", False),
        (None, False),
    ],
)
def test_is_own_evidence_only_matches_the_platform_minted_form(
    caller, matches: bool
) -> None:
    """The predicate is deliberately narrow.

    Only the exact string ``routes/agents._persist_agent_api_receipt`` writes
    grants a read, because ``caller`` is a value the *agent* can choose.
    """
    from control_plane.agent_authorization import is_own_evidence

    class _U:
        id = 11
        is_admin = False

    assert is_own_evidence(caller, _U()) is matches
    assert is_own_evidence(caller, None) is False


# --------------------------------------------------------------------------
# Organization readers — the policy must match routes/compliance.py, which
# serves the identical signed_token + payload for org-owned agents.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
@pytest.mark.parametrize("principal", ["org_admin", "org_maintainer"])
async def test_org_agent_editors_are_evidence_readers(
    env, agent_name: str, principal: str
) -> None:
    """Whoever ``decide_agent_access`` lets edit the agent may read its evidence.

    A maintainer can push new source to this agent and redeploy it. Denying them
    the receipts of the code they ship — while an admin who can do strictly less
    to the source reads them freely — is a line nobody drew on purpose, and it
    broke org co-maintainers when the evidence rule was first written by hand.
    """
    headers = _bearer(issue_token(getattr(env, principal).id))
    for url in _read_urls(env, agent_name):
        resp = await env.client.get(url, headers=headers)
        assert resp.status_code == 200, f"{url} -> {resp.status_code} {resp.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_evidence_reads_track_decide_agent_access_edit_existing(
    env, agent_name: str
) -> None:
    """The receipts gate and ``edit_existing`` must not be two opinions.

    Asserted against the real decision function, so a future change to
    ``_SOURCE_WRITER_ROLES`` moves both or neither.
    """
    from control_plane.agent_authorization import (
        decide_agent_access,
        may_read_agent_evidence,
    )

    async with env.Session() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one()
        for principal in ("owner", "org_admin", "org_maintainer", "org_member",
                          "intruder"):
            user = (
                await session.execute(
                    select(User).where(User.id == getattr(env, principal).id)
                )
            ).scalar_one()
            may_edit = (
                await decide_agent_access(
                    session, user=user, agent=agent, action="edit_existing"
                )
            ).allowed
            may_read = await may_read_agent_evidence(
                session, user=user, agent=agent
            )
            assert may_read is may_edit, principal


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
async def test_plain_org_member_is_not_an_evidence_reader(
    env, agent_name: str
) -> None:
    """Discovering / invoking an agent must not imply reading its callers' prompts.

    A plain member can see the org's private agent, so the collections resolve
    — but scoped to their own calls. No other member's prompt, hash, grant id
    or replay event is reachable.
    """
    headers = _bearer(issue_token(env.org_member.id))
    mine = {env.member_receipt} if agent_name == PRIVATE_AGENT else set()

    listed = await env.client.get(
        f"/v1/agents/{agent_name}/receipts?limit=200", headers=headers
    )
    assert listed.status_code == 200, listed.text
    assert {r["receipt_id"] for r in listed.json()} == mine, listed.text
    assert "signed_token" not in listed.text
    assert SECRET_PROMPT not in listed.text

    index = await env.client.get(
        f"/v1/agents/{agent_name}/sessions?limit=200", headers=headers
    )
    assert index.status_code == 200, index.text
    assert index.json() == [], index.text
    _assert_no_leak(index)

    detail = await env.client.get(
        f"/v1/agents/{agent_name}/receipts/{env.receipts[agent_name]}", headers=headers
    )
    assert detail.status_code == 404, detail.text
    _assert_no_leak(detail)

    denied = await env.client.get(
        f"/v1/sessions/{env.sessions[agent_name]}", headers=headers
    )
    assert denied.status_code == 403, denied.text
    _assert_no_leak(denied)


@pytest.mark.asyncio
async def test_agent_detached_from_its_org_is_owner_only(env) -> None:
    """Detaching the agent from the org revokes the org admin immediately."""
    async with env.Session() as session:
        await session.execute(
            update(Agent)
            .where(Agent.name == PRIVATE_AGENT)
            .values(organization_id=None)
        )
        await session.commit()
    resp = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts",
        headers=_bearer(issue_token(env.org_admin.id)),
    )
    # Detached *and* private: the org admin can no longer even discover it, so
    # the denial is indistinguishable from a missing agent.
    assert resp.status_code == 404, resp.text
    _assert_no_leak(resp)
    denied = await env.client.get(
        f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}",
        headers=_bearer(issue_token(env.org_admin.id)),
    )
    assert denied.status_code == 403, denied.text
    _assert_no_leak(denied)


@pytest.mark.asyncio
async def test_inactive_org_admin_is_not_an_evidence_reader(env) -> None:
    async with env.Session() as session:
        await session.execute(
            update(OrganizationMember)
            .where(OrganizationMember.user_id == env.org_admin.id)
            .values(active=False)
        )
        await session.commit()
    resp = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts",
        headers=_bearer(issue_token(env.org_admin.id)),
    )
    assert resp.status_code == 404, resp.text
    _assert_no_leak(resp)
    denied = await env.client.get(
        f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}",
        headers=_bearer(issue_token(env.org_admin.id)),
    )
    assert denied.status_code == 403, denied.text
    _assert_no_leak(denied)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal, allowed",
    [
        ("owner", True),
        ("org_admin", True),
        # The case that used to make this test decorative: the compliance route
        # wrote its own org-role check, so a maintainer the receipts route
        # admits was refused there — or, before the receipts route was gated at
        # all, admitted there and refused here. Either way the "policy" was
        # whichever file you happened to read.
        ("org_maintainer", True),
        ("org_member", False),
        ("intruder", False),
        ("buyer", False),
    ],
)
async def test_receipt_route_and_compliance_route_agree(
    env, principal: str, allowed: bool
) -> None:
    """The two routes that serve the same receipt bytes must agree.

    ``/v1/me/organizations/{slug}/compliance/decision-records/skill_execution/{id}``
    returns ``signed_token`` + the full payload — the caller's prompt text
    included. Both routes now resolve the same
    ``agent_authorization.may_read_agent_evidence``; this asserts the observable
    consequence, not the shared import.
    """
    headers = _bearer(issue_token(getattr(env, principal).id))
    receipt_id = env.receipts[PRIVATE_AGENT]

    direct = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts/{receipt_id}", headers=headers
    )
    sibling = await env.client.get(
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
        f"/skill_execution/{receipt_id}",
        headers=headers,
    )

    assert (direct.status_code == 200) is allowed, direct.text
    assert (sibling.status_code == 200) is allowed, sibling.text
    if allowed:
        assert direct.json()["signed_token"] == sibling.json()["signed_token"]
        # Both bodies really do carry the caller's prompt, so _assert_no_leak
        # on the denied side is testing something that exists.
        assert SECRET_PROMPT in direct.text
        assert SECRET_PROMPT in sibling.text
    else:
        _assert_no_leak(direct)
        _assert_no_leak(sibling)


@pytest.mark.asyncio
async def test_compliance_route_is_org_scoped_and_never_wider(env) -> None:
    """The one asymmetry, asserted so it cannot silently invert.

    The compliance route is addressed *through an organization*, so it also
    requires membership of that org: a platform admin who is not a member gets
    404 there while reading the receipt directly. Narrower is never a leak — but
    the day it becomes wider than the receipts route, this fails.
    """
    receipt_id = env.receipts[PRIVATE_AGENT]
    headers = _bearer(issue_token(env.admin.id))

    direct = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts/{receipt_id}", headers=headers
    )
    sibling = await env.client.get(
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
        f"/skill_execution/{receipt_id}",
        headers=headers,
    )
    assert direct.status_code == 200, direct.text
    assert sibling.status_code == 404, sibling.text
    _assert_no_leak(sibling)


@pytest.mark.asyncio
async def test_compliance_agent_owner_who_is_a_plain_member_still_reads(env) -> None:
    """Owning the agent is enough on both routes, whatever the org role is."""
    async with env.Session() as session:
        await session.execute(
            update(OrganizationMember)
            .where(OrganizationMember.user_id == env.owner.id)
            .values(role="member")
        )
        await session.commit()

    headers = _bearer(issue_token(env.owner.id))
    receipt_id = env.receipts[PRIVATE_AGENT]
    direct = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts/{receipt_id}", headers=headers
    )
    sibling = await env.client.get(
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
        f"/skill_execution/{receipt_id}",
        headers=headers,
    )
    assert direct.status_code == 200, direct.text
    assert sibling.status_code == 200, sibling.text
    assert direct.json()["signed_token"] == sibling.json()["signed_token"]


@pytest.mark.asyncio
async def test_compliance_route_agrees_on_the_callers_own_receipt(env) -> None:
    """Both routes admit the caller of the receipt, and only for that receipt.

    A plain org member is not an evidence reader, so the org's own compliance
    surface must not be a way around that — except for the one record that is a
    record *of them*. This is the case that would have re-split the two routes:
    ``compliance.py`` gates on org role, ``agent_receipts.py`` on the row's
    caller, and only sharing ``is_own_evidence`` keeps them equal.
    """
    headers = _bearer(issue_token(env.org_member.id))
    compliance = (
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records/skill_execution"
    )

    mine_direct = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts/{env.member_receipt}", headers=headers
    )
    mine_sibling = await env.client.get(
        f"{compliance}/{env.member_receipt}", headers=headers
    )
    assert mine_direct.status_code == 200, mine_direct.text
    assert mine_sibling.status_code == 200, mine_sibling.text
    assert mine_direct.json()["signed_token"] == mine_sibling.json()["signed_token"]

    theirs_direct = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts/{env.receipts[PRIVATE_AGENT]}",
        headers=headers,
    )
    theirs_sibling = await env.client.get(
        f"{compliance}/{env.receipts[PRIVATE_AGENT]}", headers=headers
    )
    assert theirs_direct.status_code != 200, theirs_direct.text
    assert theirs_sibling.status_code != 200, theirs_sibling.text
    _assert_no_leak(theirs_direct)
    _assert_no_leak(theirs_sibling)


@pytest.mark.asyncio
async def test_compliance_other_record_kinds_stay_org_admin_only(env) -> None:
    """Relaxing the org-role gate for receipts must not relax the audit trail.

    ``authorization`` / ``admin_action`` records are org-wide surfaces with no
    per-agent owner to consult, so they keep the owner/admin requirement.
    """
    headers = _bearer(issue_token(env.org_maintainer.id))
    for kind, record_id in (
        ("authorization", "g-1"),
        ("admin_action", "orgaudit-1"),
    ):
        resp = await env.client.get(
            f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
            f"/{kind}/{record_id}",
            headers=headers,
        )
        assert resp.status_code == 403, f"{kind} -> {resp.status_code} {resp.text}"


# --------------------------------------------------------------------------
# Credential handling — the reason this suite uses real tokens.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_studio_job_token_can_still_list_receipts(env) -> None:
    """agent-studio evaluates a build by diffing the target's receipt list.

    ``PlatformHelperClient.list_receipts`` sends the job token minted by
    ``agent_studio_worker``; ``auth._authorize_studio_job_request`` maps
    ``GET /v1/agents/{x}/receipts`` to the ``receipt:read`` scope. Guarding the
    route with plain ``optional_current_user`` silently demoted that worker to
    anonymous (404), failing every ``requires_receipt`` evaluation.
    """
    token = issue_studio_job_token(
        env.owner.id,
        run_id=STUDIO_RUN_ID,
        target_agent=PUBLIC_AGENT,
        ttl_seconds=600,
    )
    resp = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=50", headers=_bearer(token)
    )
    assert resp.status_code == 200, f"studio job token -> {resp.status_code} {resp.text}"
    assert env.receipts[PUBLIC_AGENT] in {r["receipt_id"] for r in resp.json()}


@pytest.mark.asyncio
async def test_studio_job_token_reaches_only_the_receipt_list(env) -> None:
    """The Studio scope map covers exactly one evidence read.

    ``auth._authorize_studio_job_request`` maps ``GET
    /v1/agents/{x}/receipts`` to ``receipt:read`` and nothing else, so the
    detail body (signed token + payload) and the replay index stay outside the
    job token's reach. Asserted here so completing the matrix does not quietly
    become an argument for widening it.
    """
    token = _bearer(
        issue_studio_job_token(
            env.owner.id,
            run_id=STUDIO_RUN_ID,
            target_agent=PUBLIC_AGENT,
            ttl_seconds=600,
        )
    )
    for url in (
        f"/v1/agents/{PUBLIC_AGENT}/receipts/{env.receipts[PUBLIC_AGENT]}",
        f"/v1/agents/{PUBLIC_AGENT}/sessions",
    ):
        resp = await env.client.get(url, headers=token)
        assert resp.status_code == 403, f"{url} -> {resp.status_code} {resp.text}"
        _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_studio_job_token_cannot_open_a_session_or_compliance_record(
    env,
) -> None:
    """Neither route names an agent the Studio map can bind, so neither is reachable.

    ``/v1/sessions/{id}`` downgrades an unbindable credential to anonymous (404);
    the compliance route requires a real user session (401).
    """
    token = _bearer(
        issue_studio_job_token(
            env.owner.id,
            run_id=STUDIO_RUN_ID,
            target_agent=PUBLIC_AGENT,
            ttl_seconds=600,
        )
    )
    session_resp = await env.client.get(
        f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}", headers=token
    )
    assert session_resp.status_code == 404, session_resp.text
    _assert_no_leak(session_resp)

    compliance_resp = await env.client.get(
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
        f"/skill_execution/{env.receipts[PRIVATE_AGENT]}",
        headers=token,
    )
    assert compliance_resp.status_code == 401, compliance_resp.text
    _assert_no_leak(compliance_resp)


@pytest.mark.asyncio
async def test_compliance_record_needs_a_credential(env) -> None:
    resp = await env.client.get(
        f"/v1/me/organizations/{ORG_SLUG}/compliance/decision-records"
        f"/skill_execution/{env.receipts[PRIVATE_AGENT]}"
    )
    assert resp.status_code == 401, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_studio_job_token_is_bound_to_its_target_agent(env) -> None:
    token = issue_studio_job_token(
        env.owner.id,
        run_id=STUDIO_RUN_ID,
        target_agent=PUBLIC_AGENT,
        ttl_seconds=600,
    )
    resp = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts", headers=_bearer(token)
    )
    assert resp.status_code == 403, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_studio_job_token_without_receipt_scope_is_denied(env) -> None:
    token = issue_studio_job_token(
        env.owner.id,
        run_id=STUDIO_RUN_ID,
        target_agent=PUBLIC_AGENT,
        ttl_seconds=600,
        scopes=("agent:read",),
    )
    resp = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts", headers=_bearer(token)
    )
    assert resp.status_code == 403, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_studio_job_token_dies_with_its_run(env) -> None:
    async with env.Session() as session:
        await session.execute(
            update(AgentStudioRun)
            .where(AgentStudioRun.run_id == STUDIO_RUN_ID)
            .values(status="failed")
        )
        await session.commit()
    token = issue_studio_job_token(
        env.owner.id,
        run_id=STUDIO_RUN_ID,
        target_agent=PUBLIC_AGENT,
        ttl_seconds=600,
    )
    resp = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts", headers=_bearer(token)
    )
    assert resp.status_code == 403, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_studio_job_token_of_a_non_owner_reads_nothing_extra(env) -> None:
    """A Studio credential is scoped, not privileged: it cannot out-read its user.

    Asserted as an equality against what that user reads with a plain session
    token, so the invariant survives any future change to the denial *shape*.
    """
    async with env.Session() as session:
        session.add(
            AgentStudioRun(
                run_id="studio-run-intruder",
                user_id=env.intruder.id,
                agent_name=PUBLIC_AGENT,
                status="building",
            )
        )
        await session.commit()
    token = issue_studio_job_token(
        env.intruder.id,
        run_id="studio-run-intruder",
        target_agent=PUBLIC_AGENT,
        ttl_seconds=600,
    )
    as_job = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=200", headers=_bearer(token)
    )
    as_user = await env.client.get(
        f"/v1/agents/{PUBLIC_AGENT}/receipts?limit=200",
        headers=_bearer(issue_token(env.intruder.id)),
    )
    assert as_job.status_code == as_user.status_code == 200
    assert as_job.json() == as_user.json() == []
    _assert_no_leak(as_job)


@pytest.mark.asyncio
async def test_malformed_authorization_headers_never_500(env) -> None:
    """``Authorization: Bearer`` + whitespace used to raise IndexError -> 500."""
    matrix: dict[str, Any] = {}
    for raw in ("", " ", "   ", "\t", "not-a-jwt", "a.b.c", "Bearer", "Bearer x"):
        resp = await env.client.get(
            f"/v1/agents/{PRIVATE_AGENT}/receipts",
            headers={"Authorization": f"Bearer {raw}"},
        )
        matrix[raw] = resp.status_code
        _assert_no_leak(resp)
    assert all(status == 404 for status in matrix.values()), matrix


@pytest.mark.asyncio
async def test_odd_authorization_headers_never_500(env) -> None:
    for value in ("", "Bearer", "bearer   ", "Basic abc", "Bearer\tx"):
        for url in _read_urls(env, PRIVATE_AGENT) + [
            f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}"
        ]:
            resp = await env.client.get(url, headers={"Authorization": value})
            assert resp.status_code == 404, f"{value!r} {url} -> {resp.status_code}"


@pytest.mark.parametrize(
    "authorization, expected",
    [
        ("Bearer", None),  # no trailing space: not a bearer header at all
        ("Bearer ", None),  # used to raise IndexError -> HTTP 500
        ("Bearer    ", None),
        ("bearer \t ", None),
        ("Bearer abc", "abc"),
        ("bearer   abc  ", "abc"),
        ("Basic abc", None),
        ("", None),
        (None, None),
    ],
)
def test_credential_token_never_raises(authorization, expected) -> None:
    """Unit guard for the shared helper every credentialed route depends on.

    The 500 this prevents was not specific to these endpoints — it lived in
    ``auth._credential_token`` and fired on any route with a user dependency.
    """
    from control_plane.auth import _credential_token

    assert _credential_token(authorization, None) == expected


@pytest.mark.asyncio
async def test_agent_frontend_session_token_is_not_a_platform_credential(env) -> None:
    """A hosted agent's visitor cookie must not read the platform's receipts."""
    token, _ = mint_agent_session_token(
        user_id=env.owner.id, agent=PRIVATE_AGENT, ttl_seconds=600
    )
    resp = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts", headers=_bearer(token)
    )
    assert resp.status_code == 404, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_agent_invoke_token_cannot_read_receipts(env) -> None:
    """The cp_jwt handed to third-party agent code carries no receipt scope."""
    token = issue_agent_invoke_token(
        env.owner.id, agent=PRIVATE_AGENT, ttl_seconds=600
    )
    resp = await env.client.get(
        f"/v1/agents/{PRIVATE_AGENT}/receipts", headers=_bearer(token)
    )
    assert resp.status_code in {403, 404}, resp.text
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_path_tricks_do_not_bypass_the_gate(env) -> None:
    for url in (
        f"/v1/agents/{PRIVATE_AGENT}/receipts/",
        f"/v1/agents/{PRIVATE_AGENT.upper()}/receipts",
        f"/v1/agents/{PRIVATE_AGENT}/receipts?limit=200",
        f"/v1/agents/{PRIVATE_AGENT}%2f../{PRIVATE_AGENT}/receipts",
    ):
        resp = await env.client.get(url, follow_redirects=True)
        assert resp.status_code in {404, 307, 405}, f"{url} -> {resp.status_code}"
        _assert_no_leak(resp)


# --------------------------------------------------------------------------
# GET /v1/sessions/{session_id} — published-proof-run grant, not a capability
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_agent_session_is_not_anonymously_readable_by_default(
    env,
) -> None:
    """The bug this closes: a public agent's caller had their inputs published.

    ``a2a_pack.agent`` records ``skill_start`` with the *complete* validated
    arguments. "Public agent" means anyone may call it, so an ordinary call to a
    public agent must not put the caller's prompt on an anonymous URL.
    """
    resp = await env.client.get(f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}")
    assert resp.status_code == 404, resp.text
    _assert_no_leak(resp)
    assert SECRET_PROMPT not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_name", [PUBLIC_AGENT, PRIVATE_AGENT])
@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_a_proof_run_can_never_publish_a_session(
    env, agent_name: str, status: str
) -> None:
    """The bypass this closes: publication driven by owner-controlled data.

    ``run_agent_proof`` stores ``run.events`` verbatim from the agent process,
    which is owner-supplied code. When the anonymous grant joined on that blob,
    a public agent's owner could emit a ``replay_sealed`` event naming **a
    buyer's** session id and make that buyer's complete ``skill_start``
    arguments world-readable — a publication decision the control plane never
    made. No proof run, of any status, for any agent, opens a session now.
    """
    session_id = env.sessions[agent_name]
    await proof_run_naming_session(
        env.Session, agent_name=agent_name, session_id=session_id, status=status
    )

    anon = await env.client.get(f"/v1/sessions/{session_id}")
    assert anon.status_code == 404, anon.text
    _assert_no_leak(anon)

    for principal in ("intruder", "org_member", "buyer"):
        resp = await env.client.get(
            f"/v1/sessions/{session_id}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert resp.status_code == 403, f"{principal} -> {resp.status_code}"
        _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_a_proof_run_cannot_publish_a_third_partys_session(env) -> None:
    """Named for the exact reproduction: the owner points a run at a buyer's id.

    The buyer keeps their own read; nobody gains one, and the session that
    carries the buyer's prompt never becomes anonymous.
    """
    await proof_run_naming_session(
        env.Session, agent_name=PUBLIC_AGENT, session_id=BUYER_SESSION
    )

    anon = await env.client.get(f"/v1/sessions/{BUYER_SESSION}")
    assert anon.status_code == 404, anon.text
    assert SECRET_PROMPT not in anon.text
    _assert_no_leak(anon)

    stranger = await env.client.get(
        f"/v1/sessions/{BUYER_SESSION}", headers=_bearer(issue_token(env.intruder.id))
    )
    assert stranger.status_code == 403, stranger.text
    _assert_no_leak(stranger)

    theirs = await env.client.get(
        f"/v1/sessions/{BUYER_SESSION}", headers=_bearer(issue_token(env.buyer.id))
    )
    assert theirs.status_code == 200, theirs.text


@pytest.mark.asyncio
async def test_session_gate_cost_is_independent_of_proof_run_volume(env) -> None:
    """No unauthenticated request amplification, and no scan window to age out.

    The removed grant scanned the newest 25 passing proof runs' ``events``
    blobs on every non-reader read — megabytes of owner-controlled JSON per
    request — and silently revoked an already-published session once 25 newer
    runs existed. Counting statements pins both: the gate is a constant number
    of queries whatever the proof-run volume.
    """
    from sqlalchemy import event

    counter = {"n": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(env.engine.sync_engine, "before_cursor_execute", _before)
    try:
        session_id = env.sessions[PUBLIC_AGENT]
        first = await env.client.get(f"/v1/sessions/{session_id}")
        assert first.status_code == 404, first.text
        baseline = counter["n"]
        assert baseline > 0

        await proof_run_naming_session(
            env.Session,
            agent_name=PUBLIC_AGENT,
            session_id=session_id,
            count=30,
            padding=20_000,
        )

        counter["n"] = 0
        again = await env.client.get(f"/v1/sessions/{session_id}")
        assert again.status_code == 404, again.text
        assert counter["n"] == baseline, (
            f"30 proof runs changed the gate from {baseline} to {counter['n']} queries"
        )
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", _before)


@pytest.mark.asyncio
async def test_session_id_entropy_is_not_the_policy(env) -> None:
    """``build_session`` honours a caller-chosen id; the gate does not care.

    A low-entropy id used to be excluded from the anonymous grant as a floor
    under it. With no anonymous grant, the id shape decides nothing: the same
    principals read ``session-1`` and a 128-bit id, and no one else does.
    """
    created = await env.client.post(
        f"/v1/agents/{PUBLIC_AGENT}/sessions",
        json={
            "signed_token": _session_token(PUBLIC_AGENT, session_id=GUESSABLE_SESSION)
        },
    )
    assert created.status_code == 201, created.text
    await proof_run_naming_session(
        env.Session, agent_name=PUBLIC_AGENT, session_id=GUESSABLE_SESSION
    )

    for session_id in (GUESSABLE_SESSION, env.sessions[PUBLIC_AGENT]):
        anon = await env.client.get(f"/v1/sessions/{session_id}")
        assert anon.status_code == 404, f"{session_id} -> {anon.text}"
        _assert_no_leak(anon)

        stranger = await env.client.get(
            f"/v1/sessions/{session_id}",
            headers=_bearer(issue_token(env.intruder.id)),
        )
        assert stranger.status_code == 403, stranger.text
        _assert_no_leak(stranger)

        owner = await env.client.get(
            f"/v1/sessions/{session_id}", headers=_bearer(issue_token(env.owner.id))
        )
        assert owner.status_code == 200, owner.text


@pytest.mark.asyncio
async def test_private_agent_session_is_404_for_anonymous(env) -> None:
    resp = await env.client.get(f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}")
    assert resp.status_code == 404
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_private_agent_session_is_403_for_logged_in_non_reader(env) -> None:
    for principal in ("intruder", "org_member"):
        resp = await env.client.get(
            f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert resp.status_code == 403, f"{principal} -> {resp.status_code}"
        _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_private_agent_session_is_readable_by_every_evidence_reader(env) -> None:
    for principal in ("owner", "admin", "org_admin", "org_maintainer"):
        resp = await env.client.get(
            f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert resp.status_code == 200, f"{principal} -> {resp.status_code} {resp.text}"
        assert resp.headers["X-A2A-Replay-Token"]


@pytest.mark.asyncio
async def test_since_cursor_does_not_bypass_the_session_gate(env) -> None:
    resp = await env.client.get(f"/v1/sessions/{env.sessions[PRIVATE_AGENT]}?since=0")
    assert resp.status_code == 404
    _assert_no_leak(resp)


@pytest.mark.asyncio
async def test_unknown_session_id_is_404_for_everyone(env) -> None:
    for principal in (None, "intruder", "owner", "admin"):
        headers = (
            {}
            if principal is None
            else _bearer(issue_token(getattr(env, principal).id))
        )
        resp = await env.client.get("/v1/sessions/no-such-session", headers=headers)
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_orphaned_session_is_not_public_by_default(env) -> None:
    """agents.py nulls agent_id on delete; the session must not become public."""
    async with env.Session() as session:
        await session.execute(
            update(AgentSession)
            .where(AgentSession.session_id == env.sessions[PUBLIC_AGENT])
            .values(agent_id=None)
        )
        await session.commit()

    anon = await env.client.get(f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}")
    assert anon.status_code == 404
    _assert_no_leak(anon)

    for principal in ("intruder", "owner", "org_admin"):
        denied = await env.client.get(
            f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert denied.status_code == 403, f"{principal} -> {denied.status_code}"

    allowed = await env.client.get(
        f"/v1/sessions/{env.sessions[PUBLIC_AGENT]}",
        headers=_bearer(issue_token(env.admin.id)),
    )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_an_orphaned_session_still_belongs_to_the_caller(env) -> None:
    """Deleting the agent removes the *owner*, not the caller's own record.

    ``is_own_evidence`` is checked before the agent row is loaded, so a buyer
    whose agent has been deleted keeps their replay while everyone but a
    platform admin loses it (asserted above).
    """
    async with env.Session() as session:
        await session.execute(
            update(AgentSession)
            .where(AgentSession.session_id == BUYER_SESSION)
            .values(agent_id=None)
        )
        await session.commit()

    theirs = await env.client.get(
        f"/v1/sessions/{BUYER_SESSION}", headers=_bearer(issue_token(env.buyer.id))
    )
    assert theirs.status_code == 200, theirs.text

    anon = await env.client.get(f"/v1/sessions/{BUYER_SESSION}")
    assert anon.status_code == 404, anon.text
    _assert_no_leak(anon)

    for principal in ("intruder", "owner", "org_maintainer"):
        denied = await env.client.get(
            f"/v1/sessions/{BUYER_SESSION}",
            headers=_bearer(issue_token(getattr(env, principal).id)),
        )
        assert denied.status_code == 403, f"{principal} -> {denied.status_code}"
        _assert_no_leak(denied)


# ---------------------------------------------------------------------------
# Public proof runs
#
# The third surface carrying this material, and the one that was still serving
# it to the world. ``routes/agent_proofs`` records the run's arguments, the
# agent's full return value, the event stream (whose ``replay_sealed`` payload
# is a sealed session and therefore the caller's complete validated arguments
# again), the workspace paths it touched, the grant it minted and the source
# repository URL — and ``GET /v1/public/agent-proofs`` published every one of
# them, for every public agent, to anonymous callers.
#
# That contradicted the projection the repo had already written down twice:
# ``PublicProofDrop`` in the public proof page contract ("does not contain
# invocation arguments, raw results, events, file paths, or grant material"),
# and ``routes/agent_receipts._resolve_agent_for_receipt_read``, which points
# at this very route as "the deliberately narrow public sharing surface".
#
# It also could not be defended as the owner's own publication decision:
# ``a2a init`` scaffolded ``expose.public: true``, so a practice agent — and
# the arguments and results of every proof run against it — was published by
# default, by a template.
#
# Policy asserted below: the public proof surfaces answer with
# ``PublicAgentProofOut`` (agent, version, skill, pass/fail, counts, hashes,
# timings, a derived summary) for everyone except the agent's evidence readers,
# who keep the whole record. Same ``may_read_agent_evidence`` as the receipts
# and replay routes above.
# ---------------------------------------------------------------------------

PROOF_SECRETS = (
    "PROOF_ARGS_DO_NOT_LEAK",
    "PROOF_RESULT_DO_NOT_LEAK",
    "PROOF_EVENT_DO_NOT_LEAK",
    "PROOF_FILE_PATH_DO_NOT_LEAK",
    "PROOF_GRANT_DO_NOT_LEAK",
    "PROOF_REPO_CREDENTIAL_DO_NOT_LEAK",
    "PROOF_SUMMARY_DO_NOT_LEAK",
    "PROOF_ERROR_DO_NOT_LEAK",
)

# Exactly what an unauthenticated caller may see about a proof run. Asserted as
# an equality, never as "the secret string is absent": a field that is present
# but null still tells a stranger the platform holds it, and a client that
# reads ``latest.file_ops.length`` breaks differently from one that never sees
# the key at all.
PUBLIC_PROOF_KEYS = {
    "proof_id",
    "agent_name",
    "agent_description",
    "agent_version",
    "skill_name",
    "skill_description",
    "status",
    "badge",
    "summary",
    "events_count",
    "file_ops_count",
    "card_hash",
    "head_sha",
    "image",
    "agent_url",
    "elapsed_ms",
    "created_at",
    "started_at",
    "completed_at",
}

PUBLIC_PROOF_SUMMARY_KEYS = {
    "agent_name",
    "badge",
    "latest",
    "total_runs",
    "passed_runs",
    "failed_runs",
}

# The payload fields, plus the two column names a serializer bug could spill.
# None of these may appear on an anonymous response.
WITHHELD_PROOF_KEYS = {
    "args_preview",
    "args_json",
    "result",
    "events",
    "file_ops",
    "grant_id",
    "repo_url",
    "error",
    "user_id",
}

# Exactly what an evidence reader gets instead: ``AgentProofRunOut``, unchanged
# from before this policy existed. Pinned as an equality so "the owner keeps
# the whole record" is a fact about the response, not a hope.
EVIDENCE_READER_PROOF_KEYS = {
    "id",
    "agent_name",
    "skill_name",
    "grant_id",
    "status",
    "badge",
    "summary",
    "error",
    "args_preview",
    "result",
    "events",
    "file_ops",
    "events_count",
    "file_ops_count",
    "card_hash",
    "repo_url",
    "head_sha",
    "image",
    "agent_url",
    "elapsed_ms",
    "created_at",
    "started_at",
    "completed_at",
}


async def seed_proof_run(
    Session: Any,
    *,
    agent_name: str,
    status: str = "passed",
) -> int:
    """One proof run with every payload field populated and marked."""
    async with Session() as session:
        agent = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one()
        run = AgentProofRun(
            agent_id=agent.id,
            agent_name=agent_name,
            user_id=agent.owner_id,
            skill_name="search",
            grant_id="PROOF_GRANT_DO_NOT_LEAK",
            status=status,
            summary="PROOF_SUMMARY_DO_NOT_LEAK /workspace/out.txt",
            error="PROOF_ERROR_DO_NOT_LEAK",
            args_json='{"prompt":"PROOF_ARGS_DO_NOT_LEAK"}',
            result={"answer": "PROOF_RESULT_DO_NOT_LEAK"},
            events=[
                {"kind": "llm_call", "payload": {"prompt": "PROOF_EVENT_DO_NOT_LEAK"}},
                {"kind": "replay_sealed", "payload": {"token": "sealed-token"}},
            ],
            file_ops=[
                {"op": "create", "path": "/PROOF_FILE_PATH_DO_NOT_LEAK.txt", "size": 12}
            ],
            card_hash="card-hash-abc",
            repo_url="https://PROOF_REPO_CREDENTIAL_DO_NOT_LEAK@gitea.example/r.git",
            head_sha="feedface",
            image=f"registry.a2acloud.io/agents/{agent_name}:latest",
            agent_url=f"https://{agent_name}.a2acloud.io",
            elapsed_ms=1234,
        )
        session.add(run)
        await session.commit()
        return int(run.id)


def _assert_no_proof_leak(resp) -> None:
    body = resp.text
    for secret in PROOF_SECRETS:
        assert secret not in body, secret


def _latest_of(payload: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in payload:
        if entry["agent_name"] == name:
            return entry["latest"]
    return None


@pytest.mark.asyncio
async def test_public_proof_index_is_allowlisted_for_anonymous_callers(env) -> None:
    """The regression this section exists for, asserted on the key set."""
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)

    for suffix in ("", "?compact=true"):
        resp = await env.client.get(f"/v1/public/agent-proofs{suffix}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [entry["agent_name"] for entry in body] == [PUBLIC_AGENT]
        assert set(body[0]) == PUBLIC_PROOF_SUMMARY_KEYS
        latest = body[0]["latest"]
        assert set(latest) == PUBLIC_PROOF_KEYS, suffix
        assert WITHHELD_PROOF_KEYS.isdisjoint(latest), suffix
        _assert_no_proof_leak(resp)

        # Still a proof: it names the agent, the skill, the verdict and the
        # shape of the work, which is what makes it worth publishing.
        assert latest["status"] == "passed"
        assert latest["badge"] == "verified"
        assert latest["skill_name"] == "search"
        assert latest["head_sha"] == "feedface"
        assert latest["card_hash"] == "card-hash-abc"
        assert latest["events_count"] == 2
        assert latest["file_ops_count"] == 1
        assert latest["elapsed_ms"] == 1234
        assert latest["summary"] == (
            "completed successfully with 1 recorded file operation"
        )
        assert body[0]["total_runs"] == 1
        assert body[0]["passed_runs"] == 1


@pytest.mark.asyncio
async def test_public_proof_latest_is_allowlisted_for_anonymous_callers(env) -> None:
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)

    resp = await env.client.get(f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == PUBLIC_PROOF_SUMMARY_KEYS
    assert set(body["latest"]) == PUBLIC_PROOF_KEYS
    assert WITHHELD_PROOF_KEYS.isdisjoint(body["latest"])
    _assert_no_proof_leak(resp)


@pytest.mark.asyncio
async def test_public_proof_drop_is_allowlisted_for_every_principal(env) -> None:
    """The share link stays one shape for everybody, including its owner.

    A ProofDrop URL is made to be forwarded, so the bytes behind it must not
    depend on who happens to open it — an owner pasting the link into Slack
    would otherwise be publishing a view nobody else sees, and would not know.
    """
    proof_id = await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)
    url = f"/v1/public/agents/{PUBLIC_AGENT}/proofs/{proof_id}"

    for principal in (None, "owner", "admin", "org_maintainer", "intruder"):
        headers = (
            {}
            if principal is None
            else _bearer(issue_token(getattr(env, principal).id))
        )
        resp = await env.client.get(url, headers=headers)
        assert resp.status_code == 200, f"{principal} -> {resp.text}"
        assert set(resp.json()) == PUBLIC_PROOF_KEYS, principal
        assert WITHHELD_PROOF_KEYS.isdisjoint(resp.json()), principal
        _assert_no_proof_leak(resp)


@pytest.mark.asyncio
async def test_evidence_readers_still_get_the_whole_proof_run(env) -> None:
    """Owners lose nothing: same principals as receipts and replay sessions."""
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)

    for principal in ("owner", "admin", "org_admin", "org_maintainer"):
        headers = _bearer(issue_token(getattr(env, principal).id))
        for url in (
            "/v1/public/agent-proofs",
            f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest",
        ):
            resp = await env.client.get(url, headers=headers)
            assert resp.status_code == 200, f"{principal} {url} -> {resp.text}"
            body = resp.json()
            latest = (
                _latest_of(body, PUBLIC_AGENT)
                if isinstance(body, list)
                else body["latest"]
            )
            assert latest is not None, f"{principal} {url}"
            assert set(latest) == EVIDENCE_READER_PROOF_KEYS, (principal, url)
            assert latest["args_preview"] == {"prompt": "PROOF_ARGS_DO_NOT_LEAK"}
            assert latest["result"] == {"answer": "PROOF_RESULT_DO_NOT_LEAK"}
            assert len(latest["events"]) == 2
            assert latest["file_ops"][0]["path"] == (
                "/PROOF_FILE_PATH_DO_NOT_LEAK.txt"
            )
            assert latest["grant_id"] == "PROOF_GRANT_DO_NOT_LEAK"
            assert latest["repo_url"].endswith("@gitea.example/r.git")
            assert latest["error"] == "PROOF_ERROR_DO_NOT_LEAK"
            assert latest["summary"] == "PROOF_SUMMARY_DO_NOT_LEAK /workspace/out.txt"


@pytest.mark.asyncio
async def test_signing_in_without_standing_does_not_widen_the_proof(env) -> None:
    """Discover/invoke is not evidence. Same line the receipts routes draw.

    ``org_member`` can call this agent and read its card; ``buyer`` has paid
    for calls against it. Neither may read what the *owner's* proof run was
    invoked with or what it returned.
    """
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)

    for principal in ("intruder", "org_member", "buyer"):
        headers = _bearer(issue_token(getattr(env, principal).id))
        index = await env.client.get("/v1/public/agent-proofs", headers=headers)
        assert index.status_code == 200, index.text
        latest = _latest_of(index.json(), PUBLIC_AGENT)
        assert latest is not None
        assert set(latest) == PUBLIC_PROOF_KEYS, principal
        _assert_no_proof_leak(index)

        detail = await env.client.get(
            f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest", headers=headers
        )
        assert detail.status_code == 200, detail.text
        assert set(detail.json()["latest"]) == PUBLIC_PROOF_KEYS, principal
        _assert_no_proof_leak(detail)


@pytest.mark.asyncio
async def test_private_agent_proofs_stay_off_every_public_proof_surface(env) -> None:
    """Not even for their own owner: this feed speaks only for public agents."""
    proof_id = await seed_proof_run(env.Session, agent_name=PRIVATE_AGENT)

    for principal in (None, "owner", "admin"):
        headers = (
            {}
            if principal is None
            else _bearer(issue_token(getattr(env, principal).id))
        )
        index = await env.client.get("/v1/public/agent-proofs", headers=headers)
        assert index.status_code == 200, index.text
        assert PRIVATE_AGENT not in [e["agent_name"] for e in index.json()]
        _assert_no_proof_leak(index)

        latest = await env.client.get(
            f"/v1/public/agents/{PRIVATE_AGENT}/proof/latest", headers=headers
        )
        assert latest.status_code == 404, f"{principal} -> {latest.status_code}"

        drop = await env.client.get(
            f"/v1/public/agents/{PRIVATE_AGENT}/proofs/{proof_id}", headers=headers
        )
        assert drop.status_code == 404, f"{principal} -> {drop.status_code}"
        _assert_no_proof_leak(drop)


@pytest.mark.asyncio
async def test_failed_proof_is_public_as_a_verdict_not_as_an_error(env) -> None:
    """A degraded badge is real information; the exception text is not."""
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT, status="failed")

    resp = await env.client.get(f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["badge"] == "degraded"
    assert body["failed_runs"] == 1
    assert body["latest"]["status"] == "failed"
    assert body["latest"]["summary"] == "did not complete successfully"
    assert set(body["latest"]) == PUBLIC_PROOF_KEYS
    _assert_no_proof_leak(resp)


# A security review raised the omission of ``repo_url`` as cosmetic: for a
# *managed* public agent, ``/v1/public/agents/{name}`` publishes the identical
# string as ``source_url``, so ``source_url`` + the still-published
# ``head_sha`` reconstructs the commit URL byte for byte. That reconstruction
# is real, and it is the intended behaviour — the listing publishes the source
# on purpose so a stranger can read the code behind the badge.
#
# What makes the omission load-bearing is the case below, where the listing has
# *declined* to publish. ``_public_source_url`` returns None for an external
# agent or one with no managed repo; ``AgentProofRun.repo_url`` is written
# unconditionally by ``_public_repo_url(name, owner=agent.gitea_owner)`` and so
# still contains the owner's Gitea account name. Republishing the run's copy
# would disclose that account for exactly the agents the listing withholds it
# for — and it would hand out a URL that resolves to nothing.
GITEA_ACCOUNT = "OWNER_GITEA_ACCOUNT_DO_NOT_LEAK"


@pytest.mark.asyncio
async def test_public_proof_never_republishes_a_withheld_source_url(
    env, monkeypatch
) -> None:
    from control_plane.routes import agents as agents_routes
    from control_plane.routes import public as public_routes

    async with env.Session() as session:
        await session.execute(
            update(Agent)
            .where(Agent.name == PUBLIC_AGENT)
            .values(gitea_owner=GITEA_ACCOUNT)
        )
        await session.commit()
        agent = (
            await session.execute(select(Agent).where(Agent.name == PUBLIC_AGENT))
        ).scalar_one()

    # Written exactly the way ``run_agent_proof`` writes it.
    recorded_repo_url = agents_routes._public_repo_url(
        agent.name, owner=agent.gitea_owner
    )
    assert GITEA_ACCOUNT in recorded_repo_url

    async with env.Session() as session:
        run = AgentProofRun(
            agent_id=agent.id,
            agent_name=agent.name,
            user_id=agent.owner_id,
            skill_name="search",
            status="passed",
            repo_url=recorded_repo_url,
            head_sha="feedface",
        )
        session.add(run)
        await session.commit()
        proof_id = int(run.id)

    # The listing declines to publish this agent's source: no managed repo.
    monkeypatch.setattr(public_routes, "repo_exists", lambda *a, **k: False)
    assert await public_routes._public_source_url(agent) is None

    for url in (
        "/v1/public/agent-proofs",
        f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest",
        f"/v1/public/agents/{PUBLIC_AGENT}/proofs/{proof_id}",
    ):
        resp = await env.client.get(url)
        assert resp.status_code == 200, (url, resp.text)
        assert GITEA_ACCOUNT not in resp.text, url
        assert recorded_repo_url not in resp.text, url

    # And the same for its evidence readers' full record: they *do* get
    # ``repo_url``, which is the point of the distinction being about who asks
    # rather than about the field being unpublishable.
    owner_view = await env.client.get(
        f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest",
        headers=_bearer(issue_token(env.owner.id)),
    )
    assert owner_view.status_code == 200, owner_view.text
    assert owner_view.json()["latest"]["repo_url"] == recorded_repo_url


@pytest.mark.asyncio
async def test_principal_dependent_proof_routes_are_not_cached_by_url_alone(
    env,
) -> None:
    """A body that depends on the credential must say so to every cache.

    The two index routes answered identically for everyone until this policy
    existed, so a cache keyed on the URL was correct for them. It no longer is,
    and there is a real shared cache on this path: public site fetchers fetch
    ``/v1/public/agent-proofs`` through Next's data cache with
    ``revalidate: 300``, shared across every visitor of a2acloud.io. Without
    these headers an owner's full record could be stored under the bare URL and
    replayed to strangers — the original bug, reintroduced by a cache instead
    of by a serializer.

    The ProofDrop route is deliberately *absent* from this list: it has one
    body for every principal, so it stays plainly cacheable.
    """
    await seed_proof_run(env.Session, agent_name=PUBLIC_AGENT)
    owner = _bearer(issue_token(env.owner.id))

    for url in (
        "/v1/public/agent-proofs",
        f"/v1/public/agents/{PUBLIC_AGENT}/proof/latest",
    ):
        anon = await env.client.get(url)
        assert anon.status_code == 200, url
        assert anon.headers["vary"] == "Authorization, Cookie", url
        # The anonymous body is the same for everyone, so it stays cacheable —
        # public site fetchers depend on that.
        assert "no-store" not in anon.headers.get("cache-control", ""), url

        signed_in = await env.client.get(url, headers=owner)
        assert signed_in.status_code == 200, url
        assert signed_in.headers["vary"] == "Authorization, Cookie", url
        assert signed_in.headers["cache-control"] == "private, no-store", url
        # Proves the two bodies really do differ, so the headers are load-bearing.
        assert anon.text != signed_in.text, url

    drop = await env.client.get(
        f"/v1/public/agents/{PUBLIC_AGENT}/proofs/"
        f"{(await env.client.get('/v1/public/agent-proofs')).json()[0]['latest']['proof_id']}"
    )
    assert drop.status_code == 200
    assert "vary" not in {k.lower() for k in drop.headers}
    assert (
        drop.text
        == (
            await env.client.get(drop.request.url.path, headers=owner)
        ).text
    )
