from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Callable
from importlib import import_module
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import User

router = APIRouter(prefix="/v1/me", tags=["work-ledger"])


class ActivityPageOut(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None


class JobEventsPageOut(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    next_cursor: str | None = None


class CancelJobIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


def _ledger_module() -> Any:
    try:
        return import_module("control_plane.work_ledger")
    except ModuleNotFoundError as exc:
        if exc.name == "control_plane.work_ledger":
            raise HTTPException(503, "work ledger is unavailable") from exc
        raise


def _find_helper(*names: str, required: bool = True) -> Callable[..., Any] | None:
    module = _ledger_module()
    for name in names:
        helper = getattr(module, name, None)
        if callable(helper):
            return helper
    if required:
        raise HTTPException(503, f"work ledger helper unavailable: {names[0]}")
    return None


def _filtered_kwargs(fn: Callable[..., Any], values: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return values
    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return values
    return {
        name: values[name]
        for name, param in params.items()
        if name in values
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


async def _call_helper(
    names: tuple[str, ...],
    *,
    session: AsyncSession,
    user: User,
    **kwargs: Any,
) -> Any:
    helper = _find_helper(*names)
    values = {
        "session": session,
        "db": session,
        "user": user,
        "current_user": user,
        "user_id": user.id,
        "owner_id": user.id,
        **kwargs,
    }
    try:
        result = helper(**_filtered_kwargs(helper, values))
        if inspect.isawaitable(result):
            return await result
        return result
    except HTTPException:
        raise
    except Exception as exc:
        _raise_helper_http(exc)
        raise


def _raise_helper_http(exc: Exception) -> None:
    if exc.__class__.__name__ == "WorkLedgerModelsUnavailable":
        raise HTTPException(503, str(exc)) from exc
    if exc.__class__.__name__ == "WorkJobNotFound":
        raise HTTPException(404, "job not found") from exc


async def _get_job_or_404(
    job_id: str,
    *,
    session: AsyncSession,
    user: User,
) -> dict[str, Any]:
    job = await _call_helper(
        ("get_job", "get_work_job", "get_work_ledger_job"),
        session=session,
        user=user,
        job_id=job_id,
    )
    if job is None:
        raise HTTPException(404, "job not found")
    return _serialize_job(job)


def _raw_page(raw: Any, items_key: str) -> tuple[list[Any], str | None]:
    if raw is None:
        return [], None
    if isinstance(raw, tuple) and len(raw) == 2:
        items, next_cursor = raw
    elif isinstance(raw, dict):
        fallback_key = "items" if items_key != "items" else "events"
        items = raw.get(items_key, raw.get(fallback_key, raw.get("results", [])))
        next_cursor = raw.get("next_cursor", raw.get("cursor"))
    elif isinstance(raw, list):
        items = raw
        next_cursor = None
    else:
        items = getattr(raw, items_key, None)
        if items is None:
            fallback_key = "items" if items_key != "items" else "events"
            items = getattr(raw, fallback_key, [])
        next_cursor = getattr(raw, "next_cursor", getattr(raw, "cursor", None))

    if items is None:
        normalized_items: list[Any] = []
    elif isinstance(items, list):
        normalized_items = items
    elif isinstance(items, tuple):
        normalized_items = list(items)
    else:
        normalized_items = [items]
    return normalized_items, str(next_cursor) if next_cursor is not None else None


def _serialize_job(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        encoded = jsonable_encoder(row)
    else:
        serializer = _find_helper("serialize_job", required=False)
        encoded = jsonable_encoder(serializer(row) if serializer else row)
    return encoded if isinstance(encoded, dict) else {"value": encoded}


def _serialize_event(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        encoded = jsonable_encoder(row)
    else:
        serializer = _find_helper("serialize_event", required=False)
        encoded = jsonable_encoder(serializer(row) if serializer else row)
    return encoded if isinstance(encoded, dict) else {"value": encoded}


def _activity_item(row: Any) -> dict[str, Any]:
    item = _serialize_job(row)
    item["source"] = _activity_source(item.get("source", item.get("kind")))
    return item


def _activity_source(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in {"subagent_run", "subagent_runs"}:
        return "subagent"
    return raw


def _activity_source_kind(value: str | None) -> str | None:
    if value in {"subagent", "subagent_runs"}:
        return "subagent_run"
    return value


def _item_cursor(item: dict[str, Any]) -> str | None:
    for key in ("id", "event_id", "job_id", "cursor"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return None


def _drop_through_cursor(
    items: list[dict[str, Any]],
    cursor: str | None,
) -> list[dict[str, Any]]:
    if cursor is None:
        return items
    for index, item in enumerate(items):
        if cursor in {_item_cursor(item), str(item.get("job_id"))}:
            return items[index + 1:]
    try:
        cursor_int = int(cursor)
    except ValueError:
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        item_cursor = _item_cursor(item)
        if item_cursor is None:
            continue
        try:
            if int(item_cursor) < cursor_int:
                out.append(item)
        except ValueError:
            continue
    return out


def _matches_activity_filter(
    item: dict[str, Any],
    *,
    source: str | None,
    status: str | None,
    q: str | None,
) -> bool:
    if source is not None and _activity_source(
        item.get("source", item.get("kind"))
    ) != _activity_source(source):
        return False
    if status is not None and item.get("status") != status:
        return False
    if q is None:
        return True
    needle = q.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("job_id", "source", "kind", "status", "title", "summary", "error")
    ).lower()
    return needle in haystack


def _cursor_page(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    page_items = items[:limit]
    next_cursor = _item_cursor(page_items[-1]) if len(items) > limit and page_items else None
    return {"items": page_items, "next_cursor": next_cursor}


def _sse_safe(value: Any) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def _event_field(event: Any, *keys: str) -> Any:
    if isinstance(event, dict):
        for key in keys:
            value = event.get(key)
            if value is not None:
                return value
    for key in keys:
        value = getattr(event, key, None)
        if value is not None:
            return value
    return None


def _event_id(event: Any) -> str | None:
    value = _event_field(event, "id", "event_id", "sequence")
    return str(value) if value is not None else None


def _event_type(event: Any) -> str | None:
    value = _event_field(event, "event_type", "type", "event")
    return str(value) if value is not None else None


def _after_filter(events: list[dict[str, Any]], after: str | None) -> list[dict[str, Any]]:
    if after is None:
        return events
    try:
        after_int = int(after)
    except ValueError:
        after_int = None

    filtered: list[dict[str, Any]] = []
    for event in events:
        event_id = _event_id(event)
        if event_id is None:
            filtered.append(event)
            continue
        if after_int is not None:
            try:
                if int(event_id) > after_int:
                    filtered.append(event)
                continue
            except ValueError:
                pass
        if event_id > after:
            filtered.append(event)
    return filtered


def _sse_event(event: Any) -> bytes:
    lines: list[str] = []
    event_id = _event_id(event)
    if event_id is not None:
        lines.append(f"id: {_sse_safe(event_id)}")
    event_type = _event_type(event)
    if event_type:
        lines.append(f"event: {_sse_safe(event_type)}")
    lines.append(
        "data: "
        + json.dumps(jsonable_encoder(event), separators=(",", ":"), ensure_ascii=False)
    )
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def _iter_any_events(raw: Any) -> AsyncIterator[Any]:
    if hasattr(raw, "__aiter__"):
        async for event in raw:
            yield _serialize_event(event)
        return
    for event in _raw_page(raw, "events")[0]:
        yield _serialize_event(event)


async def _event_stream(
    *,
    job_id: str,
    after: str | None,
    user: User,
    session: AsyncSession,
) -> AsyncIterator[bytes]:
    stream_helper = _find_helper(
        "stream_job_events",
        "iter_job_events",
        "watch_job_events",
        required=False,
    )
    if stream_helper is not None:
        values = {
            "session": session,
            "db": session,
            "user": user,
            "current_user": user,
            "user_id": user.id,
            "owner_id": user.id,
            "job_id": job_id,
            "after": after,
            "after_id": after,
            "after_event_id": after,
        }
        try:
            raw = stream_helper(**_filtered_kwargs(stream_helper, values))
            if inspect.isawaitable(raw):
                raw = await raw
        except HTTPException:
            raise
        except Exception as exc:
            _raise_helper_http(exc)
            raise
        async for event in _iter_any_events(raw):
            yield _sse_event(event)
        return

    raw = await _call_helper(
        ("list_job_events", "list_work_job_events", "get_job_events"),
        session=session,
        user=user,
        job=job_id,
        job_id=job_id,
        after=after,
        after_id=after,
        after_event_id=after,
        cursor=after,
        limit=1000,
    )
    events = [_serialize_event(event) for event in _raw_page(raw, "events")[0]]
    for event in _after_filter(events, after):
        yield _sse_event(event)


@router.get("/activity", response_model=ActivityPageOut)
async def list_my_activity(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    fetch_limit = 500 if cursor or q else min(limit + 1, 500)
    raw = await _call_helper(
        (
            "list_activity",
            "list_work_activity",
            "list_work_ledger_activity",
            "list_user_activity",
        ),
        session=session,
        user=user,
        source=source,
        source_filter=source,
        kind=_activity_source_kind(source),
        status=status,
        status_filter=status,
        q=q,
        query=q,
        search=q,
        cursor=cursor,
        limit=fetch_limit,
    )
    items = [_activity_item(row) for row in _raw_page(raw, "items")[0]]
    items = _drop_through_cursor(items, cursor)
    items = [
        item
        for item in items
        if _matches_activity_filter(item, source=source, status=status, q=q)
    ]
    return _cursor_page(items, limit)


@router.get("/jobs/{job_id}")
async def get_my_job(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _get_job_or_404(job_id, session=session, user=user)


@router.post("/jobs/{job_id}/cancel")
async def cancel_my_job(
    job_id: str,
    body: CancelJobIn | None = None,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = await _call_helper(
        ("cancel_job", "cancel_work_job", "cancel_work_ledger_job"),
        session=session,
        user=user,
        job=job_id,
        job_id=job_id,
        reason=(body.reason if body is not None else None),
    )
    return _serialize_job(job)


@router.get("/jobs/{job_id}/events", response_model=JobEventsPageOut)
async def list_my_job_events(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    await _get_job_or_404(job_id, session=session, user=user)
    fetch_limit = 1000 if cursor else min(limit + 1, 1000)
    raw = await _call_helper(
        ("list_job_events", "list_work_job_events", "get_job_events"),
        session=session,
        user=user,
        job=job_id,
        job_id=job_id,
        cursor=cursor,
        limit=fetch_limit,
    )
    events = [_serialize_event(event) for event in _raw_page(raw, "events")[0]]
    events = _after_filter(events, cursor)
    page_events = events[:limit]
    next_cursor = _event_id(page_events[-1]) if len(events) > limit and page_events else None
    return {"events": page_events, "next_cursor": next_cursor}


@router.get("/jobs/{job_id}/events/stream")
async def stream_my_job_events(
    job_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    after: str | None = Query(default=None),
) -> StreamingResponse:
    await _get_job_or_404(job_id, session=session, user=user)

    async def gen() -> AsyncIterator[bytes]:
        async for chunk in _event_stream(
            job_id=job_id,
            after=after,
            user=user,
            session=session,
        ):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
