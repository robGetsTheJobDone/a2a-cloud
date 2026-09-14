from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_schedules import _args_object, _visible_agent, enqueue_schedule_run
from ..auth import current_user
from ..cron import next_cron_time, normalize_cron, validate_timezone
from ..db import get_session
from ..models import AgentSchedule, User

router = APIRouter(prefix="/v1/me/schedules", tags=["schedules"])


TargetType = Literal["main_agent", "agent"]


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    enabled: bool = True
    cron: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    target_type: TargetType
    prompt: str | None = Field(default=None, max_length=20_000)
    agent_name: str | None = Field(default=None, max_length=128)
    skill_name: str | None = Field(default=None, max_length=128)
    args_json: str = Field(default="{}", max_length=50_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SchedulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    target_type: TargetType | None = None
    prompt: str | None = Field(default=None, max_length=20_000)
    agent_name: str | None = Field(default=None, max_length=128)
    skill_name: str | None = Field(default=None, max_length=128)
    args_json: str | None = Field(default=None, max_length=50_000)
    metadata: dict[str, Any] | None = None


class ScheduleOut(BaseModel):
    schedule_id: str
    name: str
    enabled: bool
    cron: str
    timezone: str
    target_type: str
    prompt: str | None
    agent_name: str | None
    skill_name: str | None
    args_json: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_run_job_id: str | None
    last_error: str | None
    run_count: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@router.get("", response_model=list[ScheduleOut])
async def list_schedules(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ScheduleOut]:
    rows = (
        await session.execute(
            select(AgentSchedule)
            .where(AgentSchedule.user_id == user.id)
            .order_by(desc(AgentSchedule.created_at))
        )
    ).scalars().all()
    return [_schedule_out(row) for row in rows]


@router.post("", response_model=ScheduleOut, status_code=201)
async def create_schedule(
    body: ScheduleIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    values = await _validated_values(body.model_dump(), user=user, session=session)
    row = AgentSchedule(
        schedule_id=_new_schedule_id(),
        user_id=user.id,
        **values,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _schedule_out(row)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: str,
    body: SchedulePatch,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    row = await _get_schedule(schedule_id, user, session)
    current = {
        "name": row.name,
        "enabled": row.enabled,
        "cron": row.cron,
        "timezone": row.timezone,
        "target_type": row.target_type,
        "prompt": row.prompt,
        "agent_name": row.agent_name,
        "skill_name": row.skill_name,
        "args_json": row.args_json,
        "metadata": row.metadata_json or {},
    }
    patch = body.model_dump(exclude_unset=True)
    current.update(patch)
    values = await _validated_values(current, user=user, session=session)
    for key, value in values.items():
        if key == "metadata":
            row.metadata_json = value
        else:
            setattr(row, key, value)
    await session.commit()
    await session.refresh(row)
    return _schedule_out(row)


@router.post("/{schedule_id}/run", response_model=ScheduleOut)
async def run_schedule_now(
    schedule_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    row = await _get_schedule(schedule_id, user, session)
    row.last_run_status = "queued"
    await session.commit()
    await session.refresh(row)
    enqueue_schedule_run(request.app, row.schedule_id, manual=True)
    return _schedule_out(row)


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(
    schedule_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    row = await _get_schedule(schedule_id, user, session)
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


async def _validated_values(
    values: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    try:
        cron = normalize_cron(str(values.get("cron") or ""))
        timezone = validate_timezone(str(values.get("timezone") or "UTC"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    target_type = values.get("target_type")
    if target_type not in {"main_agent", "agent"}:
        raise HTTPException(400, "target_type must be main_agent or agent")

    prompt = _clean_optional(values.get("prompt"))
    agent_name = _clean_optional(values.get("agent_name"))
    skill_name = _clean_optional(values.get("skill_name"))
    args_json = values.get("args_json") or "{}"
    metadata = values.get("metadata") if isinstance(values.get("metadata"), dict) else {}

    if target_type == "main_agent":
        if not prompt:
            raise HTTPException(400, "main_agent schedules require a prompt")
        agent_name = None
        skill_name = None
        args_json = "{}"
    else:
        if not agent_name or not skill_name:
            raise HTTPException(400, "agent schedules require agent_name and skill_name")
        try:
            _args_object(str(args_json))
            await _visible_agent(
                session,
                user=user,
                name=agent_name,
                skill_name=skill_name,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        prompt = None
        args_json = _compact_json(args_json)

    name = str(values.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")

    return {
        "name": name,
        "enabled": bool(values.get("enabled", True)),
        "cron": cron,
        "timezone": timezone,
        "target_type": target_type,
        "prompt": prompt,
        "agent_name": agent_name,
        "skill_name": skill_name,
        "args_json": args_json,
        "next_run_at": next_cron_time(cron, timezone),
        "metadata_json": metadata,
    }


async def _get_schedule(
    schedule_id: str,
    user: User,
    session: AsyncSession,
) -> AgentSchedule:
    row = (
        await session.execute(
            select(AgentSchedule).where(
                AgentSchedule.schedule_id == schedule_id,
                AgentSchedule.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "schedule not found")
    return row


def _schedule_out(row: AgentSchedule) -> ScheduleOut:
    return ScheduleOut(
        schedule_id=row.schedule_id,
        name=row.name,
        enabled=row.enabled,
        cron=row.cron,
        timezone=row.timezone,
        target_type=row.target_type,
        prompt=row.prompt,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        args_json=row.args_json,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,
        last_run_job_id=row.last_run_job_id,
        last_error=row.last_error,
        run_count=int(row.run_count or 0),
        metadata=row.metadata_json or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _clean_optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _compact_json(value: Any) -> str:
    parsed = _args_object(str(value or "{}"))
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


def _new_schedule_id() -> str:
    from uuid import uuid4

    return uuid4().hex
