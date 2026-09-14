from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .minio_client import list_files
from .models import SubagentRun, SubagentRunEvent
from .work_ledger import fail_job, get_job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _grant_id(event: dict[str, Any]) -> str | None:
    gid = event.get("grant_id")
    if isinstance(gid, str) and gid:
        return gid
    handoff = event.get("handoff")
    if isinstance(handoff, dict):
        gid = handoff.get("grant_id")
        if isinstance(gid, str) and gid:
            return gid
    return None


def _handoff_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") == "approval_required":
        handoff = event.get("handoff")
        return handoff if isinstance(handoff, dict) else None
    if event.get("type") == "agent_handoff":
        return event
    if event.get("type") in {
        "agent_invoke_started",
        "agent_invoke_complete",
        "agent_invoke_error",
    }:
        return {
            "to": event.get("to") or event.get("agent"),
            "skill": event.get("skill"),
            "args_json": event.get("args_json"),
            "scopes": event.get("scopes"),
        }
    return None


_TASK_STATE_TO_RUN_STATUS = {
    "TASK_STATE_SUBMITTED": "running",
    "TASK_STATE_WORKING": "running",
    "TASK_STATE_INPUT_REQUIRED": "input_required",
    "TASK_STATE_AUTH_REQUIRED": "auth_required",
    "TASK_STATE_COMPLETED": "complete",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_CANCELLED": "canceled",
    "TASK_STATE_FAILED": "error",
    "TASK_STATE_REJECTED": "error",
}


def _normalize_task_state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.upper().replace("-", "_")
    if not normalized.startswith("TASK_STATE_"):
        normalized = f"TASK_STATE_{normalized}"
    return normalized


def _run_status_for_task_state(value: Any) -> str | None:
    state = _normalize_task_state(value)
    if state is None:
        return None
    return _TASK_STATE_TO_RUN_STATUS.get(state)


