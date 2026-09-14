from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_proofs import preview_args, proof_badge
from ..auth import current_user
from ..db import get_session
from ..models import (
    Agent,
    AgentProofRun,
    SubagentRun,
    SubagentRunEvent,
    TrialRoom,
    TrialRun,
    User,
)

router = APIRouter(prefix="/v1/agents/{name}/insights", tags=["agent-insights"])


class AgentCallLogOut(BaseModel):
    id: str
    source: str
    agent_name: str
    skill_name: str
    status: str
    badge: str | None = None
    summary: str | None = None
    error: str | None = None
    grant_id: str | None = None
    args_preview: dict[str, Any] = Field(default_factory=dict)
    result_preview: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    file_ops: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/calls", response_model=list[AgentCallLogOut])
async def list_agent_call_logs(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=75, ge=1, le=250),
) -> list[AgentCallLogOut]:
    agent = await _owned_agent(name, user, session)

    handoffs = (
        await session.execute(
            select(SubagentRun)
            .where(SubagentRun.agent_name == agent.name)
            .order_by(desc(SubagentRun.id))
            .limit(limit)
        )
    ).scalars().all()
    handoff_events = await _events_by_run_id(handoffs, session)

    trials = (
        await session.execute(
            select(TrialRun, TrialRoom)
            .join(TrialRoom, TrialRun.trial_room_id == TrialRoom.id)
            .where(TrialRun.agent_name == agent.name)
            .order_by(desc(TrialRun.id))
            .limit(limit)
        )
    ).all()

    proofs = (
        await session.execute(
            select(AgentProofRun)
            .where(AgentProofRun.agent_name == agent.name)
            .order_by(desc(AgentProofRun.id))
            .limit(limit)
        )
    ).scalars().all()

    out: list[AgentCallLogOut] = []
    out.extend(_handoff_out(run, handoff_events.get(run.id, [])) for run in handoffs)
    out.extend(_trial_out(run, room) for run, room in trials)
    out.extend(_proof_out(run) for run in proofs)
    out.sort(key=lambda row: row.created_at, reverse=True)
    return out[:limit]


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name, Agent.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


async def _events_by_run_id(
    runs: list[SubagentRun],
    session: AsyncSession,
) -> dict[int, list[SubagentRunEvent]]:
    ids = [run.id for run in runs]
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(SubagentRunEvent)
            .where(SubagentRunEvent.run_id.in_(ids))
            .order_by(SubagentRunEvent.id.asc())
        )
    ).scalars().all()
    grouped: dict[int, list[SubagentRunEvent]] = {run.id: [] for run in runs}
    for row in rows:
        grouped.setdefault(row.run_id, []).append(row)
    return grouped


def _handoff_out(
    run: SubagentRun,
    events: list[SubagentRunEvent],
) -> AgentCallLogOut:
    return AgentCallLogOut(
        id=f"handoff:{run.grant_id}",
        source="handoff",
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        status=run.status,
        summary=run.summary,
        grant_id=run.grant_id,
        args_preview=_args_preview(run.args_json),
        events=[
            {
                "type": event.event_type,
                "payload": event.payload or {},
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ],
        file_ops=run.file_ops or [],
        elapsed_ms=_elapsed_ms(run.created_at, run.completed_at),
        created_at=run.created_at,
        started_at=run.created_at,
        completed_at=run.completed_at,
        metadata={
            "thread_id": run.thread_id,
            "rerun_of_grant_id": run.rerun_of_grant_id,
            "scope_mode": (run.scopes or {}).get("mode"),
        },
    )


def _trial_out(run: TrialRun, room: TrialRoom) -> AgentCallLogOut:
    return AgentCallLogOut(
        id=f"trial:{run.id}",
        source="trial",
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        status=run.status,
        summary=run.summary,
        error=run.error,
        grant_id=run.grant_id,
        args_preview=_args_preview(run.args_json),
        result_preview=_preview_mapping(run.result or {}),
        events=run.events or [],
        file_ops=run.file_ops or [],
        elapsed_ms=run.elapsed_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        metadata={
            "trial_room_slug": room.slug,
            "trial_room_title": room.title,
            "score": run.score,
            "evaluator_notes": run.evaluator_notes,
        },
    )


def _proof_out(run: AgentProofRun) -> AgentCallLogOut:
    return AgentCallLogOut(
        id=f"proof:{run.id}",
        source="proof",
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        status=run.status,
        badge=proof_badge(run.status),
        summary=run.summary,
        error=run.error,
        grant_id=run.grant_id,
        args_preview=_args_preview(run.args_json),
        result_preview=_preview_mapping(run.result or {}),
        events=run.events or [],
        file_ops=run.file_ops or [],
        elapsed_ms=run.elapsed_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        metadata={
            "card_hash": run.card_hash,
            "head_sha": run.head_sha,
            "repo_url": run.repo_url,
            "agent_url": run.agent_url,
        },
    )


def _args_preview(args_json: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(args_json or "{}")
    except json.JSONDecodeError:
        return {}
    return preview_args(parsed) if isinstance(parsed, dict) else {}


def _preview_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return preview_args(value, limit=320) if isinstance(value, dict) else {}


def _elapsed_ms(started: datetime | None, completed: datetime | None) -> int | None:
    if started is None or completed is None:
        return None
    return max(0, int((completed - started).total_seconds() * 1000))
