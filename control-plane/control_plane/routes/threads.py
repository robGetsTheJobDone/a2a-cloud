"""Chat-thread CRUD endpoints.

Threads exist server-side as :class:`ChatThread` rows for sidebar
listing + titles. The actual conversation state (messages, tool calls,
virtual files) lives in LangGraph's postgres checkpoint tables keyed
by the same thread id.
"""
from __future__ import annotations

import base64
import json
from binascii import Error as BinasciiError
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import (
    ChatThread,
    ChatThreadEvent,
    ChatThreadMessage,
    DagRun,
    DagRunNode,
    SubagentRun,
    SubagentRunEvent,
    User,
)
from ..thread_messages import (
    append_thread_messages,
    list_persisted_thread_messages,
    normalize_thread_messages,
)
from ..thread_events import list_thread_events
from .chat import _evidence_events_from_chat_event

router = APIRouter(prefix="/v1/me/threads", tags=["chat-threads"])

_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 200


class _ThreadOut(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    settings: dict[str, Any] = {}


class _ThreadPageOut(BaseModel):
    items: list[_ThreadOut]
    next_cursor: str | None
    limit: int


class _ThreadCursor(BaseModel):
    updated_at: datetime
    id: str


class _ThreadCreateIn(BaseModel):
    title: str | None = None
    settings: dict[str, Any] | None = None


class _ThreadPatchIn(BaseModel):
    title: str | None = None
    settings: dict[str, Any] | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(t: ChatThread) -> _ThreadOut:
    return _ThreadOut(
        id=t.id, title=t.title,
        created_at=t.created_at.isoformat(),
        updated_at=t.updated_at.isoformat(),
        settings=_thread_settings(t.settings_json),
    )


def _thread_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    numeric_limits = {
        "run_budget_cents": (0, 1_000_000),
        "max_agent_calls_per_run": (1, 100),
    }
    for field, (minimum, maximum) in numeric_limits.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        out[field] = max(minimum, min(parsed, maximum))
    for field in (
        "require_approval_for_file_writes",
        "deny_external_network",
        "only_approved_agents",
        "pii_safe_mode",
    ):
        if isinstance(raw.get(field), bool):
            out[field] = raw[field]
    if isinstance(raw.get("approved_agents"), list):
        out["approved_agents"] = [
            str(item).strip()
            for item in raw["approved_agents"][:200]
            if str(item).strip()
        ]
    return out


def _encode_cursor(t: ChatThread) -> str:
    raw = json.dumps(
        {"updated_at": t.updated_at.isoformat(), "id": t.id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> _ThreadCursor:
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        return _ThreadCursor.model_validate(payload)
    except (
        BinasciiError,
        JSONDecodeError,
        TypeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def _search_pattern(search: str | None) -> str | None:
    if search is None:
        return None
    term = search.strip()
    if not term:
        return None
    escaped = (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


async def _get_thread_or_404(
    thread_id: str,
    *,
    user: User,
    session: AsyncSession,
) -> ChatThread:
    thread = (
        await session.execute(
            select(ChatThread).where(
                ChatThread.id == thread_id,
                ChatThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if thread is None:
        raise HTTPException(404, "thread not found")
    return thread


def _safe_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_with_replay_meta(event: dict[str, Any], at: datetime | None) -> dict[str, Any]:
    out = dict(event)
    out["replayed"] = True
    if at is not None:
        out["replayed_at"] = at.isoformat()
    return out


def _sort_datetime(value: datetime | None) -> datetime:
    if value is None:
        return _utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _expand_replay_event(
    event: dict[str, Any],
    at: datetime | None,
) -> list[tuple[datetime, int, dict[str, Any]]]:
    when = _sort_datetime(at)
    replayed = _event_with_replay_meta(event, at)
    out: list[tuple[datetime, int, dict[str, Any]]] = [(when, 0, replayed)]
    for index, evidence in enumerate(_evidence_events_from_chat_event(replayed), start=1):
        out.append((when, index, _event_with_replay_meta(evidence, at)))
    return out


def _dag_node_started_event(run: DagRun, node: DagRunNode) -> dict[str, Any]:
    return {
        "type": "dag_node_started",
        "dag_run_id": run.dag_run_id,
        "node_id": node.node_id,
        "agent": node.agent_name,
        "skill": node.skill_name,
        "deps": list(node.deps or []),
        "args_preview": _safe_json_object(node.args_json),
    }


def _dag_node_terminal_event(run: DagRun, node: DagRunNode) -> dict[str, Any]:
    event_type = "dag_node_skipped" if node.status == "skipped" else "dag_node_complete"
    return {
        "type": event_type,
        "dag_run_id": run.dag_run_id,
        "node_id": node.node_id,
        "agent": node.agent_name,
        "skill": node.skill_name,
        "ok": node.status not in {"error", "failed"},
        "grant_id": node.grant_id,
        "summary": node.summary or "",
        "result": node.result or {},
        "elapsed_ms": node.elapsed_ms or 0,
        "file_ops": list(node.file_ops or []),
    }


async def _thread_activity_events(
    thread_id: str,
    *,
    user: User,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    entries: list[tuple[datetime, int, dict[str, Any]]] = []
    order = 0

    subagent_rows = (
        await session.execute(
            select(SubagentRunEvent)
            .join(SubagentRun, SubagentRun.id == SubagentRunEvent.run_id)
            .where(
                SubagentRun.thread_id == thread_id,
                SubagentRun.user_id == user.id,
                SubagentRunEvent.user_id == user.id,
            )
            .order_by(SubagentRunEvent.created_at.asc(), SubagentRunEvent.id.asc())
        )
    ).scalars().all()
    for row in subagent_rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if not isinstance(payload.get("type"), str):
            continue
        for entry in _expand_replay_event(payload, row.created_at):
            entries.append((entry[0], order + entry[1], entry[2]))
        order += 100

    dag_runs = (
        await session.execute(
            select(DagRun)
            .where(DagRun.thread_id == thread_id, DagRun.user_id == user.id)
            .order_by(DagRun.created_at.asc(), DagRun.id.asc())
        )
    ).scalars().all()
    for run in dag_runs:
        started = {
            "type": "dag_started",
            "dag_run_id": run.dag_run_id,
            "goal": run.goal,
            "nodes": list(run.nodes_json or []),
        }
        for entry in _expand_replay_event(started, run.created_at):
            entries.append((entry[0], order + entry[1], entry[2]))
        order += 100

        nodes = (
            await session.execute(
                select(DagRunNode)
                .where(
                    DagRunNode.dag_run_id == run.dag_run_id,
                    DagRunNode.user_id == user.id,
                )
                .order_by(DagRunNode.id.asc())
            )
        ).scalars().all()
        for node in nodes:
            if node.started_at is not None:
                for entry in _expand_replay_event(
                    _dag_node_started_event(run, node),
                    node.started_at,
                ):
                    entries.append((entry[0], order + entry[1], entry[2]))
                order += 100
            if node.completed_at is not None or node.status in {"complete", "error", "skipped"}:
                for entry in _expand_replay_event(
                    _dag_node_terminal_event(run, node),
                    node.completed_at or run.updated_at,
                ):
                    entries.append((entry[0], order + entry[1], entry[2]))
                order += 100

        if run.completed_at is not None:
            completed = {
                "type": "dag_complete",
                "dag_run_id": run.dag_run_id,
                "ok": run.status not in {"error", "failed"},
                "summary": run.summary or "",
            }
            for entry in _expand_replay_event(completed, run.completed_at):
                entries.append((entry[0], order + entry[1], entry[2]))
            order += 100

    entries.sort(key=lambda item: (item[0], item[1]))
    return [event for _, __, event in entries]


async def _logged_thread_activity_events(
    thread_id: str,
    *,
    user: User,
    session: AsyncSession,
    after_seq: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in await list_thread_events(
        session,
        thread_id=thread_id,
        user_id=user.id,
        after_seq=after_seq,
        limit=limit,
        latest=limit is not None and after_seq is None,
    ):
        payload = row.payload if isinstance(row.payload, dict) else {}
        if not isinstance(payload.get("type"), str):
            continue
        event = _event_with_replay_meta(payload, row.created_at)
        event["seq"] = row.seq
        events.append(event)
    return events


@router.get("", response_model=list[_ThreadOut] | _ThreadPageOut)
async def list_threads(
    request: Request,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int | None = Query(default=None, ge=1, le=_MAX_PAGE_LIMIT),
    search: str | None = Query(default=None, max_length=160),
    page_mode: bool = Query(default=False, alias="page"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[_ThreadOut] | _ThreadPageOut:
    wants_page = (
        page_mode
        or cursor is not None
        or request.query_params.get("page_mode", "").lower()
        in {"1", "true", "yes", "on"}
    )
    cursor_state = _decode_cursor(cursor) if cursor else None
    pattern = _search_pattern(search)
    stmt = select(ChatThread).where(ChatThread.user_id == user.id)
    if pattern is not None:
        stmt = stmt.where(ChatThread.title.ilike(pattern, escape="\\"))
    if cursor_state is not None:
        stmt = stmt.where(
            or_(
                ChatThread.updated_at < cursor_state.updated_at,
                and_(
                    ChatThread.updated_at == cursor_state.updated_at,
                    ChatThread.id < cursor_state.id,
                ),
            )
        )
    stmt = stmt.order_by(desc(ChatThread.updated_at), desc(ChatThread.id))

    if wants_page:
        page_limit = limit or _DEFAULT_PAGE_LIMIT
        rows = (
            await session.execute(stmt.limit(page_limit + 1))
        ).scalars().all()
        page_rows = rows[:page_limit]
        next_cursor = _encode_cursor(page_rows[-1]) if len(rows) > page_limit else None
        return _ThreadPageOut(
            items=[_serialize(t) for t in page_rows],
            next_cursor=next_cursor,
            limit=page_limit,
        )

    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (
        await session.execute(stmt)
    ).scalars().all()
    return [_serialize(t) for t in rows]


@router.post("", response_model=_ThreadOut, status_code=201)
async def create_thread(
    body: _ThreadCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> _ThreadOut:
    t = ChatThread(
        id=str(uuid4()), user_id=user.id,
        title=(body.title or "New chat")[:255],
        settings_json=_thread_settings(body.settings),
    )
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return _serialize(t)


@router.patch("/{thread_id}", response_model=_ThreadOut)
async def rename_thread(
    thread_id: str, body: _ThreadPatchIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> _ThreadOut:
    t = (
        await session.execute(
            select(ChatThread).where(
                ChatThread.id == thread_id, ChatThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "thread not found")
    if body.title is not None:
        t.title = body.title[:255] or t.title
    if body.settings is not None:
        t.settings_json = _thread_settings(body.settings)
    await session.commit()
    await session.refresh(t)
    return _serialize(t)


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: str, request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    t = (
        await session.execute(
            select(ChatThread).where(
                ChatThread.id == thread_id, ChatThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "thread not found")
    await session.execute(
        delete(ChatThreadEvent).where(ChatThreadEvent.thread_id == thread_id)
    )
    await session.execute(
        delete(ChatThreadMessage).where(ChatThreadMessage.thread_id == thread_id)
    )
    await session.delete(t)
    await session.commit()
    # Also drop the checkpointer's rows for this thread so a recycled
    # uuid (extremely unlikely) wouldn't replay stale state.
    cp = getattr(request.app.state, "checkpointer", None)
    if cp is not None and hasattr(cp, "adelete_thread"):
        try:
            await cp.adelete_thread(thread_id)
        except Exception:  # noqa: BLE001
            pass


@router.get("/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str, request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    t = (
        await session.execute(
            select(ChatThread).where(
                ChatThread.id == thread_id, ChatThread.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "thread not found")
    persisted = await list_persisted_thread_messages(session, thread_id)
    if persisted:
        return {"id": thread_id, "title": t.title, "settings": _thread_settings(t.settings_json), "messages": persisted}
    cp = getattr(request.app.state, "checkpointer", None)
    if cp is None:
        return {"id": thread_id, "title": t.title, "settings": _thread_settings(t.settings_json), "messages": []}
    try:
        snap = await cp.aget_tuple(
            {"configurable": {"thread_id": thread_id}},
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "id": thread_id,
            "title": t.title,
            "settings": _thread_settings(t.settings_json),
            "messages": [],
            "warning": f"checkpoint read failed: {exc}",
        }
    values = ((snap.checkpoint if snap else {}) or {}).get("channel_values", {})
    out = normalize_thread_messages(values.get("messages"))
    if out:
        await append_thread_messages(session, thread_id, out)
    return {"id": thread_id, "title": t.title, "settings": _thread_settings(t.settings_json), "messages": out}


@router.get("/{thread_id}/activity")
async def get_thread_activity(
    thread_id: str,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    thread = await _get_thread_or_404(thread_id, user=user, session=session)
    events = await _logged_thread_activity_events(
        thread_id,
        user=user,
        session=session,
        after_seq=after_seq,
        limit=limit,
    )
    if not events and after_seq is None:
        events = await _thread_activity_events(thread_id, user=user, session=session)
    last_seq = max(
        (
            int(event["seq"])
            for event in events
            if isinstance(event.get("seq"), int)
        ),
        default=after_seq,
    )
    return {
        "id": thread_id,
        "title": thread.title,
        "events": events,
        "last_seq": last_seq,
    }
