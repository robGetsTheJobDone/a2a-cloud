"""Bounty endpoints.

A poster describes a problem they want an agent to solve. Any user with a
deployed public agent can ``claim`` the bounty by linking that agent; only
the poster can ``fulfill`` or ``cancel``.
"""
from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..idempotency import IdempotencyContext, begin_idempotency
from ..models import Agent, Bounty, User
from ..schemas import BountyClaimIn, BountyIn, BountyOut

router = APIRouter(prefix="/v1/bounties", tags=["bounties"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_VALID_STATUSES = {"open", "claimed", "fulfilled", "cancelled"}
_IDEMPOTENCY_SCOPE_PREFIX = "bounties"


def _slugify(title: str) -> str:
    """Title → kebab slug. Suffixed with a short random tag on collisions."""
    base = _SLUG_RE.sub("-", title.lower()).strip("-")
    base = base[:64] or "bounty"
    return f"{base}-{secrets.token_hex(3)}"


def _clean_text(value: str, *, field: str, min_length: int, max_length: int) -> str:
    cleaned = value.strip()
    if len(cleaned) < min_length:
        raise HTTPException(400, f"{field} must be at least {min_length} characters")
    if len(cleaned) > max_length:
        raise HTTPException(400, f"{field} must be at most {max_length} characters")
    return cleaned


def _normalize_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        tag = re.sub(r"\s+", "-", raw.strip().lower())
        if not tag:
            continue
        if len(tag) > 40:
            raise HTTPException(400, "tags must be 40 characters or less")
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out



def _validate_status(status: str | None) -> str | None:
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(
            400,
            f"status must be one of {', '.join(sorted(_VALID_STATUSES))}",
        )
    return status


def _disabled_idempotency() -> IdempotencyContext:
    return IdempotencyContext(
        key=None,
        request_hash=None,
        body_hash=None,
        record=None,
    )


async def _begin_bounty_idempotency(
    request: Request | None,
    session: AsyncSession,
    *,
    action: str,
    user: User,
) -> IdempotencyContext:
    if request is None:
        return _disabled_idempotency()
    return await begin_idempotency(
        request,
        session,
        scope=f"{_IDEMPOTENCY_SCOPE_PREFIX}:{action}:user:{user.id}",
    )


async def _store_bounty_success(
    idempotency: IdempotencyContext,
    *,
    status_code: int,
    bounty: Bounty,
    body: BountyOut,
) -> None:
    await idempotency.store_success(
        status_code=status_code,
        body=body,
        extra={
            "result_type": "bounty",
            "result_id": bounty.slug,
            "bounty_id": bounty.id,
        },
    )


async def _serialize(bounty: Bounty, session: AsyncSession) -> BountyOut:
    poster_emails, claimed_agents = await _related_bounty_data([bounty], session)
    return _serialize_with_related(bounty, poster_emails, claimed_agents)


async def _serialize_many(
    bounties: Sequence[Bounty], session: AsyncSession
) -> list[BountyOut]:
    if not bounties:
        return []
    poster_emails, claimed_agents = await _related_bounty_data(bounties, session)
    return [
        _serialize_with_related(bounty, poster_emails, claimed_agents)
        for bounty in bounties
    ]


async def _related_bounty_data(
    bounties: Sequence[Bounty], session: AsyncSession
) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    poster_ids = {
        bounty.posted_by_id
        for bounty in bounties
        if bounty.posted_by_id is not None
    }
    claimed_agent_ids = {
        bounty.claimed_agent_id
        for bounty in bounties
        if bounty.claimed_agent_id is not None
    }

    poster_emails: dict[int, str] = {}
    if poster_ids:
        poster_rows = (
            await session.execute(
                select(User.id, User.email).where(User.id.in_(poster_ids))
            )
        ).all()
        poster_emails = {row.id: row.email for row in poster_rows}

    claimed_agents: dict[int, dict[str, Any]] = {}
    if claimed_agent_ids:
        agent_rows = (
            await session.execute(
                select(
                    Agent.id,
                    Agent.name,
                    Agent.public,
                    Agent.status,
                    Agent.url,
                    Agent.version,
                    Agent.card,
                ).where(Agent.id.in_(claimed_agent_ids))
            )
        ).all()
        claimed_agents = {
            int(row.id): {
                "name": row.name,
                "public": row.public,
                "status": row.status,
                "url": row.url,
                "version": row.version,
                "card": row.card,
            }
            for row in agent_rows
        }

    return poster_emails, claimed_agents


def _serialize_with_related(
    bounty: Bounty,
    poster_emails: dict[int, str],
    claimed_agents: dict[int, dict[str, Any]],
) -> BountyOut:
    poster_email = (
        poster_emails.get(bounty.posted_by_id)
        if bounty.posted_by_id is not None
        else None
    )
    agent = (
        claimed_agents.get(bounty.claimed_agent_id)
        if bounty.claimed_agent_id is not None
        else None
    )
    agent_name = str(agent["name"]) if agent else None
    agent_status: str | None = None
    agent_url: str | None = None
    agent_version: str | None = None
    agent_card: dict[str, Any] | None = None
    if agent and agent.get("public"):
        agent_status = str(agent.get("status") or "")
        raw_url = agent.get("url")
        raw_version = agent.get("version")
        agent_url = str(raw_url) if raw_url is not None else None
        agent_version = str(raw_version) if raw_version is not None else None
        card = agent.get("card")
        agent_card = card if isinstance(card, dict) else None
    data = {
        c.name: getattr(bounty, c.name) for c in Bounty.__table__.columns
    }
    data["posted_by_email"] = poster_email
    data["claimed_agent_name"] = agent_name
    data["claimed_agent_status"] = agent_status
    data["claimed_agent_url"] = agent_url
    data["claimed_agent_version"] = agent_version
    data["claimed_agent_card"] = agent_card
    return BountyOut(**data)


@router.post("", response_model=BountyOut, status_code=201)
async def create_bounty(
    body: BountyIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BountyOut | Response:
    """Post a new bounty. Slug is generated from the title."""
    idempotency = await _begin_bounty_idempotency(
        request, session, action="create", user=user
    )
    if idempotency.cached_response is not None:
        return idempotency.cached_response

    try:
        title = _clean_text(body.title, field="title", min_length=4, max_length=160)
        description = _clean_text(
            body.description, field="description", min_length=10, max_length=8000
        )
        bounty = Bounty(
            slug=_slugify(title),
            title=title,
            description=description,
            example_input=body.example_input.strip(),
            example_output=body.example_output.strip(),
            tags=_normalize_tags(list(body.tags)),
            status="open",
            posted_by_id=user.id,
        )
        session.add(bounty)
        await session.commit()
        await session.refresh(bounty)
        out = await _serialize(bounty, session)
    except HTTPException as exc:
        await idempotency.store_http_exception(exc)
        raise

    await _store_bounty_success(
        idempotency,
        status_code=201,
        bounty=bounty,
        body=out,
    )
    return out


@router.get("", response_model=list[BountyOut])
async def list_bounties(
    response: Response,
    mine: bool = Query(default=False, description="Only my posted + claimed bounties"),
    status: str | None = Query(default=None, description="open|claimed|fulfilled|cancelled"),
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[BountyOut]:
    """List bounties the current user can see (all open, plus their own)."""
    status = _validate_status(status)
    stmt = select(Bounty)
    if mine:
        stmt = stmt.where(
            (Bounty.posted_by_id == user.id) | (Bounty.claimed_by_id == user.id)
        )
    if status:
        stmt = stmt.where(Bounty.status == status)
    stmt = stmt.order_by(Bounty.created_at.desc(), Bounty.id.desc())
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = (await session.execute(stmt)).scalars().all()
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        response.headers["X-A2A-Next-Offset"] = str(offset + limit)
    return await _serialize_many(rows, session)


@router.get("/{slug}", response_model=BountyOut)
async def get_bounty(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> BountyOut:
    """Public-readable bounty detail (no auth required for v1)."""
    bounty = (
        await session.execute(select(Bounty).where(Bounty.slug == slug))
    ).scalar_one_or_none()
    if bounty is None:
        raise HTTPException(404, "bounty not found")
    return await _serialize(bounty, session)


@router.post("/{slug}/claim", response_model=BountyOut)
async def claim_bounty(
    slug: str,
    body: BountyClaimIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BountyOut | Response:
    """Link a user-owned, deployed, public agent to an open bounty.

    Multiple agents may claim the same bounty over time — the latest
    claim wins for v1. We don't fan out yet; that arrives with the
    invocation ledger.
    """
    idempotency = await _begin_bounty_idempotency(
        request, session, action="claim", user=user
    )
    if idempotency.cached_response is not None:
        return idempotency.cached_response

    try:
        bounty = (
            await session.execute(select(Bounty).where(Bounty.slug == slug))
        ).scalar_one_or_none()
        if bounty is None:
            raise HTTPException(404, "bounty not found")
        if bounty.status not in ("open", "claimed"):
            raise HTTPException(409, f"bounty is {bounty.status}, cannot claim")
        if bounty.posted_by_id == user.id:
            raise HTTPException(409, "poster cannot claim their own bounty")

        agent = (
            await session.execute(
                select(Agent).where(
                    (Agent.name == body.agent_name) & (Agent.owner_id == user.id)
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            raise HTTPException(404, f"you do not own a deployed agent {body.agent_name!r}")
        if not agent.public:
            raise HTTPException(400, "agent must be public to claim a bounty")
        if agent.status != "running":
            raise HTTPException(409, f"agent {agent.name!r} is not running")

        bounty.claimed_agent_id = agent.id
        bounty.claimed_by_id = user.id
        bounty.claimed_at = datetime.now(timezone.utc)
        bounty.status = "claimed"
        await session.commit()
        await session.refresh(bounty)
        out = await _serialize(bounty, session)
    except HTTPException as exc:
        await idempotency.store_http_exception(exc)
        raise

    await _store_bounty_success(
        idempotency,
        status_code=200,
        bounty=bounty,
        body=out,
    )
    return out


@router.post("/{slug}/fulfill", response_model=BountyOut)
async def fulfill_bounty(
    slug: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BountyOut | Response:
    """Poster marks the bounty as fulfilled. Settlement happens out of band."""
    idempotency = await _begin_bounty_idempotency(
        request, session, action="fulfill", user=user
    )
    if idempotency.cached_response is not None:
        return idempotency.cached_response

    try:
        bounty = (
            await session.execute(select(Bounty).where(Bounty.slug == slug))
        ).scalar_one_or_none()
        if bounty is None:
            raise HTTPException(404, "bounty not found")
        if bounty.posted_by_id != user.id:
            raise HTTPException(403, "only the poster can mark fulfilled")
        if bounty.status != "claimed":
            raise HTTPException(
                409,
                f"bounty is {bounty.status}, can only fulfill a claimed bounty",
            )
        bounty.status = "fulfilled"
        bounty.fulfilled_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(bounty)
        out = await _serialize(bounty, session)
    except HTTPException as exc:
        await idempotency.store_http_exception(exc)
        raise

    await _store_bounty_success(
        idempotency,
        status_code=200,
        bounty=bounty,
        body=out,
    )
    return out


@router.delete("/{slug}", response_model=BountyOut)
async def cancel_bounty(
    slug: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> BountyOut | Response:
    """Poster cancels the bounty. Terminal."""
    idempotency = await _begin_bounty_idempotency(
        request, session, action="cancel", user=user
    )
    if idempotency.cached_response is not None:
        return idempotency.cached_response

    try:
        bounty = (
            await session.execute(select(Bounty).where(Bounty.slug == slug))
        ).scalar_one_or_none()
        if bounty is None:
            raise HTTPException(404, "bounty not found")
        if bounty.posted_by_id != user.id:
            raise HTTPException(403, "only the poster can cancel")
        if bounty.status == "fulfilled":
            raise HTTPException(409, "already fulfilled")
        bounty.status = "cancelled"
        await session.commit()
        await session.refresh(bounty)
        out = await _serialize(bounty, session)
    except HTTPException as exc:
        await idempotency.store_http_exception(exc)
        raise

    await _store_bounty_success(
        idempotency,
        status_code=200,
        bounty=bounty,
        body=out,
    )
    return out
