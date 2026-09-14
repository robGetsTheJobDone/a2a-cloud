from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Mapping

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_REPLAYED_HEADER = "Idempotency-Replayed"

STATUS_PROCESSING = "in_progress"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
PROCESSING_STATUSES = {"in_progress", "processing"}
DEFAULT_RECORD_TTL_SECONDS = 24 * 60 * 60
MAX_IDEMPOTENCY_KEY_LENGTH = 255

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

_KEY_FIELDS = ("idempotency_key", "key")
_SCOPE_FIELDS = ("scope", "tenant_key", "namespace")
_REQUEST_HASH_FIELDS = ("request_hash", "request_fingerprint", "fingerprint")
_METHOD_FIELDS = ("method", "request_method")
_PATH_FIELDS = ("path", "request_path")
_BODY_HASH_FIELDS = ("body_hash", "request_body_hash")
_STATUS_FIELDS = ("status", "state")
_STATUS_CODE_FIELDS = ("status_code", "response_status_code", "http_status_code")
_BODY_FIELDS = ("response_body", "response_json", "body")
_HEADERS_FIELDS = ("response_headers", "headers")
_MEDIA_TYPE_FIELDS = ("response_media_type", "media_type", "content_type")
_ERROR_FIELDS = ("error", "error_message", "failure_reason")
_EXTRA_FIELDS = (
    "metadata_json",
    "extra",
    "record_metadata",
    "result_metadata",
    "response_metadata",
)
_CREATED_AT_FIELDS = ("created_at",)
_UPDATED_AT_FIELDS = ("updated_at",)
_COMPLETED_AT_FIELDS = ("completed_at", "finished_at")
_LOCKED_UNTIL_FIELDS = ("locked_until",)
_EXPIRES_AT_FIELDS = ("expires_at",)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _record_model() -> type[Any]:
    try:
        from .models import IdempotencyRecord
    except ImportError as exc:
        raise RuntimeError(
            "IdempotencyRecord is not available in control_plane.models"
        ) from exc
    return IdempotencyRecord


async def _get_session() -> AsyncIterator[AsyncSession]:
    from .db import get_session

    async for session in get_session():
        yield session


def _field_name(obj: Any, candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        if hasattr(obj, name):
            return name
    return None


def _required_field(obj: Any, candidates: tuple[str, ...], purpose: str) -> str:
    name = _field_name(obj, candidates)
    if name is None:
        joined = ", ".join(candidates)
        raise RuntimeError(
            f"IdempotencyRecord must expose a {purpose} field; tried {joined}"
        )
    return name


def _get_value(obj: Any, candidates: tuple[str, ...], default: Any = None) -> Any:
    name = _field_name(obj, candidates)
    if name is None:
        return default
    return getattr(obj, name, default)


def _set_value(obj: Any, candidates: tuple[str, ...], value: Any) -> bool:
    name = _field_name(obj, candidates)
    if name is None:
        return False
    setattr(obj, name, value)
    return True


def _normalize_key(key: str | None, *, required: bool) -> str | None:
    if key is None or not key.strip():
        if required:
            raise HTTPException(400, f"missing {IDEMPOTENCY_KEY_HEADER} header")
        return None

    normalized = key.strip()
    if len(normalized) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(
            400,
            f"{IDEMPOTENCY_KEY_HEADER} must be at most "
            f"{MAX_IDEMPOTENCY_KEY_LENGTH} characters",
        )
    if any(ord(char) < 33 or ord(char) > 126 for char in normalized):
        raise HTTPException(
            400,
            f"{IDEMPOTENCY_KEY_HEADER} must contain visible ASCII characters only",
        )
    return normalized


async def _request_hash(request: Request) -> tuple[str, str]:
    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    request_hash = hashlib.sha256(
        b"\x00".join(
            [
                request.method.upper().encode("utf-8"),
                request.url.path.encode("utf-8"),
                body_hash.encode("ascii"),
            ]
        )
    ).hexdigest()
    return request_hash, body_hash


def _json_value(value: Any) -> JsonValue:
    encoded = jsonable_encoder(value)
    if isinstance(encoded, (dict, list, str, int, float, bool)) or encoded is None:
        return encoded
    return str(encoded)


def _response_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}

    excluded = {
        "connection",
        "content-length",
        "date",
        "keep-alive",
        "transfer-encoding",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in excluded
    }


