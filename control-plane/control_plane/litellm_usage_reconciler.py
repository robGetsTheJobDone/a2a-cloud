from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .control_room import normalize_llm_usage_payload, record_llm_usage
from .models import LLMUsageEvent

logger = logging.getLogger(__name__)


async def reconcile_litellm_usage_for_thread(
    session: AsyncSession,
    *,
    user_id: int,
    thread_id: str,
    started_at: datetime,
    ended_at: datetime | None = None,
) -> int:
    if not settings.litellm_usage_reconcile_enabled:
        return 0
    if not (settings.litellm_url and settings.litellm_key):
        return 0

    attempts = 6
    delay_seconds = 2.0
    for attempt in range(attempts):
        changed = await _reconcile_litellm_usage_for_thread_once(
            session,
            user_id=user_id,
            thread_id=thread_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        if changed or attempt == attempts - 1:
            return changed
        await asyncio.sleep(delay_seconds)
    return 0


async def _reconcile_litellm_usage_for_thread_once(
    session: AsyncSession,
    *,
    user_id: int,
    thread_id: str,
    started_at: datetime,
    ended_at: datetime | None = None,
) -> int:
    window = timedelta(seconds=max(0, settings.litellm_usage_reconcile_window_seconds))
    start = _naive_utc(started_at - window)
    end = _naive_utc((ended_at or _utcnow()) + window)
    rows = await _fetch_spend_logs(start=start, end=end)
    existing = list(
        (
            await session.execute(
                select(LLMUsageEvent).where(
                    LLMUsageEvent.user_id == user_id,
                    LLMUsageEvent.thread_id == thread_id,
                    LLMUsageEvent.created_at >= started_at - window,
                    LLMUsageEvent.created_at <= (ended_at or _utcnow()) + window,
                )
            )
        ).scalars()
    )
    changed = 0
    for spend_row in rows:
        row_thread_id = _row_thread_id(spend_row)
        row_user_id = _row_user_id(spend_row)
        metadata_matches = row_thread_id == thread_id and row_user_id in {None, user_id}
        if row_thread_id is not None and not metadata_matches:
            continue
        if row_user_id is not None and row_user_id != user_id:
            continue
        normalized = _usage_from_spend_log(spend_row)
        if normalized is None:
            continue
        request_id = str(spend_row.get("request_id") or "")
        target = _find_existing_usage(existing, request_id=request_id, usage=normalized)
        if not metadata_matches and target is None:
            continue
        if target is not None:
            metadata = dict(target.metadata_json or {})
            metadata.update(normalized.get("metadata") or {})
            metadata["litellm_reconciled"] = True
            target.metadata_json = metadata
            if not target.cost_usd and normalized.get("cost_usd"):
                target.cost_usd = float(normalized.get("cost_usd") or 0.0)
            if not target.prompt_tokens:
                target.prompt_tokens = int(normalized.get("prompt_tokens") or 0)
            if not target.completion_tokens:
                target.completion_tokens = int(normalized.get("completion_tokens") or 0)
            if not target.total_tokens:
                target.total_tokens = int(normalized.get("total_tokens") or 0)
            if not target.model and normalized.get("model"):
                target.model = str(normalized.get("model") or "")
            if not target.provider and normalized.get("provider"):
                target.provider = str(normalized.get("provider") or "")
            changed += 1
            continue

        metadata = dict(normalized.get("metadata") or {})
        if request_id:
            metadata["litellm_request_id"] = request_id
        metadata["litellm_reconciled"] = True
        normalized["metadata"] = metadata
        row = await record_llm_usage(
            session,
            user_id=user_id,
            thread_id=thread_id,
            source=_source_for_spend_log(spend_row),
            usage=normalized,
            grant_id=_metadata_value(spend_row, "a2a_grant_id"),
            agent_name=_metadata_value(spend_row, "a2a_agent_name"),
            skill_name=_metadata_value(spend_row, "a2a_skill_name"),
        )
        if row is not None:
            existing.append(row)
            changed += 1
    if changed:
        await session.commit()
    return changed


async def _fetch_spend_logs(*, start: datetime, end: datetime) -> list[dict[str, Any]]:
    page_size = max(1, min(100, int(settings.litellm_usage_reconcile_page_size or 100)))
    base = settings.litellm_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.litellm_key}"}
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        page = 1
        while True:
            response = await client.get(
                f"{base}/spend/logs/v2",
                headers=headers,
                params={
                    "start_date": _format_litellm_time(start),
                    "end_date": _format_litellm_time(end),
                    "page": page,
                    "page_size": page_size,
                    "sort_by": "startTime",
                    "sort_order": "asc",
                },
            )
            response.raise_for_status()
            data = response.json()
            batch = _spend_log_rows(data)
            rows.extend(batch)
            total_pages = int(data.get("total_pages") or page) if isinstance(data, dict) else page
            if page >= total_pages or not batch:
                break
            page += 1
    return rows