def _task_from(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    task = value.get("task")
    if isinstance(task, dict):
        return task
    status = value.get("status")
    if isinstance(status, dict) and (
        isinstance(value.get("id"), str)
        or isinstance(value.get("taskId"), str)
        or isinstance(status.get("state"), str)
    ):
        task_like = deepcopy(value)
        if "id" not in task_like and isinstance(task_like.get("taskId"), str):
            task_like["id"] = task_like["taskId"]
        return task_like
    result = value.get("result")
    if isinstance(result, dict):
        return _task_from(result)
    return None


def _extract_task(event: dict[str, Any]) -> dict[str, Any] | None:
    for value in (event, event.get("result"), event.get("payload")):
        task = _task_from(value)
        if task is not None:
            return task
    return None


def _message_text(message: Any) -> str | None:
    if not isinstance(message, dict):
        return None
    texts: list[str] = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    text = "\n".join(t for t in texts if t)
    return text or None


def _task_status_message(task: dict[str, Any] | None) -> str | None:
    if not isinstance(task, dict):
        return None
    status = task.get("status")
    if not isinstance(status, dict):
        return None
    return _message_text(status.get("message"))


def _artifact_from(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("artifact"), dict):
        return deepcopy(value["artifact"])
    if isinstance(value.get("artifactId"), str):
        return deepcopy(value)
    if any(k in value for k in ("uri", "url", "filename", "mediaType", "mime_type")):
        artifact: dict[str, Any] = {}
        if isinstance(value.get("name"), str):
            artifact["name"] = value["name"]
        if isinstance(value.get("path"), str):
            artifact["path"] = value["path"]
        if isinstance(value.get("uri"), str):
            artifact["uri"] = value["uri"]
        if isinstance(value.get("url"), str):
            artifact["url"] = value["url"]
        if isinstance(value.get("filename"), str):
            artifact["filename"] = value["filename"]
        media_type = value.get("mediaType") or value.get("mime_type")
        if isinstance(media_type, str):
            artifact["mediaType"] = media_type
        size = value.get("sizeBytes") or value.get("size_bytes")
        if isinstance(size, int):
            artifact["sizeBytes"] = size
        return artifact or None
    return None


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            key = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
        except TypeError:
            key = repr(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _extract_artifacts(event: dict[str, Any], task: dict[str, Any] | None) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    candidates: list[Any] = [
        event.get("artifacts"),
        event.get("artifact"),
    ]
    if isinstance(task, dict):
        candidates.append(task.get("artifacts"))
    for key in ("result", "payload"):
        value = event.get(key)
        if isinstance(value, dict):
            candidates.extend([
                value,
                value.get("artifacts"),
                value.get("artifact"),
            ])
            nested_task = _task_from(value)
            if isinstance(nested_task, dict):
                candidates.append(nested_task.get("artifacts"))
    for candidate in candidates:
        if isinstance(candidate, list):
            for item in candidate:
                artifact = _artifact_from(item)
                if artifact is not None:
                    artifacts.append(artifact)
        else:
            artifact = _artifact_from(candidate)
            if artifact is not None:
                artifacts.append(artifact)
    return _dedupe_dicts(artifacts)


def _append_message_parts(value: Any, out: list[dict[str, Any]]) -> None:
    if not isinstance(value, dict):
        return
    parts = value.get("parts")
    if isinstance(parts, list):
        out.extend(deepcopy(part) for part in parts if isinstance(part, dict))


def _extract_parts(event: dict[str, Any], task: dict[str, Any] | None) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if isinstance(task, dict):
        status = task.get("status")
        if isinstance(status, dict):
            _append_message_parts(status.get("message"), parts)
        for message in task.get("history") or []:
            _append_message_parts(message, parts)
        for artifact in task.get("artifacts") or []:
            _append_message_parts(artifact, parts)
    for container in (event, event.get("result"), event.get("payload")):
        if not isinstance(container, dict):
            continue
        _append_message_parts(container.get("message"), parts)
        _append_message_parts(container, parts)
        for artifact in container.get("artifacts") or []:
            _append_message_parts(artifact, parts)
        artifact = _artifact_from(container.get("artifact"))
        if artifact is not None:
            _append_message_parts(artifact, parts)
    return parts


def _part_is_file(part: dict[str, Any]) -> bool:
    if any(k in part for k in ("raw", "url", "filename", "file")):
        return True
    file_value = part.get("file")
    return isinstance(file_value, dict) and any(
        k in file_value for k in ("raw", "url", "filename", "mediaType")
    )


def _part_is_data(part: dict[str, Any]) -> bool:
    return "data" in part or (
        isinstance(part.get("metadata"), dict)
        and (
            "schema" in part["metadata"]
            or part["metadata"].get("mediaType") == "application/json"
        )
    )


def _enrich_a2a_event(event: dict[str, Any]) -> dict[str, Any]:
    task = _extract_task(event)
    artifacts = _extract_artifacts(event, task)
    parts = _extract_parts(event, task)
    a2a: dict[str, Any] = {}
    if task is not None:
        state = None
        status = task.get("status")
        if isinstance(status, dict):
            state = status.get("state")
        normalized_state = _normalize_task_state(state)
        if normalized_state is not None:
            a2a["task_state"] = normalized_state
            run_status = _run_status_for_task_state(normalized_state)
            if run_status is not None:
                a2a["task_status"] = run_status
        if isinstance(task.get("id"), str):
            a2a["task_id"] = task["id"]
        if isinstance(task.get("contextId"), str):
            a2a["context_id"] = task["contextId"]
        status_text = _task_status_message(task)
        if status_text:
            a2a["status_message"] = status_text
    if artifacts:
        a2a["artifacts"] = artifacts
    file_parts = _dedupe_dicts([deepcopy(part) for part in parts if _part_is_file(part)])
    data_parts = _dedupe_dicts([deepcopy(part) for part in parts if _part_is_data(part)])
    if file_parts:
        a2a["file_parts"] = file_parts
    if data_parts:
        a2a["data_parts"] = data_parts
    if not a2a:
        return event
    enriched = {**event}
    existing = enriched.get("a2a")
    if isinstance(existing, dict):
        a2a = {**existing, **a2a}
    enriched["a2a"] = a2a
    return enriched


def snapshot_files(bucket: str, prefix: str = "") -> dict[str, dict[str, Any]]:
    """Return a metadata-only object snapshot, optionally scoped to a prefix."""
    try:
        return {
            f["path"]: {
                "path": f["path"],
                "size": int(f.get("size") or 0),
                "modified_at": f.get("modified_at") or "",
                "content_type": f.get("content_type") or "application/octet-stream",
            }
            for f in list_files(bucket, prefix=prefix)
        }
    except Exception:  # noqa: BLE001
        return {}


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before_keys = set(before)
    after_keys = set(after)
    for path in sorted(after_keys - before_keys):
        item = after[path]
        out.append({
            "op": "create",
            "path": path,
            "size": int(item.get("size") or 0),
            "content_type": item.get("content_type") or "application/octet-stream",
        })
    for path in sorted(before_keys & after_keys):
        old = before[path]
        new = after[path]
        if (
            old.get("size") != new.get("size")
            or old.get("modified_at") != new.get("modified_at")
            or old.get("content_type") != new.get("content_type")
        ):
            out.append({
                "op": "update",
                "path": path,
                "size": int(new.get("size") or 0),
                "content_type": new.get("content_type") or "application/octet-stream",
            })
    for path in sorted(before_keys - after_keys):
        old = before[path]
        out.append({
            "op": "delete",
            "path": path,
            "size": int(old.get("size") or 0),
            "content_type": old.get("content_type") or "application/octet-stream",
        })
    return out


class SubagentRunRecorder:
    def __init__(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        thread_id: str | None,
        rerun_of_grant_id: str | None = None,
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.thread_id = thread_id
        self.rerun_of_grant_id = rerun_of_grant_id

    async def record(self, event: dict[str, Any]) -> dict[str, Any]:
        gid = _grant_id(event)
        if gid is None:
            return event
        etype = str(event.get("type") or "event")
        event = _enrich_a2a_event(event)
        run = await self._ensure_run(gid, event)
        if run is None:
            return event

        a2a = event.get("a2a")
        task_status = a2a.get("task_status") if isinstance(a2a, dict) else None
        status_message = a2a.get("status_message") if isinstance(a2a, dict) else None

        if etype in {"agent_handoff", "agent_invoke_started"}:
            run.status = "running"
        elif etype in {
            "agent_task_status",
            "agent_auth_required",
            "agent_input_required",
            "agent_progress",
            "agent_artifact",
        } and isinstance(task_status, str):
            run.status = task_status
            if task_status in {"auth_required", "input_required"} and status_message:
                run.summary = status_message
            if task_status in {"complete", "error", "canceled"}:
                run.completed_at = run.completed_at or _utcnow()
        elif etype in {"handoff_complete", "agent_invoke_complete", "agent_invoke_error"}:
            ok = bool(event.get("ok"))
            run.summary = str(event.get("summary") or "")
            if ok and isinstance(task_status, str):
                run.status = task_status
                if task_status in {"auth_required", "input_required", "running"}:
                    if status_message and not run.summary:
                        run.summary = status_message
                    run.completed_at = None
                else:
                    run.completed_at = _utcnow()
            else:
                run.status = "complete" if ok else "error"
                run.completed_at = _utcnow()
        elif etype == "handoff_denied":
            run.status = "denied"
            run.completed_at = _utcnow()

        run.updated_at = _utcnow()
        self.session.add(SubagentRunEvent(
            run_id=run.id,
            grant_id=run.grant_id,
            user_id=self.user_id,
            event_type=etype,
            payload=event,
        ))
        await self.session.commit()
        return event

    async def _ensure_run(
        self, grant_id: str, event: dict[str, Any]
    ) -> SubagentRun | None:
        run = (
            await self.session.execute(
                select(SubagentRun).where(
                    SubagentRun.grant_id == grant_id,
                    SubagentRun.user_id == self.user_id,
                )
            )
        ).scalar_one_or_none()
        handoff = _handoff_payload(event)
        if run is None and handoff is None:
            return None
        if run is None:
            run = SubagentRun(
                grant_id=grant_id,
                rerun_of_grant_id=self.rerun_of_grant_id,
                user_id=self.user_id,
                thread_id=self.thread_id,
                agent_name=str(handoff.get("to") or ""),
                skill_name=str(handoff.get("skill") or ""),
                args_json=str(handoff.get("args_json") or "{}"),
                scopes=handoff.get("scopes") if isinstance(handoff.get("scopes"), dict) else {},
                status=(
                    "proposed"
                    if event.get("type") == "approval_required"
                    else "running"
                ),
            )
            self.session.add(run)
            try:
                await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                run = (
                    await self.session.execute(
                        select(SubagentRun).where(SubagentRun.grant_id == grant_id)
                    )
                ).scalar_one_or_none()
            return run
        if handoff is not None:
            run.thread_id = run.thread_id or self.thread_id
            run.agent_name = str(handoff.get("to") or run.agent_name)
            run.skill_name = str(handoff.get("skill") or run.skill_name)
            run.args_json = str(handoff.get("args_json") or run.args_json or "{}")
            scopes = handoff.get("scopes")
            if isinstance(scopes, dict):
                run.scopes = scopes
        return run


ACTIVE_RUN_STATUSES = {
    "proposed",
    "queued",
    "waiting",
    "pending",
    "running",
    "auth_required",
    "input_required",
}


async def close_stale_subagent_runs(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
    limit: int = 100,
    status: str = "stale",
    reason: str = "subagent run lost its execution stream before a terminal event",
) -> int:
    cutoff = _utcnow() - timedelta(seconds=max(1, int(stale_after_seconds)))
    rows = (
        await session.execute(
            select(SubagentRun)
            .where(SubagentRun.completed_at.is_(None))
            .where(SubagentRun.status.in_(ACTIVE_RUN_STATUSES))
            .where(
                or_(
                    SubagentRun.updated_at.is_(None),
                    SubagentRun.updated_at <= cutoff,
                )
            )
            .order_by(SubagentRun.updated_at.asc(), SubagentRun.id.asc())
            .limit(max(1, int(limit)))
        )
    ).scalars().all()
    now = _utcnow()
    for run in rows:
        run.status = status
        run.summary = reason
        run.file_ops = []
        run.updated_at = now
        run.completed_at = now
        event = {
            "type": "agent_invoke_stale",
            "grant_id": run.grant_id,
            "to": run.agent_name,
            "agent": run.agent_name,
            "skill": run.skill_name,
            "args_json": run.args_json or "{}",
            "scopes": run.scopes or {},
            "ok": False,
            "summary": reason,
            "error": reason,
            "status": status,
            "file_ops": [],
        }
        session.add(
            SubagentRunEvent(
                run_id=run.id,
                grant_id=run.grant_id,
                user_id=run.user_id,
                event_type="agent_invoke_stale",
                payload=event,
            )
        )
        if run.user_id is not None:
            ledger_job = await get_job(session, run.grant_id, user_id=run.user_id)
            if ledger_job is not None:
                await fail_job(
                    session,
                    ledger_job,
                    error=reason,
                    result={"event": event},
                    summary=reason,
                    user_id=run.user_id,
                    status=status,
                    event_type="agent_invoke_stale",
                    commit=False,
                )
    if rows:
        await session.commit()
    return len(rows)
