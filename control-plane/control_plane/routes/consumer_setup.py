from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_credential_token, current_user, user_for_agent_audience
from ..agent_authorization import require_agent_access
from ..consumer_setup import (
    ConsumerSetupRequired,
    delete_consumer_setup_value,
    install_agent,
    list_installed_agents as list_installed_agent_records,
    require_consumer_setup_invocation,
    resolve_consumer_setup,
    setup_required_payload,
    upsert_consumer_setup_values,
)
from ..db import get_session
from ..models import Agent, User
from ..schemas import AgentOut

router = APIRouter(prefix="/v1/agents/{name}/consumer-setup", tags=["consumer-setup"])
installed_router = APIRouter(prefix="/v1/installed-agents", tags=["installed-agents"])


class ConsumerSetupFieldOut(BaseModel):
    name: str
    kind: str
    label: str
    description: str = ""
    required: bool = True
    input_type: str = "text"
    options: list[str] = Field(default_factory=list)


class ConsumerSetupValueOut(BaseModel):
    name: str
    kind: str
    configured: bool
    source: str | None = None
    value_redacted: str | None = None
    updated_at: datetime | None = None


class ConsumerSetupOrganizationOut(BaseModel):
    id: int
    slug: str
    name: str
    role: str


class ConsumerSetupStatusOut(BaseModel):
    declaration: dict[str, list[ConsumerSetupFieldOut]]
    values: list[ConsumerSetupValueOut]
    missing_required: list[str]
    complete: bool
    organization: ConsumerSetupOrganizationOut | None = None
    can_manage_org: bool = False


class ConsumerSetupUpsertIn(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    organization_slug: str | None = Field(default=None, max_length=96)


class ConsumerSetupInvocationOut(BaseModel):
    consumer_config: dict[str, Any]
    consumer_secrets: dict[str, str]


class InstalledAgentOrganizationOut(BaseModel):
    id: int
    slug: str
    name: str
    role: str


class InstalledAgentSetupValueOut(ConsumerSetupValueOut):
    label: str
    required: bool
    input_type: str
    scope: str
    organization: InstalledAgentOrganizationOut | None = None


class InstalledAgentAuthConnectionOut(BaseModel):
    id: int
    agent_name: str
    scheme_name: str
    scheme_type: str
    credential_scope: str
    status: str
    expires_at: datetime | None
    last_verified_at: datetime | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class InstalledAgentOut(BaseModel):
    agent: AgentOut
    setup_values: list[InstalledAgentSetupValueOut]
    auth_connections: list[InstalledAgentAuthConnectionOut]
    missing_required: list[str]
    updated_at: datetime | None = None


@installed_router.get("", response_model=list[InstalledAgentOut])
async def list_installed_agents(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[InstalledAgentOut]:
    rows = await list_installed_agent_records(user=user, session=session)
    return [InstalledAgentOut.model_validate(row) for row in rows]


@installed_router.post("/{name}", response_model=InstalledAgentOut)
async def install_marketplace_agent(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> InstalledAgentOut:
    agent = await _visible_agent(name, user, session)
    row = await install_agent(agent=agent, user=user, session=session)
    return InstalledAgentOut.model_validate(row)


@router.get("", response_model=ConsumerSetupStatusOut)
async def get_consumer_setup(
    name: str,
    organization_slug: str | None = Query(default=None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConsumerSetupStatusOut:
    agent = await _visible_agent(name, user, session)
    resolution = await resolve_consumer_setup(
        agent=agent,
        user=user,
        session=session,
        organization_slug=organization_slug,
    )
    return ConsumerSetupStatusOut.model_validate(resolution.payload())


async def _invocation_caller(
    name: str,
    credential: str = Depends(current_credential_token),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the visitor on whose behalf ``name`` is being invoked.

    A hosted agent reads its own origin-bound session cookie and presents it
    here while serving that visitor. The token is scoped to a single agent and
    ``name`` is the agent being invoked, so it cannot surface another agent's
    setup: presented for any other ``name`` it simply fails to resolve.
    """
    return await user_for_agent_audience(session, credential, name)


@router.post("/invocation", response_model=ConsumerSetupInvocationOut)
async def get_consumer_setup_invocation(
    name: str,
    organization_slug: str | None = Query(default=None),
    user: User = Depends(_invocation_caller),
    session: AsyncSession = Depends(get_session),
) -> ConsumerSetupInvocationOut:
    agent = await _visible_agent(name, user, session)
    try:
        resolution = await require_consumer_setup_invocation(
            agent=agent,
            user=user,
            session=session,
            organization_slug=organization_slug,
        )
    except ConsumerSetupRequired as exc:
        raise HTTPException(
            409,
            setup_required_payload(agent=agent, resolution=exc.resolution),
        ) from exc
    return ConsumerSetupInvocationOut.model_validate(resolution.invocation_payload())


@router.put("", response_model=ConsumerSetupStatusOut)
async def upsert_user_consumer_setup(
    name: str,
    body: ConsumerSetupUpsertIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConsumerSetupStatusOut:
    agent = await _visible_agent(name, user, session)
    resolution = await upsert_consumer_setup_values(
        agent=agent,
        user=user,
        session=session,
        values=body.values,
        scope="user",
        organization_slug=body.organization_slug,
    )
    return ConsumerSetupStatusOut.model_validate(resolution.payload())


@router.put("/org", response_model=ConsumerSetupStatusOut)
async def upsert_org_consumer_setup(
    name: str,
    body: ConsumerSetupUpsertIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConsumerSetupStatusOut:
    agent = await _visible_agent(name, user, session)
    resolution = await upsert_consumer_setup_values(
        agent=agent,
        user=user,
        session=session,
        values=body.values,
        scope="org",
        organization_slug=body.organization_slug,
    )
    return ConsumerSetupStatusOut.model_validate(resolution.payload())


@router.delete("/{field_name}", response_model=ConsumerSetupStatusOut)
async def delete_consumer_setup(
    name: str,
    field_name: str,
    scope: Literal["user", "org"] = Query(default="user"),
    organization_slug: str | None = Query(default=None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ConsumerSetupStatusOut:
    agent = await _visible_agent(name, user, session)
    resolution = await delete_consumer_setup_value(
        agent=agent,
        user=user,
        session=session,
        field_name=field_name,
        scope=scope,
        organization_slug=organization_slug,
    )
    return ConsumerSetupStatusOut.model_validate(resolution.payload())


async def _visible_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    await require_agent_access(
        session,
        user=user,
        agent=agent,
        action="use_existing",
    )
    return agent