def _cached_response(record: Any, *, key: str) -> Response | None:
    status_code = _get_value(record, _STATUS_CODE_FIELDS)
    if status_code is None:
        return None

    body = _get_value(record, _BODY_FIELDS)
    media_type = _get_value(record, _MEDIA_TYPE_FIELDS, "application/json")
    headers = _response_headers(_get_value(record, _HEADERS_FIELDS, {}))
    headers[IDEMPOTENCY_REPLAYED_HEADER] = "true"
    headers[IDEMPOTENCY_KEY_HEADER] = key

    if body is None or status_code in (204, 304):
        return Response(
            status_code=int(status_code),
            headers=headers,
            media_type=media_type,
        )

    if media_type == "application/json":
        return JSONResponse(
            content=body,
            status_code=int(status_code),
            headers=headers,
        )

    return Response(
        content=str(body),
        status_code=int(status_code),
        headers=headers,
        media_type=media_type,
    )


async def _find_record(
    session: AsyncSession,
    record_cls: type[Any],
    *,
    key: str,
    scope: str | None,
) -> Any | None:
    key_field = _required_field(record_cls, _KEY_FIELDS, "key")
    stmt = select(record_cls).where(getattr(record_cls, key_field) == key)

    scope_field = _field_name(record_cls, _SCOPE_FIELDS)
    if scope_field is not None:
        stmt = stmt.where(getattr(record_cls, scope_field) == scope)

    return (await session.execute(stmt)).scalar_one_or_none()


def _new_record(
    record_cls: type[Any],
    *,
    key: str,
    scope: str | None,
    request_hash: str,
    body_hash: str,
    method: str,
    path: str,
    ttl_seconds: int,
) -> Any:
    now = _utcnow()
    locked_until = now + timedelta(seconds=ttl_seconds)
    record = record_cls()
    _set_value(record, _KEY_FIELDS, key)
    _set_value(record, _SCOPE_FIELDS, scope)
    _set_value(record, _REQUEST_HASH_FIELDS, request_hash)
    _set_value(record, _BODY_HASH_FIELDS, body_hash)
    _set_value(record, _METHOD_FIELDS, method)
    _set_value(record, _PATH_FIELDS, path)
    _set_value(record, _STATUS_FIELDS, STATUS_PROCESSING)
    _set_value(
        record,
        _EXTRA_FIELDS,
        {
            "method": method,
            "path": path,
            "body_hash": body_hash,
        },
    )
    _set_value(record, _CREATED_AT_FIELDS, now)
    _set_value(record, _UPDATED_AT_FIELDS, now)
    _set_value(record, _LOCKED_UNTIL_FIELDS, locked_until)
    _set_value(record, _EXPIRES_AT_FIELDS, locked_until)
    return record


def _ensure_same_request(record: Any, request_hash: str) -> None:
    stored_hash = _get_value(record, _REQUEST_HASH_FIELDS)
    if stored_hash and stored_hash != request_hash:
        raise HTTPException(
            409,
            f"{IDEMPOTENCY_KEY_HEADER} was already used for a different request",
        )


def _ensure_not_processing(record: Any) -> None:
    status = _get_value(record, _STATUS_FIELDS)
    if status in PROCESSING_STATUSES:
        raise HTTPException(
            409,
            f"request with this {IDEMPOTENCY_KEY_HEADER} is still processing",
        )


