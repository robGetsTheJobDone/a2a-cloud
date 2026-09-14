from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import IdempotencyRecord, User, WorkEvent, WorkJob
from control_plane.routes.work_ledger import (
    _activity_item,
    cancel_my_job,
    _serialize_event as route_serialize_event,
    _serialize_job as route_serialize_job,
    get_my_job,
    list_my_activity,
    list_my_job_events,
    CancelJobIn,
)
from control_plane.work_ledger import (
    IdempotencyConflict,
    append_event,
    cancel_job,
    complete_job,
    create_job,
    fail_job,
    get_idempotency_record,
    get_job,
    list_job_events,
    list_user_activity,
    serialize_event,
    serialize_idempotency_record,
    serialize_job,
)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()


async def _add_user(session: AsyncSession, user_id: int = 1) -> User:
    user = User(id=user_id, email=f"worker-{user_id}@example.com", password_hash="x")
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_create_job_records_event_and_idempotency_record() -> None:
    async with _session() as session:
        user = await _add_user(session, user_id=7)

        job = await create_job(
            session,
            user_id=user.id,
            kind="proof",
            payload={"claim": "ok"},
            title="Check proof",
            metadata={"priority_label": "urgent"},
            job_id="job-proof-1",
            queue="proofs",
            priority=5,
            idempotency_key="idem-proof-1",
        )

        assert isinstance(job, WorkJob)
        assert job.job_id == "job-proof-1"
        assert job.root_job_id == "job-proof-1"
        assert job.input_payload == {"claim": "ok"}
        assert job.metadata_json == {
            "priority_label": "urgent",
            "title": "Check proof",
        }

        assert await get_job(session, "job-proof-1", user_id=user.id) == job
        assert await get_job(session, job.id, user_id=user.id) == job
        assert await get_job(session, "job-proof-1", user_id=999) is None

        activity = await list_user_activity(
            session,
            user_id=user.id,
            kind="proof",
            status="queued",
        )
        assert [row.job_id for row in activity] == ["job-proof-1"]

        events = await list_job_events(session, job.job_id, user_id=user.id)
        assert len(events) == 1
        assert isinstance(events[0], WorkEvent)
        assert events[0].event_seq == 1
        assert events[0].event_type == "job_created"
        assert events[0].payload == {"kind": "proof", "payload": {"claim": "ok"}}

        record = await get_idempotency_record(
            session,
            key="idem-proof-1",
            user_id=user.id,
            scope="work_ledger",
        )
        assert isinstance(record, IdempotencyRecord)
        encoded_record = serialize_idempotency_record(record)
        assert encoded_record["key"] == "idem-proof-1"
        assert encoded_record["job_id"] == "job-proof-1"
        assert encoded_record["response"]["job"]["job_id"] == "job-proof-1"

        same_job = await create_job(
            session,
            user_id=user.id,
            kind="proof",
            payload={"claim": "ok"},
            title="Check proof",
            metadata={"priority_label": "urgent"},
            idempotency_key="idem-proof-1",
        )
        assert same_job.id == job.id

        jobs = (await session.execute(select(WorkJob))).scalars().all()
        assert [row.job_id for row in jobs] == ["job-proof-1"]
        assert len(await list_job_events(session, job.job_id)) == 1

        with pytest.raises(IdempotencyConflict):
            await create_job(
                session,
                user_id=user.id,
                kind="proof",
                payload={"claim": "changed"},
                title="Check proof",
                metadata={"priority_label": "urgent"},
                idempotency_key="idem-proof-1",
            )


@pytest.mark.asyncio
async def test_job_lifecycle_helpers_update_rows_and_append_ordered_events() -> None:
    async with _session() as session:
        user = await _add_user(session, user_id=8)
        job = await create_job(
            session,
            user_id=user.id,
            kind="dag",
            payload={"goal": "write report"},
            job_id="job-dag-1",
            status="running",
            record_event=False,
        )

        started = await append_event(
            session,
            job.job_id,
            event_type="node_started",
            event_id="evt-start",
            payload={"node": "research"},
            message="research started",
            status="running",
            stage="research",
            severity="debug",
            user_id=user.id,
        )
        completed = await complete_job(
            session,
            job.job_id,
            result={"path": "outputs/report.md"},
            summary="report written",
            user_id=user.id,
        )

        events = await list_job_events(session, job.job_id, user_id=user.id)
        assert [event.event_seq for event in events] == [1, 2]
        assert [event.event_type for event in events] == [
            "node_started",
            "job_completed",
        ]
        assert events[0].id == started.id

        started_out = serialize_event(started)
        assert started_out["event_id"] == "evt-start"
        assert started_out["job_id"] == "job-dag-1"
        assert started_out["event_type"] == "node_started"
        assert started_out["payload"] == {"node": "research"}
        assert started_out["created_at"].startswith(str(datetime.now().year))

        completed_out = serialize_job(completed)
        assert completed_out["job_id"] == "job-dag-1"
        assert completed_out["status"] == "complete"
        assert completed_out["summary"] == "report written"
        assert completed_out["result"] == {"path": "outputs/report.md"}
        assert completed_out["metadata"]["summary"] == "report written"

        failed = await create_job(
            session,
            user_id=user.id,
            kind="deploy",
            payload={"target": "staging"},
            job_id="job-deploy-1",
            record_event=False,
        )
        failed = await fail_job(
            session,
            failed,
            error="deploy failed",
            user_id=user.id,
        )
        failed_out = serialize_job(failed)
        assert failed_out["status"] == "error"
        assert failed_out["summary"] == "deploy failed"
        assert failed_out["error"] == "deploy failed"
        assert failed_out["error_payload"] == {"error": "deploy failed"}

        failure_events = await list_job_events(session, failed.job_id, user_id=user.id)
        assert len(failure_events) == 1
        assert failure_events[0].event_type == "job_failed"
        assert failure_events[0].payload["error"] == "deploy failed"


