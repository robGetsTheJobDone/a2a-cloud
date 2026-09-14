from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ..auth import _credential_token, current_user, current_user_or_agent_invoke
from ..config import settings
from ..control_room import (
    assert_monthly_budget_allows_start,
    get_or_create_policy,
    normalize_llm_usage_payload,
    policy_dict,
    record_llm_usage,
)
from ..db import SessionLocal, get_session
from ..models import AgentReceipt, GrantAudit, SubagentRun, SubagentRunEvent, User
from ..subagent_runs import SubagentRunRecorder
from .chat import _build_hooks

router = APIRouter(prefix="/v1/me/subagent-runs", tags=["subagent-runs"])
logger = logging.getLogger(__name__)
_TRACKING_SIDE_EFFECT_TASKS: set[asyncio.Task[None]] = set()
_TRACKING_SIDE_EFFECT_CHAINS: dict[str, asyncio.Task[None]] = {}


class _RerunPendingStore:
    async def wait(self, **_kwargs: Any) -> Any:
        from ..pending_actions import TIMEOUT

        return TIMEOUT


class FileOpOut(BaseModel):
    op: str
    path: str
    size: int
    content_type: str | None = None


class SubagentRunEventOut(BaseModel):
    id: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class SubagentHandoffOut(BaseModel):
    id: str
    from_agent: str | None = None
    to_agent: str
    skill: str
    grant_id: str
    status: str
    summary: str | None = None
    scopes: dict[str, Any] = Field(default_factory=dict)
    args_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    completed_at: datetime | None = None


class SubagentGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    grant_id: str
    parent_grant_id: str | None
    issuer: str
    audience: str
    bucket: str
    mode: str
    allow_patterns: list[str]
    deny_patterns: list[str]
    outputs_prefix: str | None
    ttl_seconds: int
    decision: str
    decided_by: str
    reason: str | None
    created_at: datetime


class SubagentReceiptOut(BaseModel):
    id: str
    kind: str
    label: str
    event_id: int | None = None
    receipt_id: str | None = None
    agent_name: str | None = None
    skill_name: str | None = None
    task_id: str | None = None
    status: str | None = None
    signed_token: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class SubagentRunOut(BaseModel):
    grant_id: str
    rerun_of_grant_id: str | None
    thread_id: str | None
    agent_name: str
    skill_name: str
    args_json: str
    scopes: dict[str, Any]
    status: str
    summary: str | None
    error: str | None
    file_ops: list[FileOpOut]
    file_ops_count: int = 0
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    original_request: dict[str, Any] | None = None
    handoffs: list[SubagentHandoffOut] = Field(default_factory=list)
    grants: list[SubagentGrantOut] = Field(default_factory=list)
    receipts: list[SubagentReceiptOut] = Field(default_factory=list)
    events: list[SubagentRunEventOut] = Field(default_factory=list)


class TrackEventOut(BaseModel):
    ok: bool
    grant_id: str | None


_ERROR_STATUSES = {"error", "failed", "canceled", "denied", "stale"}
_ERROR_EVENT_TYPES = {
    "agent_invoke_error",
    "handoff_denied",
}


