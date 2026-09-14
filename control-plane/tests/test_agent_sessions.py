from __future__ import annotations

import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import time

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.replay import EventRecorder, seal_replay_session

from control_plane import object_store
from control_plane.db import Base
from control_plane.models import Agent, User
from control_plane.object_store import InMemoryReplayObjectStore
from control_plane.routes import agent_sessions as session_routes
from control_plane.routes.agent_sessions import SignedTokenIn


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="owner@example.com", password_hash="hash")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="research-agent",
            description="",
            version="1.0.0",
            image="registry.example.com/agents/research-agent:latest",
            public=True,
            status="running",
            url="https://research-agent.example.com",
            card={"skills": [{"name": "search"}]},
        )
        session.add(agent)
        await session.commit()
        yield session
    await engine.dispose()


async def _owner(session) -> User:
    """The fixture's agent owner — the per-agent session index is owner-scoped
    since the receipt/session authz fix (see tests/test_agent_receipts_authz.py)."""
    from sqlalchemy import select

    return (
        await session.execute(select(User).where(User.email == "owner@example.com"))
    ).scalar_one()


@pytest.fixture
def fake_store():
    store = InMemoryReplayObjectStore()
    object_store.set_default_store(store)
    try:
        yield store
    finally:
        object_store.set_default_store(None)


def _make_token(
    *,
    agent_name: str = "research-agent",
    skill_name: str = "search",
    caller: str = "user-7",
    session_id: str | None = None,
    receipt_id: str = "",
) -> tuple[str, str, int]:
    started = int(time.time())
    recorder = EventRecorder(
        agent_name=agent_name,
        skill_name=skill_name,
        caller=caller,
        receipt_id=receipt_id,
    )
    recorder.record("skill_start", {"input": "owls"})
    recorder.record("llm_call", {"prompt": "search the web for owls"})
    recorder.record("llm_response", {"hits": 3})
    recorder.record("skill_end", {"ok": True})
    built = recorder.build_session(
        session_id=session_id,
        ended_at=started + 2,
        receipt_id=receipt_id or None,
    )
    session, token = seal_replay_session(built)
    return token, session.session_id, len(session.events)


@pytest.mark.asyncio
async def test_post_session_persists_header_and_events(db_session, fake_store) -> None:
    token, session_id, event_count = _make_token()

    out = await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    assert out.session_id == session_id
    assert out.agent_name == "research-agent"
    assert out.skill_name == "search"
    assert out.caller == "user-7"
    assert out.event_count == event_count
    assert out.signed_token == token
    assert out.events_object_key.endswith(f"/{session_id}.jsonl")
    # Object store has the events tuple.
    stored = fake_store.get_all(out.events_object_key)
    assert len(stored) == event_count
    assert stored[0]["kind"] == "skill_start"
    assert stored[-1]["kind"] == "skill_end"


@pytest.mark.asyncio
async def test_post_session_rejects_tampered_signature(db_session, fake_store) -> None:
    token, _, _ = _make_token()
    last = "A" if token[-1] != "A" else "B"
    tampered = token[:-1] + last

    with pytest.raises(HTTPException) as exc:
        await session_routes.post_agent_session(
            "research-agent",
            SignedTokenIn(signed_token=tampered),
            db_session,
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_post_session_rejects_duplicate_session_id(db_session, fake_store) -> None:
    token, _, _ = _make_token(session_id="dup-session-id")
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    with pytest.raises(HTTPException) as exc:
        await session_routes.post_agent_session(
            "research-agent",
            SignedTokenIn(signed_token=token),
            db_session,
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_post_session_400_when_agent_name_mismatches_path(
    db_session, fake_store
) -> None:
    token, _, _ = _make_token(agent_name="some-other-agent")
    with pytest.raises(HTTPException) as exc:
        await session_routes.post_agent_session(
            "research-agent",
            SignedTokenIn(signed_token=token),
            db_session,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_post_session_404_when_agent_unknown(db_session, fake_store) -> None:
    token, _, _ = _make_token(agent_name="ghost-agent")
    with pytest.raises(HTTPException) as exc:
        await session_routes.post_agent_session(
            "ghost-agent",
            SignedTokenIn(signed_token=token),
            db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_sessions_returns_header_only_recent_first(
    db_session, fake_store
) -> None:
    token_a, id_a, _ = _make_token(caller="user-a", session_id="session-a")
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token_a),
        db_session,
    )
    time.sleep(1.01)  # bump started_at (epoch seconds) so order is deterministic
    token_b, id_b, _ = _make_token(caller="user-b", session_id="session-b")
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token_b),
        db_session,
    )

    rows = await session_routes.list_agent_sessions(
        "research-agent",
        limit=50,
        before=None,
        user=await _owner(db_session),
        session=db_session,
    )

    ids = [r.session_id for r in rows]
    assert set(ids) == {id_a, id_b}
    # newer session (id_b) should be first under desc(started_at)
    assert rows[0].session_id == id_b
    # header model deliberately hides signed_token + events_object_key
    assert not hasattr(rows[0], "signed_token")
    assert not hasattr(rows[0], "events_object_key")


