from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user_or_agent_invoke
from ..db import get_session
from ..models import Agent, MetaAgentRun, User, _utcnow

router = APIRouter(prefix="/v1/agents/{name}/meta-runs", tags=["meta-agent-runs"])

_TERMINAL_STATUSES = {"complete", "completed", "error", "failed", "blocked", "cancelled"}


class MetaRunPlanNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1, max_length=128)
    agent: str = Field(..., min_length=1, max_length=256)
    skill: str = Field(..., min_length=1, max_length=256)
    deps: list[str] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)


class MetaRunPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool | None = None
    goal: str | None = Field(default=None, max_length=16000)
    max_nodes: int | None = Field(default=None, ge=1)
    max_parallel: int | None = Field(default=None, ge=1)
    rounds: list[list[str]] = Field(default_factory=list)
    nodes: list[MetaRunPlanNode] = Field(default_factory=list)


class MetaRunCreateIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=16000)
    success_criteria: list[str] = Field(default_factory=list)
    thread_id: str | None = Field(default=None, max_length=96)
    run_id: str | None = Field(default=None, max_length=64)
    status: str = Field(default="planning", max_length=32)
    current_plan: MetaRunPlan = Field(default_factory=MetaRunPlan)
    progress: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None


class MetaRunPatchIn(BaseModel):
    goal: str | None = Field(default=None, max_length=16000)
    success_criteria: list[str] | None = None
    thread_id: str | None = Field(default=None, max_length=96)
    status: str | None = Field(default=None, max_length=32)
    current_plan: MetaRunPlan | None = None
    progress: list[dict[str, Any]] | None = None
    state: dict[str, Any] | None = None
    summary: str | None = None
    error: str | None = None


class MetaRunOut(BaseModel):
    run_id: str
    agent_name: str
    thread_id: str | None
    goal: str
    success_criteria: list[str]
    status: str
    current_plan: MetaRunPlan
    progress: list[dict[str, Any]]
    state: dict[str, Any]
    summary: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@router.post("", response_model=MetaRunOut, status_code=201)
async def create_meta_run(
    name: str,
    body: MetaRunCreateIn,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> MetaRunOut:
    agent = await _owned_agent(name, user, session)
    run = MetaAgentRun(
        run_id=_validate_run_id(body.run_id or f"mar-{secrets.token_hex(8)}"),
        agent_id=agent.id,
        user_id=user.id,
        agent_name=agent.name,
        thread_id=body.thread_id,
        goal=body.goal.strip(),
        success_criteria=[item.strip() for item in body.success_criteria if item.strip()],
        status=_normalize_status(body.status),
        current_plan=_plan_payload(body.current_plan),
        progress=list(body.progress),
        state=dict(body.state),
        summary=body.summary,
    )
    if run.status in _TERMINAL_STATUSES:
        run.completed_at = _utcnow()
    session.add(run)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(400, f"db error: {exc}") from exc
    await session.refresh(run)
    return _to_out(run)


@router.get("", response_model=list[MetaRunOut])
async def list_meta_runs(
    name: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
    thread_id: str | None = Query(default=None, max_length=96),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MetaRunOut]:
    agent = await _owned_agent(name, user, session)
    stmt = (
        select(MetaAgentRun)
        .where(MetaAgentRun.agent_id == agent.id, MetaAgentRun.user_id == user.id)
        .order_by(desc(MetaAgentRun.updated_at), desc(MetaAgentRun.id))
        .limit(limit)
    )
    if thread_id:
        stmt = stmt.where(MetaAgentRun.thread_id == thread_id)
    if status:
        stmt = stmt.where(MetaAgentRun.status == _normalize_status(status))
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_out(row) for row in rows]


@router.get("/{run_id}", response_model=MetaRunOut)
async def get_meta_run(
    name: str,
    run_id: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> MetaRunOut:
    agent = await _owned_agent(name, user, session)
    run = await _get_run(agent, user, session, run_id)
    if run is None:
        raise HTTPException(404, "meta-agent run not found")
    return _to_out(run)


@router.patch("/{run_id}", response_model=MetaRunOut)
async def patch_meta_run(
    name: str,
    run_id: str,
    body: MetaRunPatchIn,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> MetaRunOut:
    agent = await _owned_agent(name, user, session)
    run = await _get_run(agent, user, session, run_id)
    if run is None:
        raise HTTPException(404, "meta-agent run not found")
    if body.goal is not None:
        run.goal = body.goal.strip()
    if body.success_criteria is not None:
        run.success_criteria = [
            item.strip() for item in body.success_criteria if item.strip()
        ]
    if body.thread_id is not None:
        run.thread_id = body.thread_id
    if body.status is not None:
        run.status = _normalize_status(body.status)
        run.completed_at = _utcnow() if run.status in _TERMINAL_STATUSES else None
    if body.current_plan is not None:
        run.current_plan = _plan_payload(body.current_plan)
    if body.progress is not None:
        run.progress = list(body.progress)
    if body.state is not None:
        run.state = dict(body.state)
    if body.summary is not None:
        run.summary = body.summary
    if body.error is not None:
        run.error = body.error
    run.updated_at = _utcnow()
    await session.commit()
    await session.refresh(run)
    return _to_out(run)


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name, Agent.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


async def _get_run(
    agent: Agent,
    user: User,
    session: AsyncSession,
    run_id: str,
) -> MetaAgentRun | None:
    return (
        await session.execute(
            select(MetaAgentRun).where(
                MetaAgentRun.agent_id == agent.id,
                MetaAgentRun.user_id == user.id,
                MetaAgentRun.run_id == _validate_run_id(run_id),
            )
        )
    ).scalar_one_or_none()


def _to_out(run: MetaAgentRun) -> MetaRunOut:
    return MetaRunOut(
        run_id=run.run_id,
        agent_name=run.agent_name,
        thread_id=run.thread_id,
        goal=run.goal,
        success_criteria=list(run.success_criteria or []),
        status=run.status,
        current_plan=_plan_from_payload(run.current_plan),
        progress=list(run.progress or []),
        state=dict(run.state or {}),
        summary=run.summary,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
    )


def _validate_run_id(raw: str) -> str:
    run_id = raw.strip()
    if not run_id or len(run_id) > 64 or "/" in run_id or ".." in run_id:
        raise HTTPException(400, "run_id must be a safe identifier")
    return run_id


def _normalize_status(raw: str) -> str:
    status = raw.strip().lower()
    if not status or len(status) > 32 or "/" in status:
        raise HTTPException(400, "status must be a safe identifier")
    return status


def _plan_payload(plan: MetaRunPlan) -> dict[str, Any]:
    return plan.model_dump(mode="json", exclude_none=True, exclude_unset=True)


def _plan_from_payload(value: Any) -> MetaRunPlan:
    if isinstance(value, MetaRunPlan):
        return value
    if isinstance(value, dict):
        try:
            return MetaRunPlan.model_validate(value)
        except ValidationError:
            return MetaRunPlan.model_validate({"legacy_plan": value})
    return MetaRunPlan()