@dataclass
class IdempotencyContext:
    key: str | None
    request_hash: str | None
    body_hash: str | None
    record: Any | None
    cached_response: Response | None = None
    _session: AsyncSession | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.key is not None and self.record is not None

    @property
    def is_replay(self) -> bool:
        return self.cached_response is not None

    async def store_success(
        self,
        *,
        status_code: int,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        media_type: str = "application/json",
        extra: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        await self.store_response(
            status=STATUS_SUCCEEDED,
            status_code=status_code,
            body=body,
            headers=headers,
            media_type=media_type,
            extra=extra,
            commit=commit,
        )

    async def store_failure(
        self,
        *,
        status_code: int,
        body: Any = None,
        error: str | None = None,
        headers: Mapping[str, str] | None = None,
        media_type: str = "application/json",
        extra: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        if body is None:
            body = {"detail": error or "request failed"}

        await self.store_response(
            status=STATUS_FAILED,
            status_code=status_code,
            body=body,
            headers=headers,
            media_type=media_type,
            error=error,
            extra=extra,
            commit=commit,
        )

    async def store_http_exception(
        self,
        exc: HTTPException,
        *,
        commit: bool = True,
    ) -> None:
        await self.store_failure(
            status_code=exc.status_code,
            body={"detail": exc.detail},
            error=str(exc.detail),
            headers=exc.headers,
            commit=commit,
        )

    async def store_response(
        self,
        *,
        status_code: int,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        media_type: str = "application/json",
        status: str | None = None,
        error: str | None = None,
        extra: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        if not self.enabled:
            return
        if self._session is None or self.record is None:
            raise RuntimeError("idempotency context is not bound to a session")

        now = _utcnow()
        final_status = status or (
            STATUS_SUCCEEDED if status_code < 400 else STATUS_FAILED
        )
        response_body = _json_value(body)
        if response_body is None:
            response_body = {}

        _set_value(self.record, _STATUS_FIELDS, final_status)
        _set_value(self.record, _STATUS_CODE_FIELDS, int(status_code))
        _set_value(self.record, _BODY_FIELDS, response_body)
        _set_value(self.record, _HEADERS_FIELDS, _response_headers(headers))
        _set_value(self.record, _MEDIA_TYPE_FIELDS, media_type)
        _set_value(self.record, _ERROR_FIELDS, error)
        existing_extra = _get_value(self.record, _EXTRA_FIELDS, {})
        if not isinstance(existing_extra, dict):
            existing_extra = {}
        _set_value(
            self.record,
            _EXTRA_FIELDS,
            {
                **existing_extra,
                **dict(extra or {}),
            },
        )
        _set_value(self.record, _UPDATED_AT_FIELDS, now)
        _set_value(self.record, _COMPLETED_AT_FIELDS, now)
        _set_value(self.record, _LOCKED_UNTIL_FIELDS, None)

        self._session.add(self.record)
        if commit:
            await self._session.commit()
            await self._session.refresh(self.record)
        else:
            await self._session.flush()


async def begin_idempotency(
    request: Request,
    session: AsyncSession,
    *,
    key: str | None = None,
    required: bool = False,
    scope: str | None = "global",
    ttl_seconds: int = DEFAULT_RECORD_TTL_SECONDS,
) -> IdempotencyContext:
    idempotency_key = _normalize_key(
        key if key is not None else request.headers.get(IDEMPOTENCY_KEY_HEADER),
        required=required,
    )
    if idempotency_key is None:
        return IdempotencyContext(
            key=None,
            request_hash=None,
            body_hash=None,
            record=None,
        )

    request_hash, body_hash = await _request_hash(request)
    record_cls = _record_model()
    record = await _find_record(
        session,
        record_cls,
        key=idempotency_key,
        scope=scope,
    )

    if record is not None:
        _ensure_same_request(record, request_hash)
        cached = _cached_response(record, key=idempotency_key)
        if cached is not None:
            return IdempotencyContext(
                key=idempotency_key,
                request_hash=request_hash,
                body_hash=body_hash,
                record=record,
                cached_response=cached,
                _session=session,
            )
        _ensure_not_processing(record)
        raise HTTPException(
            409,
            f"{IDEMPOTENCY_KEY_HEADER} exists but has no cached response",
        )

    record = _new_record(
        record_cls,
        key=idempotency_key,
        scope=scope,
        request_hash=request_hash,
        body_hash=body_hash,
        method=request.method.upper(),
        path=request.url.path,
        ttl_seconds=ttl_seconds,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        record = await _find_record(
            session,
            record_cls,
            key=idempotency_key,
            scope=scope,
        )
        if record is None:
            raise HTTPException(
                409,
                f"{IDEMPOTENCY_KEY_HEADER} reservation conflicted",
            ) from exc
        _ensure_same_request(record, request_hash)
        cached = _cached_response(record, key=idempotency_key)
        if cached is not None:
            return IdempotencyContext(
                key=idempotency_key,
                request_hash=request_hash,
                body_hash=body_hash,
                record=record,
                cached_response=cached,
                _session=session,
            )
        _ensure_not_processing(record)
        raise HTTPException(
            409,
            f"{IDEMPOTENCY_KEY_HEADER} reservation conflicted",
        ) from exc

    await session.refresh(record)
    return IdempotencyContext(
        key=idempotency_key,
        request_hash=request_hash,
        body_hash=body_hash,
        record=record,
        _session=session,
    )


