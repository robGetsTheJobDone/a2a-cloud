from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
import types
from typing import Any

import pytest
import pydantic.networks
from fastapi import HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)
sys.modules.setdefault("bcrypt", types.SimpleNamespace())
sys.modules.setdefault(
    "email_validator",
    types.SimpleNamespace(validate_email=lambda value, **_kwargs: value),
)
pydantic.networks.version = lambda _package: "2.0.0"

from control_plane.models import Agent, Bounty, User  # noqa: E402
from control_plane.db import Base  # noqa: E402
from control_plane.routes import bounties  # noqa: E402
from control_plane.routes.bounties import (  # noqa: E402
    _normalize_tags,
    _serialize,
    _validate_status,
    claim_bounty,
)
from control_plane.schemas import BountyClaimIn, BountyIn  # noqa: E402


class _Result:
    def __init__(self, value: Any):
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def all(self) -> list[Any]:
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class _Session:
    def __init__(self, *values: Any):
        self.values = list(values)
        self.added: list[Any] = []
        self.commits = 0
        self.refreshed: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self.values.pop(0))

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)
        if isinstance(obj, Bounty):
            now = datetime.now(timezone.utc)
            if obj.id is None:
                obj.id = 20
            if obj.created_at is None:
                obj.created_at = now
            if obj.updated_at is None:
                obj.updated_at = now


class _FakeIdempotency:
    def __init__(self, cached_response: Any | None = None):
        self.cached_response = cached_response
        self.success_calls: list[dict[str, Any]] = []
        self.failure_calls: list[HTTPException] = []

    async def store_success(self, **kwargs: Any) -> None:
        self.success_calls.append(kwargs)

    async def store_http_exception(
        self, exc: HTTPException, *, commit: bool = True
    ) -> None:
        self.failure_calls.append(exc)


@pytest.mark.asyncio
async def test_serialize_includes_claimed_agent_public_proof() -> None:
    now = datetime.now(timezone.utc)
    poster = User(id=1, email="poster@example.com", password_hash="x")
    agent = Agent(
        id=10,
        owner_id=2,
        name="invoice-bot",
        description="Extract invoices",
        version="1.2.3",
        image="registry/invoice-bot:latest",
        public=True,
        status="running",
        url="https://invoice-bot.example.com",
        card={"skills": [{"name": "extract_invoice"}], "tools_used": ["ocr"]},
    )
    bounty = Bounty(
        id=20,
        slug="extract-invoices-abc123",
        title="Extract invoices",
        description="Need an invoice extractor",
        example_input="pdf",
        example_output="json",
        tags=["ocr"],
        status="claimed",
        posted_by_id=poster.id,
        claimed_agent_id=agent.id,
        claimed_by_id=agent.owner_id,
        claimed_at=now,
        created_at=now,
    )

    out = await _serialize(bounty, _Session(poster, agent))

    assert out.claimed_agent_name == "invoice-bot"
    assert out.claimed_agent_status == "running"
    assert out.claimed_agent_url == "https://invoice-bot.example.com"
    assert out.claimed_agent_version == "1.2.3"
    assert out.claimed_agent_card == {
        "skills": [{"name": "extract_invoice"}],
        "tools_used": ["ocr"],
    }


