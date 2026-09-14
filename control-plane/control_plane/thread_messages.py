from __future__ import annotations

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ChatThreadMessage

_PERSISTED_ROLES = {"user", "assistant", "system"}


def normalize_thread_messages(raw_msgs: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for m in raw_msgs or []:
        role = (
            getattr(m, "type", None)
            or getattr(m, "role", None)
            or (m.get("role") if isinstance(m, dict) else None)
            or "assistant"
        )
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        content = (
            getattr(m, "content", None)
            or (m.get("content") if isinstance(m, dict) else None)
            or ""
        )
        if not isinstance(content, str):
            try:
                content = "".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            except Exception:  # noqa: BLE001
                content = str(content)
        if role in {"tool", "function"}:
            continue
        if role not in _PERSISTED_ROLES:
            continue
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


async def list_persisted_thread_messages(
    session: AsyncSession,
    thread_id: str,
    *,
    limit: int | None = None,
    max_chars: int | None = None,
) -> list[dict[str, str]]:
    stmt = select(ChatThreadMessage).where(ChatThreadMessage.thread_id == thread_id)
    if limit is not None:
        clean_limit = max(1, int(limit))
        stmt = stmt.order_by(desc(ChatThreadMessage.id)).limit(clean_limit)
        rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
    else:
        rows = list((await session.execute(stmt.order_by(ChatThreadMessage.id))).scalars().all())
    messages = [{"role": row.role, "content": row.content} for row in rows]
    return _trim_messages_to_char_budget(messages, max_chars=max_chars)


def _trim_messages_to_char_budget(
    messages: list[dict[str, str]],
    *,
    max_chars: int | None,
) -> list[dict[str, str]]:
    if max_chars is None:
        return messages
    clean_limit = max(1, int(max_chars))
    selected: list[dict[str, str]] = []
    remaining = clean_limit
    for message in reversed(messages):
        content = message["content"]
        if len(content) <= remaining:
            selected.append(message)
            remaining -= len(content)
            continue
        if not selected:
            prefix = "[earlier chat content omitted]\n"
            if clean_limit > len(prefix):
                content = prefix + content[-(clean_limit - len(prefix)):]
            else:
                content = content[-clean_limit:]
            selected.append({"role": message["role"], "content": content})
        break
    selected.reverse()
    return selected


async def append_thread_messages(
    session: AsyncSession,
    thread_id: str,
    raw_msgs: Any,
) -> int:
    messages = normalize_thread_messages(raw_msgs)
    if not messages:
        return 0

    last = (
        await session.execute(
            select(ChatThreadMessage)
            .where(ChatThreadMessage.thread_id == thread_id)
            .order_by(desc(ChatThreadMessage.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None:
        match_index = next(
            (
                idx
                for idx in range(len(messages) - 1, -1, -1)
                if (
                    messages[idx]["role"] == last.role
                    and messages[idx]["content"] == last.content
                )
            ),
            None,
        )
        messages = (
            messages[match_index + 1 :]
            if match_index is not None
            else messages[-1:]
        )

    added = 0
    for message in messages:
        if (
            last is not None
            and message["role"] == last.role
            and message["content"] == last.content
        ):
            continue
        row = ChatThreadMessage(
            thread_id=thread_id,
            role=message["role"],
            content=message["content"],
        )
        session.add(row)
        last = row
        added += 1
    if added:
        await session.commit()
    return added
