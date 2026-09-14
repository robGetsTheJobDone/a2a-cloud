"""Agent Evidence DAG read endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import optional_current_user
from ..db import get_session
from ..evidence_dag import (
    AgentDossierResponse,
    EvidenceDagResponse,
    EvidenceTimelineResponse,
    EvidenceView,
    TimelineLane,
    build_agent_dossier,
    build_evidence_dag,
    build_evidence_timeline,
)
from ..models import Agent, User

router = APIRouter(prefix="/v1/agents", tags=["agent-evidence"])


async def _resolve_agent_for_evidence(
    *,
    name: str,
    user: User | None,
    session: AsyncSession,
) -> tuple[Agent, EvidenceView]:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if user is not None and agent.owner_id == user.id:
        return agent, "owner"
    if agent.public:
        return agent, "public"
    if user is not None:
        raise HTTPException(403, "not authorized to view this agent evidence")
    raise HTTPException(404, "agent not found")


@router.get("/{name}/evidence-dag", response_model=EvidenceDagResponse)
async def get_agent_evidence_dag(
    name: str,
    include_payloads: Annotated[bool, Query()] = False,
    include_warnings: Annotated[bool, Query()] = True,
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> EvidenceDagResponse:
    agent, view = await _resolve_agent_for_evidence(
        name=name,
        user=user,
        session=session,
    )
    return await build_evidence_dag(
        session,
        agent,
        view=view,
        include_payloads=include_payloads,
        include_warnings=include_warnings,
    )


@router.get("/{name}/dossier", response_model=AgentDossierResponse)
async def get_agent_dossier(
    name: str,
    include_payloads: Annotated[bool, Query()] = False,
    include_warnings: Annotated[bool, Query()] = True,
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentDossierResponse:
    agent, view = await _resolve_agent_for_evidence(
        name=name,
        user=user,
        session=session,
    )
    dag = await build_evidence_dag(
        session,
        agent,
        view=view,
        include_payloads=include_payloads,
        include_warnings=include_warnings,
    )
    return build_agent_dossier(dag)


@router.get("/{name}/evidence-timeline", response_model=EvidenceTimelineResponse)
async def get_agent_evidence_timeline(
    name: str,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    head_sha: Annotated[str | None, Query()] = None,
    skill_name: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    severity: Annotated[str | None, Query()] = None,
    lane: Annotated[TimelineLane | None, Query()] = None,
    include_payloads: Annotated[bool, Query()] = False,
    include_inferred: Annotated[bool, Query()] = True,
    include_warnings: Annotated[bool, Query()] = True,
    user: User | None = Depends(optional_current_user),
    session: AsyncSession = Depends(get_session),
) -> EvidenceTimelineResponse:
    agent, view = await _resolve_agent_for_evidence(
        name=name,
        user=user,
        session=session,
    )
    dag = await build_evidence_dag(
        session,
        agent,
        view=view,
        include_payloads=include_payloads,
        include_warnings=include_warnings,
    )
    return build_evidence_timeline(
        dag,
        since=since,
        until=until,
        limit=limit,
        head_sha=head_sha,
        skill_name=skill_name,
        event_type=event_type,
        status=status,
        severity=severity,
        lane=lane,
        include_inferred=include_inferred,
    )
