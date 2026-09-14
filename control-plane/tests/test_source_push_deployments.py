from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane import source_push_deployments as spd
from control_plane.db import Base
from control_plane.models import Agent, AgentDeployment, AgentDeploymentEvent, User, WorkJob


@pytest.fixture(autouse=True)
def disable_source_push_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spd.settings, "source_push_deploy_debounce_seconds", 0.0)
    monkeypatch.setattr(spd, "repo_head_sha", lambda *a, **kw: "a" * 40)


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


async def _seed_agent(session: AsyncSession) -> Agent:
    user = User(email="owner@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name="invoice-bot",
        description="Invoice bot",
        version="0.1.0",
        image="registry.a2acloud.io/agents/invoice-bot:latest",
        public=True,
        status="running",
        url="https://invoice-bot.a2acloud.io",
        card={},
        gitea_owner="a2a-acme",
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _seed_always_on_agent(session: AsyncSession) -> Agent:
    agent = await _seed_agent(session)
    agent.card = {"runtime": {"availability": "always_on"}}
    await session.commit()
    await session.refresh(agent)
    return agent


async def _enqueue(session: AsyncSession, agent: Agent, source_sha: str = "a" * 40) -> WorkJob:
    return await spd.enqueue_source_push_deploy_job(
        session,
        agent,
        owner="a2a-acme",
        repo=agent.name,
        source_sha=source_sha,
        changed_paths=["agent.py"],
        delivery_id="delivery-1",
        ref="refs/heads/main",
    )


def _patch_success(monkeypatch: pytest.MonkeyPatch, calls: dict[str, object]) -> None:
    from control_plane.routes import agents as agent_routes

    monkeypatch.setattr(
        spd,
        "repo_head_sha",
        lambda *a, **kw: str(calls.get("repo_head", "a" * 40)),
    )
    monkeypatch.setattr(
        spd,
        "ensure_repo",
        lambda *a, **kw: ("http://gitea/source.git", "http://gitea/source.git"),
    )
    monkeypatch.setattr(
        spd,
        "_read_entrypoint_from_repo",
        lambda repo_url, source_sha: "agent:InvoiceBot",
    )
    monkeypatch.setattr(
        spd,
        "source_tarball_from_repo",
        lambda *args, **kwargs: (
            b"managed-source",
            str(calls.get("repo_head", "a" * 40)),
        ),
    )
    monkeypatch.setattr(
        spd,
        "read_agent_database_declarations_from_tarball_bytes",
        lambda _bundle: list(calls.get("database_declarations_to_read", [])),
    )

    async def fake_reconcile_databases(
        session: AsyncSession,
        *,
        agent: Agent,
        user: User,
        declarations: list[object],
    ) -> SimpleNamespace:
        calls["database_reconcile"] = {
            "agent": agent.name,
            "user_id": user.id,
            "declarations": declarations,
        }
        return SimpleNamespace(
            requested=["invoice-data"] if declarations else [],
            removed=[],
        )

    monkeypatch.setattr(
        spd,
        "reconcile_agent_database_bindings",
        fake_reconcile_databases,
    )
    monkeypatch.setattr(
        agent_routes,
        "_ensure_runtime_repo",
        lambda name, description: ("http://gitea/runtime.git", "http://gitea/runtime.git"),
    )

    def fake_commit_runtime(**kwargs: object) -> str:
        calls["runtime"] = kwargs
        return "b" * 40

    monkeypatch.setattr(spd, "commit_and_push_runtime_from_repo", fake_commit_runtime)
    monkeypatch.setattr(
        spd,
        "enqueue_deploy_review",
        lambda app, **kwargs: calls.setdefault("review", kwargs) or "review-1",
    )


@pytest.mark.asyncio
async def test_worker_restamps_runtime_and_records_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    _patch_success(monkeypatch, calls)
    database_declaration = object()
    calls["database_declarations_to_read"] = [database_declaration]
    invalidated: list[str] = []

    async def fake_invalidate_agent_card(name: str) -> None:
        invalidated.append(name)

    monkeypatch.setattr(spd, "invalidate_agent_card", fake_invalidate_agent_card)

    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent)

        processed = await spd.SourcePushDeployWorker(
            SimpleNamespace(state=SimpleNamespace())
        ).run_once(session)
        await session.refresh(job)
        await session.refresh(agent)
        deploy = (await session.execute(select(AgentDeployment))).scalar_one()
        events = (
            await session.execute(
                select(AgentDeploymentEvent).order_by(AgentDeploymentEvent.id)
            )
        ).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["deploy_id"] == deploy.deploy_id
    assert deploy.trigger == "source_push"
    assert deploy.status == "building"
    assert deploy.head_sha == "a" * 40
    assert deploy.image == "registry.a2acloud.io/agents/invoice-bot:" + "a" * 40
    assert agent.status == "building"
    assert agent.image == "registry.a2acloud.io/agents/invoice-bot:" + "a" * 40
    assert [event.stage for event in events] == [
        "source",
        "database",
        "runtime",
        "build",
        "argo",
    ]
    assert invalidated == ["invoice-bot"]
    assert calls["runtime"] == {
        "name": "invoice-bot",
        "entrypoint": "agent:InvoiceBot",
        "source_repo_url": "http://gitea/source.git",
        "source_sha": "a" * 40,
        "push_url": "http://gitea/runtime.git",
        "image_tag": "a" * 40,
        "allow_always_on": False,
    }
    assert calls["review"]["ref"] == "a" * 40
    assert calls["review"]["deploy_id"] == deploy.deploy_id
    assert calls["database_reconcile"] == {
        "agent": "invoice-bot",
        "user_id": agent.owner_id,
        "declarations": [database_declaration],
    }


