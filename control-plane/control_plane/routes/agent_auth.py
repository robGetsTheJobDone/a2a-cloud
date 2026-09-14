from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..imported_agent_auth import (
    connection_public_dict,
    create_needs_setup_connections,
    detect_auth_requirements,
    list_connections,
    normalize_scheme_type,
    select_requirement_for_setup,
    upsert_connection,
)
from ..models import Agent, AgentAuthConnection, User

router = APIRouter(prefix="/v1/agents/{name}/auth", tags=["agent-auth"])


class AgentAuthRequirementOut(BaseModel):
    scheme_name: str
    scheme_type: str
    required: bool
    supported: bool
    description: str = ""
    location: str | None = None
    name: str | None = None
    scheme: str | None = None
    authorization_url: str | None = None
    token_url: str | None = None
    open_id_connect_url: str | None = None
    scopes: list[str] = Field(default_factory=list)


class AgentAuthConnectionOut(BaseModel):
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


class AgentAuthStatusOut(BaseModel):
    agent_name: str
    status: str
    requirements: list[AgentAuthRequirementOut]
    connections: list[AgentAuthConnectionOut]


class AgentAuthConnectIn(BaseModel):
    scheme_name: str | None = Field(default=None, max_length=128)
    scheme_type: Literal["api_key", "http", "bearer", "oauth2", "oidc", "mtls"]
    credential_scope: Literal["agent", "user"] = "agent"

    value: str | None = Field(default=None, max_length=32768)
    token: str | None = Field(default=None, max_length=32768)
    scheme: str | None = Field(default=None, max_length=32)
    location: Literal["header", "query"] | None = None
    name: str | None = Field(default=None, max_length=128)

    access_token: str | None = Field(default=None, max_length=32768)
    refresh_token: str | None = Field(default=None, max_length=32768)
    token_type: str | None = Field(default="Bearer", max_length=32)
    token_url: str | None = Field(default=None, max_length=1024)
    authorization_url: str | None = Field(default=None, max_length=1024)
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = Field(default=None, max_length=32768)
    expires_in: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    scopes: list[str] = []

    cert_pem: str | None = Field(default=None, max_length=131072)
    key_pem: str | None = Field(default=None, max_length=131072)
    ca_pem: str | None = Field(default=None, max_length=131072)


