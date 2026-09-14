from __future__ import annotations

import os
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, AsyncIterator

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from control_plane.auth import issue_token
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane.models import ChatThread, ChatThreadMessage, User
from control_plane.pending_actions import create_pending_actions_store
from control_plane.routes import chat as chat_routes


def test_build_hooks_respects_chat_approval_mode() -> None:
    async def emit(event: dict[str, Any]) -> None:
        pass

    async def wait(**kwargs: Any) -> str:
        raise AssertionError("approval wait should not be called")

    hooks = chat_routes._build_hooks(
        emit=emit,
        approval_mode=True,
        session=object(),
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=wait),
    )

    assert hooks.approval_mode is True
    assert hooks.auto_approve is False
    assert hooks.wait_for_handoff_approval is not None
    assert hooks.wait_for_scope_approval is not None


def test_build_hooks_keeps_auto_approve_when_approval_mode_is_off() -> None:
    async def emit(event: dict[str, Any]) -> None:
        pass

    async def wait(**kwargs: Any) -> str:
        raise AssertionError("approval wait should not be called")

    hooks = chat_routes._build_hooks(
        emit=emit,
        approval_mode=False,
        session=object(),
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=wait),
    )

    assert hooks.approval_mode is False
    assert hooks.auto_approve is True


@pytest.mark.asyncio
async def test_build_hooks_skips_consumer_setup_db_for_cards_without_fields() -> None:
    async def emit(event: dict[str, Any]) -> None:
        pass

    async def wait(**kwargs: Any) -> str:
        raise AssertionError("pending wait should not be called")

    hooks = chat_routes._build_hooks(
        emit=emit,
        approval_mode=False,
        session=object(),
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=wait),
    )

    assert hooks.resolve_consumer_setup is not None
    payload = await hooks.resolve_consumer_setup(
        "plain-agent",
        {"consumer_setup": {"fields": []}, "skills": []},
    )

    assert payload == {"ok": True, "consumer_config": {}, "consumer_secrets": {}}


@pytest.mark.asyncio
async def test_build_hooks_does_not_skip_consumer_setup_db_for_empty_card() -> None:
    async def emit(event: dict[str, Any]) -> None:
        pass

    async def wait(**kwargs: Any) -> str:
        raise AssertionError("pending wait should not be called")

    class FakeSession:
        rolled_back = False

        async def execute(self, statement: Any) -> Any:
            raise RuntimeError("db touched")

        async def rollback(self) -> None:
            self.rolled_back = True

    session = FakeSession()
    hooks = chat_routes._build_hooks(
        emit=emit,
        approval_mode=False,
        session=session,
        user=SimpleNamespace(id=7),
        pending_store=SimpleNamespace(wait=wait),
    )

    assert hooks.resolve_consumer_setup is not None
    with pytest.raises(RuntimeError, match="db touched"):
        await hooks.resolve_consumer_setup("plain-agent", {})
    assert session.rolled_back is True


def test_chat_policy_overrides_merge_run_scoped_controls() -> None:
    policy = {
        "monthly_budget_cents": 5000,
        "run_budget_cents": 500,
        "max_agent_calls_per_run": 8,
        "require_approval_for_file_writes": False,
        "deny_external_network": False,
        "only_approved_agents": True,
        "pii_safe_mode": False,
        "approved_agents": ["base-agent"],
    }

    out = chat_routes._effective_policy_controls(
        policy,
        {
            "monthly_budget_cents": 1,
            "run_budget_cents": "25",
            "max_agent_calls_per_run": "99",
            "require_approval_for_file_writes": True,
            "deny_external_network": True,
            "only_approved_agents": False,
            "pii_safe_mode": True,
            "approved_agents": [" search-agent ", ""],
        },
    )

    assert out["monthly_budget_cents"] == 5000
    assert out["run_budget_cents"] == 25
    assert out["max_agent_calls_per_run"] == 99
    assert out["require_approval_for_file_writes"] is True
    assert out["deny_external_network"] is True
    assert out["only_approved_agents"] is False
    assert out["pii_safe_mode"] is True
    assert out["approved_agents"] == ["search-agent"]


def test_extract_delta_content_preserves_streaming_whitespace() -> None:
    class Chunk:
        content = " "

    assert chat_routes._extract_delta_content(Chunk()) == " "
    assert chat_routes._extract_delta_content(
        {"content": [{"text": "hello"}, " ", {"text": "world"}]}
    ) == "hello world"
    assert chat_routes._extract_delta_content({"content": ""}) is None


def test_preview_args_bounds_nested_payloads() -> None:
    preview = chat_routes._preview_args(
        {
            "content": "x" * 25,
            "items": [{"body": "y" * 25} for _ in range(22)],
            **{f"k{i}": i for i in range(20)},
        },
        limit=10,
    )

    assert preview["content"] == "x" * 10 + "… (+15 chars)"
    assert preview["items"][0] == {"body": "y" * 10 + "… (+15 chars)"}
    assert preview["items"][-1] == "… (+2 items)"
    assert preview["..."] == "+10 keys"


