from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import agent_secrets as secret_store
from ..auth import current_user
from ..db import get_session
from ..models import Agent, AgentSecret, User

router = APIRouter(prefix="/v1/agents/{name}/secrets", tags=["agent-secrets"])

_SECRET_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESERVED_PREFIXES = ("A2A_", "KUBERNETES_")
_RESERVED_KEYS = {"PORT", "HOST", "PYTHONPATH"}


class AgentSecretIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=32768)


class AgentSecretOut(BaseModel):
    id: int
    agent_name: str
    key: str
    value_redacted: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=list[AgentSecretOut])
async def list_agent_secrets(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[AgentSecretOut]:
    agent = await _owned_agent(name, user, session)
    rows = (
        await session.execute(
            select(AgentSecret)
            .where(
                AgentSecret.agent_id == agent.id,
                AgentSecret.user_id == user.id,
            )
            .order_by(AgentSecret.key)
        )
    ).scalars().all()
    return [AgentSecretOut.model_validate(row) for row in rows]


@router.post("", response_model=AgentSecretOut, status_code=201)
async def upsert_agent_secret(
    name: str,
    body: AgentSecretIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentSecretOut:
    agent = await _owned_agent(name, user, session)
    key = _validate_secret_key(body.key)

    try:
        secret_store.upsert_agent_secret_value(
            agent_name=agent.name,
            key=key,
            value=body.value,
            owner_id=user.id,
        )
        secret_store.ensure_agent_secret_projection(agent.name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not apply secret to agent runtime: {exc}") from exc

    row = (
        await session.execute(
            select(AgentSecret).where(
                AgentSecret.agent_id == agent.id,
                AgentSecret.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgentSecret(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            key=key,
            value_redacted=_redact(body.value),
        )
        session.add(row)
    else:
        row.value_redacted = _redact(body.value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(400, f"db error: {exc}") from exc
    await session.refresh(row)
    return AgentSecretOut.model_validate(row)


@router.delete("/{key}", status_code=204)
async def delete_agent_secret(
    name: str,
    key: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _owned_agent(name, user, session)
    key = _validate_secret_key(key)
    row = (
        await session.execute(
            select(AgentSecret).where(
                AgentSecret.agent_id == agent.id,
                AgentSecret.user_id == user.id,
                AgentSecret.key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "secret not found")

    try:
        secret_store.delete_agent_secret_value(agent_name=agent.name, key=key)
        secret_store.ensure_agent_secret_projection(agent.name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"could not remove secret from agent runtime: {exc}") from exc

    await session.delete(row)
    await session.commit()


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name, Agent.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


def _validate_secret_key(raw: str) -> str:
    key = raw.strip()
    if not _SECRET_KEY_RE.match(key):
        raise HTTPException(
            400,
            "secret key must be a valid environment variable name",
        )
    upper = key.upper()
    if upper in _RESERVED_KEYS or any(upper.startswith(prefix) for prefix in _RESERVED_PREFIXES):
        raise HTTPException(400, "secret key uses a reserved platform name")
    return key


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