@pytest.mark.asyncio
async def test_cancel_job_marks_active_job_terminal_and_preserves_cancel() -> None:
    async with _session() as session:
        user = await _add_user(session, user_id=88)
        job = await create_job(
            session,
            user_id=user.id,
            kind="chat_orchestrator",
            payload={"thread_id": "thread-1"},
            job_id="job-cancel-1",
            status="running",
            record_event=False,
        )

        canceled = await cancel_job(
            session,
            job,
            reason="operator requested cancel",
            user_id=user.id,
        )
        assert canceled.status == "canceled"
        assert canceled.completed_at is not None
        assert canceled.output_payload == {
            "canceled": True,
            "reason": "operator requested cancel",
        }

        completed = await complete_job(
            session,
            job.job_id,
            result={"content": "late answer"},
            summary="late completion",
            user_id=user.id,
        )
        assert completed.status == "canceled"
        assert completed.output_payload == {
            "canceled": True,
            "reason": "operator requested cancel",
        }

        failed = await fail_job(
            session,
            job.job_id,
            error="late error",
            user_id=user.id,
        )
        assert failed.status == "canceled"

        events = await list_job_events(session, job.job_id, user_id=user.id)
        assert [event.event_type for event in events] == [
            "job_canceled",
            "job_completion_ignored_after_cancel",
            "job_failure_ignored_after_cancel",
        ]


@pytest.mark.asyncio
async def test_terminal_update_refreshes_stale_job_object_after_cancel() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as worker_session:
            user = await _add_user(worker_session, user_id=89)
            stale_job = await create_job(
                worker_session,
                user_id=user.id,
                kind="chat_orchestrator",
                payload={"thread_id": "thread-stale"},
                job_id="job-stale-cancel-1",
                status="running",
                record_event=False,
            )

            async with Session() as cancel_session:
                await cancel_job(
                    cancel_session,
                    stale_job.job_id,
                    reason="cancel in another session",
                    user_id=user.id,
                )

            assert stale_job.status == "running"
            completed = await complete_job(
                worker_session,
                stale_job,
                result={"content": "late answer"},
                summary="late completion",
                user_id=user.id,
                event_type="chat_orchestrator_completed",
            )

            assert completed.status == "canceled"
            assert completed.output_payload == {
                "canceled": True,
                "reason": "cancel in another session",
            }

            events = await list_job_events(
                worker_session,
                stale_job.job_id,
                user_id=user.id,
            )
            assert [event.event_type for event in events] == [
                "job_canceled",
                "job_completion_ignored_after_cancel",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_work_ledger_routes_serialize_models_and_page_events() -> None:
    async with _session() as session:
        user = await _add_user(session, user_id=9)
        job = await create_job(
            session,
            user_id=user.id,
            kind="dag",
            payload={"goal": "ship"},
            title="Ship plan",
            job_id="job-route-1",
            status="running",
        )
        followup = await append_event(
            session,
            job,
            event_type="node_completed",
            event_id="evt-route-2",
            payload={"node": "review"},
            message="review complete",
            status="running",
            user_id=user.id,
        )

        encoded_job = route_serialize_job(job)
        assert encoded_job["job_id"] == "job-route-1"
        assert encoded_job["kind"] == "dag"
        assert encoded_job["payload"] == {"goal": "ship"}
        assert encoded_job["title"] == "Ship plan"

        encoded_event = route_serialize_event(followup)
        assert encoded_event["event_id"] == "evt-route-2"
        assert encoded_event["event_type"] == "node_completed"
        assert encoded_event["payload"] == {"node": "review"}

        activity = await list_my_activity(
            user=user,
            session=session,
            source="dag",
            status="running",
            q="ship",
            cursor=None,
            limit=10,
        )
        assert activity["next_cursor"] is None
        assert len(activity["items"]) == 1
        assert activity["items"][0]["job_id"] == "job-route-1"
        assert activity["items"][0]["source"] == "dag"

        route_job = await get_my_job("job-route-1", user=user, session=session)
        assert route_job["job_id"] == "job-route-1"
        assert route_job["status"] == "running"

        first_page = await list_my_job_events(
            "job-route-1",
            user=user,
            session=session,
            cursor=None,
            limit=1,
        )
        assert len(first_page["events"]) == 1
        assert first_page["next_cursor"] == str(first_page["events"][0]["id"])

        second_page = await list_my_job_events(
            "job-route-1",
            user=user,
            session=session,
            cursor=first_page["next_cursor"],
            limit=10,
        )
        assert second_page["next_cursor"] is None
        assert [event["event_type"] for event in second_page["events"]] == [
            "node_completed",
        ]

        canceled = await cancel_my_job(
            "job-route-1",
            CancelJobIn(reason="route cancel"),
            user=user,
            session=session,
        )
        assert canceled["job_id"] == "job-route-1"
        assert canceled["status"] == "canceled"
        assert canceled["result"] == {"canceled": True, "reason": "route cancel"}

        dict_created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        dict_job = route_serialize_job(
            {"job_id": "dict-job", "kind": "manual", "created_at": dict_created_at}
        )
        assert dict_job["created_at"] == "2026-01-01T00:00:00+00:00"
        assert _activity_item(dict_job)["source"] == "manual"
