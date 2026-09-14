from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.models import (
    ChatThread,
    ChatThreadEvent,
    DagRun,
    DagRunNode,
    SubagentRun,
    SubagentRunEvent,
    User,
)
from control_plane.routes.threads import (
    _ThreadCreateIn,
    _ThreadPatchIn,
    create_thread,
    get_thread_messages,
    _logged_thread_activity_events,
    rename_thread,
    _thread_activity_events,
    get_thread_activity,
)
from control_plane.thread_events import append_thread_event
from control_plane.thread_messages import (
    append_thread_messages,
    list_persisted_thread_messages,
    normalize_thread_messages,
)


def test_normalize_thread_messages_skips_tool_messages_and_flattens_content() -> None:
    class Msg:
        def __init__(self, type_: str, content: object) -> None:
            self.type = type_
            self.content = content

    messages = normalize_thread_messages(
        [
            Msg("human", "hello"),
            {"role": "ai", "content": ["a", {"text": "b"}]},
            {"role": "tool", "content": "ignore"},
            {"role": "function", "content": "ignore too"},
            {"role": "assistant", "content": ""},
        ]
    )

    assert messages == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "ab"},
    ]


def test_normalize_thread_messages_reads_role_attributes() -> None:
    class Msg:
        def __init__(self, role: str, content: str) -> None:
            self.role = role
            self.content = content

    assert normalize_thread_messages([Msg("user", "hello")]) == [
        {"role": "user", "content": "hello"}
    ]


