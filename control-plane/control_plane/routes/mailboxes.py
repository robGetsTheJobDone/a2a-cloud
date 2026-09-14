from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..config import settings
from ..db import get_session
from ..mailbox_provisioner import mailbox_provisioner_health
from ..mailbox_resources import agent_mailbox_address, get_agent_mailbox
from ..models import Agent, AgentMailbox, MailboxProvisionEvent, User

router = APIRouter(prefix="/v1/agents/{name}/mailbox", tags=["agent-mailboxes"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_ALLOWED_SENDERS = 50


class AgentMailboxOut(BaseModel):
    agent_name: str
    address: str
    status: str
    quota_bytes: int
    allowed_senders: list[str] = Field(default_factory=list)
    daily_send_limit: int
    outbound_count: int
    disabled_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentMailboxPatchIn(BaseModel):
    allowed_senders: list[str] = Field(max_length=_MAX_ALLOWED_SENDERS)


class MailboxEventOut(BaseModel):
    id: int
    event_type: str
    status: str
    message: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


async def _owned_agent(name: str, user: User, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent {name!r} not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not your agent")
    return agent


def _out(mailbox: AgentMailbox) -> AgentMailboxOut:
    return AgentMailboxOut(
        agent_name=mailbox.agent_name,
        address=mailbox.address,
        status=mailbox.status,
        quota_bytes=mailbox.quota_bytes,
        allowed_senders=list(mailbox.allowed_senders_json or []),
        daily_send_limit=settings.agent_mail_daily_send_limit,
        outbound_count=int(mailbox.outbound_count or 0),
        disabled_at=mailbox.disabled_at,
        created_at=mailbox.created_at,
        updated_at=mailbox.updated_at,
    )


@router.get("", response_model=AgentMailboxOut)
async def get_mailbox(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMailboxOut:
    agent = await _owned_agent(name, user, session)
    mailbox = await get_agent_mailbox(session, agent.id)
    if mailbox is None or mailbox.status == "removed":
        raise HTTPException(404, f"agent {name!r} has no mailbox")
    return _out(mailbox)


@router.post("", response_model=AgentMailboxOut, status_code=201)
async def request_mailbox(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMailboxOut:
    """Request a mailbox without redeploying (equivalent to declaring
    ``resources.mailbox: true`` in ``a2a.yaml``)."""

    agent = await _owned_agent(name, user, session)
    mailbox = await get_agent_mailbox(session, agent.id)
    if mailbox is None:
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            address=agent_mailbox_address(agent.name),
            status="pending",
            quota_bytes=settings.agent_mailbox_quota_bytes,
        )
        session.add(mailbox)
        await session.flush()
    elif mailbox.status in {"removed", "failed", "disabled"}:
        mailbox.status = "pending"
        session.add(mailbox)
    session.add(
        MailboxProvisionEvent(
            mailbox_id=mailbox.id,
            agent_id=agent.id,
            actor_user_id=user.id,
            event_type="agent_mailbox_requested",
            status="queued",
            message=f"mailbox requested via API for {agent.name}",
            data={"address": mailbox.address},
        )
    )
    await session.commit()
    await session.refresh(mailbox)
    return _out(mailbox)


@router.patch("", response_model=AgentMailboxOut)
async def update_mailbox(
    name: str,
    body: AgentMailboxPatchIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentMailboxOut:
    agent = await _owned_agent(name, user, session)
    mailbox = await get_agent_mailbox(session, agent.id)
    if mailbox is None or mailbox.status == "removed":
        raise HTTPException(404, f"agent {name!r} has no mailbox")
    senders: list[str] = []
    for item in body.allowed_senders:
        address = item.strip().lower()
        if not _EMAIL_RE.match(address):
            raise HTTPException(400, f"allowed_senders entry is not an email: {item!r}")
        senders.append(address)
    mailbox.allowed_senders_json = senders
    session.add(mailbox)
    await session.commit()
    await session.refresh(mailbox)
    return _out(mailbox)


@router.delete("", status_code=204)
async def delete_mailbox(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    agent = await _owned_agent(name, user, session)
    mailbox = await get_agent_mailbox(session, agent.id)
    if mailbox is None or mailbox.status == "removed":
        return
    mailbox.status = "removed"
    session.add(mailbox)
    session.add(
        MailboxProvisionEvent(
            mailbox_id=mailbox.id,
            agent_id=agent.id,
            actor_user_id=user.id,
            event_type="agent_mailbox_removed",
            status="queued",
            message=f"mailbox delete requested via API for {agent.name}",
            data={"address": mailbox.address},
        )
    )
    await session.commit()


@router.get("/events", response_model=list[MailboxEventOut])
async def list_mailbox_events(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MailboxEventOut]:
    agent = await _owned_agent(name, user, session)
    rows = (
        await session.execute(
            select(MailboxProvisionEvent)
            .where(MailboxProvisionEvent.agent_id == agent.id)
            .order_by(MailboxProvisionEvent.created_at.desc(), MailboxProvisionEvent.id.desc())
            .limit(100)
        )
    ).scalars().all()
    return [
        MailboxEventOut(
            id=row.id,
            event_type=row.event_type,
            status=row.status,
            message=row.message,
            data=row.data or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


# Unauthenticated operator probe for the provisioner loop, not a customer API.
health_router = APIRouter(
    prefix="/v1/platform/mailboxes",
    tags=["agent-mailboxes"],
    include_in_schema=False,
)


@health_router.get("/health")
async def mailbox_health() -> dict:
    return mailbox_provisioner_health()