def test_chat_wire_event_omits_internal_handoff_payloads() -> None:
    handoff = {
        "type": "agent_handoff",
        "grant_id": "grant-1",
        "to": "worker",
        "skill": "run",
        "args_json": '{"large":"payload"}',
        "args_preview": {"large": "payload"},
    }
    approval = {
        "type": "approval_required",
        "approval_id": "approval-1",
        "handoff": dict(handoff),
    }
    complete = {
        "type": "handoff_complete",
        "grant_id": "grant-1",
        "ok": True,
        "summary": "ok",
        "result": {"large": "payload"},
    }

    assert "args_json" not in chat_routes._chat_wire_event(handoff)
    approval_wire = chat_routes._chat_wire_event(approval)
    assert "args_json" not in approval_wire["handoff"]
    assert "result" not in chat_routes._chat_wire_event(complete)
    assert "args_json" in handoff
    assert "result" in complete


@pytest.mark.asyncio
async def test_delta_coalescer_batches_small_chunks_until_flush() -> None:
    published: list[tuple[dict[str, Any], bool]] = []

    async def publish(event: dict[str, Any], *, persist: bool = True) -> None:
        published.append((event, persist))

    coalescer = chat_routes._DeltaCoalescer(
        publish,
        flush_seconds=999.0,
        flush_chars=10,
    )

    await coalescer.push("hel")
    await coalescer.push("lo")
    assert published == []

    await coalescer.push("world")
    assert published == [({"type": "delta", "content": "helloworld"}, False)]

    await coalescer.push("!")
    await coalescer.flush()
    assert published[-1] == ({"type": "delta", "content": "!"}, False)


@pytest.mark.asyncio
async def test_chat_forwards_session_cookie_token_to_orchestrator(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    async def fake_resolve_main_llm_creds(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(chat_routes, "_resolve_main_llm_creds", fake_resolve_main_llm_creds)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        session.add(User(id=7, email="dev@example.com", password_hash="oidc"))
        await session.commit()

    app = FastAPI()
    app.state.pending_actions = create_pending_actions_store(None)
    app.include_router(chat_routes.router)

    async def override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session

    seen: dict[str, Any] = {}

    async def fake_stream_orchestrator(
        messages: list[dict[str, Any]],
        user: User,
        jwt: str | None,
        session: Any,
        approval_mode: bool,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        seen["jwt"] = jwt
        seen["user_id"] = user.id
        yield chat_routes._sse({"type": "final", "content": "ok"})
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(
        chat_routes,
        "_stream_orchestrator",
        fake_stream_orchestrator,
    )

    token = issue_token(7)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )
    client.cookies.set(settings.session_cookie_name, token)
    try:
        response = await client.post(
            "/v1/me/chat",
            json={"messages": [{"role": "user", "content": "list my agents"}]},
        )
    finally:
        await client.aclose()
        close = getattr(app.state.pending_actions, "close", None)
        if close is not None:
            with suppress(Exception):
                await close()
        await engine.dispose()

    assert response.status_code == 200
    assert "ok" in response.text
    assert seen == {"jwt": token, "user_id": 7}


@pytest.mark.asyncio
async def test_chat_rebuilds_history_from_db_without_checkpointer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "test-secret")

    async def fake_resolve_main_llm_creds(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(chat_routes, "_resolve_main_llm_creds", fake_resolve_main_llm_creds)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        session.add(User(id=8, email="history@example.com", password_hash="oidc"))
        session.add(ChatThread(id="thread-history", user_id=8, title="History"))
        session.add(
            ChatThreadMessage(
                thread_id="thread-history",
                role="user",
                content="first turn",
            )
        )
        session.add(
            ChatThreadMessage(
                thread_id="thread-history",
                role="assistant",
                content="first answer",
            )
        )
        await session.commit()

    app = FastAPI()
    app.state.pending_actions = create_pending_actions_store(None)
    app.state.checkpointer = None
    app.include_router(chat_routes.router)

    async def override_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    seen: dict[str, Any] = {}

    async def fake_stream_orchestrator(
        messages: list[dict[str, Any]],
        *_args: Any,
        **_kwargs: Any,
    ) -> AsyncIterator[bytes]:
        seen["messages"] = messages
        yield chat_routes._sse({"type": "final", "content": "ok"})
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "_stream_orchestrator", fake_stream_orchestrator)

    token = issue_token(8)
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    )
    try:
        response = await client.post(
            "/v1/me/chat",
            headers={"authorization": f"bearer {token}"},
            json={
                "thread_id": "thread-history",
                "messages": [{"role": "user", "content": "second turn"}],
            },
        )
    finally:
        await client.aclose()
        close = getattr(app.state.pending_actions, "close", None)
        if close is not None:
            with suppress(Exception):
                await close()
        await engine.dispose()

    assert response.status_code == 200
    assert seen["messages"] == [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second turn"},
    ]