@pytest.mark.asyncio
async def test_append_thread_messages_stores_only_new_tail() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Hi")
            session.add_all([user, thread])
            await session.commit()

            await append_thread_messages(
                session,
                "thread-1",
                [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello"},
                ],
            )
            await append_thread_messages(
                session,
                "thread-1",
                [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello"},
                    {"role": "user", "content": "What happened?"},
                ],
            )

            assert await list_persisted_thread_messages(session, "thread-1") == [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
                {"role": "user", "content": "What happened?"},
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_persisted_thread_messages_bounds_recent_history() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Hi")
            session.add_all([user, thread])
            await session.commit()

            await append_thread_messages(
                session,
                "thread-1",
                [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "user", "content": "three"},
                    {"role": "assistant", "content": "four"},
                    {"role": "user", "content": "five"},
                ],
            )

            assert await list_persisted_thread_messages(
                session,
                "thread-1",
                limit=3,
                max_chars=9,
            ) == [
                {"role": "assistant", "content": "four"},
                {"role": "user", "content": "five"},
            ]

        async with Session() as session:
            thread = ChatThread(id="thread-2", user_id=1, title="Long")
            session.add(thread)
            await session.commit()
            await append_thread_messages(
                session,
                "thread-2",
                [{"role": "user", "content": "abcdefghij"}],
            )

            assert await list_persisted_thread_messages(
                session,
                "thread-2",
                limit=1,
                max_chars=4,
            ) == [{"role": "user", "content": "ghij"}]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_thread_messages_tolerates_checkpoint_read_failure() -> None:
    class BrokenCheckpointer:
        async def aget_tuple(self, _config: object) -> object:
            raise RuntimeError("checkpoint row is corrupt")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Hi")
            session.add_all([user, thread])
            await session.commit()

            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(checkpointer=BrokenCheckpointer())
                )
            )
            response = await get_thread_messages(
                "thread-1",
                request,
                user=user,
                session=session,
            )

            assert response["id"] == "thread-1"
            assert response["messages"] == []
            assert "checkpoint read failed" in response["warning"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_thread_settings_persist_run_policy_overrides() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            created = await create_thread(
                _ThreadCreateIn(
                    title="Policy chat",
                    settings={
                        "run_budget_cents": "250",
                        "max_agent_calls_per_run": "42",
                        "require_approval_for_file_writes": True,
                        "deny_external_network": False,
                        "only_approved_agents": True,
                        "pii_safe_mode": True,
                        "approved_agents": [" search-agent ", "", "writer"],
                    },
                ),
                user=user,
                session=session,
            )

            assert created.settings == {
                "run_budget_cents": 250,
                "max_agent_calls_per_run": 42,
                "require_approval_for_file_writes": True,
                "deny_external_network": False,
                "only_approved_agents": True,
                "pii_safe_mode": True,
                "approved_agents": ["search-agent", "writer"],
            }

            patched = await rename_thread(
                created.id,
                _ThreadPatchIn(
                    settings={
                        "run_budget_cents": "-1",
                        "max_agent_calls_per_run": "999",
                        "only_approved_agents": False,
                    }
                ),
                user=user,
                session=session,
            )
            assert patched.settings == {
                "run_budget_cents": 0,
                "max_agent_calls_per_run": 100,
                "only_approved_agents": False,
            }

            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(checkpointer=None)))
            response = await get_thread_messages(
                created.id,
                request,
                user=user,
                session=session,
            )
            assert response["settings"] == patched.settings
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_append_thread_event_assigns_ordered_sequence_numbers() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            session.add_all([user, thread])
            await session.commit()

            first = await append_thread_event(
                session,
                thread_id="thread-1",
                user_id=1,
                event={"type": "tool_call", "id": "call-1"},
            )
            second = await append_thread_event(
                session,
                thread_id="thread-1",
                user_id=1,
                event={"id": "missing-type"},
            )

            assert first.seq == 1
            assert second.seq == 2
            assert second.event_type == "event"
            assert second.payload["type"] == "event"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_thread_activity_prefers_exact_event_log_over_reconstructed_fallback() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            subagent = SubagentRun(
                grant_id="grant-1",
                user_id=1,
                thread_id="thread-1",
                agent_name="kernel-agent",
                skill_name="simulate",
                args_json="{}",
                scopes={},
            )
            session.add_all([user, thread, subagent])
            await session.flush()
            session.add_all(
                [
                    SubagentRunEvent(
                        run_id=subagent.id,
                        grant_id="grant-1",
                        user_id=1,
                        event_type="agent_progress",
                        payload={
                            "type": "agent_progress",
                            "grant_id": "grant-1",
                            "payload": {"message": "fallback"},
                        },
                        created_at=now,
                    ),
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=1,
                        seq=2,
                        event_type="final",
                        payload={"type": "final", "content": "done"},
                        created_at=now,
                    ),
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=1,
                        seq=1,
                        event_type="tool_call",
                        payload={"type": "tool_call", "id": "call-1"},
                        created_at=now,
                    ),
                ]
            )
            await session.commit()

            response = await get_thread_activity(
                "thread-1", user=user, session=session
            )

            assert [event["type"] for event in response["events"]] == [
                "tool_call",
                "final",
            ]
            assert [event["seq"] for event in response["events"]] == [1, 2]
            assert all(event["replayed"] is True for event in response["events"])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_thread_activity_supports_after_seq_and_recent_limit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            session.add_all([user, thread])
            await session.flush()
            for seq in range(1, 6):
                session.add(
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=1,
                        seq=seq,
                        event_type="tool_call",
                        payload={"type": "tool_call", "id": f"call-{seq}"},
                    )
                )
            await session.commit()

            recent = await get_thread_activity(
                "thread-1",
                limit=2,
                user=user,
                session=session,
            )
            assert [event["seq"] for event in recent["events"]] == [4, 5]
            assert recent["last_seq"] == 5

            delta = await get_thread_activity(
                "thread-1",
                after_seq=3,
                limit=10,
                user=user,
                session=session,
            )
            assert [event["seq"] for event in delta["events"]] == [4, 5]
            assert delta["last_seq"] == 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_logged_thread_activity_ignores_malformed_and_other_user_events() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            other = User(id=2, email="other@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            session.add_all([user, other, thread])
            await session.flush()
            session.add_all(
                [
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=1,
                        seq=1,
                        event_type="bad",
                        payload=["not", "an", "object"],
                    ),
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=2,
                        seq=2,
                        event_type="tool_call",
                        payload={"type": "tool_call", "id": "other"},
                    ),
                    ChatThreadEvent(
                        thread_id="thread-1",
                        user_id=1,
                        seq=3,
                        event_type="tool_call",
                        payload={"type": "tool_call", "id": "ours"},
                    ),
                ]
            )
            await session.commit()

            events = await _logged_thread_activity_events(
                "thread-1", user=user, session=session
            )

            assert [event["id"] for event in events] == ["ours"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_thread_activity_replays_subagent_and_dag_events_with_evidence() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            subagent = SubagentRun(
                grant_id="grant-1",
                user_id=1,
                thread_id="thread-1",
                agent_name="kernel-agent",
                skill_name="simulate",
                args_json="{}",
                scopes={},
                status="running",
            )
            dag = DagRun(
                dag_run_id="dag-1",
                user_id=1,
                thread_id="thread-1",
                goal="simulate kernel",
                status="complete",
                nodes_json=[
                    {
                        "id": "simulate",
                        "agent": "kernel-agent",
                        "skill": "simulate",
                        "deps": [],
                        "args": {},
                    }
                ],
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
            session.add_all([user, thread, subagent, dag])
            await session.flush()
            session.add_all(
                [
                    SubagentRunEvent(
                        run_id=subagent.id,
                        grant_id="grant-1",
                        user_id=1,
                        event_type="agent_progress",
                        payload={
                            "type": "agent_progress",
                            "grant_id": "grant-1",
                            "kind": "progress",
                            "payload": {"message": "running simulation"},
                        },
                        created_at=now,
                    ),
                    DagRunNode(
                        dag_run_id="dag-1",
                        node_id="simulate",
                        user_id=1,
                        agent_name="kernel-agent",
                        skill_name="simulate",
                        deps=[],
                        args_json="{}",
                        status="complete",
                        summary="suite done",
                        result={
                            "events": [
                                {
                                    "event_id": "evt-suite",
                                    "event_type": "arena_suite_recorded",
                                    "payload": {
                                        "suite_id": "suite-1",
                                        "passed": True,
                                        "scoreboard": {
                                            "participants": [{"participant_id": "alpha"}],
                                            "winner_events": [{"event_id": "winner-1"}],
                                        },
                                    },
                                }
                            ]
                        },
                        file_ops=[],
                        elapsed_ms=12,
                        started_at=now,
                        completed_at=now,
                    ),
                ]
            )
            await session.commit()

            events = await _thread_activity_events("thread-1", user=user, session=session)

            event_types = [event["type"] for event in events]
            assert "agent_progress" in event_types
            assert "dag_started" in event_types
            assert "dag_node_complete" in event_types
            assert "dag_complete" in event_types
            evidence = [event for event in events if event["type"] == "evidence_event"]
            assert len(evidence) == 1
            assert evidence[0]["evidence_kind"] == "arena_suite"
            assert evidence[0]["payload"]["suite_id"] == "suite-1"
            assert all(event["replayed"] is True for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_thread_activity_ignores_other_users_and_malformed_events() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="thread@example.com", password_hash="x")
            other = User(id=2, email="other@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Kernel run")
            other_thread = ChatThread(id="thread-2", user_id=2, title="Other")
            run = SubagentRun(
                grant_id="grant-1",
                user_id=1,
                thread_id="thread-1",
                agent_name="kernel-agent",
                skill_name="simulate",
                args_json="{}",
                scopes={},
            )
            session.add_all([user, other, thread, other_thread, run])
            await session.flush()
            session.add(
                SubagentRunEvent(
                    run_id=run.id,
                    grant_id="grant-1",
                    user_id=1,
                    event_type="broken",
                    payload={"payload": {"message": "missing type"}},
                )
            )
            await session.commit()

            assert await _thread_activity_events("thread-1", user=user, session=session) == []
            assert await _thread_activity_events("thread-1", user=other, session=session) == []
    finally:
        await engine.dispose()
