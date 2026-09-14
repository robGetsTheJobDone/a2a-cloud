from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from types import SimpleNamespace
from typing import Any, AsyncIterator

from fastapi import HTTPException
import pytest
from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from control_plane import idempotency
from control_plane.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_REPLAYED_HEADER,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    begin_idempotency,
)


class _Base(DeclarativeBase):
    pass


class _IdempotencyRecord(_Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("scope", "idempotency_key", name="uq_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(96))
    body_hash: Mapped[str | None] = mapped_column(String(96), nullable=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(
        JSON, nullable=True
    )
    response_media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[Any | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Any | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)


class _Request:
    def __init__(
        self,
        *,
        key: str = "idem-key",
        body: bytes = b'{"amount":100}',
        method: str = "post",
        path: str = "/v1/operations",
    ) -> None:
        self.headers = {IDEMPOTENCY_KEY_HEADER: key}
        self.method = method
        self.url = SimpleNamespace(path=path)
        self._body = body

    async def body(self) -> bytes:
        return self._body


@asynccontextmanager
async def _session(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncSession]:
    monkeypatch.setattr(idempotency, "_record_model", lambda: _IdempotencyRecord)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()


async def _record_by_key(session: AsyncSession, key: str) -> _IdempotencyRecord:
    result = await session.execute(
        select(_IdempotencyRecord).where(_IdempotencyRecord.idempotency_key == key)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_begin_idempotency_reserves_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(monkeypatch) as session:
        request = _Request(key="reserve-1")

        context = await begin_idempotency(request, session, scope="payments")
        record = await _record_by_key(session, "reserve-1")

        assert context.enabled is True
        assert context.is_replay is False
        assert context.cached_response is None
        assert context.record is record
        assert context.body_hash == hashlib.sha256(b'{"amount":100}').hexdigest()
        assert record.scope == "payments"
        assert record.status == STATUS_PROCESSING
        assert record.method == "POST"
        assert record.path == "/v1/operations"
        assert record.request_hash == context.request_hash
        assert record.body_hash == context.body_hash
        assert record.metadata_json == {
            "method": "POST",
            "path": "/v1/operations",
            "body_hash": context.body_hash,
        }
        assert record.locked_until is not None
        assert record.expires_at is not None


@pytest.mark.asyncio
async def test_begin_idempotency_replays_cached_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(monkeypatch) as session:
        request = _Request(key="replay-1")
        context = await begin_idempotency(request, session, scope="payments")
        await context.store_success(
            status_code=201,
            body={"job_id": "job-1"},
            headers={"X-Request-Id": "req-1", "Content-Length": "ignored"},
            extra={"result_type": "job"},
        )

        replay = await begin_idempotency(_Request(key="replay-1"), session, scope="payments")
        cached = replay.cached_response
        record = await _record_by_key(session, "replay-1")

        assert replay.is_replay is True
        assert cached is not None
        assert cached.status_code == 201
        assert json.loads(cached.body) == {"job_id": "job-1"}
        assert cached.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert cached.headers[IDEMPOTENCY_KEY_HEADER] == "replay-1"
        assert cached.headers["X-Request-Id"] == "req-1"
        assert record.status == STATUS_SUCCEEDED
        assert record.response_status_code == 201
        assert record.response_body == {"job_id": "job-1"}
        assert record.response_headers == {"X-Request-Id": "req-1"}
        assert record.metadata_json["result_type"] == "job"
        assert record.locked_until is None
        assert record.completed_at is not None


@pytest.mark.asyncio
async def test_begin_idempotency_rejects_same_key_with_different_request_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(monkeypatch) as session:
        first = await begin_idempotency(_Request(key="conflict-1"), session)
        original_hash = first.request_hash

        with pytest.raises(HTTPException) as exc_info:
            await begin_idempotency(
                _Request(key="conflict-1", body=b'{"amount":200}'),
                session,
            )

        record = await _record_by_key(session, "conflict-1")
        assert exc_info.value.status_code == 409
        assert "different request" in str(exc_info.value.detail)
        assert record.request_hash == original_hash
        assert record.status == STATUS_PROCESSING


@pytest.mark.asyncio
async def test_store_http_exception_persists_error_metadata_for_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _session(monkeypatch) as session:
        request = _Request(key="error-1")
        context = await begin_idempotency(request, session, scope="payments")

        await context.store_http_exception(
            HTTPException(
                status_code=422,
                detail="invalid transfer",
                headers={"Retry-After": "30", "Content-Length": "ignored"},
            )
        )

        record = await _record_by_key(session, "error-1")
        assert record.status == STATUS_FAILED
        assert record.response_status_code == 422
        assert record.response_body == {"detail": "invalid transfer"}
        assert record.response_headers == {"Retry-After": "30"}
        assert record.error == "invalid transfer"
        assert record.locked_until is None
        assert record.completed_at is not None

        replay = await begin_idempotency(_Request(key="error-1"), session, scope="payments")
        cached = replay.cached_response
        assert replay.is_replay is True
        assert cached is not None
        assert cached.status_code == 422
        assert json.loads(cached.body) == {"detail": "invalid transfer"}
        assert cached.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
        assert cached.headers[IDEMPOTENCY_KEY_HEADER] == "error-1"
        assert cached.headers["Retry-After"] == "30"
