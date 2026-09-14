"""Persistence + retrieval for signed replay sessions.

The wire schema is owned by :mod:`a2a_pack.replay`. This router only
verifies + stores + serves; we never re-sign.

Storage layout:
    * **Object store** holds the events tuple as newline-delimited JSON at
      ``sessions/{agent_id}/{session_id}.jsonl``. Sessions append-only and
      events arrays grow large; JSONB scans are expensive.
    * **OLTP row** (``agent_sessions``) holds the header fields + the full
      signed token + the object key. Querying / pagination stays cheap, and
      the signed token survives even if the object store fetch fails so a
      forensic reader can re-verify the run.

Inserts are idempotent on ``session_id``; a duplicate post is rejected 409.
"""
from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from a2a_pack.replay import ReplayInvalid, verify_replay_session

from ..agent_authorization import (
    EvidenceRead,
    authorize_session_read,
    resolve_agent_for_evidence_read,
)
from ..auth import optional_current_user, optional_current_user_or_studio_job
from ..db import get_session
from ..models import Agent, AgentSession, User
from ..object_store import (
    ReplayObjectStore,
    get_default_store,
    session_events_key,
)


# Per-agent index + post lives under the agent-name prefix to match
# the receipts router shape.
agent_router = APIRouter(
    prefix="/v1/agents/{name}/sessions", tags=["agent-sessions"]
)

# Lookup by session_id alone has no agent path scope. Sessions are global
# identifiers; the row carries the agent linkage.
session_router = APIRouter(prefix="/v1/sessions", tags=["agent-sessions"])


class SignedTokenIn(BaseModel):
    signed_token: str


class AgentSessionHeader(BaseModel):
    session_id: str
    agent_name: str
    agent_version: str
    caller: str
    task_id: str
    skill_name: str
    receipt_id: str | None
    started_at: int
    ended_at: int
    event_count: int


class AgentSessionPostOut(AgentSessionHeader):
    signed_token: str
    events_object_key: str


def _header(row: AgentSession) -> AgentSessionHeader:
    return AgentSessionHeader(
        session_id=row.session_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        caller=row.caller,
        task_id=row.task_id,
        skill_name=row.skill_name,
        receipt_id=row.receipt_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        event_count=row.event_count,
    )


def _post_out(row: AgentSession) -> AgentSessionPostOut:
    return AgentSessionPostOut(
        **_header(row).model_dump(),
        signed_token=row.signed_token,
        events_object_key=row.events_object_key,
    )


