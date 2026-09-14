from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import (
    GrantAudit,
    LLMUsageEvent,
    SubagentRun,
    SubagentRunEvent,
    User,
    WorkEvent,
    WorkJob,
)
import control_plane.subagent_runs as subagent_runs
from control_plane.subagent_runs import close_stale_subagent_runs, diff_snapshots
import control_plane.subagent_stale_closer_worker as stale_closer_worker
import control_plane.routes.subagent_runs as subagent_run_routes
from control_plane.routes.subagent_runs import (
    get_subagent_run,
    list_subagent_runs,
    rerun_subagent_run,
    track_subagent_event,
)
import main_agent.tools.handoff as handoff_tools
from main_agent.tools.handoff import (
    _handoff_llm_creds,
    _summarize_result,
    build_handoff_tools,
)


def test_diff_snapshots_tracks_created_updated_deleted_files():
    before = {
        "data/input.csv": {
            "path": "data/input.csv",
            "size": 10,
            "modified_at": "2026-01-01T00:00:00Z",
            "content_type": "text/csv",
        },
        "old.txt": {
            "path": "old.txt",
            "size": 3,
            "modified_at": "2026-01-01T00:00:00Z",
            "content_type": "text/plain",
        },
    }
    after = {
        "data/input.csv": {
            "path": "data/input.csv",
            "size": 11,
            "modified_at": "2026-01-01T00:00:01Z",
            "content_type": "text/csv",
        },
        "outputs/report.md": {
            "path": "outputs/report.md",
            "size": 22,
            "modified_at": "2026-01-01T00:00:01Z",
            "content_type": "text/markdown",
        },
    }

    assert diff_snapshots(before, after) == [
        {
            "op": "create",
            "path": "outputs/report.md",
            "size": 22,
            "content_type": "text/markdown",
        },
        {
            "op": "update",
            "path": "data/input.csv",
            "size": 11,
            "content_type": "text/csv",
        },
        {
            "op": "delete",
            "path": "old.txt",
            "size": 3,
            "content_type": "text/plain",
        },
    ]


@pytest.mark.asyncio
async def test_only_approved_agents_empty_list_fails_closed() -> None:
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            agents_namespace_dns="{name}.agents.svc.cluster.local",
        ),
        hooks=None,
        bucket="user-1-files",
        user_id=1,
        policy_controls={"only_approved_agents": True, "approved_agents": []},
    )
    call_agent = build_handoff_tools(ctx)[0]

    out = await call_agent.ainvoke({"name": "graph-agent", "skill": "chart", "args_json": "{}"})

    assert "not on the approved list" in out
    assert "graph-agent" in out



