from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane import self_healing, template_updates
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentCodeEditorOptIn,
    AgentDeployment,
    AgentReceipt,
    User,
    WorkEvent,
    WorkJob,
)
from control_plane.routes import agents as agent_routes


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_agent(session: AsyncSession, *, enabled: bool = True) -> Agent:
    user = User(email="healing-owner@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name="healing-agent",
        description="Healing test agent",
        version="0.1.0",
        image="registry.example.com/agents/healing-agent:latest",
        public=True,
        status="running",
        url="https://healing-agent.example.com",
        card={
            "version": "0.1.0",
            "capabilities": {
                "self_healing": {
                    "enabled": enabled,
                    "consecutive_failures": 1,
                    "window_seconds": 300,
                    "cooldown_seconds": 60,
                    "max_repairs_per_day": 3,
                    "max_turns": 12,
                    "deployment_timeout_seconds": 60,
                    "require_tests": True,
                }
            },
            "skills": [{"name": "explode"}],
        },
        gitea_owner="a2a-test",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _receipt(session: AsyncSession, agent: Agent, *, error_type: str) -> AgentReceipt:
    now = datetime.now(timezone.utc)
    row = AgentReceipt(
        receipt_id=f"rcpt-{error_type}",
        agent_id=agent.id,
        agent_name=agent.name,
        agent_version=agent.version,
        caller="user:test",
        task_id="",
        skill_name="explode",
        status="error",
        started_at=now,
        ended_at=now,
        signed_token="signed",
        payload={"error_type": error_type},
    )
    session.add(row)
    await session.commit()
    return row


@pytest.mark.asyncio
async def test_manifest_opt_in_queues_sanitized_runtime_repair(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        receipt = await _receipt(session, agent, error_type="HTTP500")
        job = await self_healing.maybe_enqueue_runtime_failure(
            session,
            agent_id=agent.id,
            receipt_id=receipt.receipt_id,
            skill_name="explode",
            error_type="HTTP500",
            error_preview="boom bearer sk-super-secret-value-123456",
        )

    assert job is not None
    assert job.kind == self_healing.SELF_HEALING_JOB_KIND
    assert job.input_payload["failure"]["error_preview"] == "boom [redacted]"
    assert job.input_payload["policy"]["max_repairs_per_day"] == 3


@pytest.mark.asyncio
async def test_non_actionable_or_disabled_failure_does_not_queue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session, enabled=False)
        receipt = await _receipt(session, agent, error_type="HTTP400")
        job = await self_healing.maybe_enqueue_runtime_failure(
            session,
            agent_id=agent.id,
            receipt_id=receipt.receipt_id,
            skill_name="explode",
            error_type="HTTP400",
            error_preview="invalid user input",
        )

    assert job is None


@pytest.mark.asyncio
async def test_worker_records_patch_and_only_completes_exact_live_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = "a" * 40
    after = "b" * 40
    monkeypatch.setattr(self_healing, "repo_head_sha", lambda *a, **kw: before)
    monkeypatch.setattr(
        self_healing,
        "source_changed_paths_since",
        lambda *a, **kw: ["agent.py", "tests/test_agent.py"],
    )
    monkeypatch.setattr(template_updates, "repo_exists", lambda *a, **kw: True)

    async def fake_runner(
        session: AsyncSession,
        user: User,
        agent: Agent,
        job: WorkJob,
        target_agent: str,
        skill: str,
        args: dict[str, object],
    ) -> dict[str, object]:
        assert target_agent == "code-editor-agent"
        assert skill == "turn"
        assert args["push_on_failure"] is False
        assert "Sanitized failure evidence" in str(args["prompt"])
        return {
            "ok": True,
            "result": {
                "ok": True,
                "agent_name": agent.name,
                "sync": {"head_sha": before},
                "changes": {"dirty": True, "files": ["agent.py", "tests/test_agent.py"]},
                "push": {"ok": True, "attempted": True, "head_sha": after},
                "parsed": {
                    "root_cause": "uncaught None",
                    "tests": {"command": "pytest", "status": "passed"},
                },
            },
        }

    async with session_factory() as session:
        agent = await _seed_agent(session)
        receipt = await _receipt(session, agent, error_type="HTTP500")
        job = await self_healing.maybe_enqueue_runtime_failure(
            session,
            agent_id=agent.id,
            receipt_id=receipt.receipt_id,
            skill_name="explode",
            error_type="HTTP500",
            error_preview="uncaught None",
        )
        assert job is not None
        live = AgentDeployment(
            deploy_id="dpl_exact",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="live",
            head_sha=after,
            agent_url=agent.url,
            verification={"card": "passed", "runtime": "passed"},
        )
        session.add(live)
        await session.commit()

        async def exact_deploy(*_args: object, **_kwargs: object) -> AgentDeployment:
            return live

        monkeypatch.setattr(self_healing, "_wait_for_exact_deployment", exact_deploy)
        processed = await self_healing.SelfHealingWorker(fake_runner).run_once(session)
        await session.refresh(job)
        events = (
            await session.execute(select(WorkEvent).where(WorkEvent.job_id == job.job_id))
        ).scalars().all()
        deployment_job = (
            await session.execute(
                select(WorkJob).where(WorkJob.kind == "agent.source_push_deploy")
            )
        ).scalar_one()
        opt_in = (await session.execute(select(AgentCodeEditorOptIn))).scalar_one()
        owner = await session.get(User, agent.owner_id)
        assert owner is not None
        history = await agent_routes.list_agent_self_healing(
            agent.name,
            user=owner,
            session=session,
            limit=20,
        )

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["healed"] is True
    assert job.output_payload["source_head_before"] == before
    assert job.output_payload["source_head_after"] == after
    assert job.output_payload["changed_files"] == ["agent.py", "tests/test_agent.py"]
    assert job.output_payload["deployment"]["deploy_id"] == "dpl_exact"
    assert deployment_job.input_payload["source_sha"] == after
    assert deployment_job.input_payload["changed_paths"] == [
        "agent.py",
        "tests/test_agent.py",
    ]
    assert deployment_job.metadata_json["self_healing_job_id"] == job.job_id
    assert opt_in.status == "enabled"
    assert history["policy"]["enabled"] is True
    assert history["runs"][0]["result"]["healed"] is True
    assert history["runs"][0]["events"]
    assert {
        "self_healing_diagnosing",
        "self_healing_patching",
        "self_healing_patch_pushed",
        "self_healing_deployment_queued",
        "self_healing_deployment_verified",
        "self_healing_completed",
    }.issubset({event.event_type for event in events})


@pytest.mark.asyncio
async def test_exact_deployment_waiter_refreshes_only_the_deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = WorkJob(
            job_id="job_waiter_identity_map",
            kind=self_healing.SELF_HEALING_JOB_KIND,
            status="running",
            input_payload={"agent_id": agent.id},
        )
        deployment = AgentDeployment(
            deploy_id="dpl_waiter_exact",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="building",
            head_sha="c" * 40,
        )
        session.add_all([job, deployment])
        await session.commit()

        async with session_factory() as updater:
            await updater.execute(
                update(AgentDeployment)
                .where(AgentDeployment.id == deployment.id)
                .values(status="live")
            )
            await updater.commit()

        observed = await self_healing._wait_for_exact_deployment(
            session,
            agent=agent,
            head_sha="c" * 40,
            started_at=deployment.created_at,
            timeout_seconds=1,
        )

    assert observed.deploy_id == "dpl_waiter_exact"
    assert observed.status == "live"
    assert agent.name == "healing-agent"
    assert job.job_id == "job_waiter_identity_map"


@pytest.mark.asyncio
async def test_worker_reconciles_a_pushed_repair_that_is_already_live(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = "d" * 40
    after = "e" * 40
    async with session_factory() as session:
        agent = await _seed_agent(session)
        receipt = await _receipt(session, agent, error_type="HTTP500")
        job = await self_healing.maybe_enqueue_runtime_failure(
            session,
            agent_id=agent.id,
            receipt_id=receipt.receipt_id,
            skill_name="explode",
            error_type="HTTP500",
            error_preview="uncaught None",
        )
        assert job is not None
        await self_healing.append_event(
            session,
            job,
            event_type="self_healing_patch_pushed",
            payload={
                "source_head_before": before,
                "source_head_after": after,
                "changed_files": ["agent.py"],
                "test_evidence": {"result": "pytest: passed"},
            },
            status="running",
            commit=True,
        )
        job.status = "error"
        job.error = "worker bookkeeping failed after source push"
        deployment = AgentDeployment(
            deploy_id="dpl_reconciled_exact",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="live",
            head_sha=after,
            agent_url=agent.url,
            verification={"runtime": {"ok": True}},
        )
        session.add(deployment)
        await session.commit()

        processed = await self_healing.SelfHealingWorker().run_once(session)
        await session.refresh(job)
        events = (
            await session.execute(select(WorkEvent).where(WorkEvent.job_id == job.job_id))
        ).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.error is None
    assert job.output_payload["healed"] is True
    assert job.output_payload["reconciled_after_worker_error"] is True
    assert job.output_payload["deployment"]["deploy_id"] == "dpl_reconciled_exact"
    assert "self_healing_deployment_verified" in {event.event_type for event in events}