async def _resolve_agent(name: str, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


async def _resolve_agent_for_session_index(
    *,
    name: str,
    user: User | None,
    session: AsyncSession,
) -> EvidenceRead:
    """Evidence gate for the per-agent session index.

    The index maps ``session_id`` -> caller + receipt_id, so publishing it
    leaks who called the agent. ``agent.public`` means anyone may **call** the
    agent, not that its replay index is browsable — an evidence reader browses
    every row, anyone else who can discover the agent sees only the sessions
    they themselves produced.

    The policy lives in
    :func:`..agent_authorization.resolve_agent_for_evidence_read`, shared with
    the receipts router.
    """
    return await resolve_agent_for_evidence_read(session, name=name, user=user)


@agent_router.post("", response_model=AgentSessionPostOut, status_code=201)
async def post_agent_session(
    name: str,
    body: SignedTokenIn,
    session: AsyncSession = Depends(get_session),
) -> AgentSessionPostOut:
    # Intentionally has no user dependency: this is the runtime's
    # self-reporting path, authenticated by the Ed25519 signature that
    # ``verify_replay_session`` checks below.
    agent = await _resolve_agent(name, session)
    try:
        replay = verify_replay_session(body.signed_token)
    except ReplayInvalid as exc:
        raise HTTPException(401, f"replay signature mismatch: {exc}") from exc

    if replay.agent_name and replay.agent_name != name:
        raise HTTPException(
            400,
            f"session agent_name {replay.agent_name!r} != path {name!r}",
        )

    existing = (
        await session.execute(
            select(AgentSession).where(AgentSession.session_id == replay.session_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"session {replay.session_id} already exists")

    events_key = session_events_key(agent.id, replay.session_id)
    store: ReplayObjectStore = get_default_store()
    store.put_jsonl(
        events_key,
        (event.model_dump(mode="json") for event in replay.events),
    )

    row = AgentSession(
        session_id=replay.session_id,
        agent_id=agent.id,
        agent_name=replay.agent_name or name,
        agent_version=replay.agent_version,
        caller=replay.caller,
        task_id=replay.task_id,
        skill_name=replay.skill_name,
        receipt_id=replay.receipt_id or None,
        started_at=int(replay.started_at),
        ended_at=int(replay.ended_at),
        event_count=len(replay.events),
        signed_token=body.signed_token,
        events_object_key=events_key,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, f"session {replay.session_id} already exists") from exc
    await session.refresh(row)
    return _post_out(row)


@agent_router.get("", response_model=list[AgentSessionHeader])
async def list_agent_sessions(
    name: str,
    limit: int = Query(default=50, ge=1, le=200),
    before: int | None = Query(
        default=None,
        description="Only sessions whose started_at is strictly less than this (epoch seconds).",
    ),
    user: User | None = Depends(optional_current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> list[AgentSessionHeader]:
    grant = await _resolve_agent_for_session_index(
        name=name, user=user, session=session
    )
    stmt = (
        select(AgentSession)
        .where(AgentSession.agent_id == grant.agent.id)
        .order_by(desc(AgentSession.started_at), desc(AgentSession.created_at))
        .limit(limit)
    )
    if grant.own_caller is not None:
        stmt = stmt.where(AgentSession.caller == grant.own_caller)
    if before is not None:
        stmt = stmt.where(AgentSession.started_at < before)
    rows = (await session.execute(stmt)).scalars().all()
    return [_header(r) for r in rows]


@session_router.get("/{session_id}")
async def get_session_payload(
    session_id: str,
    since: int = Query(
        default=0, ge=0, description="Skip events with idx < since (resume cursor)."
    ),
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream session header + events as ``application/x-ndjson``.

    The first line is the header JSON; subsequent lines are individual event
    JSON records starting at ``idx >= since``. ndjson keeps the wire shape
    cheap for large sessions and lets the client incrementally render the
    timeline without buffering the whole tuple.

    The event stream carries ``skill_start`` with the caller's **complete**
    validated arguments, so holding the id is not a grant:
    :func:`..agent_authorization.authorize_session_read` serves it to the
    agent's evidence readers and to the caller who produced it, and to nobody
    else. There is no anonymous branch — see that function for why the
    proof-run "publication" grant was removed rather than narrowed.

    Unlike the ``/v1/agents/{name}/...`` reads this keeps plain
    ``optional_current_user``: no path segment names an agent, so the Studio
    scope map cannot bind a job token here, and downgrading an unrelated
    credential to anonymous is the *correct* outcome — an unbindable
    credential must not read more than no credential at all.
    """
    row = (
        await session.execute(
            select(AgentSession).where(AgentSession.session_id == session_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "session not found")
    await authorize_session_read(session, row=row, user=user)

    header = _header(row).model_dump(mode="json")
    store: ReplayObjectStore = get_default_store()

    async def stream() -> AsyncIterator[bytes]:
        import json as _json

        yield (_json.dumps({"header": header}, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            for event in store.iter_jsonl(row.events_object_key, since=since):
                yield (
                    _json.dumps({"event": event}, separators=(",", ":")) + "\n"
                ).encode("utf-8")
        except FileNotFoundError:
            yield (
                _json.dumps(
                    {"error": "events object missing", "key": row.events_object_key},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")

    # The gateway can only know the replay token after a streaming execution
    # finishes.  Direct callers receive this stable URL up front; expose the
    # persisted signed token here so the URL remains independently verifiable.
    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-A2A-Replay-Token": row.signed_token},
    )
