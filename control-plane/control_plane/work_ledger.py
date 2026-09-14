from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Mapping, TypeAlias, TypedDict
from uuid import uuid4

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

JsonObject: TypeAlias = dict[str, Any]
ModelInstance: TypeAlias = Any
ModelClass: TypeAlias = type[Any]


class SerializedWorkJob(TypedDict, total=False):
    id: int
    job_id: str
    user_id: int | None
    thread_id: str | None
    kind: str | None
    status: str | None
    title: str | None
    summary: str | None
    error: str | None
    payload: Any
    result: Any
    error_payload: Any
    metadata: JsonObject
    created_at: str | None
    updated_at: str | None
    started_at: str | None
    completed_at: str | None


class SerializedWorkEvent(TypedDict, total=False):
    id: int
    job_id: str | int | None
    user_id: int | None
    event_type: str | None
    event_id: str | None
    event_seq: int | None
    status: str | None
    stage: str | None
    severity: str | None
    message: str | None
    payload: Any
    created_at: str | None


class SerializedIdempotencyRecord(TypedDict, total=False):
    id: int
    key: str | None
    user_id: int | None
    request_hash: str | None
    job_id: str | int | None
    response: Any
    created_at: str | None
    updated_at: str | None


class WorkLedgerModelsUnavailable(RuntimeError):
    """Raised when the durable ledger models are not available yet."""


class WorkJobNotFound(LookupError):
    """Raised when a requested work job cannot be found."""


class IdempotencyConflict(RuntimeError):
    """Raised when an idempotency key is reused with a different request."""