@pytest.mark.asyncio
async def test_track_subagent_event_records_direct_agent_invoke() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-1",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "args_json": '{"prompt":"sales"}',
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-1",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "ok": True,
                    "summary": "path=outputs/chart.png",
                    "result": {"path": "outputs/chart.png"},
                    "elapsed_ms": 25,
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )

            run = await get_subagent_run("invoke-1", user=user, session=session)

            assert run.agent_name == "graph-agent"
            assert run.skill_name == "chart"
            assert run.status == "complete"
            assert run.summary == "path=outputs/chart.png"
            assert run.events[0].event_type == "agent_invoke_started"
            assert run.events[1].event_type == "agent_invoke_complete"

            rows = (await session.execute(SubagentRun.__table__.select())).all()
            assert len(rows) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_get_subagent_run_returns_handoffs_grants_and_receipts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_handoff",
                    "from": "main-agent",
                    "to": "support-agent",
                    "skill": "refund_request",
                    "grant_id": "grant-rich",
                    "args_json": '{"ticket":"REQ-1"}',
                    "args_preview": {"ticket": "REQ-1"},
                    "scopes": {
                        "bucket": "user-1-files",
                        "mode": "read_only",
                        "allow_patterns": ["tickets/**"],
                    },
                },
                user=user,
                session=session,
            )
            session.add(
                GrantAudit(
                    grant_id="grant-rich",
                    parent_grant_id=None,
                    issuer="main-agent",
                    audience="support-agent",
                    bucket="user-1-files",
                    mode="read_only",
                    allow_patterns=["tickets/**"],
                    deny_patterns=[],
                    outputs_prefix="outputs/",
                    ttl_seconds=600,
                    user_id=user.id,
                    decision="auto_approve",
                    decided_by="policy",
                    reason="support refund lookup",
                )
            )
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_event",
                    "grant_id": "grant-rich",
                    "agent": "support-agent",
                    "skill": "refund_request",
                    "kind": "receipt_sealed",
                    "summary": "receipt_sealed",
                    "payload": {
                        "token": "signed-receipt-token",
                        "receipt_id": "receipt-1",
                        "agent_name": "support-agent",
                        "skill_name": "refund_request",
                        "task_id": "task-1",
                        "status": "ok",
                    },
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_event",
                    "grant_id": "grant-rich",
                    "agent": "support-agent",
                    "skill": "refund_request",
                    "kind": "replay_sealed",
                    "summary": "replay_sealed",
                    "payload": {
                        "token": "signed-replay-token",
                        "session_id": "replay-1",
                        "agent_name": "support-agent",
                        "skill_name": "refund_request",
                        "task_id": "task-1",
                    },
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "handoff_complete",
                    "grant_id": "grant-rich",
                    "ok": True,
                    "summary": "refund approved",
                    "result": {"decision": "approved"},
                },
                user=user,
                session=session,
            )

            run = await get_subagent_run("grant-rich", user=user, session=session)

            assert run.original_request is not None
            assert run.original_request["args"] == {"ticket": "REQ-1"}
            assert run.handoffs[0].from_agent == "main-agent"
            assert run.handoffs[0].to_agent == "support-agent"
            assert run.handoffs[0].status == "complete"
            assert run.handoffs[0].summary == "refund approved"
            assert [grant.grant_id for grant in run.grants] == ["grant-rich"]
            assert run.grants[0].decision == "auto_approve"
            assert {receipt.kind for receipt in run.receipts} == {"receipt", "replay"}
            assert {receipt.signed_token for receipt in run.receipts} == {
                "signed-receipt-token",
                "signed-replay-token",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subagent_recorder_does_not_snapshot_bucket(monkeypatch) -> None:
    def fail_snapshot(bucket: str) -> dict[str, dict[str, object]]:
        raise AssertionError(f"unexpected bucket snapshot: {bucket}")

    monkeypatch.setattr(subagent_runs, "snapshot_files", fail_snapshot)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-no-snapshot",
                    "to": "tasks-api",
                    "agent": "tasks-api",
                    "skill": "list_epics",
                    "args_json": "{}",
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-no-snapshot",
                    "to": "tasks-api",
                    "agent": "tasks-api",
                    "skill": "list_epics",
                    "ok": True,
                    "summary": "ok",
                    "result": {"ok": True},
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )

            run = (
                await session.execute(
                    select(SubagentRun).where(
                        SubagentRun.grant_id == "invoke-no-snapshot"
                    )
                )
            ).scalar_one()
            assert run.start_files == {}
            assert run.file_ops == []

            events = (
                await session.execute(
                    select(SubagentRunEvent).where(
                        SubagentRunEvent.grant_id == "invoke-no-snapshot"
                    )
                )
            ).scalars().all()
            assert all("file_ops" not in event.payload for event in events)
    finally:
        await engine.dispose()


def test_snapshot_files_scopes_object_listing_to_prefix(monkeypatch) -> None:
    observed: dict[str, str] = {}

    def fake_list_files(bucket: str, prefix: str = ""):
        observed.update(bucket=bucket, prefix=prefix)
        return [
            {
                "path": f"{prefix}result.json",
                "size": 12,
                "modified_at": "2026-07-23T00:00:00Z",
                "content_type": "application/json",
            }
        ]

    monkeypatch.setattr(subagent_runs, "list_files", fake_list_files)

    result = subagent_runs.snapshot_files(
        "user-2-files",
        prefix="proof/developer-video-agent/",
    )

    assert observed == {
        "bucket": "user-2-files",
        "prefix": "proof/developer-video-agent/",
    }
    assert list(result) == ["proof/developer-video-agent/result.json"]


