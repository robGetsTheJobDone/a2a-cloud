"""Persistence + retrieval for signed execution receipts.

The wire schema is owned by :mod:`a2a_pack.receipts`. This router only
verifies + stores + serves; we never re-sign.

Inserts are idempotent on ``receipt_id`` — agents may retry the POST after
a control-plane timeout, but the platform must never accept a second body
under the same id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from a2a_pack.receipts import ReceiptInvalid, verify_receipt

from ..agent_authorization import EvidenceRead, resolve_agent_for_evidence_read
from ..auth import optional_current_user_or_studio_job
from ..db import get_session
from ..models import Agent, AgentReceipt, User

router = APIRouter(prefix="/v1/agents/{name}/receipts", tags=["agent-receipts"])


class SignedTokenIn(BaseModel):
    signed_token: str


class AgentReceiptHeader(BaseModel):
    receipt_id: str
    agent_name: str
    agent_version: str
    caller: str
    task_id: str
    skill_name: str
    status: str
    eval_score: float | None
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_ms: int
    created_at: datetime


class AgentReceiptOut(AgentReceiptHeader):
    signed_token: str
    payload: dict[str, Any]


def _ts_to_dt(seconds: int) -> datetime | None:
    if not seconds:
        return None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def _header(row: AgentReceipt) -> AgentReceiptHeader:
    return AgentReceiptHeader(
        receipt_id=row.receipt_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        caller=row.caller,
        task_id=row.task_id,
        skill_name=row.skill_name,
        status=row.status,
        eval_score=row.eval_score,
        started_at=row.started_at,
        ended_at=row.ended_at,
        elapsed_ms=row.elapsed_ms,
        created_at=row.created_at,
    )


def _full(row: AgentReceipt) -> AgentReceiptOut:
    return AgentReceiptOut(
        **_header(row).model_dump(),
        signed_token=row.signed_token,
        payload=row.payload or {},
    )


async def _resolve_agent(name: str, session: AsyncSession) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


async def _resolve_agent_for_receipt_read(
    *,
    name: str,
    user: User | None,
    session: AsyncSession,
) -> EvidenceRead:
    """Evidence gate for reading receipts.

    A receipt carries the *caller's* input/result previews, input hash and
    grant ids. ``agent.public`` means anyone may **call** the agent, not that
    every caller's prompt becomes world-readable, so unlike the evidence DAG
    there is no public view here. The deliberately narrow public sharing
    surface stays :mod:`.agent_proofs` / ``/v1/public/agent-proofs``.

    The returned :class:`..agent_authorization.EvidenceRead` says *which rows*:
    every row for an evidence reader, and only the caller's own rows for anyone
    else who can discover the agent — a buyer is billed off the same ``caller``
    string, so they can fetch what they paid for and nothing else.

    The policy itself lives in
    :func:`..agent_authorization.resolve_agent_for_evidence_read` so that this
    route, the replay-session index and the org compliance decision-record
    detail route cannot drift apart again.
    """
    return await resolve_agent_for_evidence_read(session, name=name, user=user)


@router.post("", response_model=AgentReceiptOut, status_code=201)
async def post_agent_receipt(
    name: str,
    body: SignedTokenIn,
    session: AsyncSession = Depends(get_session),
) -> AgentReceiptOut:
    # Intentionally has no user dependency: this is the runtime's
    # self-reporting path and is authenticated by the Ed25519 signature that
    # ``verify_receipt`` checks below. Adding a session/bearer requirement here
    # would break agents posting their own receipts.
    agent = await _resolve_agent(name, session)
    try:
        receipt = verify_receipt(body.signed_token)
    except ReceiptInvalid as exc:
        raise HTTPException(401, f"receipt signature mismatch: {exc}") from exc

    if receipt.agent_name and receipt.agent_name != name:
        raise HTTPException(
            400,
            f"receipt agent_name {receipt.agent_name!r} != path {name!r}",
        )

    existing = (
        await session.execute(
            select(AgentReceipt).where(AgentReceipt.receipt_id == receipt.receipt_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"receipt {receipt.receipt_id} already exists")

    row = AgentReceipt(
        receipt_id=receipt.receipt_id,
        agent_id=agent.id,
        agent_name=receipt.agent_name or name,
        agent_version=receipt.agent_version,
        caller=receipt.caller,
        task_id=receipt.task_id,
        skill_name=receipt.skill_name,
        status=receipt.status,
        eval_score=receipt.eval_score,
        started_at=_ts_to_dt(receipt.started_at),
        ended_at=_ts_to_dt(receipt.ended_at),
        elapsed_ms=int(receipt.elapsed_ms),
        signed_token=body.signed_token,
        payload=receipt.model_dump(mode="json"),
    )
    session.add(row)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, f"receipt {receipt.receipt_id} already exists") from exc
    await session.refresh(row)
    return _full(row)


@router.get("", response_model=list[AgentReceiptHeader])
async def list_agent_receipts(
    name: str,
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(default=None),
    user: User | None = Depends(optional_current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> list[AgentReceiptHeader]:
    grant = await _resolve_agent_for_receipt_read(
        name=name, user=user, session=session
    )
    stmt = (
        select(AgentReceipt)
        .where(AgentReceipt.agent_id == grant.agent.id)
        .order_by(desc(AgentReceipt.started_at), desc(AgentReceipt.created_at))
        .limit(limit)
    )
    if grant.own_caller is not None:
        stmt = stmt.where(AgentReceipt.caller == grant.own_caller)
    if before is not None:
        stmt = stmt.where(AgentReceipt.started_at < before)
    rows = (await session.execute(stmt)).scalars().all()
    return [_header(r) for r in rows]


@router.get("/{receipt_id}", response_model=AgentReceiptOut)
async def get_agent_receipt(
    name: str,
    receipt_id: str,
    user: User | None = Depends(optional_current_user_or_studio_job),
    session: AsyncSession = Depends(get_session),
) -> AgentReceiptOut:
    grant = await _resolve_agent_for_receipt_read(
        name=name, user=user, session=session
    )
    stmt = (
        select(AgentReceipt)
        .where(AgentReceipt.agent_id == grant.agent.id)
        .where(AgentReceipt.receipt_id == receipt_id)
    )
    if grant.own_caller is not None:
        # Someone else's receipt is not "forbidden" to this caller, it is not
        # theirs to address: 404 keeps the id from confirming it exists.
        stmt = stmt.where(AgentReceipt.caller == grant.own_caller)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "receipt not found")
    return _full(row)