def _spend_log_rows(data: Any) -> list[dict[str, Any]]:
    values = data.get("data") if isinstance(data, dict) else data
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


def _usage_from_spend_log(row: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata(row)
    response_metadata = {
        "model": row.get("model"),
        "provider": row.get("custom_llm_provider"),
        "spend": row.get("spend"),
        "request_id": row.get("request_id"),
        "usage_object": {
            "prompt_tokens": row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "total_tokens": row.get("total_tokens"),
        },
        **({"cost_breakdown": metadata.get("cost_breakdown")} if isinstance(metadata.get("cost_breakdown"), dict) else {}),
    }
    usage = {
        "prompt_tokens": row.get("prompt_tokens"),
        "completion_tokens": row.get("completion_tokens"),
        "total_tokens": row.get("total_tokens"),
        "cost_usd": row.get("spend"),
        "model": row.get("model") or row.get("model_group") or "",
        "provider": row.get("custom_llm_provider"),
        "request_id": row.get("request_id"),
        "metadata": {
            "litellm_model_group": row.get("model_group"),
            "litellm_model_id": row.get("model_id"),
            "litellm_status": row.get("status"),
            **metadata,
        },
    }
    return normalize_llm_usage_payload(usage, response_metadata=response_metadata)


def _find_existing_usage(
    rows: Iterable[LLMUsageEvent],
    *,
    request_id: str,
    usage: dict[str, Any],
) -> LLMUsageEvent | None:
    if request_id:
        for row in rows:
            if str((row.metadata_json or {}).get("litellm_request_id") or "") == request_id:
                return row
    total_tokens = int(usage.get("total_tokens") or 0)
    model = str(usage.get("model") or "")
    candidates = [
        row
        for row in rows
        if not (row.metadata_json or {}).get("litellm_request_id")
        and int(row.total_tokens or 0) == total_tokens
        and (not model or not row.model or row.model == model or model.endswith(row.model))
    ]
    return candidates[0] if len(candidates) == 1 else None


def _source_for_spend_log(row: dict[str, Any]) -> str:
    if _metadata_value(row, "a2a_grant_id"):
        return "litellm_subagent_reconciled"
    return "litellm_reconciled"


def _row_thread_id(row: dict[str, Any]) -> str | None:
    return _str_or_none(
        _metadata_value(row, "a2a_thread_id")
        or _metadata_value(row, "session_id")
        or row.get("session_id")
    )


def _row_user_id(row: dict[str, Any]) -> int | None:
    value = _metadata_value(row, "a2a_user_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _metadata_value(row: dict[str, Any], key: str) -> Any:
    metadata = _metadata(row)
    trace_metadata = metadata.get("trace_metadata")
    if key in metadata:
        return metadata.get(key)
    if isinstance(trace_metadata, dict):
        return trace_metadata.get(key)
    return None


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return dict(value) if isinstance(value, dict) else {}


def _format_litellm_time(value: datetime) -> str:
    return _naive_utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