_CANCELED_STATUSES = {"canceled", "cancelled"}
_TERMINAL_STATUSES = _CANCELED_STATUSES | {
    "complete",
    "completed",
    "success",
    "succeeded",
    "ok",
    "error",
    "failed",
    "failure",
    "blocked",
    "denied",
    "stale",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _model(name: str) -> ModelClass:
    models = import_module(".models", package=__package__)
    model = getattr(models, name, None)
    if model is None:
        raise WorkLedgerModelsUnavailable(
            f"control_plane.models.{name} is not available; "
            "the durable work ledger models must be defined before using work_ledger"
        )
    return model


def _column_names(model: ModelClass) -> set[str]:
    table = getattr(model, "__table__", None)
    if table is not None:
        return {str(column.key) for column in table.columns}
    annotations = getattr(model, "__annotations__", {})
    return set(annotations) if isinstance(annotations, dict) else set()


def _has_column(model: ModelClass, name: str) -> bool:
    return name in _column_names(model) and hasattr(model, name)


def _first_column(model: ModelClass, names: tuple[str, ...]) -> str | None:
    columns = _column_names(model)
    return next((name for name in names if name in columns and hasattr(model, name)), None)


def _column_python_type(model: ModelClass, name: str) -> type[Any] | None:
    table = getattr(model, "__table__", None)
    if table is None or name not in table.columns:
        return None
    try:
        return table.columns[name].type.python_type
    except (NotImplementedError, TypeError):
        return None


def _coerce_for_column(model: ModelClass, name: str, value: Any) -> Any:
    py_type = _column_python_type(model, name)
    if py_type is str and isinstance(value, (dict, list, tuple)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if py_type is int and value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


def _model_kwargs(model: ModelClass, values: Mapping[str, Any]) -> JsonObject:
    columns = _column_names(model)
    return {
        name: _coerce_for_column(model, name, value)
        for name, value in values.items()
        if name in columns
    }


def _set_first(row: ModelInstance, names: tuple[str, ...], value: Any) -> bool:
    model = type(row)
    for name in names:
        if _has_column(model, name):
            setattr(row, name, _coerce_for_column(model, name, value))
            return True
    return False


def _merge_first_mapping(
    row: ModelInstance,
    names: tuple[str, ...],
    values: Mapping[str, Any],
) -> None:
    existing = _value(row, names)
    if isinstance(existing, str):
        existing = _jsonish(existing)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update({key: value for key, value in values.items() if value is not None})
    _set_first(row, names, merged)


def _value(row: ModelInstance, names: tuple[str, ...]) -> Any:
    for name in names:
        if hasattr(row, name):
            value = getattr(row, name)
            if value is not None:
                return value
    return None


def _public_job_id(job: ModelInstance) -> str:
    value = _value(job, ("job_id", "work_id", "external_id", "id"))
    return str(value) if value is not None else ""


def _is_int_like(value: Any) -> bool:
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return value.isdecimal()
    return False


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    return value


def _stable_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _status(row: ModelInstance) -> str:
    return str(_value(row, ("status",)) or "").strip().lower()


def _is_canceled(row: ModelInstance) -> bool:
    return _status(row) in _CANCELED_STATUSES


def _is_terminal(row: ModelInstance) -> bool:
    return _status(row) in _TERMINAL_STATUSES


def serialize_record(row: ModelInstance) -> JsonObject:
    """Serialize any SQLAlchemy model row into JSON-safe column values."""

    data: JsonObject = {}
    for name in sorted(_column_names(type(row))):
        if hasattr(row, name):
            data[name] = _serialize_value(_jsonish(getattr(row, name)))
    return data


def serialize_job(job: ModelInstance) -> SerializedWorkJob:
    data = serialize_record(job)
    out: SerializedWorkJob = {}
    for key in (
        "id",
        "job_id",
        "user_id",
        "thread_id",
        "root_job_id",
        "parent_job_id",
        "correlation_id",
        "queue",
        "priority",
        "status",
        "title",
        "summary",
        "error",
        "created_at",
        "updated_at",
        "queued_at",
        "started_at",
        "heartbeat_at",
        "leased_until",
        "completed_at",
    ):
        if key in data:
            out[key] = data[key]  # type: ignore[literal-required]
    out["job_id"] = _public_job_id(job)
    kind = _value(job, ("kind", "job_type", "type"))
    if kind is not None:
        out["kind"] = str(kind)
    payload = _value(
        job,
        ("input_payload", "payload", "payload_json", "input", "input_json", "request"),
    )
    if payload is not None:
        out["payload"] = _serialize_value(_jsonish(payload))
    result = _value(
        job,
        ("output_payload", "result", "result_json", "response", "response_json"),
    )
    if result is not None:
        out["result"] = _serialize_value(_jsonish(result))
    error_payload = _value(job, ("error_payload",))
    if error_payload is not None:
        out["error_payload"] = _serialize_value(_jsonish(error_payload))
    metadata = _value(job, ("metadata_json", "meta", "data"))
    if isinstance(metadata, str):
        metadata = _jsonish(metadata)
    if isinstance(metadata, dict):
        out["metadata"] = _serialize_value(metadata)
        for key in ("title", "summary"):
            if key not in out and isinstance(metadata.get(key), str):
                out[key] = metadata[key]  # type: ignore[literal-required]
    if "summary" not in out and isinstance(out.get("result"), dict):
        summary = out["result"].get("summary")
        if isinstance(summary, str):
            out["summary"] = summary
    if "error" not in out and isinstance(out.get("error_payload"), dict):
        error = out["error_payload"].get("error")
        if isinstance(error, str):
            out["error"] = error
    return out


def serialize_event(event: ModelInstance) -> SerializedWorkEvent:
    data = serialize_record(event)
    out: SerializedWorkEvent = {}
    for key in (
        "id",
        "event_id",
        "event_seq",
        "job_id",
        "user_id",
        "status",
        "stage",
        "severity",
        "message",
        "created_at",
    ):
        if key in data:
            out[key] = data[key]  # type: ignore[literal-required]
    event_type = _value(event, ("event_type", "type", "kind"))
    if event_type is not None:
        out["event_type"] = str(event_type)
    payload = _value(event, ("payload", "payload_json", "data", "metadata_json"))
    if payload is not None:
        out["payload"] = _serialize_value(_jsonish(payload))
    if "job_id" not in out:
        out["job_id"] = _value(event, ("work_job_id", "job_pk", "job_row_id"))
    return out


def serialize_idempotency_record(record: ModelInstance) -> SerializedIdempotencyRecord:
    data = serialize_record(record)
    out: SerializedIdempotencyRecord = {}
    for key in ("id", "user_id", "created_at", "updated_at"):
        if key in data:
            out[key] = data[key]  # type: ignore[literal-required]
    key = _value(record, ("idempotency_key", "key"))
    if key is not None:
        out["key"] = str(key)
    request_hash = _value(record, ("request_hash", "body_hash", "payload_hash"))
    if request_hash is not None:
        out["request_hash"] = str(request_hash)
    job_id = _value(record, ("job_id", "work_job_id", "job_pk", "job_row_id"))
    if job_id is not None:
        out["job_id"] = job_id
    response = _value(
        record,
        ("response_body", "response", "response_json", "result", "result_json"),
    )
    if response is not None:
        out["response"] = _serialize_value(_jsonish(response))
    return out


async def get_job(
    session: AsyncSession,
    job_id: str | int,
    *,
    user_id: int | None = None,
) -> ModelInstance | None:
    """Return a work job by public ``job_id`` or integer primary key."""

    WorkJob = _model("WorkJob")
    identity_filters: list[Any] = []
    if _has_column(WorkJob, "job_id"):
        identity_filters.append(WorkJob.job_id == str(job_id))
    if _has_column(WorkJob, "id") and _is_int_like(job_id):
        identity_filters.append(WorkJob.id == int(job_id))
    if not identity_filters:
        raise WorkLedgerModelsUnavailable("WorkJob has no usable job identity column")

    filters: list[Any] = [or_(*identity_filters) if len(identity_filters) > 1 else identity_filters[0]]
    if user_id is not None and _has_column(WorkJob, "user_id"):
        filters.append(WorkJob.user_id == user_id)
    return (await session.execute(select(WorkJob).where(*filters))).scalar_one_or_none()


async def get_idempotency_record(
    session: AsyncSession,
    *,
    key: str,
    user_id: int | None = None,
    scope: str | None = None,
) -> ModelInstance | None:
    """Return an idempotency row for ``key`` if the model exists."""

    IdempotencyRecord = _model("IdempotencyRecord")
    key_column = _first_column(IdempotencyRecord, ("idempotency_key", "key"))
    if key_column is None:
        raise WorkLedgerModelsUnavailable(
            "IdempotencyRecord needs an idempotency_key or key column"
        )
    filters: list[Any] = [getattr(IdempotencyRecord, key_column) == key]
    if user_id is not None and _has_column(IdempotencyRecord, "user_id"):
        filters.append(IdempotencyRecord.user_id == user_id)
    if scope is not None:
        scope_column = _first_column(IdempotencyRecord, ("scope", "namespace"))
        if scope_column is not None:
            filters.append(getattr(IdempotencyRecord, scope_column) == scope)
    return (
        await session.execute(select(IdempotencyRecord).where(*filters))
    ).scalar_one_or_none()


async def create_job(
    session: AsyncSession,
    *,
    user_id: int | None,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    thread_id: str | None = None,
    title: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    job_id: str | None = None,
    status: str = "queued",
    queue: str = "default",
    priority: int = 0,
    max_attempts: int = 1,
    root_job_id: str | None = None,
    parent_job_id: str | None = None,
    correlation_id: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    worker_type: str | None = None,
    worker_name: str | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    proof_refs: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
    idempotency_scope: str | None = "work_ledger",
    record_event: bool = True,
    commit: bool = True,
) -> ModelInstance:
    """Create a durable work job, optionally binding it to an idempotency key."""

    body = dict(payload or {})
    meta = dict(metadata or {})
    if title:
        meta.setdefault("title", title)
    effective_request_hash = request_hash or _stable_hash(
        {"kind": kind, "payload": body, "metadata": meta}
    )
    effective_scope = idempotency_scope or "work_ledger"

    if idempotency_key:
        existing_record = await get_idempotency_record(
            session,
            key=idempotency_key,
            user_id=user_id,
            scope=effective_scope,
        )
        if existing_record is not None:
            _assert_same_request(existing_record, effective_request_hash)
            existing_job = await _job_for_idempotency_record(
                session, existing_record, user_id=user_id
            )
            if existing_job is not None:
                return existing_job
            raise IdempotencyConflict(
                "idempotency key is already recorded without a linked work job"
            )

    WorkJob = _model("WorkJob")
    now = _utcnow()
    public_id = job_id or uuid4().hex
    values: JsonObject = {
        "job_id": public_id,
        "work_id": public_id,
        "external_id": public_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "root_job_id": root_job_id or public_id,
        "parent_job_id": parent_job_id,
        "correlation_id": correlation_id,
        "kind": kind,
        "job_type": kind,
        "type": kind,
        "status": status,
        "queue": queue,
        "priority": priority,
        "max_attempts": max_attempts,
        "title": title or kind,
        "source_type": source_type,
        "source_id": source_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "worker_type": worker_type,
        "worker_name": worker_name,
        "input_payload": body,
        "payload": body,
        "payload_json": body,
        "input": body,
        "input_json": body,
        "request": body,
        "artifact_refs": artifact_refs or [],
        "proof_refs": proof_refs or [],
        "metadata_json": meta,
        "meta": meta,
        "data": meta,
        "idempotency_key": idempotency_key,
        "created_at": now,
        "updated_at": now,
        "queued_at": now,
    }
    job = WorkJob(**_model_kwargs(WorkJob, values))
    session.add(job)
    await session.flush()

    if idempotency_key:
        _add_idempotency_record(
            session,
            key=idempotency_key,
            user_id=user_id,
            job=job,
            request_hash=effective_request_hash,
            scope=effective_scope,
        )
    if record_event:
        await append_event(
            session,
            job,
            event_type="job_created",
            payload={"kind": kind, "payload": body},
            status=status,
            user_id=user_id,
            correlation_id=correlation_id,
            source_type=source_type,
            source_id=source_id,
            commit=False,
        )
    if commit:
        await session.commit()
        await session.refresh(job)
    else:
        await session.flush()
    return job


async def append_event(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    event_type: str,
    event_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    message: str | None = None,
    status: str | None = None,
    user_id: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    stage: str | None = None,
    severity: str = "info",
    parent_event_id: str | None = None,
    correlation_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    proof_refs: list[dict[str, Any]] | None = None,
    metrics: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> ModelInstance:
    """Append a timeline event for a work job."""

    WorkEvent = _model("WorkEvent")
    job_row = await _resolve_job(session, job, user_id=user_id)
    now = _utcnow()
    body = dict(payload or {})
    meta = dict(metadata or {})
    event_seq = await _next_event_seq(session, WorkEvent, job_row)
    values: JsonObject = {
        **_event_job_values(WorkEvent, job_row),
        "event_id": event_id or uuid4().hex,
        "event_seq": event_seq,
        "parent_event_id": parent_event_id,
        "correlation_id": correlation_id or getattr(job_row, "correlation_id", None),
        "user_id": user_id if user_id is not None else getattr(job_row, "user_id", None),
        "event_type": event_type,
        "type": event_type,
        "kind": event_type,
        "stage": stage,
        "status": status,
        "severity": severity,
        "message": message or "",
        "actor_type": actor_type,
        "actor_id": actor_id,
        "source_type": source_type,
        "source_id": source_id,
        "payload": body,
        "payload_json": body,
        "data": body,
        "artifact_refs": artifact_refs or [],
        "proof_refs": proof_refs or [],
        "metrics": dict(metrics or {}),
        "metadata_json": meta or body,
        "created_at": now,
    }
    event = WorkEvent(**_model_kwargs(WorkEvent, values))
    session.add(event)
    if commit:
        await session.commit()
        await session.refresh(event)
    else:
        await session.flush()
    return event


async def complete_job(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    result: Mapping[str, Any] | None = None,
    summary: str | None = None,
    user_id: int | None = None,
    status: str = "complete",
    event_type: str = "job_completed",
    commit: bool = True,
) -> ModelInstance:
    """Mark a work job complete and append a completion event."""

    job_row = await _resolve_current_job(session, job, user_id=user_id)
    if _is_canceled(job_row):
        await append_event(
            session,
            job_row,
            event_type="job_completion_ignored_after_cancel",
            payload={"result": dict(result or {}), "summary": summary},
            message="completion ignored because job is canceled",
            status=_status(job_row) or "canceled",
            user_id=user_id,
            commit=False,
        )
        if commit:
            await session.commit()
            await session.refresh(job_row)
        else:
            await session.flush()
        return job_row
    now = _utcnow()
    body = dict(result or {})
    _set_first(job_row, ("status",), status)
    if summary is not None:
        _set_first(job_row, ("summary", "message"), summary)
        _merge_first_mapping(job_row, ("metadata_json", "meta", "data"), {"summary": summary})
    _set_first(
        job_row,
        ("output_payload", "result", "result_json", "response", "response_json"),
        body,
    )
    _set_first(job_row, ("error",), None)
    _set_first(job_row, ("error_payload",), {})
    _set_first(job_row, ("completed_at", "finished_at"), now)
    _set_first(job_row, ("updated_at",), now)
    await append_event(
        session,
        job_row,
        event_type=event_type,
        payload={"result": body, "summary": summary},
        message=summary,
        status=status,
        user_id=user_id,
        commit=False,
    )
    if commit:
        await session.commit()
        await session.refresh(job_row)
    else:
        await session.flush()
    return job_row


async def fail_job(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    error: str,
    result: Mapping[str, Any] | None = None,
    summary: str | None = None,
    user_id: int | None = None,
    status: str = "error",
    event_type: str = "job_failed",
    commit: bool = True,
) -> ModelInstance:
    """Mark a work job failed and append a failure event."""

    job_row = await _resolve_current_job(session, job, user_id=user_id)
    if _is_canceled(job_row):
        await append_event(
            session,
            job_row,
            event_type="job_failure_ignored_after_cancel",
            payload={
                "error": error,
                "result": dict(result or {"error": error}),
                "summary": summary or error,
            },
            message="failure ignored because job is canceled",
            status=_status(job_row) or "canceled",
            user_id=user_id,
            commit=False,
        )
        if commit:
            await session.commit()
            await session.refresh(job_row)
        else:
            await session.flush()
        return job_row
    now = _utcnow()
    body = dict(result or {"error": error})
    message = summary or error
    _set_first(job_row, ("status",), status)
    _set_first(job_row, ("summary", "message"), message)
    _set_first(job_row, ("error",), error)
    _merge_first_mapping(
        job_row,
        ("metadata_json", "meta", "data"),
        {"summary": message, "error": error},
    )
    _set_first(job_row, ("error_payload",), body)
    _set_first(
        job_row,
        ("output_payload", "result", "result_json", "response", "response_json"),
        body,
    )
    _set_first(job_row, ("completed_at", "finished_at"), now)
    _set_first(job_row, ("updated_at",), now)
    await append_event(
        session,
        job_row,
        event_type=event_type,
        payload={"error": error, "result": body, "summary": message},
        message=message,
        status=status,
        user_id=user_id,
        commit=False,
    )
    if commit:
        await session.commit()
        await session.refresh(job_row)
    else:
        await session.flush()
    return job_row


async def cancel_job(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    reason: str | None = None,
    user_id: int | None = None,
    status: str = "canceled",
    event_type: str = "job_canceled",
    commit: bool = True,
) -> ModelInstance:
    """Mark a queued/running work job canceled and append a cancel event."""

    job_row = await _resolve_current_job(session, job, user_id=user_id)
    current_status = _status(job_row)
    message = reason or "canceled by user"
    if _is_terminal(job_row):
        await append_event(
            session,
            job_row,
            event_type="job_cancel_noop",
            payload={"reason": message, "existing_status": current_status},
            message=f"cancel ignored because job is already {current_status}",
            status=current_status,
            user_id=user_id,
            commit=False,
        )
        if commit:
            await session.commit()
            await session.refresh(job_row)
        else:
            await session.flush()
        return job_row

    now = _utcnow()
    body = {"canceled": True, "reason": message}
    _set_first(job_row, ("status",), status)
    _set_first(job_row, ("summary", "message"), message)
    _set_first(job_row, ("error",), message)
    _merge_first_mapping(
        job_row,
        ("metadata_json", "meta", "data"),
        {"summary": message, "canceled": True},
    )
    _set_first(job_row, ("error_payload",), body)
    _set_first(
        job_row,
        ("output_payload", "result", "result_json", "response", "response_json"),
        body,
    )
    _set_first(job_row, ("completed_at", "finished_at"), now)
    _set_first(job_row, ("updated_at",), now)
    await append_event(
        session,
        job_row,
        event_type=event_type,
        payload=body,
        message=message,
        status=status,
        user_id=user_id,
        commit=False,
    )
    if commit:
        await session.commit()
        await session.refresh(job_row)
    else:
        await session.flush()
    return job_row


async def list_user_activity(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 50,
    status: str | None = None,
    kind: str | None = None,
) -> list[ModelInstance]:
    """List recent work jobs for a user, newest first."""

    WorkJob = _model("WorkJob")
    if not _has_column(WorkJob, "user_id"):
        raise WorkLedgerModelsUnavailable("WorkJob needs a user_id column for activity")
    filters: list[Any] = [WorkJob.user_id == user_id]
    if status is not None and _has_column(WorkJob, "status"):
        filters.append(WorkJob.status == status)
    if kind is not None:
        kind_column = _first_column(WorkJob, ("kind", "job_type", "type"))
        if kind_column is not None:
            filters.append(getattr(WorkJob, kind_column) == kind)
    stmt = select(WorkJob).where(*filters)
    order_columns = [
        name
        for name in ("updated_at", "created_at", "id")
        if _has_column(WorkJob, name)
    ]
    for name in order_columns:
        stmt = stmt.order_by(desc(getattr(WorkJob, name)))
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    return list((await session.execute(stmt)).scalars().all())


async def list_job_events(
    session: AsyncSession,
    job: ModelInstance | str | int | None = None,
    *,
    job_id: str | int | None = None,
    user_id: int | None = None,
    cursor: str | None = None,
    after: str | None = None,
    after_id: str | None = None,
    after_event_id: str | None = None,
    limit: int = 500,
) -> list[ModelInstance]:
    """List events for a work job in insertion order."""

    WorkEvent = _model("WorkEvent")
    target = job if job is not None else job_id
    if target is None:
        raise WorkJobNotFound("work job not found: missing job_id")
    job_row = await _resolve_job(session, target, user_id=user_id)
    filters = _event_job_filters(WorkEvent, job_row)
    if user_id is not None and _has_column(WorkEvent, "user_id"):
        filters.append(WorkEvent.user_id == user_id)
    event_cursor = after_event_id or after_id or after or cursor
    if event_cursor is not None:
        if _has_column(WorkEvent, "id") and _is_int_like(event_cursor):
            filters.append(WorkEvent.id > int(event_cursor))
        elif _has_column(WorkEvent, "event_id"):
            filters.append(WorkEvent.event_id > str(event_cursor))
    stmt = select(WorkEvent).where(*filters)
    for name in ("id", "created_at"):
        if _has_column(WorkEvent, name):
            stmt = stmt.order_by(getattr(WorkEvent, name).asc())
            break
    stmt = stmt.limit(max(1, min(int(limit), 1000)))
    return list((await session.execute(stmt)).scalars().all())


async def _resolve_job(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    user_id: int | None = None,
) -> ModelInstance:
    if isinstance(job, (str, int)):
        row = await get_job(session, job, user_id=user_id)
        if row is None:
            raise WorkJobNotFound(f"work job not found: {job}")
        return row
    return job


async def _resolve_current_job(
    session: AsyncSession,
    job: ModelInstance | str | int,
    *,
    user_id: int | None = None,
) -> ModelInstance:
    job_row = await _resolve_job(session, job, user_id=user_id)
    if isinstance(job, (str, int)):
        return job_row
    try:
        await session.refresh(job_row)
        return job_row
    except Exception:
        public_id = _value(job_row, ("job_id", "work_id", "external_id"))
        if public_id is not None:
            row = await get_job(session, public_id, user_id=user_id)
            if row is not None:
                return row
        raise


def _event_job_values(event_model: ModelClass, job: ModelInstance) -> JsonObject:
    values: JsonObject = {}
    row_id = getattr(job, "id", None)
    public_id = _value(job, ("job_id", "work_id", "external_id"))
    if _has_column(event_model, "work_job_id") and row_id is not None:
        values["work_job_id"] = row_id
    if _has_column(event_model, "job_pk") and row_id is not None:
        values["job_pk"] = row_id
    if _has_column(event_model, "job_row_id") and row_id is not None:
        values["job_row_id"] = row_id
    if _has_column(event_model, "job_id"):
        if _column_python_type(event_model, "job_id") is int and row_id is not None:
            values["job_id"] = row_id
        else:
            values["job_id"] = public_id if public_id is not None else row_id
    return values


def _event_job_filters(event_model: ModelClass, job: ModelInstance) -> list[Any]:
    values = _event_job_values(event_model, job)
    filters = [
        getattr(event_model, name) == value
        for name, value in values.items()
        if value is not None and hasattr(event_model, name)
    ]
    if not filters:
        raise WorkLedgerModelsUnavailable(
            "WorkEvent needs a job_id, work_job_id, job_pk, or job_row_id column"
        )
    return [or_(*filters)] if len(filters) > 1 else filters


async def _next_event_seq(
    session: AsyncSession,
    event_model: ModelClass,
    job: ModelInstance,
) -> int | None:
    if not _has_column(event_model, "event_seq"):
        return None
    current = (
        await session.execute(
            select(func.coalesce(func.max(event_model.event_seq), 0)).where(
                *_event_job_filters(event_model, job)
            )
        )
    ).scalar_one()
    return int(current or 0) + 1


def _add_idempotency_record(
    session: AsyncSession,
    *,
    key: str,
    user_id: int | None,
    job: ModelInstance,
    request_hash: str | None,
    scope: str | None,
) -> ModelInstance:
    IdempotencyRecord = _model("IdempotencyRecord")
    now = _utcnow()
    public_id = _value(job, ("job_id", "work_id", "external_id"))
    row_id = getattr(job, "id", None)
    response = {"job": serialize_job(job)}
    result_id = str(public_id if public_id is not None else row_id or "")
    values: JsonObject = {
        "idempotency_key": key,
        "key": key,
        "user_id": user_id,
        "request_hash": request_hash,
        "body_hash": request_hash,
        "payload_hash": request_hash,
        "job_id": public_id if public_id is not None else row_id,
        "work_job_id": row_id,
        "job_pk": row_id,
        "job_row_id": row_id,
        "status": "succeeded",
        "result_type": "work_job",
        "result_id": result_id,
        "response_status_code": 202,
        "response_body": response,
        "response": response,
        "response_json": response,
        "result": response,
        "result_json": response,
        "scope": scope or "work_ledger",
        "namespace": scope or "work_ledger",
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
    }
    record = IdempotencyRecord(**_model_kwargs(IdempotencyRecord, values))
    session.add(record)
    return record


def _assert_same_request(
    record: ModelInstance,
    request_hash: str | None,
) -> None:
    if request_hash is None:
        return
    stored = _value(record, ("request_hash", "body_hash", "payload_hash"))
    if stored is not None and str(stored) != request_hash:
        raise IdempotencyConflict("idempotency key was reused with a different request")


async def _job_for_idempotency_record(
    session: AsyncSession,
    record: ModelInstance,
    *,
    user_id: int | None,
) -> ModelInstance | None:
    for name in ("job_id", "work_job_id", "job_pk", "job_row_id"):
        value = getattr(record, name, None)
        if value is None:
            continue
        job = await get_job(session, value, user_id=user_id)
        if job is not None:
            return job
    response = _value(
        record,
        ("response_body", "response", "response_json", "result", "result_json"),
    )
    response = _jsonish(response)
    if isinstance(response, dict):
        job_data = response.get("job") if isinstance(response.get("job"), dict) else response
        for key in ("job_id", "id"):
            value = job_data.get(key) if isinstance(job_data, dict) else None
            if value is None:
                continue
            job = await get_job(session, value, user_id=user_id)
            if job is not None:
                return job
    return None