@pytest.mark.asyncio
async def test_worker_allows_always_on_for_preapproved_agent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    _patch_success(monkeypatch, calls)

    async with session_factory() as session:
        agent = await _seed_always_on_agent(session)
        await _enqueue(session, agent)

        processed = await spd.SourcePushDeployWorker(
            SimpleNamespace(state=SimpleNamespace())
        ).run_once(session)

    assert processed is True
    assert calls["runtime"]["allow_always_on"] is True


@pytest.mark.asyncio
async def test_enqueue_source_push_deploy_job_waits_for_repo_quiet_period(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spd.settings, "source_push_deploy_debounce_seconds", 15.0)

    async with session_factory() as session:
        agent = await _seed_agent(session)
        before = spd.utcnow()
        job = await _enqueue(session, agent)
        after = spd.utcnow()

    assert job.queued_at is not None
    queued_at = job.queued_at
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=before.tzinfo)
    assert before + timedelta(seconds=14) <= queued_at <= after + timedelta(seconds=16)
    assert job.summary == "Source push deployment waiting for repository changes to settle"


@pytest.mark.asyncio
async def test_worker_skips_stale_source_push_without_creating_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spd, "repo_head_sha", lambda *a, **kw: "b" * 40)

    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent, source_sha="a" * 40)

        processed = await spd.SourcePushDeployWorker().run_once(session)
        await session.refresh(job)
        deployments = (await session.execute(select(AgentDeployment))).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["skipped"] is True
    assert job.output_payload["reason"] == "stale_source_push"
    assert job.output_payload["observed_main_head_sha"] == "b" * 40
    assert deployments == []


@pytest.mark.asyncio
async def test_next_job_claim_marks_running_and_prevents_second_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        await _enqueue(session, agent)
        worker = spd.SourcePushDeployWorker()

        first = await worker._next_job(session)
        second = await worker._next_job(session)

    assert first is not None
    assert first.status == "running"
    assert first.attempt == 1
    assert first.started_at is not None
    assert first.leased_until is not None
    assert second is None


@pytest.mark.asyncio
async def test_next_job_requeues_expired_running_source_push_job(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent)
        job.status = "running"
        job.queued_at = spd.utcnow() - timedelta(minutes=10)
        job.heartbeat_at = spd.utcnow() - timedelta(minutes=10)
        job.leased_until = spd.utcnow() - timedelta(minutes=1)
        await session.commit()

        claimed = await spd.SourcePushDeployWorker()._next_job(session)

    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == "running"
    assert claimed.attempt == 1
    assert claimed.leased_until is not None


