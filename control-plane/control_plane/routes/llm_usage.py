from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..control_room import llm_usage_status
from ..db import get_session
from ..models import LLMUsageEvent, User

router = APIRouter(prefix="/v1/me/llm-usage", tags=["llm-usage"])


class LLMUsageOut(BaseModel):
    id: int
    thread_id: str | None
    dag_run_id: str | None
    grant_id: str | None
    agent_name: str | None
    skill_name: str | None
    source: str
    provider: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


@router.get("", response_model=list[LLMUsageOut])
async def list_llm_usage(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    thread_id: str | None = Query(default=None),
    grant_id: str | None = Query(default=None),
    dag_run_id: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[LLMUsageOut]:
    stmt = (
        select(LLMUsageEvent)
        .where(LLMUsageEvent.user_id == user.id)
        .order_by(desc(LLMUsageEvent.created_at), desc(LLMUsageEvent.id))
        .limit(limit)
    )
    if thread_id:
        stmt = stmt.where(LLMUsageEvent.thread_id == thread_id)
    if grant_id:
        stmt = stmt.where(LLMUsageEvent.grant_id == grant_id)
    if dag_run_id:
        stmt = stmt.where(LLMUsageEvent.dag_run_id == dag_run_id)
    if agent:
        stmt = stmt.where(LLMUsageEvent.agent_name == agent)
    rows = (await session.execute(stmt)).scalars().all()
    return [_llm_usage_out(row) for row in rows]


def _llm_usage_out(row: LLMUsageEvent) -> LLMUsageOut:
    return LLMUsageOut(
        id=row.id,
        thread_id=row.thread_id,
        dag_run_id=row.dag_run_id,
        grant_id=row.grant_id,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        source=row.source,
        provider=row.provider,
        model=row.model,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        cost_usd=row.cost_usd,
        status=llm_usage_status(row),
        metadata=row.metadata_json or {},
        created_at=row.created_at,
    )
