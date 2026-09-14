from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChatThreadEvent


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    if not isinstance(payload.get("type"), str) or not payload.get("type"):
        payload["type"] = "event"
    return payload


async def append_thread_event(
    session: AsyncSession,
    *,
    thread_id: str,
    user_id: int,
    event: dict[str, Any],
    seq_hint: int | None = None,
) -> ChatThreadEvent:
    """Append one ordered chat event to a thread transcript log.

    ``seq_hint`` is the last seq this writer appended; the first attempt
    uses ``seq_hint + 1`` directly, skipping the MAX(seq) query. A
    concurrent writer surfaces as IntegrityError on the (thread_id, seq)
    unique constraint, after which the loop falls back to re-querying MAX.
    """
    payload = _event_payload(event)
    event_type = str(payload.get("type") or "event")[:64]
    last_error: IntegrityError | None = None
    for _ in range(3):
        if seq_hint is not None:
            seq = int(seq_hint) + 1
            seq_hint = None
        else:
            seq = int(
                (
                    await session.execute(
                        select(func.coalesce(func.max(ChatThreadEvent.seq), 0)).where(
                            ChatThreadEvent.thread_id == thread_id
                        )
                    )
                ).scalar_one()
                or 0
            ) + 1
        row = ChatThreadEvent(
            thread_id=thread_id,
            user_id=user_id,
            seq=seq,
            event_type=event_type,
            payload=payload,
        )
        session.add(row)
        try:
            await session.commit()
            return row
        except IntegrityError as exc:
            last_error = exc
            await session.rollback()
    assert last_error is not None
    raise last_error


async def list_thread_events(
    session: AsyncSession,
    *,
    thread_id: str,
    user_id: int,
    after_seq: int | None = None,
    limit: int | None = None,
    latest: bool = False,
) -> list[ChatThreadEvent]:
    stmt = (
        select(ChatThreadEvent)
        .where(
            ChatThreadEvent.thread_id == thread_id,
            ChatThreadEvent.user_id == user_id,
        )
    )
    if after_seq is not None:
        stmt = stmt.where(ChatThreadEvent.seq > after_seq)
    if latest and after_seq is None:
        stmt = stmt.order_by(ChatThreadEvent.seq.desc(), ChatThreadEvent.id.desc())
    else:
        stmt = stmt.order_by(ChatThreadEvent.seq.asc(), ChatThreadEvent.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (
        await session.execute(stmt)
    ).scalars().all()
    if latest and after_seq is None:
        return sorted(rows, key=lambda row: (row.seq, row.id))
    return rows