@pytest.mark.asyncio
async def test_worker_refreshes_due_active_source_push_deployment_without_job(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    async def fake_sync_deployment_verification(
        session: AsyncSession,
        agent: Agent,
        deploy: AgentDeployment,
        **_kwargs: object,
    ) -> AgentDeployment:
        calls["deploy_id"] = deploy.deploy_id
        calls["agent_name"] = agent.name
        deploy.status = "live"
        agent.status = "running"
        await session.commit()
        await session.refresh(deploy)
        return deploy

    monkeypatch.setattr(
        spd,
        "sync_deployment_verification",
        fake_sync_deployment_verification,
    )

    async with session_factory() as session:
        agent = await _seed_agent(session)
        active = AgentDeployment(
            deploy_id="dpl_source_active",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="building",
            head_sha="b" * 40,
            started_at=spd.utcnow() - timedelta(minutes=5),
        )
        session.add(active)
        await session.commit()

        processed = await spd.SourcePushDeployWorker().run_once(session)
        await session.refresh(active)

    assert processed is True
    assert calls == {"deploy_id": "dpl_source_active", "agent_name": "invoice-bot"}
    assert active.status == "live"


@pytest.mark.asyncio
async def test_worker_does_not_refresh_recent_active_source_push_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_if_called(*_args: object, **_kwargs: object) -> AgentDeployment:
        raise AssertionError("recent source-push deployments should not refresh")

    monkeypatch.setattr(spd, "sync_deployment_verification", fail_if_called)

    async with session_factory() as session:
        agent = await _seed_agent(session)
        active = AgentDeployment(
            deploy_id="dpl_source_recent",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="building",
            head_sha="b" * 40,
            started_at=spd.utcnow(),
        )
        session.add(active)
        await session.commit()

        processed = await spd.SourcePushDeployWorker().run_once(session)

    assert processed is False


@pytest.mark.asyncio
async def test_worker_defers_when_agent_has_active_deployment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        active = AgentDeployment(
            deploy_id="dpl_active",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="from_tarball",
            status="building",
            head_sha="old",
        )
        session.add(active)
        await session.commit()
        job = await _enqueue(session, agent)

        processed = await spd.SourcePushDeployWorker().run_once(session)
        await session.refresh(job)
        deployments = (await session.execute(select(AgentDeployment))).scalars().all()
        events = (
            await session.execute(
                select(WorkJob).where(WorkJob.job_id == job.job_id)
            )
        ).scalar_one()

    assert processed is True
    assert job.status == "queued"
    assert job.summary == "Source push deployment waiting for active deployment"
    assert job.error is None
    assert job.queued_at is not None
    assert job.leased_until is None
    assert events.output_payload == {}
    assert len(deployments) == 1


@pytest.mark.asyncio
async def test_worker_refreshes_stale_active_deployment_before_blocking(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    _patch_success(monkeypatch, calls)
    calls["repo_head"] = "c" * 40

    async def fake_sync_deployment_verification(
        session: AsyncSession,
        agent: Agent,
        deploy: AgentDeployment,
        **_kwargs: object,
    ) -> AgentDeployment:
        calls["refreshed"] = deploy.deploy_id
        deploy.status = "failed"
        deploy.error = "Timed out waiting for the agent to become live."
        deploy.completed_at = spd.utcnow()
        await session.commit()
        await session.refresh(deploy)
        return deploy

    monkeypatch.setattr(
        spd,
        "sync_deployment_verification",
        fake_sync_deployment_verification,
    )

    async with session_factory() as session:
        agent = await _seed_agent(session)
        stale_active = AgentDeployment(
            deploy_id="dpl_stale_active",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="building",
            head_sha="b" * 40,
            started_at=spd.utcnow() - timedelta(hours=2),
        )
        session.add(stale_active)
        await session.commit()
        job = await _enqueue(session, agent, source_sha="c" * 40)

        processed = await spd.SourcePushDeployWorker(
            SimpleNamespace(state=SimpleNamespace())
        ).run_once(session)
        await session.refresh(job)
        await session.refresh(stale_active)
        deployments = (await session.execute(select(AgentDeployment))).scalars().all()

    new_deployments = [
        deploy for deploy in deployments if deploy.deploy_id != "dpl_stale_active"
    ]
    assert processed is True
    assert calls["refreshed"] == "dpl_stale_active"
    assert stale_active.status == "failed"
    assert job.status == "complete"
    assert len(new_deployments) == 1
    assert new_deployments[0].status == "building"
    assert new_deployments[0].head_sha == "c" * 40


@pytest.mark.asyncio
async def test_next_job_skips_deferred_source_push_until_retry_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        deferred = await _enqueue(session, agent, source_sha="b" * 40)
        deferred.queued_at = spd.utcnow() + timedelta(seconds=30)
        ready = await _enqueue(session, agent, source_sha="c" * 40)
        ready.queued_at = spd.utcnow() - timedelta(seconds=1)
        await session.commit()

        claimed = await spd.SourcePushDeployWorker()._next_job(session)

    assert claimed is not None
    assert claimed.job_id == ready.job_id


@pytest.mark.asyncio
async def test_worker_ignores_active_deployment_superseded_by_newer_live(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    _patch_success(monkeypatch, calls)
    calls["repo_head"] = "c" * 40

    async with session_factory() as session:
        agent = await _seed_agent(session)
        stale_created_at = spd.utcnow() - timedelta(days=2)
        live_created_at = stale_created_at + timedelta(days=1)
        stale_active = AgentDeployment(
            deploy_id="dpl_stale_active",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="building",
            head_sha="old",
            created_at=stale_created_at,
        )
        live = AgentDeployment(
            deploy_id="dpl_live",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="source_push",
            status="live",
            head_sha="b" * 40,
            created_at=live_created_at,
        )
        session.add_all([stale_active, live])
        await session.commit()
        job = await _enqueue(session, agent, source_sha="c" * 40)

        processed = await spd.SourcePushDeployWorker(
            SimpleNamespace(state=SimpleNamespace())
        ).run_once(session)
        await session.refresh(job)
        deployments = (await session.execute(select(AgentDeployment))).scalars().all()

    queued = [deploy for deploy in deployments if deploy.deploy_id.startswith("dpl_")]
    new_deployments = [
        deploy
        for deploy in queued
        if deploy.deploy_id not in {"dpl_stale_active", "dpl_live"}
    ]
    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["source_sha"] == "c" * 40
    assert len(new_deployments) == 1
    assert new_deployments[0].status == "building"
    assert new_deployments[0].head_sha == "c" * 40


@pytest.mark.asyncio
async def test_worker_noops_when_source_sha_is_already_live(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        agent = await _seed_agent(session)
        live = AgentDeployment(
            deploy_id="dpl_live",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger="from_tarball",
            status="live",
            head_sha="a" * 40,
        )
        session.add(live)
        await session.commit()
        job = await _enqueue(session, agent)

        monkeypatch.setattr(
            spd,
            "commit_and_push_runtime_from_repo",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("should not restamp")),
        )
        processed = await spd.SourcePushDeployWorker().run_once(session)
        await session.refresh(job)
        deployments = (await session.execute(select(AgentDeployment))).scalars().all()

    assert processed is True
    assert job.status == "complete"
    assert job.output_payload["already_deployed"] is True
    assert job.output_payload["deploy_id"] == "dpl_live"
    assert len(deployments) == 1


@pytest.mark.asyncio
async def test_worker_failure_marks_job_and_deployment_failed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    _patch_success(monkeypatch, calls)

    def fail_commit_runtime(**_kwargs: object) -> str:
        raise RuntimeError("runtime push failed")

    monkeypatch.setattr(spd, "commit_and_push_runtime_from_repo", fail_commit_runtime)

    async with session_factory() as session:
        agent = await _seed_agent(session)
        job = await _enqueue(session, agent)

        processed = await spd.SourcePushDeployWorker().run_once(session)
        await session.refresh(job)
        deploy = (await session.execute(select(AgentDeployment))).scalar_one()

    assert processed is True
    assert job.status == "error"
    assert "runtime push failed" in (job.error or "")
    assert deploy.status == "failed"
    assert "runtime push failed" in (deploy.error or "")