@pytest.mark.asyncio
async def test_list_sessions_respects_before_cursor(db_session, fake_store) -> None:
    token_a, id_a, _ = _make_token(caller="user-a", session_id="session-a")
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token_a),
        db_session,
    )
    time.sleep(1.01)
    token_b, _, _ = _make_token(caller="user-b", session_id="session-b")
    out_b = await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token_b),
        db_session,
    )

    rows = await session_routes.list_agent_sessions(
        "research-agent",
        limit=50,
        before=out_b.started_at,
        user=await _owner(db_session),
        session=db_session,
    )

    ids = [r.session_id for r in rows]
    assert ids == [id_a]


async def _consume_stream_lines(resp) -> list[bytes]:
    """Drive the streaming response to a list of raw lines."""
    chunks: list[bytes] = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    raw = b"".join(chunks)
    return [line for line in raw.split(b"\n") if line]


@pytest.mark.asyncio
async def test_get_session_streams_header_then_events(db_session, fake_store) -> None:
    token, session_id, event_count = _make_token()
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    # Holding the id is not a grant any more: the events carry the caller's
    # arguments, so an unpublished session needs an evidence reader
    # (tests/test_agent_receipts_authz.py owns the policy matrix).
    resp = await session_routes.get_session_payload(
        session_id,
        since=0,
        user=await _owner(db_session),
        session=db_session,
    )

    assert resp.headers["X-A2A-Replay-Token"] == token
    lines = await _consume_stream_lines(resp)
    import json as _json

    parsed = [_json.loads(line) for line in lines]
    assert parsed[0]["header"]["session_id"] == session_id
    assert parsed[0]["header"]["event_count"] == event_count
    events = [p["event"] for p in parsed[1:]]
    assert len(events) == event_count
    assert events[0]["kind"] == "skill_start"
    assert events[0]["idx"] == 0
    assert events[-1]["kind"] == "skill_end"


@pytest.mark.asyncio
async def test_get_session_with_since_skips_earlier_events(
    db_session, fake_store
) -> None:
    token, session_id, event_count = _make_token()
    await session_routes.post_agent_session(
        "research-agent",
        SignedTokenIn(signed_token=token),
        db_session,
    )

    resp = await session_routes.get_session_payload(
        session_id,
        since=2,
        user=await _owner(db_session),
        session=db_session,
    )

    lines = await _consume_stream_lines(resp)
    import json as _json

    parsed = [_json.loads(line) for line in lines]
    events = [p["event"] for p in parsed[1:]]
    assert [e["idx"] for e in events] == list(range(2, event_count))


@pytest.mark.asyncio
async def test_get_session_returns_404_when_missing(db_session, fake_store) -> None:
    with pytest.raises(HTTPException) as exc:
        await session_routes.get_session_payload(
            "no-such-session",
            since=0,
            user=None,
            session=db_session,
        )
    assert exc.value.status_code == 404
