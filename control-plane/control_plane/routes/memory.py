from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user_or_agent_invoke
from ..config import settings
from ..db import get_session
from ..memory_vector_search import MemoryVectorSearch
from ..models import Agent, AgentMemoryEntry, User, _utcnow

router = APIRouter(prefix="/v1/agents/{name}/memory", tags=["agent-memory"])
log = logging.getLogger(__name__)
memory_vector_search = MemoryVectorSearch.from_settings()

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class MemoryIn(BaseModel):
    namespace: str = Field(default="notes", min_length=1, max_length=96)
    key: str = Field(..., min_length=1, max_length=512)
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryOut(BaseModel):
    id: int
    agent_name: str
    namespace: str
    key: str
    value: Any
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MemorySearchOut(MemoryOut):
    score: float


@router.get("", response_model=list[MemoryOut])
async def list_agent_memory(
    name: str,
    namespace: str = Query(default="notes", min_length=1, max_length=96),
    q: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> list[MemoryOut]:
    agent = await _owned_agent(name, user, session)
    namespace = _validate_namespace(namespace)
    stmt = (
        select(AgentMemoryEntry)
        .where(
            AgentMemoryEntry.agent_id == agent.id,
            AgentMemoryEntry.user_id == user.id,
            AgentMemoryEntry.namespace == namespace,
        )
        .order_by(AgentMemoryEntry.updated_at.desc(), AgentMemoryEntry.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if q:
        needle = q.casefold().strip()
        rows = [
            row
            for row in rows
            if needle in row.key.casefold()
            or needle in str(row.value).casefold()
            or needle in str(row.metadata_json).casefold()
        ]
    return [_to_out(row) for row in rows[:limit]]


@router.get("/search", response_model=list[MemorySearchOut])
async def search_agent_memory(
    name: str,
    q: str = Query(..., min_length=1, max_length=512),
    namespace: str = Query(default="notes", min_length=1, max_length=96),
    limit: int = Query(default=10, ge=1, le=100),
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> list[MemorySearchOut]:
    agent = await _owned_agent(name, user, session)
    namespace = _validate_namespace(namespace)
    if memory_vector_search.configured:
        try:
            matches = await memory_vector_search.query_memory(
                q,
                agent_id=agent.id,
                user_id=user.id,
                namespace=namespace,
                limit=limit,
                score_threshold=settings.memory_semantic_score_threshold,
            )
            rows = await _memory_rows_by_vector_matches(
                session=session,
                agent=agent,
                user=user,
                namespace=namespace,
                matches=matches,
            )
            scores = {match.memory_id: match.score for match in matches}
            return [_to_search_out(row, score=scores[row.id]) for row in rows]
        except Exception:  # noqa: BLE001
            log.warning("memory vector search failed; falling back to lexical", exc_info=True)
    rows = (
        await session.execute(
            select(AgentMemoryEntry).where(
                AgentMemoryEntry.agent_id == agent.id,
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.namespace == namespace,
            )
        )
    ).scalars().all()
    if not rows:
        return []
    scored = [(_lexical_score(q, row), row) for row in rows]
    threshold = settings.memory_semantic_score_threshold
    scored = [
        item for item in scored
        if threshold is None or item[0] >= threshold
    ]
    scored.sort(key=lambda item: (item[0], item[1].updated_at, item[1].id), reverse=True)
    return [_to_search_out(row, score=score) for score, row in scored[:limit]]


@router.get("/{namespace}/{key:path}", response_model=MemoryOut)
async def get_agent_memory(
    name: str,
    namespace: str,
    key: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> MemoryOut:
    agent = await _owned_agent(name, user, session)
    row = await _get_memory_row(agent, user, session, namespace=namespace, key=key)
    if row is None:
        raise HTTPException(404, "memory entry not found")
    return _to_out(row)


@router.put("", response_model=MemoryOut)
@router.post("", response_model=MemoryOut, status_code=201)
async def upsert_agent_memory(
    name: str,
    body: MemoryIn,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> MemoryOut:
    agent = await _owned_agent(name, user, session)
    namespace = _validate_namespace(body.namespace)
    key = _validate_key(body.key)
    row = await _get_memory_row(agent, user, session, namespace=namespace, key=key)
    now = _utcnow()
    if row is None:
        row = AgentMemoryEntry(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            namespace=namespace,
            key=key,
            value=body.value,
            metadata_json=dict(body.metadata),
            updated_at=now,
        )
        session.add(row)
    else:
        row.value = body.value
        row.metadata_json = dict(body.metadata)
        row.updated_at = now
    await session.flush()
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(400, f"db error: {exc}") from exc
    await session.refresh(row)
    await _upsert_memory_vector(row)
    return _to_out(row)


@router.delete("/{namespace}/{key:path}", status_code=204)
async def delete_agent_memory(
    name: str,
    namespace: str,
    key: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _owned_agent(name, user, session)
    row = await _get_memory_row(agent, user, session, namespace=namespace, key=key)
    if row is None:
        raise HTTPException(404, "memory entry not found")
    memory_id = row.id
    await session.delete(row)
    await session.commit()
    await _delete_memory_vector(memory_id)


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name, Agent.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


async def _get_memory_row(
    agent: Agent,
    user: User,
    session: AsyncSession,
    *,
    namespace: str,
    key: str,
) -> AgentMemoryEntry | None:
    namespace = _validate_namespace(namespace)
    key = _validate_key(key)
    return (
        await session.execute(
            select(AgentMemoryEntry).where(
                AgentMemoryEntry.agent_id == agent.id,
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.namespace == namespace,
                AgentMemoryEntry.key == key,
            )
        )
    ).scalar_one_or_none()


def _to_out(row: AgentMemoryEntry) -> MemoryOut:
    return MemoryOut(
        id=row.id,
        agent_name=row.agent_name,
        namespace=row.namespace,
        key=row.key,
        value=row.value,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_search_out(row: AgentMemoryEntry, *, score: float) -> MemorySearchOut:
    data = _to_out(row).model_dump()
    return MemorySearchOut(**data, score=score)


async def _memory_rows_by_vector_matches(
    *,
    session: AsyncSession,
    agent: Agent,
    user: User,
    namespace: str,
    matches: list[Any],
) -> list[AgentMemoryEntry]:
    if not matches:
        return []
    memory_ids = [match.memory_id for match in matches]
    rows = (
        await session.execute(
            select(AgentMemoryEntry).where(
                AgentMemoryEntry.id.in_(memory_ids),
                AgentMemoryEntry.agent_id == agent.id,
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.namespace == namespace,
            )
        )
    ).scalars().all()
    rows_by_id = {row.id: row for row in rows}
    return [rows_by_id[memory_id] for memory_id in memory_ids if memory_id in rows_by_id]


async def _upsert_memory_vector(row: AgentMemoryEntry) -> None:
    if not memory_vector_search.configured:
        return
    try:
        await memory_vector_search.upsert_memory(row)
    except Exception:  # noqa: BLE001
        log.warning("memory vector index update failed for row %s", row.id, exc_info=True)


async def _delete_memory_vector(memory_id: int) -> None:
    if not memory_vector_search.configured:
        return
    try:
        await memory_vector_search.delete_memory(memory_id)
    except Exception:  # noqa: BLE001
        log.warning("memory vector index delete failed for row %s", memory_id, exc_info=True)


def _lexical_score(query: str, row: AgentMemoryEntry) -> float:
    tokens = set(re.findall(r"[A-Za-z0-9_]+", query.casefold()))
    if not tokens:
        return 0.0
    haystack = " ".join(
        [
            row.key,
            str(row.value),
            str(row.metadata_json),
        ]
    ).casefold()
    matches = sum(1 for token in tokens if token in haystack)
    return matches / len(tokens)


def _validate_namespace(raw: str) -> str:
    namespace = raw.strip()
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise HTTPException(400, "memory namespace must be a safe identifier")
    return namespace


def _validate_key(raw: str) -> str:
    key = raw.replace("\\", "/").strip("/")
    parts = key.split("/")
    if (
        not parts
        or len(key) > 512
        or any(not part or part in {".", ".."} for part in parts)
        or any(not _KEY_SEGMENT_RE.fullmatch(part) for part in parts)
    ):
        raise HTTPException(400, "memory key must be a safe relative path")
    return "/".join(parts)