def _payload_text(payload: dict[str, Any]) -> str | None:
    for key in ("error", "message", "summary", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    a2a = payload.get("a2a")
    if isinstance(a2a, dict):
        for key in ("status_message", "task_status", "task_state"):
            value = a2a.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _grant_id_from_event(event: dict[str, Any]) -> str | None:
    grant_id = event.get("grant_id")
    if isinstance(grant_id, str) and grant_id:
        return grant_id
    handoff = event.get("handoff")
    if isinstance(handoff, dict):
        grant_id = handoff.get("grant_id")
        if isinstance(grant_id, str) and grant_id:
            return grant_id
    return None


def _ledger_helper(*names: str) -> Callable[..., Any] | None:
    try:
        module = import_module("control_plane.work_ledger")
    except ModuleNotFoundError as exc:
        if exc.name == "control_plane.work_ledger":
            return None
        raise
    for name in names:
        helper = getattr(module, name, None)
        if callable(helper):
            return helper
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


async def _call_ledger_helper(
    fn: Callable[..., Any],
    values: dict[str, Any],
) -> Any:
    result = fn(**_filtered_kwargs(fn, values))
    if inspect.isawaitable(result):
        return await result
    return result


def _set_if_present(row: Any, name: str, value: Any) -> None:
    if hasattr(row, name):
        setattr(row, name, value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def _dict_from_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _usage_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return normalize_llm_usage_payload(
        value,
        response_metadata=value.get("response_metadata")
        if isinstance(value.get("response_metadata"), dict)
        else None,
    )


def _subagent_llm_usages(event: dict[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("llm_usage")
    if raw is None:
        raw = event.get("usage")
    values = raw if isinstance(raw, list) else [raw]
    return [usage for value in values if (usage := _usage_dict(value)) is not None]


async def _record_subagent_llm_usage(
    event: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
) -> None:
    usages = _subagent_llm_usages(event)
    if not usages:
        return
    grant_id = _grant_id_from_event(event)
    if grant_id is None:
        return
    run = (
        await session.execute(
            select(SubagentRun).where(
                SubagentRun.grant_id == grant_id,
                SubagentRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return

    for usage in usages:
        metadata = dict(usage.get("metadata") or {})
        metadata.update(
            {
                "subagent_event_type": str(event.get("type") or "event"),
                "tracking_source": event.get("source"),
                "elapsed_ms": event.get("elapsed_ms"),
                "usage_projection": "litellm_response",
            }
        )
        usage["metadata"] = metadata
        await record_llm_usage(
            session,
            user_id=user.id,
            thread_id=run.thread_id,
            source="litellm_subagent",
            usage=usage,
            grant_id=run.grant_id,
            agent_name=run.agent_name,
            skill_name=run.skill_name,
        )


def _merge_metadata(row: Any, values: dict[str, Any]) -> None:
    if not hasattr(row, "metadata_json"):
        return
    existing = _dict_from_jsonish(getattr(row, "metadata_json"))
    existing.update({key: value for key, value in values.items() if value is not None})
    setattr(row, "metadata_json", existing)


def _ledger_title(run: SubagentRun) -> str:
    if run.agent_name and run.skill_name:
        return f"{run.agent_name}.{run.skill_name}"[:255]
    if run.agent_name:
        return run.agent_name[:255]
    return "Subagent run"


def _ledger_job_payload(run: SubagentRun) -> dict[str, Any]:
    return {
        "grant_id": run.grant_id,
        "rerun_of_grant_id": run.rerun_of_grant_id,
        "agent_name": run.agent_name,
        "skill_name": run.skill_name,
        "args_json": run.args_json or "{}",
        "scopes": run.scopes or {},
    }


def _ledger_metadata(run: SubagentRun) -> dict[str, Any]:
    return {
        "grant_id": run.grant_id,
        "rerun_of_grant_id": run.rerun_of_grant_id,
        "agent_name": run.agent_name,
        "skill_name": run.skill_name,
        "source": "subagent_runs",
    }


def _ledger_event_stage(event_type: str, status: str) -> str:
    if event_type in {"agent_handoff", "agent_invoke_started"}:
        return "started"
    if status == "complete":
        return "completed"
    if status in _ERROR_STATUSES or status == "denied":
        return "failed"
    return "progress"


def _ledger_event_severity(event: dict[str, Any], status: str) -> str:
    if event.get("ok") is False or status in _ERROR_STATUSES or status == "denied":
        return "error"
    return "info"


def _ledger_message(event: dict[str, Any], run: SubagentRun) -> str:
    return (
        _payload_text(event)
        or run.summary
        or f"{run.agent_name}.{run.skill_name} {run.status}".strip()
    )


def _sync_ledger_job(job: Any, run: SubagentRun, event: dict[str, Any]) -> None:
    _set_if_present(job, "status", run.status)
    _set_if_present(job, "thread_id", run.thread_id)
    _set_if_present(job, "worker_name", run.agent_name)
    _set_if_present(job, "updated_at", run.updated_at)
    _set_if_present(job, "completed_at", run.completed_at)
    if run.summary is not None:
        _set_if_present(job, "summary", run.summary)
    if getattr(job, "started_at", None) is None:
        _set_if_present(job, "started_at", run.created_at)

    if run.status == "complete":
        _set_if_present(job, "error", None)
        _set_if_present(job, "error_payload", {})
        result = event.get("result")
        _set_if_present(
            job,
            "output_payload",
            _json_object(result) if result is not None else {"event": event},
        )
    elif run.status in _ERROR_STATUSES or run.status == "denied":
        error = _payload_text(event) or run.summary or run.status
        _set_if_present(job, "error", error)
        _set_if_present(job, "error_payload", {"error": error, "event": event})

    metadata = _ledger_metadata(run)
    if run.summary:
        metadata["summary"] = run.summary
    _merge_metadata(job, metadata)


async def _mirror_tracked_event_to_ledger(
    event: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
) -> None:
    get_job = _ledger_helper("get_job", "get_work_job", "get_work_ledger_job")
    create_job = _ledger_helper(
        "create_job", "create_work_job", "create_work_ledger_job"
    )
    append_event = _ledger_helper("append_event", "append_work_event")
    if get_job is None or create_job is None or append_event is None:
        return

    grant_id = _grant_id_from_event(event)
    if grant_id is None:
        return
    run = (
        await session.execute(
            select(SubagentRun).where(
                SubagentRun.grant_id == grant_id,
                SubagentRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return

    job = await _call_ledger_helper(
        get_job,
        {
            "session": session,
            "db": session,
            "job_id": run.grant_id,
            "user_id": user.id,
            "owner_id": user.id,
        },
    )
    if job is None:
        job = await _call_ledger_helper(
            create_job,
            {
                "session": session,
                "db": session,
                "user_id": user.id,
                "owner_id": user.id,
                "kind": "subagent_run",
                "payload": _ledger_job_payload(run),
                "thread_id": run.thread_id,
                "title": _ledger_title(run),
                "metadata": _ledger_metadata(run),
                "job_id": run.grant_id,
                "status": run.status,
                "queue": "subagents",
                "parent_job_id": run.rerun_of_grant_id,
                "correlation_id": run.grant_id,
                "source_type": "subagent_run",
                "source_id": run.grant_id,
                "subject_type": "subagent_run",
                "subject_id": run.grant_id,
                "worker_type": "subagent",
                "worker_name": run.agent_name,
                "record_event": False,
                "commit": False,
            },
        )
    if job is None:
        return

    event_type = str(event.get("type") or "event")
    _sync_ledger_job(job, run, event)
    session.add(job)
    await _call_ledger_helper(
        append_event,
        {
            "session": session,
            "db": session,
            "job": job,
            "event_type": event_type,
            "payload": event,
            "message": _ledger_message(event, run),
            "status": run.status,
            "user_id": user.id,
            "owner_id": user.id,
            "metadata": _ledger_metadata(run),
            "stage": _ledger_event_stage(event_type, run.status),
            "severity": _ledger_event_severity(event, run.status),
            "actor_type": "subagent",
            "actor_id": run.agent_name,
            "source_type": "subagent_run",
            "source_id": run.grant_id,
            "commit": False,
        },
    )
    await session.commit()


async def _record_and_mirror_subagent_event(
    recorder: SubagentRunRecorder,
    event: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    tracked = await recorder.record(event)
    await _apply_tracked_event_side_effects(tracked, user=user, session=session)
    return tracked


async def _apply_tracked_event_side_effects(
    tracked: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
) -> None:
    try:
        await _mirror_tracked_event_to_ledger(tracked, user=user, session=session)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.debug("failed to mirror subagent run to work ledger", exc_info=True)
    try:
        await _record_subagent_llm_usage(tracked, user=user, session=session)
    except Exception:  # noqa: BLE001
        await session.rollback()
        logger.debug("failed to record subagent LLM usage", exc_info=True)


async def _apply_tracked_event_side_effects_for_user(
    tracked: dict[str, Any],
    user_id: int,
) -> None:
    try:
        async with SessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None:
                return
            await _apply_tracked_event_side_effects(
                tracked,
                user=user,
                session=session,
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "failed to run deferred subagent tracking side effects", exc_info=True
        )


def _schedule_tracked_event_side_effects(
    tracked: dict[str, Any],
    user_id: int,
) -> None:
    grant_id = _grant_id_from_event(tracked)
    if grant_id is None:
        return
    previous = _TRACKING_SIDE_EFFECT_CHAINS.get(grant_id)

    async def run_after_previous() -> None:
        if previous is not None:
            try:
                await previous
            except Exception:  # noqa: BLE001
                pass
        await _apply_tracked_event_side_effects_for_user(tracked, user_id)

    task = asyncio.create_task(run_after_previous())
    _TRACKING_SIDE_EFFECT_TASKS.add(task)
    _TRACKING_SIDE_EFFECT_CHAINS[grant_id] = task

    def cleanup(done: asyncio.Task[None]) -> None:
        _TRACKING_SIDE_EFFECT_TASKS.discard(done)
        if _TRACKING_SIDE_EFFECT_CHAINS.get(grant_id) is done:
            _TRACKING_SIDE_EFFECT_CHAINS.pop(grant_id, None)

    task.add_done_callback(cleanup)


def _event_error(event: SubagentRunEvent) -> str | None:
    payload = event.payload or {}
    if event.event_type in _ERROR_EVENT_TYPES:
        return _payload_text(payload) or event.event_type
    if payload.get("ok") is False:
        return _payload_text(payload) or "agent returned an error"
    a2a = payload.get("a2a")
    if isinstance(a2a, dict) and a2a.get("task_status") in _ERROR_STATUSES:
        return _payload_text(payload) or str(a2a["task_status"])
    return None


def _run_error(
    run: SubagentRun,
    events: list[SubagentRunEvent] | None = None,
) -> str | None:
    for event in reversed(events or []):
        err = _event_error(event)
        if err:
            return err
    if run.status in _ERROR_STATUSES:
        return (
            run.summary or f"{run.agent_name}.{run.skill_name} ended with {run.status}"
        )
    return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _run_original_request(run: SubagentRun) -> dict[str, Any]:
    return {
        "agent_name": run.agent_name,
        "skill_name": run.skill_name,
        "grant_id": run.grant_id,
        "args": _dict_from_jsonish(run.args_json),
        "scopes": run.scopes or {},
    }


def _handoff_source(payload: dict[str, Any]) -> dict[str, Any]:
    handoff = payload.get("handoff")
    return handoff if isinstance(handoff, dict) else payload


def _handoff_agent(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _handoffs_from_events(
    run: SubagentRun,
    events: list[SubagentRunEvent],
) -> list[SubagentHandoffOut]:
    by_grant: dict[str, SubagentHandoffOut] = {}
    order: list[str] = []

    def ensure(grant_id: str) -> SubagentHandoffOut:
        existing = by_grant.get(grant_id)
        if existing is not None:
            return existing
        item = SubagentHandoffOut(
            id=grant_id,
            from_agent=None,
            to_agent=run.agent_name,
            skill=run.skill_name,
            grant_id=grant_id,
            status=run.status,
            summary=run.summary,
            scopes=run.scopes or {},
            args_preview=_dict_from_jsonish(run.args_json),
            created_at=run.created_at,
            completed_at=run.completed_at,
        )
        by_grant[grant_id] = item
        order.append(grant_id)
        return item

    for event in events:
        payload = event.payload or {}
        source = _handoff_source(payload)
        grant_id = _grant_id_from_event(payload) or run.grant_id
        event_type = event.event_type or str(payload.get("type") or "")
        if event_type in {"agent_handoff", "approval_required", "agent_invoke_started"}:
            item = ensure(grant_id)
            item.from_agent = _handoff_agent(source, "from", "from_agent", "caller")
            item.to_agent = (
                _handoff_agent(source, "to", "agent", "to_agent") or item.to_agent
            )
            item.skill = _handoff_agent(source, "skill", "skill_name") or item.skill
            item.scopes = _dict_or_empty(source.get("scopes")) or item.scopes
            item.args_preview = (
                _dict_or_empty(source.get("args_preview"))
                or _dict_from_jsonish(source.get("args_json"))
                or item.args_preview
            )
            item.status = (
                "approval_required" if event_type == "approval_required" else "running"
            )
            item.created_at = item.created_at or event.created_at
            item.summary = _payload_text(payload) or item.summary
        elif event_type in {
            "handoff_complete",
            "agent_invoke_complete",
            "dag_node_complete",
        }:
            item = ensure(grant_id)
            item.status = (
                "complete" if payload.get("ok", True) is not False else "error"
            )
            item.summary = _payload_text(payload) or item.summary
            item.completed_at = event.created_at
        elif event_type in {
            "handoff_denied",
            "agent_invoke_error",
            "agent_invoke_stale",
        }:
            item = ensure(grant_id)
            item.status = "denied" if event_type == "handoff_denied" else "error"
            item.summary = _payload_text(payload) or item.summary
            item.completed_at = event.created_at

    if not by_grant:
        ensure(run.grant_id)
    return [by_grant[grant_id] for grant_id in order]


async def _grant_chain_for_run(
    run: SubagentRun,
    *,
    user: User,
    session: AsyncSession,
) -> list[SubagentGrantOut]:
    out: dict[str, GrantAudit] = {}
    frontier = {run.grant_id}
    if run.rerun_of_grant_id:
        frontier.add(run.rerun_of_grant_id)
    visited: set[str] = set()

    while frontier:
        visited.update(frontier)
        rows = (
            (
                await session.execute(
                    select(GrantAudit).where(
                        GrantAudit.user_id == user.id,
                        GrantAudit.grant_id.in_(frontier),
                    )
                )
            )
            .scalars()
            .all()
        )
        children = (
            (
                await session.execute(
                    select(GrantAudit).where(
                        GrantAudit.user_id == user.id,
                        GrantAudit.parent_grant_id.in_(frontier),
                    )
                )
            )
            .scalars()
            .all()
        )
        next_frontier: set[str] = set()
        for row in [*rows, *children]:
            if row.grant_id not in out:
                out[row.grant_id] = row
            if row.grant_id not in visited:
                next_frontier.add(row.grant_id)
        frontier = next_frontier - visited

    return [
        SubagentGrantOut.model_validate(row)
        for row in sorted(out.values(), key=lambda item: item.id)
    ]


def _receipt_payload_without_token(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out.pop("token", None)
    out.pop("signed_token", None)
    return out


def _receipt_from_event(event: SubagentRunEvent) -> SubagentReceiptOut | None:
    payload = event.payload or {}
    kind = _string_or_none(payload.get("kind"))
    body = payload.get("payload")
    body_dict = body if isinstance(body, dict) else payload
    if kind not in {"receipt_sealed", "replay_sealed"}:
        return None
    token = _string_or_none(body_dict.get("token")) or _string_or_none(
        body_dict.get("signed_token")
    )
    normalized_kind = "replay" if kind.startswith("replay") else "receipt"
    receipt_id = _string_or_none(body_dict.get("receipt_id"))
    session_id = _string_or_none(body_dict.get("session_id"))
    artifact_id = receipt_id or session_id or f"{normalized_kind}:{event.id}"
    return SubagentReceiptOut(
        id=artifact_id,
        kind=normalized_kind,
        label="Replay session" if normalized_kind == "replay" else "Execution receipt",
        event_id=event.id,
        receipt_id=receipt_id,
        agent_name=_string_or_none(body_dict.get("agent_name")),
        skill_name=_string_or_none(body_dict.get("skill_name")),
        task_id=_string_or_none(body_dict.get("task_id")),
        status=_string_or_none(body_dict.get("status")),
        signed_token=token,
        payload=_receipt_payload_without_token(body_dict),
        created_at=event.created_at,
    )


def _task_ids_from_events(events: list[SubagentRunEvent]) -> set[str]:
    task_ids: set[str] = set()
    for event in events:
        payload = event.payload or {}
        for value in (payload.get("task_id"), payload.get("taskId")):
            if isinstance(value, str) and value.strip():
                task_ids.add(value.strip())
        a2a = payload.get("a2a")
        if isinstance(a2a, dict):
            value = a2a.get("task_id") or a2a.get("taskId")
            if isinstance(value, str) and value.strip():
                task_ids.add(value.strip())
    return task_ids


def _receipt_from_row(row: AgentReceipt) -> SubagentReceiptOut:
    return SubagentReceiptOut(
        id=row.receipt_id,
        kind="receipt",
        label="Persisted execution receipt",
        receipt_id=row.receipt_id,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        task_id=row.task_id,
        status=row.status,
        signed_token=row.signed_token,
        payload=row.payload or {},
        created_at=row.created_at,
    )


async def _receipt_artifacts_for_run(
    run: SubagentRun,
    events: list[SubagentRunEvent],
    *,
    user: User,
    session: AsyncSession,
) -> list[SubagentReceiptOut]:
    artifacts = [
        artifact
        for event in events
        if (artifact := _receipt_from_event(event)) is not None
    ]

    task_ids = _task_ids_from_events(events)
    if task_ids:
        rows = (
            (
                await session.execute(
                    select(AgentReceipt)
                    .where(
                        AgentReceipt.caller == f"user:{user.id}",
                        AgentReceipt.agent_name == run.agent_name,
                        AgentReceipt.skill_name == run.skill_name,
                        AgentReceipt.task_id.in_(task_ids),
                    )
                    .order_by(AgentReceipt.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        artifacts.extend(_receipt_from_row(row) for row in rows)

    seen: set[str] = set()
    unique: list[SubagentReceiptOut] = []
    for artifact in artifacts:
        key = artifact.receipt_id or artifact.signed_token or artifact.id
        if key in seen:
            continue
        seen.add(key)
        unique.append(artifact)
    return unique


def _run_out(
    run: SubagentRun,
    events: list[SubagentRunEvent] | None = None,
    include_file_ops: bool = True,
    file_ops_count: int | None = None,
    original_request: dict[str, Any] | None = None,
    handoffs: list[SubagentHandoffOut] | None = None,
    grants: list[SubagentGrantOut] | None = None,
    receipts: list[SubagentReceiptOut] | None = None,
) -> SubagentRunOut:
    event_rows = events or []
    file_ops = (run.file_ops or []) if include_file_ops else []
    return SubagentRunOut(
        grant_id=run.grant_id,
        rerun_of_grant_id=run.rerun_of_grant_id,
        thread_id=run.thread_id,
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        args_json=run.args_json or "{}",
        scopes=run.scopes or {},
        status=run.status,
        summary=run.summary,
        error=_run_error(run, event_rows),
        file_ops=[FileOpOut(**op) for op in file_ops] if include_file_ops else [],
        file_ops_count=len(file_ops) if file_ops_count is None else file_ops_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        original_request=original_request,
        handoffs=handoffs or [],
        grants=grants or [],
        receipts=receipts or [],
        events=[SubagentRunEventOut.model_validate(e) for e in event_rows],
    )


async def _rich_run_out(
    run: SubagentRun,
    events: list[SubagentRunEvent],
    *,
    user: User,
    session: AsyncSession,
) -> SubagentRunOut:
    grants = await _grant_chain_for_run(run, user=user, session=session)
    receipts = await _receipt_artifacts_for_run(run, events, user=user, session=session)
    return _run_out(
        run,
        events,
        original_request=_run_original_request(run),
        handoffs=_handoffs_from_events(run, events),
        grants=grants,
        receipts=receipts,
    )


def _rerun_error_message(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("error", "message", "detail", "summary", "reason"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:1000]
        result = value.get("result")
        if isinstance(result, dict):
            for key in ("error", "message", "detail", "summary", "reason"):
                item = result.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()[:1000]
    return "rerun did not produce a grant_id"


async def _record_rerun_error(
    original: SubagentRun,
    *,
    error: Any,
    recorder: SubagentRunRecorder,
    user: User,
    session: AsyncSession,
) -> SubagentRunOut:
    message = _rerun_error_message(error)
    grant_id = f"rerun-{secrets.token_hex(8)}"
    event = {
        "type": "agent_invoke_error",
        "grant_id": grant_id,
        "to": original.agent_name,
        "agent": original.agent_name,
        "skill": original.skill_name,
        "args_json": original.args_json or "{}",
        "scopes": original.scopes or {},
        "ok": False,
        "summary": message,
        "error": message,
        "result": _json_object(error),
        "rerun_of_grant_id": original.grant_id,
    }
    await _record_and_mirror_subagent_event(
        recorder,
        event,
        user=user,
        session=session,
    )
    return await get_subagent_run(grant_id, user=user, session=session)


@router.get("", response_model=list[SubagentRunOut])
async def list_subagent_runs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    agent: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SubagentRunOut]:
    file_ops_count = func.coalesce(func.json_array_length(SubagentRun.file_ops), 0)
    stmt = (
        select(SubagentRun, file_ops_count)
        .options(defer(SubagentRun.file_ops), defer(SubagentRun.start_files))
        .where(SubagentRun.user_id == user.id)
        .order_by(desc(SubagentRun.id))
        .limit(limit)
    )
    if agent:
        stmt = stmt.where(SubagentRun.agent_name == agent)
    if thread_id:
        stmt = stmt.where(SubagentRun.thread_id == thread_id)
    rows = (await session.execute(stmt)).all()
    return [
        _run_out(run, include_file_ops=False, file_ops_count=count)
        for run, count in rows
    ]


async def track_subagent_event(
    event: dict[str, Any],
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> TrackEventOut:
    return await _track_subagent_event(
        event,
        user=user,
        session=session,
        defer_side_effects=False,
    )


@router.post("/track", response_model=TrackEventOut, name="track_subagent_event")
async def track_subagent_event_route(
    event: dict[str, Any],
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> TrackEventOut:
    return await _track_subagent_event(
        event,
        user=user,
        session=session,
        defer_side_effects=True,
    )


async def _track_subagent_event(
    event: dict[str, Any],
    *,
    user: User,
    session: AsyncSession,
    defer_side_effects: bool,
) -> TrackEventOut:
    """Runtime callback used by agent pods to record direct /invoke calls.

    Chat handoffs are already tracked in-process. This endpoint covers the
    complementary path: any deployed agent invoked directly, or nested through
    ``ctx.call()``, can report start/finish when the caller's CP JWT is
    forwarded in the invoke body.
    """
    recorder = SubagentRunRecorder(
        session=session,
        user_id=user.id,
        thread_id=event.get("thread_id")
        if isinstance(event.get("thread_id"), str)
        else None,
    )
    if defer_side_effects:
        tracked = await recorder.record(event)
        _schedule_tracked_event_side_effects(tracked, user.id)
    else:
        tracked = await _record_and_mirror_subagent_event(
            recorder,
            event,
            user=user,
            session=session,
        )
    grant_id = tracked.get("grant_id")
    if not isinstance(grant_id, str):
        grant_id = None
    return TrackEventOut(ok=True, grant_id=grant_id)


@router.get("/{grant_id}", response_model=SubagentRunOut)
async def get_subagent_run(
    grant_id: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> SubagentRunOut:
    run = await _get_run(grant_id, user, session)
    events = (
        (
            await session.execute(
                select(SubagentRunEvent)
                .where(
                    SubagentRunEvent.run_id == run.id,
                    SubagentRunEvent.user_id == user.id,
                )
                .order_by(SubagentRunEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return await _rich_run_out(run, events, user=user, session=session)


@router.post("/{grant_id}/rerun", response_model=SubagentRunOut)
async def rerun_subagent_run(
    grant_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(
        default=None,
        alias=settings.session_cookie_name,
    ),
) -> SubagentRunOut:
    run = await _get_run(grant_id, user, session)
    if run.status in {"auth_required", "input_required"}:
        raise HTTPException(
            409,
            "rerun is not available while a run is waiting for auth or input",
        )
    interactive_count = (
        await session.execute(
            select(func.count(SubagentRunEvent.id)).where(
                SubagentRunEvent.run_id == run.id,
                SubagentRunEvent.event_type.in_(
                    [
                        "agent_input_request",
                        "agent_question",
                        "scope_approval_required",
                        "agent_auth_required",
                        "agent_input_required",
                    ]
                ),
            )
        )
    ).scalar_one()
    if interactive_count:
        raise HTTPException(
            409,
            "rerun is not available for runs that required interactive input",
        )

    try:
        args = json.loads(run.args_json or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"stored args_json is invalid: {exc}") from exc
    if not isinstance(args, dict):
        raise HTTPException(400, "stored args_json must decode to an object")

    from main_agent import OrchestratorContext
    from main_agent.tools.handoff import build_handoff_tools

    policy = await get_or_create_policy(session, user.id)
    await assert_monthly_budget_allows_start(session, user.id, policy)

    jwt = _credential_token(authorization, session_cookie)

    recorder = SubagentRunRecorder(
        session=session,
        user_id=user.id,
        thread_id=run.thread_id,
        rerun_of_grant_id=run.grant_id,
    )
    lock = asyncio.Lock()

    async def emit(event: dict[str, Any]) -> None:
        async with lock:
            try:
                await _record_and_mirror_subagent_event(
                    recorder,
                    event,
                    user=user,
                    session=session,
                )
            except Exception:  # noqa: BLE001
                await session.rollback()

    hooks = _build_hooks(
        emit=emit,
        approval_mode=False,
        session=session,
        user=user,
        pending_store=_RerunPendingStore(),
        db_lock=lock,
        interactive=False,
    )
    ctx = OrchestratorContext.for_user(
        user_id=user.id,
        jwt=jwt,
        hooks=hooks,
        policy_controls=policy_dict(policy),
    )
    tools = build_handoff_tools(ctx)
    call_agent = next(
        (t for t in tools if getattr(t, "name", "") == "call_agent"), None
    )
    if call_agent is None:
        raise HTTPException(500, "call_agent tool unavailable")
    try:
        raw = await asyncio.wait_for(
            call_agent.ainvoke(
                {
                    "name": run.agent_name,
                    "skill": run.skill_name,
                    "args_json": json.dumps(
                        args, separators=(",", ":"), ensure_ascii=False
                    ),
                }
            ),
            timeout=1900,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "rerun timed out") from exc
    except Exception as exc:  # noqa: BLE001
        return await _record_rerun_error(
            run,
            error={"error": f"rerun failed: {type(exc).__name__}: {exc}"},
            recorder=recorder,
            user=user,
            session=session,
        )

    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        return await _record_rerun_error(
            run,
            error={"error": f"rerun returned invalid JSON: {exc}", "raw": raw},
            recorder=recorder,
            user=user,
            session=session,
        )
    new_grant = parsed.get("grant_id") if isinstance(parsed, dict) else None
    if not isinstance(new_grant, str) or not new_grant:
        return await _record_rerun_error(
            run,
            error=parsed,
            recorder=recorder,
            user=user,
            session=session,
        )
    return await get_subagent_run(new_grant, user=user, session=session)


async def _get_run(grant_id: str, user: User, session: AsyncSession) -> SubagentRun:
    run = (
        await session.execute(
            select(SubagentRun).where(
                SubagentRun.grant_id == grant_id,
                SubagentRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "subagent run not found")
    return run