@pytest.mark.asyncio
async def test_list_subagent_runs_omits_file_ops_but_returns_count() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            session.add(
                SubagentRun(
                    grant_id="invoke-files",
                    user_id=user.id,
                    agent_name="graph-agent",
                    skill_name="chart",
                    args_json="{}",
                    scopes={},
                    status="complete",
                    file_ops=[
                        {"op": "create", "path": f"outputs/{idx}.txt", "size": idx}
                        for idx in range(3)
                    ],
                )
            )
            await session.commit()

            rows = await list_subagent_runs(
                user=user,
                session=session,
                agent=None,
                thread_id=None,
                limit=50,
            )
            detail = await get_subagent_run("invoke-files", user=user, session=session)

            assert len(rows) == 1
            assert rows[0].file_ops == []
            assert rows[0].file_ops_count == 3
            assert len(detail.file_ops) == 3
            assert detail.file_ops_count == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_track_subagent_event_mirrors_started_completed_run_to_work_ledger() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            started = await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-ledger",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "args_json": '{"prompt":"sales"}',
                    "scopes": {},
                },
                user=user,
                session=session,
            )
            completed = await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-ledger",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "ok": True,
                    "summary": "path=outputs/chart.png",
                    "result": {"path": "outputs/chart.png"},
                    "elapsed_ms": 25,
                    "scopes": {},
                },
                user=user,
                session=session,
            )

            assert started.grant_id == "invoke-ledger"
            assert completed.grant_id == "invoke-ledger"

            job = (await session.execute(select(WorkJob))).scalar_one()
            assert job.job_id == "invoke-ledger"
            assert job.kind == "subagent_run"
            assert job.status == "complete"
            assert job.user_id == user.id
            assert job.worker_name == "graph-agent"
            assert job.subject_id == "invoke-ledger"
            assert job.input_payload["skill_name"] == "chart"
            assert job.output_payload == {"path": "outputs/chart.png"}
            assert job.summary == "path=outputs/chart.png"
            assert job.error is None
            assert job.started_at is not None
            assert job.completed_at is not None

            events = (
                await session.execute(select(WorkEvent).order_by(WorkEvent.event_seq))
            ).scalars().all()
            assert [event.event_type for event in events] == [
                "agent_invoke_started",
                "agent_invoke_complete",
            ]
            assert [event.status for event in events] == ["running", "complete"]
            assert events[0].payload["grant_id"] == "invoke-ledger"
            assert events[0].stage == "started"
            assert events[1].payload["result"] == {"path": "outputs/chart.png"}
            assert events[1].stage == "completed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_track_subagent_route_defers_ledger_side_effects(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            scheduled: list[tuple[dict, int]] = []

            def capture_schedule(tracked: dict, user_id: int) -> None:
                scheduled.append((tracked, user_id))

            monkeypatch.setattr(
                subagent_run_routes,
                "_schedule_tracked_event_side_effects",
                capture_schedule,
            )

            out = await subagent_run_routes.track_subagent_event_route(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-deferred-ledger",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "args_json": '{"prompt":"sales"}',
                    "scopes": {},
                },
                user=user,
                session=session,
            )

            assert out.grant_id == "invoke-deferred-ledger"
            assert scheduled == [(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-deferred-ledger",
                    "to": "graph-agent",
                    "agent": "graph-agent",
                    "skill": "chart",
                    "args_json": '{"prompt":"sales"}',
                    "scopes": {},
                },
                user.id,
            )]
            assert (
                await session.execute(
                    select(SubagentRun).where(
                        SubagentRun.grant_id == "invoke-deferred-ledger"
                    )
                )
            ).scalar_one()
            assert (await session.execute(select(WorkJob))).scalars().all() == []

            await subagent_run_routes._apply_tracked_event_side_effects(
                scheduled[0][0],
                user=user,
                session=session,
            )

            job = (await session.execute(select(WorkJob))).scalar_one()
            assert job.job_id == "invoke-deferred-ledger"
            assert job.kind == "subagent_run"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deferred_tracking_side_effects_preserve_grant_order(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    first_started = asyncio.Event()
    finish_first = asyncio.Event()

    async def capture_apply(tracked: dict, user_id: int) -> None:
        event_type = str(tracked.get("type"))
        calls.append(("start", event_type))
        if event_type == "agent_invoke_started":
            first_started.set()
            await finish_first.wait()
        calls.append(("done", event_type))

    monkeypatch.setattr(
        subagent_run_routes,
        "_apply_tracked_event_side_effects_for_user",
        capture_apply,
    )
    try:
        subagent_run_routes._schedule_tracked_event_side_effects(
            {"type": "agent_invoke_started", "grant_id": "grant-order"},
            user_id=1,
        )
        first_task = subagent_run_routes._TRACKING_SIDE_EFFECT_CHAINS["grant-order"]
        await first_started.wait()

        subagent_run_routes._schedule_tracked_event_side_effects(
            {"type": "agent_invoke_complete", "grant_id": "grant-order"},
            user_id=1,
        )
        second_task = subagent_run_routes._TRACKING_SIDE_EFFECT_CHAINS["grant-order"]
        await asyncio.sleep(0)
        assert calls == [("start", "agent_invoke_started")]

        finish_first.set()
        await asyncio.gather(first_task, second_task)

        assert calls == [
            ("start", "agent_invoke_started"),
            ("done", "agent_invoke_started"),
            ("start", "agent_invoke_complete"),
            ("done", "agent_invoke_complete"),
        ]
    finally:
        subagent_run_routes._TRACKING_SIDE_EFFECT_TASKS.clear()
        subagent_run_routes._TRACKING_SIDE_EFFECT_CHAINS.pop("grant-order", None)


@pytest.mark.asyncio
async def test_close_stale_subagent_runs_marks_running_run_terminal() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            old = datetime.now(timezone.utc) - timedelta(hours=2)
            run = SubagentRun(
                grant_id="stale-grant",
                user_id=user.id,
                thread_id=None,
                agent_name="agent-builder",
                skill_name="build",
                args_json='{"name":"demo"}',
                scopes={},
                status="running",
                created_at=old,
                updated_at=old,
            )
            job = WorkJob(
                job_id="stale-grant",
                kind="subagent_run",
                status="running",
                queue="subagents",
                user_id=user.id,
                input_payload={"grant_id": "stale-grant"},
            )
            session.add_all([user, run, job])
            await session.commit()

            closed = await close_stale_subagent_runs(
                session,
                stale_after_seconds=60,
            )

            assert closed == 1
            await session.refresh(run)
            await session.refresh(job)
            assert run.status == "stale"
            assert run.completed_at is not None
            assert job.status == "stale"
            assert job.completed_at is not None
            event = (await session.execute(select(WorkEvent))).scalar_one()
            assert event.event_type == "agent_invoke_stale"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subagent_stale_closer_worker_closes_inactive_runs(monkeypatch) -> None:
    monkeypatch.setattr(stale_closer_worker.settings, "subagent_stale_after_seconds", 60)
    monkeypatch.setattr(stale_closer_worker.settings, "subagent_stale_batch_size", 100)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            old = datetime.now(timezone.utc) - timedelta(hours=2)
            fresh = datetime.now(timezone.utc)
            stale = SubagentRun(
                grant_id="stale-worker-grant",
                user_id=user.id,
                agent_name="agent-builder",
                skill_name="build",
                args_json="{}",
                scopes={},
                status="running",
                created_at=old,
                updated_at=old,
            )
            active = SubagentRun(
                grant_id="fresh-worker-grant",
                user_id=user.id,
                agent_name="agent-studio",
                skill_name="create_agent",
                args_json="{}",
                scopes={},
                status="running",
                created_at=fresh,
                updated_at=fresh,
            )
            session.add_all([user, stale, active])
            await session.commit()

            closed = await stale_closer_worker.SubagentStaleCloserWorker().run_once(session)

            assert closed == 1
            await session.refresh(stale)
            await session.refresh(active)
            assert stale.status == "stale"
            assert stale.completed_at is not None
            assert active.status == "running"
            assert active.completed_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_track_subagent_event_records_llm_usage() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-llm",
                    "thread_id": "thread-1",
                    "to": "tasks-api",
                    "agent": "tasks-api",
                    "skill": "auto",
                    "args_json": '{"goal":"summarize tasks"}',
                    "scopes": {},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-llm",
                    "to": "tasks-api",
                    "agent": "tasks-api",
                    "skill": "auto",
                    "ok": True,
                    "summary": "ok",
                    "llm_usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 8,
                        "total_tokens": 20,
                        "cost_usd": 0.003,
                        "model": "gpt-5",
                        "provider": "openai",
                        "metadata": {"call_count": 1},
                    },
                    "elapsed_ms": 55,
                    "source": "agent_server",
                },
                user=user,
                session=session,
            )

            usage = (await session.execute(select(LLMUsageEvent))).scalar_one()
            assert usage.user_id == user.id
            assert usage.thread_id == "thread-1"
            assert usage.grant_id == "invoke-llm"
            assert usage.agent_name == "tasks-api"
            assert usage.skill_name == "auto"
            assert usage.source == "litellm_subagent"
            assert usage.model == "gpt-5"
            assert usage.provider == "openai"
            assert usage.prompt_tokens == 12
            assert usage.completion_tokens == 8
            assert usage.total_tokens == 20
            assert usage.cost_usd == 0.003
            assert usage.metadata_json["call_count"] == 1
            assert usage.metadata_json["subagent_event_type"] == "agent_invoke_complete"
            assert usage.metadata_json["tracking_source"] == "agent_server"
            assert usage.metadata_json["elapsed_ms"] == 55
            assert usage.metadata_json["usage_projection"] == "litellm_response"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_track_subagent_event_preserves_a2a_auth_required_exchange_parts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-auth",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "args_json": '{"prompt":"approve"}',
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-auth",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "ok": True,
                    "result": {
                        "task": {
                            "id": "task-1",
                            "contextId": "ctx-1",
                            "status": {
                                "state": "TASK_STATE_AUTH_REQUIRED",
                                "message": {
                                    "messageId": "auth-msg",
                                    "role": "ROLE_AGENT",
                                    "parts": [{"text": "OAuth token required"}],
                                },
                            },
                            "history": [
                                {
                                    "messageId": "upload-msg",
                                    "role": "ROLE_USER",
                                    "parts": [
                                        {
                                            "raw": "aW1hZ2U=",
                                            "filename": "input.png",
                                            "mediaType": "image/png",
                                        },
                                        {
                                            "data": [{"ticketNumber": "REQ1"}],
                                            "metadata": {
                                                "mediaType": "application/json",
                                                "schema": {
                                                    "type": "array",
                                                    "items": {"type": "object"},
                                                },
                                            },
                                        },
                                    ],
                                }
                            ],
                            "artifacts": [
                                {
                                    "artifactId": "artifact-1",
                                    "name": "processed.png",
                                    "parts": [
                                        {
                                            "url": "https://storage.example/out.png",
                                            "filename": "out.png",
                                            "mediaType": "image/png",
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                },
                user=user,
                session=session,
            )

            run = await get_subagent_run("invoke-auth", user=user, session=session)
            complete_event = run.events[-1].payload
            a2a = complete_event["a2a"]

            assert run.status == "auth_required"
            assert run.error is None
            assert run.completed_at is None
            assert a2a["task_state"] == "TASK_STATE_AUTH_REQUIRED"
            assert a2a["task_status"] == "auth_required"
            assert a2a["task_id"] == "task-1"
            assert a2a["context_id"] == "ctx-1"
            assert a2a["status_message"] == "OAuth token required"
            assert len(a2a["artifacts"]) == 1
            assert a2a["artifacts"][0]["artifactId"] == "artifact-1"
            assert {part.get("filename") for part in a2a["file_parts"]} == {
                "input.png",
                "out.png",
            }
            assert a2a["data_parts"][0]["data"] == [{"ticketNumber": "REQ1"}]

            with pytest.raises(HTTPException) as exc_info:
                await rerun_subagent_run("invoke-auth", user=user, session=session)
            assert exc_info.value.status_code == 409
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_subagent_run_response_surfaces_error_detail() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-error",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "args_json": "{}",
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_error",
                    "grant_id": "invoke-error",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "ok": False,
                    "summary": "validation failed",
                    "error": "missing ticket number",
                },
                user=user,
                session=session,
            )

            run = await get_subagent_run("invoke-error", user=user, session=session)

            assert run.status == "error"
            assert run.error == "missing ticket number"
            assert run.summary == "validation failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerun_records_early_handoff_error_without_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def fake_get_or_create_policy(_session, _user_id):
            return SimpleNamespace()

        async def fake_budget_check(*_args, **_kwargs):
            return None

        class FakeCallAgent:
            name = "call_agent"

            async def ainvoke(self, payload):
                return (
                    '{"error":"policy denied handoff: agent is not on the approved list",'
                    '"agent":"ticket-agent","skill":"review_ticket"}'
                )

        monkeypatch.setattr(
            subagent_run_routes,
            "get_or_create_policy",
            fake_get_or_create_policy,
        )
        monkeypatch.setattr(
            subagent_run_routes,
            "assert_monthly_budget_allows_start",
            fake_budget_check,
        )
        monkeypatch.setattr(subagent_run_routes, "policy_dict", lambda _policy: {})
        monkeypatch.setattr(
            handoff_tools,
            "build_handoff_tools",
            lambda _ctx: [FakeCallAgent()],
        )

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            session.add(
                SubagentRun(
                    grant_id="invoke-error",
                    user_id=1,
                    agent_name="ticket-agent",
                    skill_name="review_ticket",
                    args_json="{}",
                    scopes={"bucket": "user-1-files"},
                    status="error",
                    summary="previous failure",
                )
            )
            await session.commit()

            rerun = await rerun_subagent_run(
                "invoke-error",
                user=user,
                session=session,
                authorization=None,
                session_cookie=None,
            )

            assert rerun.grant_id.startswith("rerun-")
            assert rerun.rerun_of_grant_id == "invoke-error"
            assert rerun.status == "error"
            assert rerun.error == "policy denied handoff: agent is not on the approved list"
            assert rerun.events[-1].event_type == "agent_invoke_error"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_track_subagent_event_marks_completed_a2a_task_terminal() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="caller@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            await track_subagent_event(
                {
                    "type": "agent_invoke_started",
                    "grant_id": "invoke-complete",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "args_json": "{}",
                    "scopes": {"bucket": "user-1-files"},
                },
                user=user,
                session=session,
            )
            await track_subagent_event(
                {
                    "type": "agent_invoke_complete",
                    "grant_id": "invoke-complete",
                    "to": "ticket-agent",
                    "agent": "ticket-agent",
                    "skill": "review_ticket",
                    "ok": True,
                    "summary": "completed",
                    "result": {
                        "task": {
                            "id": "task-2",
                            "status": {"state": "TASK_STATE_COMPLETED"},
                        }
                    },
                },
                user=user,
                session=session,
            )

            run = await get_subagent_run("invoke-complete", user=user, session=session)

            assert run.status == "complete"
            assert run.completed_at is not None
            assert run.events[-1].payload["a2a"]["task_state"] == "TASK_STATE_COMPLETED"
    finally:
        await engine.dispose()


def test_handoff_summary_understands_latest_a2a_task_results() -> None:
    assert _summarize_result({
        "task": {
            "status": {
                "state": "TASK_STATE_AUTH_REQUIRED",
                "message": {
                    "parts": [{"text": "Approve OAuth access"}],
                },
            }
        }
    }) == "auth required: Approve OAuth access"
    assert _summarize_result({
        "task": {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "a1"}],
        }
    }) == "completed, 1 artifact"


def test_handoff_llm_creds_normalizes_null_extra_body() -> None:
    assert _handoff_llm_creds({
        "base_url": "http://litellm:4000/v1",
        "api_key": "platform-key",
        "model": "a2a-user-2-kimi",
        "source": "caller",
        "litellm_model_alias": "a2a-user-2-kimi",
        "temperature_mode": "omit",
        "temperature": None,
        "extra_body": None,
    }) == {
        "base_url": "http://litellm:4000/v1",
        "api_key": "platform-key",
        "model": "a2a-user-2-kimi",
        "temperature_mode": "omit",
        "temperature": None,
        "extra_body": {},
        "metadata": {},
    }