@router.get("", response_model=AgentAuthStatusOut)
async def get_agent_auth_status(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentAuthStatusOut:
    agent = await _visible_agent(name, user, session)
    return await _auth_status(agent, session, user=user)


@router.post("", response_model=AgentAuthConnectionOut, status_code=201)
async def connect_agent_auth(
    name: str,
    body: AgentAuthConnectIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentAuthConnectionOut:
    agent = await _visible_agent(name, user, session)
    card = agent.card if isinstance(agent.card, dict) else {}
    requirements = detect_auth_requirements(card)
    scheme_type = normalize_scheme_type(body.scheme_type)
    requirement = select_requirement_for_setup(
        requirements,
        scheme_type=scheme_type,
        scheme_name=body.scheme_name,
        api_key_location=body.location,
        api_key_name=body.name,
    )
    if requirements and requirement is None:
        raise HTTPException(400, "auth connection does not match the Agent Card")

    scheme_name = (
        body.scheme_name
        or (str(requirement.get("scheme_name")) if requirement else None)
        or "default"
    )
    secret_payload, metadata, expires_at = _connect_payload(
        body,
        scheme_type=scheme_type,
        requirement=requirement or {},
    )
    row = await upsert_connection(
        session,
        agent=agent,
        user=user,
        scheme_name=scheme_name,
        scheme_type=scheme_type,
        credential_scope=body.credential_scope,
        secret_payload=secret_payload,
        metadata=metadata,
        expires_at=expires_at,
        status="connected",
    )
    rows = await list_connections(session, agent, user=user)
    if agent.owner_id == user.id:
        agent.status = "running"
        _update_import_auth_card_status(agent, rows)
    await session.commit()
    await session.refresh(row)
    return AgentAuthConnectionOut(**connection_public_dict(row))


@router.delete("/{connection_id}", status_code=204)
async def delete_agent_auth_connection(
    name: str,
    connection_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _visible_agent(name, user, session)
    row = (
        await session.execute(
            select(AgentAuthConnection).where(
                AgentAuthConnection.agent_id == agent.id,
                AgentAuthConnection.id == connection_id,
                AgentAuthConnection.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "auth connection not found")
    await session.delete(row)
    remaining = [
        item
        for item in await list_connections(session, agent, user=user)
        if item.id != connection_id
    ]
    if (
        agent.owner_id == user.id
        and detect_auth_requirements(agent.card if isinstance(agent.card, dict) else {})
        and not remaining
    ):
        agent.status = "needs_auth"
    if agent.owner_id == user.id:
        _update_import_auth_card_status(agent, remaining)
    await session.commit()


async def ensure_auth_placeholders(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    requirements: list[dict[str, Any]],
) -> list[AgentAuthConnection]:
    rows = await create_needs_setup_connections(
        session,
        agent=agent,
        user=user,
        requirements=requirements,
    )
    _update_import_auth_card_status(agent, rows)
    return rows


async def refresh_auth_card_status(
    session: AsyncSession,
    agent: Agent,
) -> list[AgentAuthConnection]:
    rows = await list_connections(session, agent)
    _update_import_auth_card_status(agent, rows)
    return rows


def _connect_payload(
    body: AgentAuthConnectIn,
    *,
    scheme_type: str,
    requirement: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], datetime | None]:
    if scheme_type == "api_key":
        value = (body.value or "").strip()
        if not value:
            raise HTTPException(400, "value is required for API-key auth")
        location = body.location or str(requirement.get("location") or "header")
        name = (body.name or str(requirement.get("name") or "")).strip()
        if not name:
            raise HTTPException(400, "name is required for API-key auth")
        metadata = {**requirement, "location": location, "name": name}
        return {"value": value, "location": location, "name": name}, metadata, None

    if scheme_type == "http":
        token = (body.token or body.value or "").strip()
        if not token:
            raise HTTPException(400, "token or value is required for HTTP auth")
        scheme = (body.scheme or str(requirement.get("scheme") or "Bearer")).strip()
        metadata = {**requirement, "scheme": scheme}
        return {"token": token, "scheme": scheme}, metadata, None

    if scheme_type in {"oauth2", "oidc"}:
        access_token = (body.access_token or body.token or body.value or "").strip()
        if not access_token:
            raise HTTPException(400, "access_token is required for OAuth/OIDC auth")
        expires_at = body.expires_at
        if expires_at is None and body.expires_in is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in)
        token_url = body.token_url or str(requirement.get("token_url") or "")
        metadata = {
            **requirement,
            "token_url": token_url or None,
            "authorization_url": body.authorization_url
            or requirement.get("authorization_url"),
            "scopes": body.scopes or requirement.get("scopes") or [],
        }
        return (
            {
                "access_token": access_token,
                "refresh_token": (body.refresh_token or "").strip() or None,
                "token_type": (body.token_type or "Bearer").strip() or "Bearer",
                "token_url": token_url or None,
                "client_id": (body.client_id or "").strip() or None,
                "client_secret": (body.client_secret or "").strip() or None,
            },
            metadata,
            expires_at,
        )

    if scheme_type == "mtls":
        cert_pem = (body.cert_pem or "").strip()
        key_pem = (body.key_pem or "").strip()
        if not cert_pem or not key_pem:
            raise HTTPException(400, "cert_pem and key_pem are required for mTLS auth")
        return (
            {
                "cert_pem": cert_pem,
                "key_pem": key_pem,
                "ca_pem": (body.ca_pem or "").strip() or None,
            },
            dict(requirement),
            None,
        )

    raise HTTPException(400, "unsupported auth scheme type")


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = await _visible_agent(name, user, session)
    if agent.owner_id != user.id:
        raise HTTPException(403, "not allowed")
    return agent


async def _visible_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    if not agent.public and agent.owner_id != user.id:
        raise HTTPException(403, "not allowed")
    return agent


async def _auth_status(
    agent: Agent,
    session: AsyncSession,
    *,
    user: User,
) -> AgentAuthStatusOut:
    requirements = detect_auth_requirements(agent.card if isinstance(agent.card, dict) else {})
    connections = await list_connections(session, agent, user=user)
    status = "connected"
    if requirements and not any(row.status == "connected" for row in connections):
        status = "needs_setup"
    elif any(row.status in {"failed", "expired"} for row in connections):
        status = "failed"
    return AgentAuthStatusOut(
        agent_name=agent.name,
        status=status,
        requirements=[AgentAuthRequirementOut(**item) for item in requirements],
        connections=[
            AgentAuthConnectionOut(**connection_public_dict(row))
            for row in connections
        ],
    )


def _update_import_auth_card_status(
    agent: Agent,
    connections: list[AgentAuthConnection],
) -> None:
    if not isinstance(agent.card, dict):
        return
    capabilities = agent.card.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    imported = capabilities.get("a2a_cloud_import")
    if not isinstance(imported, dict):
        imported = {}
    imported["auth_connections"] = [
        {
            "scheme_name": row.scheme_name,
            "scheme_type": row.scheme_type,
            "status": row.status,
        }
        for row in connections
    ]
    capabilities["a2a_cloud_import"] = imported
    agent.card = {**agent.card, "capabilities": capabilities}
