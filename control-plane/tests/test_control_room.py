from __future__ import annotations

from datetime import datetime, timezone
import os

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import ChatThread, LLMUsageEvent, SubagentRun, User, WorkJob
from control_plane.litellm_usage_reconciler import reconcile_litellm_usage_for_thread
import control_plane.litellm_usage_reconciler as litellm_usage_reconciler
from control_plane.routes.control_room import (
    ControlPolicyIn,
    get_control_room,
    get_receipt,
    update_control_policy,
)
from control_plane.control_room import normalize_llm_usage_payload, record_llm_usage
from control_plane.work_ledger import append_event, create_job


def test_normalize_llm_usage_prefers_litellm_positive_cost() -> None:
    normalized = normalize_llm_usage_payload(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 0,
            "metadata": {},
        },
        response_metadata={
            "request_id": "req-1",
            "cost_breakdown": {"total_cost": 0.025},
        },
    )

    assert normalized is not None
    assert normalized["cost_usd"] == 0.025
    assert normalized["metadata"]["litellm_request_id"] == "req-1"
    assert normalized["metadata"]["cost_source"] == "litellm_cost_breakdown"


@pytest.mark.asyncio
async def test_control_room_aggregates_policy_spend_and_receipts() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="ops@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Run agents")
            run = SubagentRun(
                grant_id="grant-1",
                user_id=1,
                thread_id="thread-1",
                agent_name="graph-agent",
                skill_name="chart",
                args_json='{"title":"Q3"}',
                scopes={"bucket": "user-1-files", "mode": "read_write_overlay"},
                status="complete",
                summary="path=outputs/chart.png",
                file_ops=[
                    {
                        "op": "create",
                        "path": "outputs/chart.png",
                        "size": 120,
                        "content_type": "image/png",
                    }
                ],
            )
            usage = LLMUsageEvent(
                user_id=1,
                thread_id="thread-1",
                source="control_plane_chat",
                model="gpt-test",
                prompt_tokens=10,
                completion_tokens=15,
                total_tokens=25,
                cost_usd=0.12,
            )
            session.add_all([user, thread, run, usage])
            await session.commit()
            loop_job = await create_job(
                session,
                user_id=1,
                kind="adversarial_review_loop",
                payload={
                    "target_agent": "graph-agent",
                    "findings_only_required": True,
                    "active_apply_enabled": False,
                    "loop_limits": {"ttl_seconds": 900, "max_iterations": 3},
                },
                title="Adversarial review loop: graph-agent",
                metadata={
                    "template_ref": "adversarial_review_loop@v1",
                    "reviewer_agent_name": "agent-reviewer",
                    "kill_switch_available": True,
                    "active_apply_enabled": False,
                    "budget_ceiling_cents": 500,
                },
                job_id="arl-control-1",
                status="queued",
                subject_type="agent",
                subject_id="graph-agent",
                worker_type="agent",
                worker_name="agent-reviewer",
                record_event=False,
            )
            await append_event(
                session,
                loop_job,
                event_type="loop_created",
                payload={"target_agent": "graph-agent", "active_apply_enabled": False},
                status="queued",
                user_id=1,
            )
            sim_job = await create_job(
                session,
                user_id=1,
                kind="protocol_simulation",
                payload={
                    "target_agent": "graph-agent",
                    "protocol_ref": {
                        "id": "market",
                        "version": 1,
                        "display_name": "Market",
                        "template_refs": ["market@v1"],
                    },
                    "limits": {"ttl_seconds": 900, "max_episodes": 2},
                    "simulation_only": True,
                    "proposal_only": True,
                    "active_apply_enabled": False,
                },
                title="Protocol simulation: Market / graph-agent",
                metadata={
                    "protocol_ref": {
                        "id": "market",
                        "version": 1,
                        "display_name": "Market",
                        "template_refs": ["market@v1"],
                    },
                    "template_ref": "market@v1",
                    "simulation_only": True,
                    "proposal_only": True,
                    "active_apply_enabled": False,
                    "kill_switch_available": True,
                    "run_budget_cents": 200,
                    "ttl_seconds": 900,
                    "max_episodes": 2,
                },
                job_id="psim-control-1",
                status="running",
                subject_type="agent",
                subject_id="graph-agent",
                worker_type="simulator",
                worker_name="market@v1",
                record_event=False,
            )
            await append_event(
                session,
                sim_job,
                event_type="invariant_checked",
                payload={
                    "invariant": "market rank cannot grant authority",
                    "active_apply_enabled": False,
                },
                status="running",
                user_id=1,
            )
            await append_event(
                session,
                sim_job,
                event_type="scenario_trace_recorded",
                payload={
                    "passed": True,
                    "trace_summary": {
                        "scenario_count": 1,
                        "scenario_ids": ["s1_route_weight"],
                        "event_count": 6,
                        "invariant_pass_count": 2,
                        "invariant_fail_count": 0,
                        "replay_pass_count": 1,
                        "alert_count": 0,
                        "alerts": [],
                        "violation_count": 0,
                        "violations": [],
                        "passed": True,
                        "active_apply_enabled": False,
                    },
                    "active_apply_enabled": False,
                },
                status="running",
                user_id=1,
            )

            policy = await update_control_policy(
                ControlPolicyIn(
                    monthly_budget_cents=1234,
                    run_budget_cents=250,
                    max_agent_calls_per_run=3,
                    deny_external_network=True,
                    approved_agents=["graph-agent"],
                ),
                user=user,
                session=session,
            )
            room = await get_control_room(user=user, session=session, limit=20)
            receipt = await get_receipt(
                "subagent",
                "grant-1",
                user=user,
                session=session,
            )

            assert policy.monthly_budget_cents == 1234
            assert policy.deny_external_network is True
            assert policy.approved_agents == ["graph-agent"]
            assert room.summary.monthly_spend_cents == 12
            assert room.summary.llm_tokens_month == 25
            assert room.summary.agent_runs == 1
            assert any(item.id == "subagent:grant-1" for item in room.timeline)
            assert any(item.id.startswith("llm:") for item in room.timeline)
            assert any(item.id == "review_loop:arl-control-1" for item in room.timeline)
            assert any(item.id == "protocol_simulation:psim-control-1" for item in room.timeline)
            sim_item = next(item for item in room.timeline if item.id == "protocol_simulation:psim-control-1")
            assert sim_item.metadata["trace_summary"]["scenario_count"] == 1
            assert sim_item.metadata["trace_summary"]["invariant_fail_count"] == 0
            assert receipt.subject == "graph-agent.chart"
            assert receipt.costs["total_tokens"] == 25
            assert receipt.file_ops[0]["path"] == "outputs/chart.png"
            loop_receipt = await get_receipt(
                "review_loop",
                "arl-control-1",
                user=user,
                session=session,
            )
            assert loop_receipt.subject == "graph-agent"
            assert loop_receipt.metadata["active_apply_enabled"] is False
            assert loop_receipt.events[0]["type"] == "loop_created"
            sim_receipt = await get_receipt(
                "protocol_simulation",
                "psim-control-1",
                user=user,
                session=session,
            )
            assert sim_receipt.subject == "graph-agent"
            assert sim_receipt.metadata["protocol_ref"]["id"] == "market"
            assert sim_receipt.metadata["active_apply_enabled"] is False
            assert sim_receipt.events[0]["type"] == "invariant_checked"
            assert sim_receipt.events[1]["type"] == "scenario_trace_recorded"
            assert sim_receipt.metadata["trace_summary"]["scenario_ids"] == ["s1_route_weight"]

            policy = await update_control_policy(
                ControlPolicyIn(
                    approved_agents=[" graph-agent ", "graph-agent", "chart-bot"],
                ),
                user=user,
                session=session,
            )
            assert policy.approved_agents == ["graph-agent", "chart-bot"]

            with pytest.raises(HTTPException) as budget_exc:
                await update_control_policy(
                    ControlPolicyIn(
                        monthly_budget_cents=100,
                        run_budget_cents=101,
                    ),
                    user=user,
                    session=session,
                )
            assert budget_exc.value.status_code == 400

            with pytest.raises(HTTPException) as receipt_exc:
                await get_receipt(
                    "llm",
                    "not-an-id",
                    user=user,
                    session=session,
                )
            assert receipt_exc.value.status_code == 404

            with pytest.raises(HTTPException) as sim_receipt_exc:
                await get_receipt(
                    "protocol_simulation",
                    "psim-missing",
                    user=user,
                    session=session,
                )
            assert sim_receipt_exc.value.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_llm_usage_mirrors_to_work_ledger() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(id=1, email="ops@example.com", password_hash="x")
            thread = ChatThread(id="thread-1", user_id=1, title="Run agents")
            session.add_all([user, thread])
            await session.commit()

            row = await record_llm_usage(
                session,
                user_id=1,
                thread_id="thread-1",
                source="control_plane_chat",
                usage={
                    "model": "mercury-2",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd": 0.02,
                },
            )

            assert row is not None
            job = (
                await session.execute(
                    select(WorkJob).where(WorkJob.job_id == f"llm-{row.id}")
                )
            ).scalar_one()
            assert job.kind == "llm"
            assert job.status == "complete"
            assert job.user_id == 1
            assert job.thread_id == "thread-1"
            assert job.worker_name == "mercury-2"
            assert job.output_payload["total_tokens"] == 15

            direct_row = await record_llm_usage(
                session,
                user_id=1,
                thread_id="thread-1",
                source="control_plane_chat_user_llm",
                usage={
                    "model": "kimi-k2.5",
                    "provider": "moonshot",
                    "metadata": {"status": "complete", "llm_source": "user"},
                },
            )
            assert direct_row is not None
            direct_job = (
                await session.execute(
                    select(WorkJob).where(WorkJob.job_id == f"llm-{direct_row.id}")
                )
            ).scalar_one()
            assert direct_job.kind == "llm"
            assert direct_job.status == "complete"
            assert direct_job.worker_name == "kimi-k2.5"
            assert direct_job.output_payload["total_tokens"] == 0
            assert direct_job.output_payload["status"] == "complete"

            failed_row = await record_llm_usage(
                session,
                user_id=1,
                thread_id="thread-1",
                source="control_plane_chat_user_llm",
                usage={
                    "model": "kimi-k2.5",
                    "provider": "moonshot",
                    "metadata": {
                        "status": "error",
                        "llm_source": "user",
                        "error": "thinking is enabled but reasoning_content is missing",
                    },
                },
            )
            assert failed_row is not None
            failed_job = (
                await session.execute(
                    select(WorkJob).where(WorkJob.job_id == f"llm-{failed_row.id}")
                )
            ).scalar_one()
            assert failed_job.kind == "llm"
            assert failed_job.status == "error"
            assert "reasoning_content" in failed_job.error
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_reconciler_updates_matching_zero_cost_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_spend_logs(
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        return [
            {
                "request_id": "req-litellm-1",
                "metadata": {"session_id": "thread-1", "a2a_user_id": 1},
                "model": "openai/gpt-5",
                "model_group": "a2a-user-1-default",
                "custom_llm_provider": "openai",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "spend": 0.031,
                "status": "success",
            }
        ]

    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_usage_reconcile_enabled", True)
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_url", "http://litellm.test")
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_key", "sk-test")
    monkeypatch.setattr(litellm_usage_reconciler, "_fetch_spend_logs", fake_fetch_spend_logs)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            session.add_all([
                User(id=1, email="ops@example.com", password_hash="x"),
                ChatThread(id="thread-1", user_id=1, title="Run agents"),
                LLMUsageEvent(
                    user_id=1,
                    thread_id="thread-1",
                    source="control_plane_chat",
                    model="gpt-5",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost_usd=0,
                ),
            ])
            await session.commit()

            changed = await reconcile_litellm_usage_for_thread(
                session,
                user_id=1,
                thread_id="thread-1",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )

            rows = (
                await session.execute(
                    select(LLMUsageEvent).where(LLMUsageEvent.thread_id == "thread-1")
                )
            ).scalars().all()
            assert changed == 1
            assert len(rows) == 1
            assert rows[0].cost_usd == pytest.approx(0.031)
            assert rows[0].metadata_json["litellm_request_id"] == "req-litellm-1"
            assert rows[0].metadata_json["litellm_reconciled"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_reconciler_matches_unique_unmetadataed_spend_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_spend_logs(
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        return [
            {
                "request_id": "req-litellm-unmetadataed",
                "metadata": {},
                "model": "openai/gpt-5",
                "model_group": "a2a-user-1-default",
                "custom_llm_provider": "openai",
                "prompt_tokens": 7331,
                "completion_tokens": 74,
                "total_tokens": 7405,
                "spend": 0.00169575,
                "status": "success",
            }
        ]

    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_usage_reconcile_enabled", True)
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_url", "http://litellm.test")
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_key", "sk-test")
    monkeypatch.setattr(litellm_usage_reconciler, "_fetch_spend_logs", fake_fetch_spend_logs)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            session.add_all([
                User(id=1, email="ops@example.com", password_hash="x"),
                ChatThread(id="thread-1", user_id=1, title="Run agents"),
                LLMUsageEvent(
                    user_id=1,
                    thread_id="thread-1",
                    source="control_plane_chat",
                    model="gpt-5",
                    prompt_tokens=7331,
                    completion_tokens=74,
                    total_tokens=7405,
                    cost_usd=0,
                ),
            ])
            await session.commit()

            changed = await reconcile_litellm_usage_for_thread(
                session,
                user_id=1,
                thread_id="thread-1",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )

            row = (
                await session.execute(
                    select(LLMUsageEvent).where(LLMUsageEvent.thread_id == "thread-1")
                )
            ).scalar_one()
            assert changed == 1
            assert row.cost_usd == pytest.approx(0.00169575)
            assert row.metadata_json["litellm_request_id"] == "req-litellm-unmetadataed"
            assert row.metadata_json["litellm_reconciled"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_litellm_reconciler_retries_for_delayed_spend_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_fetch_spend_logs(
        *,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        return [
            {
                "request_id": "req-litellm-delayed",
                "metadata": {},
                "model": "openai/gpt-5",
                "model_group": "a2a-user-1-default",
                "custom_llm_provider": "openai",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "spend": 0.031,
                "status": "success",
            }
        ]

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_usage_reconcile_enabled", True)
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_url", "http://litellm.test")
    monkeypatch.setattr(litellm_usage_reconciler.settings, "litellm_key", "sk-test")
    monkeypatch.setattr(litellm_usage_reconciler, "_fetch_spend_logs", fake_fetch_spend_logs)
    monkeypatch.setattr(litellm_usage_reconciler.asyncio, "sleep", fake_sleep)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            session.add_all([
                User(id=1, email="ops@example.com", password_hash="x"),
                ChatThread(id="thread-1", user_id=1, title="Run agents"),
                LLMUsageEvent(
                    user_id=1,
                    thread_id="thread-1",
                    source="control_plane_chat",
                    model="gpt-5",
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost_usd=0,
                ),
            ])
            await session.commit()

            changed = await reconcile_litellm_usage_for_thread(
                session,
                user_id=1,
                thread_id="thread-1",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )

            row = (
                await session.execute(
                    select(LLMUsageEvent).where(LLMUsageEvent.thread_id == "thread-1")
                )
            ).scalar_one()
            assert calls == 2
            assert changed == 1
            assert row.cost_usd == pytest.approx(0.031)
            assert row.metadata_json["litellm_request_id"] == "req-litellm-delayed"
    finally:
        await engine.dispose()
