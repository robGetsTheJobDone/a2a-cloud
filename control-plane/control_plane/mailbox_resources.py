from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tarfile
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Agent, AgentMailbox, MailboxProvisionEvent, User

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_ALLOWED_SENDERS = 50


@dataclass(frozen=True)
class AgentMailboxDeclaration:
    enabled: bool
    allowed_senders: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def agent_mailbox_address(agent_name: str) -> str:
    return f"{agent_name}@{settings.agent_mail_domain}"


def read_agent_mailbox_declaration_from_tarball(
    tarball_path: str | Path,
) -> AgentMailboxDeclaration | None:
    """Read the ``resources.mailbox`` declaration from an uploaded tarball.

    The public contract mirrors ``resources.databases``: top-level
    ``resources.mailbox`` in ``a2a.yaml``, either ``true`` or a mapping with
    ``enabled`` and ``allowed_senders``.
    """

    with tarfile.open(tarball_path) as archive:
        members = [
            member
            for member in archive.getmembers()
            if not member.isdir() and Path(member.name).name == "a2a.yaml"
        ]
        if not members:
            return None
        member = min(members, key=lambda item: len(Path(item.name).parts))
        handle = archive.extractfile(member)
        if handle is None:
            return None
        text = handle.read(512 * 1024 + 1)
    if len(text) > 512 * 1024:
        raise ValueError("a2a.yaml is too large")
    try:
        data = yaml.safe_load(text.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"a2a.yaml is not valid YAML: {exc}") from exc
    return read_agent_mailbox_declaration_from_manifest(data)


def read_agent_mailbox_declaration_from_manifest(
    data: Any,
) -> AgentMailboxDeclaration | None:
    if not isinstance(data, dict):
        return None
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return None
    mailbox = resources.get("mailbox")
    if mailbox is None or mailbox is False:
        return None
    if mailbox is True:
        return AgentMailboxDeclaration(enabled=True, raw={"enabled": True})
    if not isinstance(mailbox, dict):
        raise ValueError("resources.mailbox must be true or a mapping")
    enabled = bool(mailbox.get("enabled", True))
    if not enabled:
        return None
    raw_senders = mailbox.get("allowed_senders") or []
    if not isinstance(raw_senders, list):
        raise ValueError("resources.mailbox.allowed_senders must be a list")
    if len(raw_senders) > _MAX_ALLOWED_SENDERS:
        raise ValueError(
            f"resources.mailbox.allowed_senders supports at most {_MAX_ALLOWED_SENDERS} entries"
        )
    senders: list[str] = []
    for item in raw_senders:
        address = str(item).strip().lower()
        if not _EMAIL_RE.match(address):
            raise ValueError(f"resources.mailbox.allowed_senders entry is not an email: {item!r}")
        senders.append(address)
    return AgentMailboxDeclaration(enabled=True, allowed_senders=senders, raw=dict(mailbox))


async def get_agent_mailbox(
    session: AsyncSession,
    agent_id: int,
) -> AgentMailbox | None:
    return (
        await session.execute(
            select(AgentMailbox).where(AgentMailbox.agent_id == agent_id)
        )
    ).scalar_one_or_none()


async def reconcile_agent_mailbox(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    declaration: AgentMailboxDeclaration | None,
) -> AgentMailbox | None:
    """Persist the mailbox declared by an agent source repo.

    Declared + no row: request one (status ``pending``). Declared + existing
    row: refresh the allowlist and revive removed/failed rows. Undeclared +
    existing row: mark ``removed`` so the provisioner tears it down. Does not
    commit; callers own the transaction like ``reconcile_agent_database_bindings``.
    """

    if agent.id is None:
        await session.flush()
    mailbox = await get_agent_mailbox(session, agent.id)

    if declaration is None:
        if mailbox is None or mailbox.status == "removed":
            return mailbox
        mailbox.status = "removed"
        session.add(mailbox)
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=agent.id,
                actor_user_id=user.id,
                event_type="agent_mailbox_removed",
                status="queued",
                message=f"mailbox removed for {agent.name} (declaration dropped)",
                data={"address": mailbox.address},
            )
        )
        return mailbox

    address = agent_mailbox_address(agent.name)
    if mailbox is None:
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            address=address,
            status="pending",
            quota_bytes=settings.agent_mailbox_quota_bytes,
        )
        session.add(mailbox)
        await session.flush()
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=agent.id,
                actor_user_id=user.id,
                event_type="agent_mailbox_requested",
                status="queued",
                message=f"requested mailbox {address} for {agent.name}",
                data={"address": address},
            )
        )
    elif mailbox.status in {"removed", "failed"}:
        mailbox.status = "pending"
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=agent.id,
                actor_user_id=user.id,
                event_type="agent_mailbox_requested",
                status="queued",
                message=f"re-requested mailbox {address} for {agent.name}",
                data={"address": address},
            )
        )
    mailbox.agent_name = agent.name
    mailbox.address = address
    mailbox.user_id = agent.owner_id
    mailbox.allowed_senders_json = list(declaration.allowed_senders)
    mailbox.declaration_json = dict(declaration.raw)
    session.add(mailbox)
    return mailbox
