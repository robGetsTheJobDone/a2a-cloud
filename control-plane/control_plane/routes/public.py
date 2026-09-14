"""Unauthenticated public read endpoints.

These power the marketing site's static/ISR agent pages and sitemap.
Only ``public=True`` agents are exposed. Managed public agents include their
public source repository URL; repository credentials remain private.

``/v1/public/receipt-keys`` publishes the Ed25519 *public* key that signs
execution receipts, so anyone holding a receipt token can verify it offline.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from a2a_pack.receipts import _ed25519_private_key, _receipt_signing_key_env

try:  # a2a-pack ships the public helper from the release after 0.1.114
    from a2a_pack.receipts import receipt_public_key_b64
except ImportError:  # pragma: no cover - older SDK pin, same key, same encoding
    from a2a_pack.receipts import _ed25519_verifying_key

    def receipt_public_key_b64() -> str | None:
        verifying_key = _ed25519_verifying_key()
        if verifying_key is None:
            return None
        return base64.b64encode(verifying_key.public_bytes_raw()).decode("ascii")

from ..db import get_session
from ..gitea import repo_exists
from ..models import Agent, AgentProofRun, AgentSeoProfile, Bounty, User
from ..platform_internal import is_platform_internal_agent
from ..schemas import AgentDetailOut, AgentOut, AgentSeoProfileOut, BountyOut
from .agents import _is_external_agent, _public_repo_url, _refresh_cards_inplace
from .bounties import (
    _serialize as _serialize_bounty,
    _serialize_many as _serialize_bounties,
)

router = APIRouter(prefix="/v1/public/agents", tags=["public"])
bounties_router = APIRouter(prefix="/v1/public/bounties", tags=["public"])
receipt_keys_router = APIRouter(prefix="/v1/public", tags=["public"])
log = logging.getLogger(__name__)

_FAILED_AGENT_STATUS = "failed"

# Room for every agent id under one quality tier. Ids are a serial primary key
# and the platform is nowhere near a trillion agents, so tiers never collide.
_CURSOR_ID_SPAN = 10**12


class ReceiptKeyOut(BaseModel):
    """One published verifying key. Public halves only — never a private key."""

    kid: str
    alg: str = "Ed25519"
    public_key: str  # base64 of the raw 32-byte Ed25519 public key
    use: str = "receipt"


class ReceiptKeysOut(BaseModel):
    active_kid: str
    keys: list[ReceiptKeyOut]


def _receipt_kid(public_key_b64: str) -> str:
    """Stable key id: first 16 hex chars of sha256 over the raw key bytes."""
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:16]


def _signer_public_key_b64() -> str | None:
    """Base64 public half of ``A2A_RECEIPT_SIGNING_KEY``, if this process signs."""
    signing_key = _receipt_signing_key_env()
    if not signing_key:
        return None
    public_bytes = _ed25519_private_key(signing_key).public_key().public_bytes_raw()
    return base64.b64encode(public_bytes).decode("ascii")


@receipt_keys_router.get("/receipt-keys", response_model=ReceiptKeysOut)
async def get_receipt_keys(response: Response) -> ReceiptKeysOut:
    """Publish the receipt verifying key so third parties can check receipts.

    Governed calls return ``X-A2A-Receipt-Token``; that token verifies against
    the key served here with :func:`a2a_pack.receipts.verify_receipt`.

    When this process holds the signing key — every deployed control plane does,
    ``A2A_RECEIPT_SIGNING_KEY`` and ``A2A_RECEIPT_VERIFYING_KEY`` are mounted
    side by side — the key published is the *public half of the signer* and
    nothing else. ``A2A_RECEIPT_VERIFYING_KEY`` is then only a cross-check: a
    32-byte Ed25519 public key is indistinguishable from a 32-byte private seed,
    so publishing that variable verbatim would hand out the platform's signing
    key if the two secrets were ever swapped. A disagreement is a 503, and so is
    a missing key — never a placeholder, never an unvouched-for key.
    """
    try:
        from_signer = _signer_public_key_b64()
        configured = receipt_public_key_b64()
    except Exception as exc:  # noqa: BLE001
        log.warning("receipt key material is unreadable: %s", exc)
        raise HTTPException(503, "receipt verifying key is not configured") from exc

    if from_signer is not None and configured != from_signer:
        log.error(
            "A2A_RECEIPT_VERIFYING_KEY (kid %s) is not the public half of "
            "A2A_RECEIPT_SIGNING_KEY (kid %s); publishing no key",
            _receipt_kid(configured) if configured else "none",
            _receipt_kid(from_signer),
        )
        raise HTTPException(503, "receipt key material is misconfigured")

    public_key = from_signer or configured
    if not public_key:
        raise HTTPException(503, "receipt verifying key is not configured")
    kid = _receipt_kid(public_key)
    response.headers["Cache-Control"] = "public, max-age=300"
    # A browser-based verifier is the obvious next consumer and there is nothing
    # origin-sensitive here: unauthenticated GET, public key material only.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return ReceiptKeysOut(
        active_kid=kid,
        keys=[ReceiptKeyOut(kid=kid, public_key=public_key)],
    )


@router.get("", response_model=list[AgentOut])
async def list_public_agents(
    response: Response,
    session: AsyncSession = Depends(get_session),
    limit: int | None = Query(default=None, ge=1, le=500),
    cursor: int | None = Query(default=None, ge=1),
) -> list[AgentOut]:
    """Public registry feed, best listings first.

    Ordered by evidence rather than recency: a passed proof run outranks a
    packed browser frontend, which outranks a declared price, and only then
    does a newer agent lead an older one. Ordering by ``id`` put whatever was
    created last on the front page of the marketplace, which is how internal
    scaffolds ended up leading the registry.

    ``status == "failed"`` is dropped here so a deployment that never came up
    cannot be listed at all. That is deliberately the *only* status excluded:
    ``needs_auth`` and the rest are live agents, and hiding a customer's agent
    on a status guess is far more expensive than showing an odd one.

    Every row carries ``platform_internal``, the platform's own answer to
    whether a2a runs that agent for itself (``control_plane.platform_internal``).
    It is *published*, not filtered on: machine-facing consumers of this feed —
    the marketing sitemap and llms.txt — need every listing page that exists to
    stay discoverable, so hiding is left to the display surfaces that ask.

    Two of the three quality signals live inside the ``card`` JSON column, and
    JSON path expressions do not mean the same thing on SQLite (where the tests
    run) and Postgres (where this serves), so the rank is computed in Python
    over the public rows rather than in SQL. The public registry is small — the
    only caller that pages at all is the dashboard, and the marketing site asks
    for the whole feed in one request.
    """
    stmt = select(Agent).where(
        Agent.public == True,  # noqa: E712
        Agent.status != _FAILED_AGENT_STATUS,
    )
    rows = (await session.execute(stmt)).scalars().all()

    proven = await _agent_names_with_passed_proof(session)
    ranked = sorted(
        ((_listing_cursor(row, proven=row.name in proven), row) for row in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if cursor is not None:
        ranked = [pair for pair in ranked if pair[0] < cursor]
    if limit is not None and len(ranked) > limit:
        ranked = ranked[:limit]
        response.headers["X-A2A-Next-Cursor"] = str(ranked[-1][0])
    rows = [row for _, row in ranked]

    refresh_targets = [
        row
        for row in rows
        if not _is_external_agent(row) or not _card_has_skills(row.card)
    ]
    await _refresh_cards_inplace(refresh_targets, session)
    profiles = await _seo_profiles(session, [row.id for row in rows])
    internal_ids = await _platform_internal_ids(session, rows)
    return [
        _public_agent_out(
            row,
            profiles.get(row.id),
            platform_internal=row.id in internal_ids,
        )
        for row in rows
    ]


async def _platform_internal_ids(
    session: AsyncSession, rows: list[Agent]
) -> set[int]:
    """Ids of the agents in ``rows`` that a2a runs for itself.

    One extra query for the whole page: ``is_platform_internal_agent`` needs the
    owner's address and ``Agent`` only carries ``owner_id``.
    """
    owner_ids = {row.owner_id for row in rows}
    if not owner_ids:
        return set()
    emails = dict(
        (
            await session.execute(
                select(User.id, User.email).where(User.id.in_(owner_ids))
            )
        ).all()
    )
    return {
        row.id
        for row in rows
        if is_platform_internal_agent(row.name, emails.get(row.owner_id))
    }


async def _agent_names_with_passed_proof(session: AsyncSession) -> set[str]:
    """Names of agents the platform has invoked successfully at least once.

    A failed proof run is not proof, so only ``passed`` counts — the same rule
    ``proof_badge`` uses to decide whether a listing reads "verified".
    """
    rows = await session.execute(
        select(AgentProofRun.agent_name)
        .where(AgentProofRun.status == "passed")
        .distinct()
    )
    return set(rows.scalars().all())


def _quality_rank(agent: Agent, *, proven: bool) -> int:
    """Evidence tier, as bits so a plain integer compare is the tie-break chain.

    proof (4) > packed frontend (2). Comparing the sum is exactly comparing
    the two signals in that order.
    """
    return (
        (4 if proven else 0)
        + (2 if _card_has_frontend(agent.card) else 0)
    )


def _listing_cursor(agent: Agent, *, proven: bool) -> int:
    """Total order over the feed, as a single opaque descending integer.

    Pagination stays stateless: the cursor is this key, and the next page is
    everything strictly below it. It is not an agent id any more — callers have
    always treated ``X-A2A-Next-Cursor`` as opaque and only echo it back.
    """
    return _quality_rank(agent, proven=proven) * _CURSOR_ID_SPAN + agent.id


def _card_has_skills(card: object) -> bool:
    if not isinstance(card, dict):
        return False
    skills = card.get("skills")
    return isinstance(skills, list) and bool(skills)


def _card_has_frontend(card: object) -> bool:
    """A packed browser UI, i.e. the agent has a real ``/app`` to open."""
    if not isinstance(card, dict):
        return False
    ui = card.get("ui")
    if not isinstance(ui, dict):
        return False
    return any(
        isinstance(ui.get(key), str) and ui[key].strip()
        for key in ("entry", "url")
    )



@bounties_router.get("", response_model=list[BountyOut])
async def list_public_bounties(
    response: Response,
    session: AsyncSession = Depends(get_session),
    limit: int | None = Query(default=None, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[BountyOut]:
    """Open + claimed bounties anyone can see (fulfilled/cancelled hidden).

    Newest first.
    """
    stmt = (
        select(Bounty)
        .where(Bounty.status.in_(("open", "claimed")))
        .order_by(
            Bounty.created_at.desc(),
            Bounty.id.desc(),
        )
        .offset(offset)
    )
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = (await session.execute(stmt)).scalars().all()
    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        response.headers["X-A2A-Next-Offset"] = str(offset + limit)
    serialized = await _serialize_bounties(rows, session)
    return [_public_bounty(bounty) for bounty in serialized]


@bounties_router.get("/{slug}", response_model=BountyOut)
async def get_public_bounty(
    slug: str,
    session: AsyncSession = Depends(get_session),
) -> BountyOut:
    bounty = (
        await session.execute(select(Bounty).where(Bounty.slug == slug))
    ).scalar_one_or_none()
    if bounty is None:
        raise HTTPException(404, "bounty not found")
    return _public_bounty(await _serialize_bounty(bounty, session))


def _public_bounty(bounty: BountyOut) -> BountyOut:
    """Remove poster identity from the acquisition-safe public projection."""
    return bounty.model_copy(update={"posted_by_email": None})


@router.get("/{name}", response_model=AgentDetailOut)
async def get_public_agent(
    name: str,
    session: AsyncSession = Depends(get_session),
) -> AgentDetailOut:
    agent = (
        await session.execute(
            select(Agent).where((Agent.name == name) & (Agent.public == True))  # noqa: E712
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    await _refresh_cards_inplace([agent], session)
    profile = (
        await session.execute(
            select(AgentSeoProfile).where(AgentSeoProfile.agent_id == agent.id)
        )
    ).scalar_one_or_none()
    source_url = await _public_source_url(agent)
    owner_email = (
        await session.execute(select(User.email).where(User.id == agent.owner_id))
    ).scalar_one_or_none()
    return AgentDetailOut(
        **_public_agent_out(
            agent,
            profile,
            source_url=source_url,
            platform_internal=is_platform_internal_agent(agent.name, owner_email),
        ).model_dump()
    )


async def _seo_profiles(
    session: AsyncSession, agent_ids: list[int]
) -> dict[int, AgentSeoProfile]:
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentSeoProfile).where(AgentSeoProfile.agent_id.in_(agent_ids))
        )
    ).scalars().all()
    return {row.agent_id: row for row in rows}


def _public_agent_out(
    agent: Agent,
    profile: AgentSeoProfile | None,
    *,
    source_url: str | None = None,
    platform_internal: bool = False,
) -> AgentOut:
    profile_content = profile.content if profile is not None and isinstance(profile.content, dict) else {}
    card = agent.card if isinstance(agent.card, dict) else {}
    description = str(
        profile_content.get("summary")
        or card.get("description")
        or agent.description
        or ""
    )
    return AgentOut.model_validate(agent).model_copy(
        update={
            "description": description,
            "source_url": source_url,
            "platform_internal": platform_internal,
            "seo_profile": (
                AgentSeoProfileOut.model_validate(profile) if profile is not None else None
            )
        }
    )


async def _public_source_url(agent: Agent) -> str | None:
    if _is_external_agent(agent):
        return None
    try:
        exists = await asyncio.to_thread(
            repo_exists,
            agent.name,
            owner=agent.gitea_owner,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("public source lookup failed for %s: %s", agent.name, exc)
        return None
    if not exists:
        return None
    return _public_repo_url(agent.name, owner=agent.gitea_owner)
