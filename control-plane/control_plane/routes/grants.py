"""Read-only audit endpoint: the current user's grant chain.

Every cross-agent handoff or scope extension writes a row to
``GrantAudit`` (see ``routes/chat.py``'s ``audit_grant`` hook). This
endpoint lets the dashboard / E2E tests reconstruct the chain — including
``parent_grant_id`` links — so a reviewer can answer "who approved this
hand-off and what scope did it run under" without hitting the DB
directly.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import GrantAudit, User

router = APIRouter(prefix="/v1/me/grants", tags=["grants"])


class GrantAuditOut(BaseModel):
    id: int
    grant_id: str
    parent_grant_id: str | None
    issuer: str
    audience: str
    bucket: str
    mode: str
    allow_patterns: list[str]
    deny_patterns: list[str]
    outputs_prefix: str | None
    ttl_seconds: int
    decision: str
    decided_by: str
    reason: str | None
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=list[GrantAuditOut])
async def list_grants(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
    audience: str | None = Query(default=None),
) -> list[GrantAuditOut]:
    """Return the most recent ``limit`` grant rows for the current user.

    Filter by ``audience`` (target agent name) to see all hand-offs to a
    specific agent.
    """
    stmt = (
        select(GrantAudit)
        .where(GrantAudit.user_id == user.id)
        .order_by(GrantAudit.id.desc())
        .limit(limit)
    )
    if audience:
        stmt = stmt.where(GrantAudit.audience == audience)
    rows = (await session.execute(stmt)).scalars().all()
    return [GrantAuditOut.model_validate(r) for r in rows]


@router.get("/{grant_id}", response_model=list[GrantAuditOut])
async def chain_for_grant(
    grant_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[GrantAuditOut]:
    """Return the audit chain for a given grant: the row itself plus every
    extension that lists it as ``parent_grant_id`` (transitively)."""
    out: dict[str, GrantAudit] = {}
    frontier = {grant_id}
    while frontier:
        rows = (await session.execute(
            select(GrantAudit).where(
                GrantAudit.user_id == user.id,
                GrantAudit.grant_id.in_(frontier),
            )
        )).scalars().all()
        children = (await session.execute(
            select(GrantAudit).where(
                GrantAudit.user_id == user.id,
                GrantAudit.parent_grant_id.in_(frontier),
            )
        )).scalars().all()
        next_frontier: set[str] = set()
        for r in list(rows) + list(children):
            if r.grant_id not in out:
                out[r.grant_id] = r
                next_frontier.add(r.grant_id)
        frontier = next_frontier - set(out.keys())
    chain = sorted(out.values(), key=lambda r: r.id)
    return [GrantAuditOut.model_validate(r) for r in chain]