@pytest.mark.asyncio
async def test_list_bounties_batches_related_metadata() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    select_statements: list[str] = []

    def capture_selects(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture_selects)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            poster = User(email="poster@example.com", password_hash="x")
            claimer = User(email="claimer@example.com", password_hash="x")
            session.add_all([poster, claimer])
            await session.flush()
            agent = Agent(
                owner_id=claimer.id,
                name="invoice-bot",
                description="Extract invoices",
                version="1.2.3",
                image="registry/invoice-bot:latest",
                public=True,
                status="running",
                url="https://invoice-bot.example.com",
                card={"skills": [{"name": "extract_invoice"}]},
            )
            session.add(agent)
            await session.flush()
            session.add_all(
                [
                    Bounty(
                        slug="extract-invoices-1",
                        title="Extract invoices 1",
                        description="Need an invoice extractor",
                        example_input="pdf",
                        example_output="json",
                        tags=["ocr"],
                        status="claimed",
                        posted_by_id=poster.id,
                        claimed_agent_id=agent.id,
                        claimed_by_id=claimer.id,
                    ),
                    Bounty(
                        slug="extract-invoices-2",
                        title="Extract invoices 2",
                        description="Need another invoice extractor",
                        example_input="pdf",
                        example_output="json",
                        tags=["ocr"],
                        status="claimed",
                        posted_by_id=poster.id,
                        claimed_agent_id=agent.id,
                        claimed_by_id=claimer.id,
                    ),
                ]
            )
            await session.commit()

            select_statements.clear()
            out = await bounties.list_bounties(
                Response(),
                mine=False,
                status=None,
                limit=None,
                offset=0,
                user=poster,
                session=session,
            )

            assert [bounty.claimed_agent_name for bounty in out] == [
                "invoice-bot",
                "invoice-bot",
            ]
            assert {bounty.posted_by_email for bounty in out} == {
                "poster@example.com"
            }
            assert len(select_statements) == 3
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_selects)
        await engine.dispose()


def test_normalize_tags_trims_deduplicates_and_slugifies_spaces() -> None:
    assert _normalize_tags([" OCR ", "ocr", "Invoice Review", ""]) == [
        "ocr",
        "invoice-review",
    ]


def test_validate_status_rejects_unknown_status() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_status("waiting")

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_claim_bounty_rejects_poster_self_claim() -> None:
    now = datetime.now(timezone.utc)
    poster = User(id=1, email="poster@example.com", password_hash="x")
    bounty = Bounty(
        id=20,
        slug="extract-invoices-abc123",
        title="Extract invoices",
        description="Need an invoice extractor",
        example_input="pdf",
        example_output="json",
        tags=["ocr"],
        status="open",
        posted_by_id=poster.id,
        created_at=now,
    )

    with pytest.raises(HTTPException) as exc:
        await claim_bounty(
            bounty.slug,
            BountyClaimIn(agent_name="invoice-bot"),
            None,
            poster,
            _Session(bounty),
        )

    assert exc.value.status_code == 409
    assert "poster cannot claim" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_create_bounty_stores_idempotency_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idempotency = _FakeIdempotency()
    scopes: list[str | None] = []

    async def fake_begin_idempotency(
        request: Any,
        session: Any,
        *,
        scope: str | None = None,
    ) -> _FakeIdempotency:
        scopes.append(scope)
        return idempotency

    monkeypatch.setattr(bounties, "begin_idempotency", fake_begin_idempotency)

    poster = User(id=1, email="poster@example.com", password_hash="x")
    session = _Session(poster)
    out = await bounties.create_bounty(
        BountyIn(
            title="Extract invoices",
            description="Need an invoice extractor",
            example_input="pdf",
            example_output="json",
            tags=[" OCR "],
        ),
        object(),
        poster,
        session,
    )

    assert scopes == ["bounties:create:user:1"]
    assert session.commits == 1
    assert len(session.added) == 1
    assert idempotency.failure_calls == []
    assert idempotency.success_calls == [
        {
            "status_code": 201,
            "body": out,
            "extra": {
                "result_type": "bounty",
                "result_id": out.slug,
                "bounty_id": out.id,
            },
        }
    ]


@pytest.mark.asyncio
async def test_claim_bounty_returns_cached_idempotency_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = JSONResponse({"slug": "extract-invoices-abc123"}, status_code=200)
    idempotency = _FakeIdempotency(cached_response=cached)

    async def fake_begin_idempotency(
        request: Any,
        session: Any,
        *,
        scope: str | None = None,
    ) -> _FakeIdempotency:
        assert scope == "bounties:claim:user:2"
        return idempotency

    monkeypatch.setattr(bounties, "begin_idempotency", fake_begin_idempotency)

    out = await bounties.claim_bounty(
        "extract-invoices-abc123",
        BountyClaimIn(agent_name="invoice-bot"),
        object(),
        User(id=2, email="claimer@example.com", password_hash="x"),
        _Session(),
    )

    assert out is cached
    assert idempotency.success_calls == []
    assert idempotency.failure_calls == []
