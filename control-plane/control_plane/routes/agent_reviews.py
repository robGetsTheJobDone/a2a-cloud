"""Read-only access to advisory pre-deploy review runs."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import Agent, AgentReviewRun, User

router = APIRouter(prefix="/v1/agents", tags=["agents"])


class ReviewRunOut(BaseModel):
    review_id: str
    deploy_id: str | None
    agent_name: str
    ref: str
    status: str
    summary: str | None
    findings: list[dict[str, Any]]
    critical_count: int
    warning_count: int
    info_count: int
    error: str | None
    elapsed_ms: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    class Config:
        from_attributes = True


async def _assert_caller_owns_agent(
    session: AsyncSession, user: User, name: str
) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent {name!r} not found")
    if agent.owner_id != user.id:
        # Org membership not enforced here for v1; reviews are
        # user-scoped via the issuing JWT.
        raise HTTPException(403, "not the owner of this agent")
    return agent


@router.get("/{name}/review-runs", response_model=list[ReviewRunOut])
async def list_review_runs(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[ReviewRunOut]:
    await _assert_caller_owns_agent(session, user, name)
    rows = (
        await session.execute(
            select(AgentReviewRun)
            .where(AgentReviewRun.agent_name == name)
            .order_by(AgentReviewRun.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [ReviewRunOut.model_validate(r) for r in rows]


@router.get("/{name}/review-runs/latest", response_model=ReviewRunOut)
async def latest_review_run(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewRunOut:
    await _assert_caller_owns_agent(session, user, name)
    row = (
        await session.execute(
            select(AgentReviewRun)
            .where(AgentReviewRun.agent_name == name)
            .order_by(AgentReviewRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"no review runs for {name!r}")
    return ReviewRunOut.model_validate(row)


@router.get("/{name}/review-runs/{review_id}", response_model=ReviewRunOut)
async def get_review_run(
    name: str,
    review_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewRunOut:
    await _assert_caller_owns_agent(session, user, name)
    row = (
        await session.execute(
            select(AgentReviewRun).where(
                AgentReviewRun.agent_name == name,
                AgentReviewRun.review_id == review_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"review {review_id!r} not found")
    return ReviewRunOut.model_validate(row)
